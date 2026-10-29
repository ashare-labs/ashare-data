"""Semantic and resume regressions. Synthetic fixtures are never live evidence."""
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data.providers.free_update_semantics import validate_semantics
from ashare_data.providers.free_updates import plan


def test_event_all_pages_reconcile_and_repeated_page_is_rejected():
    from ashare_data.providers.free_update_security_events import collect
    class Capture:
        repeated = False
        def get(self, url):
            page = int(parse_qs(urlsplit(url).query)["pageNumber"][0])
            return json.dumps({"result":{"pages":2,"count":2,"data":[{"page":1 if self.repeated else page}]}}).encode()
    capture = Capture()
    rows = collect(capture,{"dataset":"dividends","symbol":"600000"})
    assert len(rows) == capture.dataset_metadata["source_count"] == 2
    assert capture.dataset_metadata["complete_requested_window"]
    capture.repeated = True
    with pytest.raises(ValueError,match="repeated"):
        collect(capture,{"dataset":"dividends","symbol":"600000"})


def test_trade_multiplicity_stable_across_order_and_overlapping_windows():
    from ashare_data.providers.free_update_security_events import preserve_trade_occurrences
    first = {"SECUCODE":"000001.SZ","TRADE_DATE":"2026-10-28","BUYER_CODE":"A","SELLER_CODE":"B","DEAL_PRICE":10,"DEAL_VOLUME":100}
    other = dict(first,TRADE_DATE="2026-10-27")
    full = preserve_trade_occurrences([first,other,first])
    reversed_rows = preserve_trade_occurrences([first,first,other])
    assert full == reversed_rows and len(full) == 3
    subset = preserve_trade_occurrences([first,first])
    assert subset == [r for r in full if r["TRADE_DATE"] == "2026-10-28"]
    assert {r["source_occurrence"] for r in subset} == {1,2}


def dividend():
    return {"SECUCODE":"000001.SZ","SECURITY_CODE":"000001","REPORT_DATE":"2026-06-30",
        "NOTICE_DATE":"2026-08-20","ASSIGN_PROGRESS":"预案","period_role":"reported","EX_DIVIDEND_DATE":"2026-11-01",
        "PRETAX_BONUS_RMB":3,"BONUS_RATIO":0,"IT_RATIO":0,"BONUS_IT_RATIO":0,
        "TOTAL_SHARES":1000,"DIVIDENT_RATIO":.03,"BASIC_EPS":1,"BVPS":10,
        "PER_CAPITAL_RESERVE":2,"PER_UNASSIGN_PROFIT":3,"PNP_YOY_RATIO":5}


def test_dividend_per_ten_shares_announcement_and_scheduled_ex_date():
    job = {"function":"security_events_em","parameters":{"dataset":"dividends","symbol":"000001"}}
    row = dividend()
    result = validate_semantics([row],job,"2026-10-28")
    assert result["units"]["PRETAX_BONUS_RMB"] == "CNY per 10 shares"
    assert result["latest_period"] == "2026-06-30" and result["latest_announcement"] == "2026-08-20"
    assert not result["gaps"]
    assert validate_semantics([dict(row,NOTICE_DATE=None)],job,"2026-10-28")["gaps"]
    for bad in [dict(row,SECURITY_CODE="600000"),dict(row,NOTICE_DATE="2026-06-01")]:
        with pytest.raises(ValueError):
            validate_semantics([bad],job,"2026-10-28")


def test_dividend_predisclosed_plan_period_is_not_an_actual_future_result():
    row=dict(dividend(),ASSIGN_PROGRESS="预披露",NOTICE_DATE="2026-04-17",REPORT_DATE="2026-12-31",period_role="planned")
    job={"function":"security_events_em","parameters":{"dataset":"dividends","symbol":"000001"}}
    result=validate_semantics([row],job,"2026-10-28")
    assert result["latest_period"] is None and result["latest_announcement"] == "2026-04-17"
    with pytest.raises(ValueError,match="distinction"):
        validate_semantics([dict(row,period_role="reported")],job,"2026-10-28")


