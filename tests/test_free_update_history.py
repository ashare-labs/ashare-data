"""Synthetic protocol fixtures only; these are never claimed as live evidence."""
import gzip
import hashlib
from importlib.resources import files
import json
from math import ceil
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data.providers.free_update_macro_history import collect, window
from ashare_data.providers.free_updates import encoded, plan


SPEC={"url":"https://datacenter-web.eastmoney.com/api/data/v1/get",
      "parameters":{"reportName":"RPT_ECONOMY_PMI","pageSize":"2"}}


def pmi_rows():
    return [{"REPORT_DATE":f"2026-{month:02}-01 00:00:00","TIME":f"2026年{month:02}月份",
             "MAKE_INDEX":50,"NMAKE_INDEX":51,"MAKE_SAME":1,"NMAKE_SAME":2} for month in (9,8,7)]


class Pages:
    def get(self,url):
        q=parse_qs(urlsplit(url).query)
        rows=pmi_rows()
        predicate=q.get("filter",[""])[0]
        for op,bound in re.findall(r"REPORT_DATE(>=|<=)'(\d{4}-\d{2}-\d{2})'",predicate):
            rows=[r for r in rows if (r["REPORT_DATE"][:10] >= bound if op == ">=" else r["REPORT_DATE"][:10] <= bound)]
        page,size=int(q["pageNumber"][0]),int(q["pageSize"][0])
        return encoded({"success":True,"result":{"count":len(rows),"pages":ceil(len(rows)/size),
            "data":rows[(page-1)*size:page*size]}})


def test_macro_pagination_date_window_and_plan_are_explicit():
    c=Pages()
    assert len(collect(c,SPEC,{})) == 3 and c.dataset_metadata["pages_received"] == 2
    assert c.dataset_metadata["complete_history"]
    assert len(collect(c,SPEC,{"end_date":"20260831"})) == 2
    assert not c.dataset_metadata["complete_history"] and c.dataset_metadata["complete_requested_window"]
    p=plan(rows=[174],source_window={"start_date":"20260801","end_date":"20261028","page_size":100})
    assert p["jobs"][0]["parameters"]["start_date"] == "2026-08-01"
    with pytest.raises(ValueError,match="only for"):
        plan(rows=[22],context={"stock":"000001"},source_window={"page_size":100})


@pytest.mark.parametrize("value",[{"end_date":"2026-09"},{"start_date":"20260930","end_date":"20260831"},
    {"page_size":True},{"page_size":0},{"page_size":1001},{"token":"irrelevant"}])
def test_macro_invalid_windows_rejected(value):
    with pytest.raises(ValueError):
        window(value)


@pytest.mark.parametrize("damage,match",[("ignore_filter","ignored"),("count_change","changed"),
    ("repeated_page","repeated"),("conflicting_period","uniqueness"),("empty","empty")])
def test_macro_false_coverage_rejected(damage,match):
    class Broken(Pages):
        def get(self,url):
            if damage == "ignore_filter":
                return super().get(re.sub(r"&filter=[^&]*","",url))
            body=json.loads(super().get(url))
            q=parse_qs(urlsplit(url).query)
            page=int(q["pageNumber"][0])
            if damage == "count_change" and page == 2:
                body["result"]["count"]+=1
            if damage == "repeated_page" and page == 2:
                body["result"]["data"]=pmi_rows()[:2]
            if damage == "conflicting_period" and page == 2:
                body["result"]["data"][0]["REPORT_DATE"]=pmi_rows()[0]["REPORT_DATE"]
            if damage == "empty":
                body["result"]["data"]=[]
            return encoded(body)
    with pytest.raises(ValueError,match=match):
        collect(Broken(),SPEC,{"end_date":"2026-08-31"} if damage == "ignore_filter" else {})


def test_industry_exact_duplicate_count_is_explicit_and_conflicts_rejected():
    class Capture:
        conflict=False
        def get(self,url):
            one={"REPORT_DATE":"2026-09-01","INDICATOR_VALUE":50}
            other=dict(one,INDICATOR_VALUE=51) if self.conflict else dict(one)
            return encoded({"result":{"count":2,"pages":1,"data":[one,other]}})
    c=Capture()
    spec={"url":SPEC["url"],"parameters":{"reportName":"RPT_INDUSTRY_INDEX","pageSize":"100"}}
    assert len(collect(c,spec,{})) == 1
    assert c.dataset_metadata["source_count"] == 2 and c.dataset_metadata["identical_source_duplicates"] == 1
    c.conflict=True
    with pytest.raises(ValueError,match="uniqueness"):
        collect(c,spec,{})