def test_block_trade_units_reject_scale_error_allow_source_rounding():
    from ashare_data.providers.free_update_security_events import preserve_trade_occurrences
    row = {"SECUCODE":"000100.SZ","SECURITY_CODE":"000100","TRADE_DATE":"2026-10-28",
        "BUYER_CODE":"A","SELLER_CODE":"B","DEAL_PRICE":4.84,"DEAL_VOLUME":445300,"DEAL_AMT":2155300,
        "CLOSE_PRICE":5,"PREMIUM_RATIO":-.032,"CHANGE_RATE":2,"TURNOVER_RATE":.1}
    job = {"function":"security_events_em","parameters":{"dataset":"block_trades","start_date":"20261001","end_date":"20261028"}}
    result=validate_semantics(preserve_trade_occurrences([row]),job,"2026-10-28")
    assert result["rounded_amount_rows"] == 1 and result["units"]["DEAL_VOLUME"] == "shares"
    with pytest.raises(ValueError,match="rounding"):
        validate_semantics(preserve_trade_occurrences([dict(row,DEAL_AMT=215.53)]),job,"2026-10-28")


def test_billboard_aggregate_exception_is_visible_without_blocking_other_fields():
    row = {"SECUCODE":"301313.SZ","SECURITY_CODE":"301313","TRADE_DATE":"2026-10-28","EXPLANATION":"严重异常期间",
        "BILLBOARD_BUY_AMT":1000,"BILLBOARD_SELL_AMT":1000,"BILLBOARD_NET_AMT":0,"BILLBOARD_DEAL_AMT":1000,
        "ACCUM_AMOUNT":1000,"FREE_MARKET_CAP":10000,"CLOSE_PRICE":10,"CHANGE_RATE":20,
        "DEAL_NET_RATIO":0,"DEAL_AMOUNT_RATIO":100,"TURNOVERRATE":3}
    job={"function":"security_events_em","parameters":{"dataset":"billboard","start_date":"20261001","end_date":"20261028"}}
    result=validate_semantics([row],job,"2026-10-28")
    assert not result["gaps"] and result["quality_issues"][0]["total_difference"] == "1000"
    assert row["BILLBOARD_DEAL_AMT"] == 1000 and result["source_rows"] == 1


def test_factor_baseline_and_divisor_not_fictitious_1900_action():
    job={"function":"stock_zh_a_daily","parameters":{"symbol":"sh600000","adjust":"qfq-factor"}}
    rows=[{"security":"sh600000","date":day,"qfq_factor":value,"baseline_sentinel":baseline,"price_formula":"raw_price / factor"}
          for day,value,baseline in [("1900-01-01","17",True),("2026-07-16","1",False)]]
    result=validate_semantics(rows,job,"2026-10-28")
    assert result["latest_period"] == "2026-07-16" and result["units"]["qfq_factor"] == "dimensionless divisor"
    for bad in [dict(rows[0],baseline_sentinel=False),dict(rows[1],date="2027-01-01"),dict(rows[1],price_formula="raw_price * factor")]:
        with pytest.raises(ValueError):
            validate_semantics([bad],job,"2026-10-28")


@pytest.mark.parametrize("name,id_,label,unit",[
    ("spot_hog_three_way_soozhu",4,"三元仔猪(15kg左右)","元/头"),
    ("spot_corn_price_soozhu",8,"玉米(14%水分)","元/公斤")])
def test_soozhu_source_product_unit_and_future_dates(name,id_,label,unit):
    from ashare_data.providers.free_update_spot_contracts import parse
    data={"ind":{"id":id_,"name":label,"unit":unit},"datalist":[["2026/10/28","173.00"]]}
    rows=parse(data,name)
    job={"function":name,"parameters":{}}
    assert not validate_semantics(rows,job,"2026-10-28")["gaps"]
    with pytest.raises(ValueError,match="unit"):
        parse(dict(data,ind=dict(data["ind"],unit="美元/头")),name)
    with pytest.raises(ValueError,match="date"):
        validate_semantics([dict(rows[0],日期="2026/10/29")],job,"2026-10-28")


@pytest.mark.parametrize("kind,code,unit",[("gold","SHAU","CNY/gram"),("silver","SHAG","CNY/kilogram")])
def test_benchmark_date_and_unit_regression_for_both_metals(kind,code,unit):
    pytest.importorskip("bs4")
    from ashare_data.providers.free_update_official import parse_sge_benchmark
    rows=parse_sge_benchmark({"zp":[[1460995200000,10]],"wp":[]},kind)
    assert rows[0]["交易时间"] == "2016-04-19" and rows[0]["品种"] == code
    job={"function":"sge_benchmark","parameters":{"kind":kind}}
    assert validate_semantics(rows,job,"2026-10-28")["units"]["早盘价"] == unit
    with pytest.raises(ValueError,match="midnight"):
        parse_sge_benchmark({"zp":[[1461024000000,10]],"wp":[]},kind)
    with pytest.raises(ValueError,match="future"):
        validate_semantics(rows,job,"2016-04-18")


def test_latest_empty_macro_row_cannot_claim_fresh_values():
    rows=[{"REPORT_DATE":day,"TIME":label,"MAKE_INDEX":value,"NMAKE_INDEX":value,"MAKE_SAME":value,"NMAKE_SAME":value}
          for day,label,value in [("2026-09-01","2026年09月份",50),("2026-10-01","2026年10月份",None)]]
    result=validate_semantics(rows,{"function":"macro_china_pmi","parameters":{}},"2026-10-28")
    assert len(result["gaps"]) == 4
    assert result["field_latest_nonnull_periods"]["MAKE_INDEX"] == "2026-09-01"


def test_frozen_queue_readonly_tamper_and_version_guards(tmp_path,monkeypatch):
    from ashare_data.providers.free_update_queue import prepare, save, run
    manifest=tmp_path/"universe.json"
    manifest.write_text(json.dumps({"schema":"ad.free-update-universe.v1","contexts":[{"stock":"000001"},{"stock":"600000"}],"scope":"two explicit fixtures"}))
    config=tmp_path/"config.json"
    config.write_text(json.dumps({"schema":"ad.free-update-queue-config.v1","root":"sample","rows":[22],
        "as_of":"2026-10-28","context_file":manifest.name,"context_sha256":hashlib.sha256(manifest.read_bytes()).hexdigest()}))
    root,queue=prepare(config)
    assert len(queue["plan"]["jobs"]) == 2 and not root.exists()
    target=save(config)
    assert save(config) == target and run(target)["pending_jobs"] == 2
    manifest.write_text('{}')
    with pytest.raises(ValueError,match="hash"):
        prepare(config)
    monkeypatch.setattr("ashare_data.providers.free_update_queue.implementation_identity",lambda:"changed")
    with pytest.raises(ValueError,match="implementation changed"):
        run(target)
    target.write_text(target.read_text().replace('"max_jobs":20','"max_jobs":30'))
    with pytest.raises(ValueError,match="hash"):
        run(target)


def test_failed_prefix_does_not_starve_new_jobs_or_freeze_retry_day(tmp_path,monkeypatch):
    from ashare_data.storage.free_updates import collect, connect
    calls=[]
    def fail(root,request_path,*args):
        request=json.loads(Path(request_path).read_text())
        calls.append(request["job"]["job_id"])
        return {"status":"failed","records":[],"receipts":[]}
    monkeypatch.setattr("ashare_data.storage.free_updates._worker_result",fail)
    p=plan(rows=[22],context=[{"stock":"600000"},{"stock":"000001"}],as_of="2026-10-28")
    root=tmp_path/"store"
    collect(root,p,max_jobs=1,attempts_per_day=1)
    collect(root,p,max_jobs=1,attempts_per_day=1)
    assert len(set(calls)) == 2
    assert all(r["status"] == "daily_attempt_limit" for r in collect(root,p,attempts_per_day=1))
    with connect(root) as db:
        db.execute("UPDATE runs SET started_at='2000-01-01T00:00:00+00:00'")
    collect(root,p,max_jobs=1,attempts_per_day=1)
    assert len(calls) == 3


def test_all_rows_have_explicit_goals_and_discontinued_never_retries():
    p=plan(as_of="2026-10-28")
    assert len(p["items"]) == 198 and all(i["collection_goal"]["target"] for i in p["items"])
    stopped=next(i for i in p["items"] if i["excel_row"] == 225)
    assert stopped["job_ids"] == [] and stopped["source_lifecycle"]["acceptance_label"] == "源指标停发，无法每日续更"
    assert not any(225 in j["menu_rows"] for j in p["jobs"])