def test_reserve_announcement_has_two_effective_dates_without_dedup_loss():
    from ashare_data.providers.free_update_semantics import validate_semantics
    from ashare_data.providers.free_update_dates import require_dates
    row={'REPORT_DATE':'2026-10-01','PUBLISH_DATE':'2026年10月01日','TRADE_DATE':'2026年10月15日',
         'INTEREST_RATE_BB':10,'INTEREST_RATE_BA':9.5,'INTEREST_RATE_SB':8,'INTEREST_RATE_SA':7.5,
         'CHANGE_RATE_B':-.5,'CHANGE_RATE_S':-.5,'NEXT_SH_RATE':None,'NEXT_SZ_RATE':None,'REMARK':'synthetic'}
    rows=[row,row|{'TRADE_DATE':'2026年11月15日'}]
    class Capture:
        def get(self,url):return encoded({'result':{'pages':1,'count':2,'data':rows}})
    spec={'url':SPEC['url'],'parameters':{'reportName':'RPT_ECONOMY_DEPOSIT_RESERVE','pageSize':'2000'}}
    assert len(collect(Capture(),spec,{}))==2
    job={'function':'macro_china_reserve_requirement_ratio','parameters':{}}
    check=validate_semantics(rows,job,'2026-10-29')
    assert check['latest_announcement']=='2026-10-01' and check['latest_period']=='2026-11-15'
    assert check['units']['CHANGE_RATE_B']=='percentage_points'
    require_dates(rows,job,'2026-10-29')
    with pytest.raises(ValueError,match='identity'):
        validate_semantics([row|{'PUBLISH_DATE':'2026年10月02日'}],job,'2026-10-29')


def test_industry_source_identity_units_and_latest_null_guard():
    from ashare_data.providers.free_update_industry_contracts import RATE_FIELDS
    from ashare_data.providers.free_update_semantics import validate_semantics
    row={"INDICATOR_ID":"EMI01516267","INDICATOR_NAME":"银行理财产品发行数量:当月值",
         "REPORT_DATE":"2026-09-01","INDICATOR_VALUE":3346,**dict.fromkeys(RATE_FIELDS,1)}
    job={"function":"macro_china_bank_financing","parameters":{}}
    result=validate_semantics([row],job,"2026-10-28")
    assert result["units"]["INDICATOR_VALUE"] == "products" and result["units"]["CHANGE_RATE"] == "percent"
    assert validate_semantics([dict(row,CHANGE_RATE=None)],job,"2026-10-28")["gaps"]
    for bad in [dict(row,INDICATOR_ID="another"),dict(row,INDICATOR_NAME="another"),dict(row,INDICATOR_VALUE=1.2),dict(row,REPORT_DATE="2026-10-29")]:
        with pytest.raises(ValueError):
            validate_semantics([bad],job,"2026-10-28")