def test_szse_etf_identity_filter_does_not_promote_lof_or_reits():
    pd=pytest.importorskip("pandas")
    from ashare_data.providers.free_update_worker import special
    class Source:
        def fund_etf_scale_szse(self):
            return pd.DataFrame([{"基金代码":code,"基金类别":kind,"基金份额":1000}
                for code,kind in [("159001","ETF"),("160001","LOF"),("180001","不动产基金")]])
    frame=special(Source(),"fund_etf_only_szse",{})
    assert list(frame["基金代码"]) == ["159001"]
    assert frame.attrs["source_coverage"]["excluded_non_etf_rows"] == 2
    job={"function":"fund_etf_only_szse","parameters":{}}
    result=validate_semantics(frame.to_dict("records"),job,"2026-10-28")
    assert result["gaps"] == [] and result["latest_period"] is None
    assert "真实接收时点" in result["limitations"][0]
    assert "统计日期" not in frame.columns
    with pytest.raises(ValueError,match="non-ETF"):
        validate_semantics([{"基金代码":"160001","基金类别":"LOF","基金份额":1000}],job,"2026-10-28")


def test_observed_etf_snapshot_keeps_unknown_date_but_rejects_bad_values():
    job = {"function": "fund_etf_only_szse", "parameters": {}}
    row = {"基金代码": "159001", "基金类别": "ETF", "基金份额": 1000, "上市日期": "1997-01-01"}
    proof = validate_semantics([row], job, "2026-10-29")
    assert proof["latest_period"] is None and proof["gaps"] == []
    assert row["上市日期"] == "1997-01-01" and "统计日期" not in row
    with pytest.raises(ValueError, match="invalid ETF shares"):
        validate_semantics([dict(row, 基金份额=-1)], job, "2026-10-29")
    with pytest.raises(ValueError, match="duplicate source business keys"):
        validate_semantics([row, row], job, "2026-10-29")


def test_partial_enterprise_boom_retains_ambiguous_growth_without_guessing_units():
    from ashare_data.providers.free_update_macro_contracts import CONTRACTS
    r={k:None for k in CONTRACTS['macro_china_enterprise_boom_index']}
    r.update(REPORT_DATE='2026-06-01',TIME='2026年第2季度',BOOM_INDEX=109.5,BOOM_INDEX_SAME=9.5)
    proof=validate_semantics([r],{'function':'macro_china_enterprise_boom_index','parameters':{}},'2026-10-29')
    assert proof['gaps']==[] and 'unverified' in proof['units']['BOOM_INDEX_SAME']
    assert proof['field_latest_nonnull_periods']['FAITH_INDEX'] is None
    assert proof['field_availability_notes']
    with pytest.raises(ValueError,match='period'):
        validate_semantics([r|{'REPORT_DATE':'2027-06-01'}],{'function':'macro_china_enterprise_boom_index','parameters':{}},'2026-10-29')


def test_monthly_equity_statistics_keep_unverified_scales_and_partial_current_month():
    fields='TOTAL_SHARES_SH TOTAL_SZARES_SZ TOTAL_MARKE_SH TOTAL_MARKE_SZ DEAL_AMOUNT_SH DEAL_AMOUNT_SZ VOLUME_SH VOLUME_SZ HIGH_INDEX_SH LOW_INDEX_SH HIGH_INDEX_SZ LOW_INDEX_SZ'.split()
    r=dict.fromkeys(fields)
    r.update(REPORT_DATE='2026-10-01',TIME='2026年10月份',HIGH_INDEX_SH=4,LOW_INDEX_SH=3,HIGH_INDEX_SZ=2,LOW_INDEX_SZ=1)
    proof=validate_semantics([r],{'function':'macro_china_stock_market_cap','parameters':{}},'2026-10-29')
    assert not proof['gaps'] and proof['units']['TOTAL_MARKE_SH']=='source_native_scale_unverified'
    assert proof['field_latest_nonnull_periods']['TOTAL_MARKE_SH'] is None