def test_history_trial_checkpoints_raw_integrity_queries_and_incremental_path(tmp_path,monkeypatch):
    from ashare_data.providers.free_update_coverage import recipe, run_trial, verify_trial, registered_coverage
    from ashare_data.storage.free_updates import connect, now, collect as collect_jobs
    from ashare_data.providers.free_updates import implementation_identity
    specs=json.loads(files('ashare_data.providers').joinpath('public-macro-requests.json').read_text())["specs"]
    calls=[]
    def fixture_worker(root,request_path,*args):
        request=json.loads(request_path.read_text())
        calls.append(request["job"]["job_id"])
        class RecordedFixture(Pages):
            receipts=[]
            def get(self,url):
                body=super().get(url)
                sha=hashlib.sha256(body).hexdigest()
                (root/"raw"/(sha+".gz")).write_bytes(gzip.compress(body,mtime=0))
                self.receipts.append({"url":url,"status":200,"protocol":"http","sha256":sha,
                    "received_at":now(),"host":"datacenter-web.eastmoney.com"})
                return body
        c=RecordedFixture()
        records=collect(c,specs[request["job"]["function"]],request["job"]["parameters"])
        result={"status":"received","records":records,"receipts":c.receipts,"coverage":c.dataset_metadata,
            "observation_as_of":"2026-10-28","implementation_sha256":implementation_identity(),"finished_at":now()}
        Path(args[0]).write_bytes(encoded(result))
        return result
    monkeypatch.setattr("ashare_data.storage.free_updates._worker_result",fixture_worker)
    root=tmp_path/"isolated"
    connect(root).close()
    c=recipe([174],as_of="2026-10-28",seed_end="2026-08-31",delta_start="2026-08-01",page_size=2)
    assert run_trial(root,c)["dry_run"] and not (root/"coverage-trials").exists()
    result=run_trial(root,c,execute=True)
    assert result["coverage_verified_count"] == 1 and len(calls) == 5
    item=result["items"][0]
    assert item["delta_new_records"] == item["delta_overlap_records"] == 1
    assert item["live_unchanged_repeats"] == 2 and not item["real_cross_publication_cycle"]
    assert run_trial(root,c,execute=True)["coverage_verified_count"] == 1 and len(calls) == 5
    trial=root/"coverage-trials"/c["recipe_id"]
    # Changing page size cannot create a second logical series.
    extra=collect_jobs(trial,plan(rows=[174],as_of="2026-10-28",source_window={"page_size":1}))
    assert extra[0]["inserted"] == 0
    assert verify_trial(trial)["coverage_verified_count"] == 1
    assert 174 in registered_coverage(root)[0]
    from ashare_data.providers.free_update_handoff import prepare_bundle,read_bundle,intake_preview
    main=collect_jobs(root,plan(rows=[174],as_of="2026-10-28"))
    (root/"verification.json").write_bytes(encoded({"implementation_sha256":implementation_identity(),
        "verified_menu_rows":[{"excel_row":174,"run_ids":[main[0]["run_id"]]}]}))
    assert prepare_bundle(root,[174])["dry_run"] and not (root/"outbox").exists()
    first=prepare_bundle(root,[174],execute=True)
    assert first["delta_records"]==3 and read_bundle(first["path"])[0]["production_published"] is False
    assert prepare_bundle(root,[174],execute=True)["bundle_id"]==first["bundle_id"]
    collect_jobs(root,plan(rows=[174],as_of="2026-10-28"),force=True)
    second=prepare_bundle(root,[174],previous=first["bundle_id"],execute=True,resume_latest=True)
    assert second["delta_records"]==0
    assert prepare_bundle(root,[174],execute=True,resume_latest=True)["already_prepared"]
    coordination=tmp_path/"writer.json"
    coordination.write_bytes(encoded({"writer_pid":123,"boundary_available":False}))
    preview=intake_preview(second["path"],coordination)
    assert preview["bundle_verified"] and not preview["production_write_attempted"] and not preview["production_publication_allowed"]
    import sys
    original=pmi_rows()
    revised=[dict(r,MAKE_INDEX=52) if r["REPORT_DATE"].startswith("2026-09") else r for r in original]
    monkeypatch.setattr(sys.modules[__name__],"pmi_rows",lambda:revised)
    collect_jobs(root,plan(rows=[174],as_of="2026-10-28"),force=True)
    third=prepare_bundle(root,[174],execute=True,resume_latest=True)
    assert third["delta_records"]==1
    monkeypatch.setattr(sys.modules[__name__],"pmi_rows",lambda:original)
    collect_jobs(root,plan(rows=[174],as_of="2026-10-28"),force=True)
    reverted=prepare_bundle(root,[174],execute=True,resume_latest=True)
    assert reverted["delta_records"]==1  # previously seen data is still a new reversion
    assert read_bundle(reverted["path"])[0]["previous_bundle"]==third["bundle_id"]
    (Path(first["path"])/"delta.json").write_bytes(b'[]')
    with pytest.raises(ValueError,match="hash"):
        read_bundle(first["path"])
    raw=next((trial/"raw").glob('*.gz'))
    raw.write_bytes(gzip.compress(b'changed',mtime=0))
    assert verify_trial(trial)["coverage_verified_count"] == 0
    assert not registered_coverage(root)[0]
    with pytest.raises(ValueError,match="implementation"):
        run_trial(root,dict(c,implementation_sha256="outdated"),execute=True)
