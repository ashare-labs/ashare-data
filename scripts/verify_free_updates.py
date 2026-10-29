"""Offline recovery and idempotence replay using explicitly selected real receipts.

Never contacts providers or modifies the source observation store. The simulated
timeout is an offline fault injection, not a claimed live provider failure.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ashare_data.providers.free_updates import encoded, implementation_identity
from ashare_data.providers.free_update_semantics import validate_semantics
from ashare_data.storage.free_updates import collect, connect, query, report


def verify(root, *, rows=None, current_only=False):
    root=Path(root).resolve()
    current=implementation_identity()
    evidence=report(root)
    jobs={}
    for item in evidence["items"]:
        if rows is not None and item["excel_row"] not in rows:
            continue
        for run in item["sample_runs"]:
            if run["status"] in {"received", "empty_source_window"}:
                jobs[run["run_id"]]=run
    output=[]
    with sqlite3.connect(f"file:{root/'observations.sqlite'}?mode=ro",uri=True) as db:
        db.row_factory=sqlite3.Row
        current_run_ids = {r[0] for r in db.execute(
            "SELECT run_id FROM runs WHERE json_extract(job_json,'$.implementation_sha256')=?", (current,))}
        for run_id in jobs:
            run=db.execute("SELECT * FROM runs WHERE run_id=?",(run_id,)).fetchone()
            job=json.loads(run["job_json"])
            if current_only and job.get("implementation_sha256") != current:
                continue
            result=json.loads((root/"runs"/(run_id+".json")).read_text())
            empty_window = run["status"] == "empty_source_window"
            item={"run_id":run_id,"job_id":job["job_id"],"menu_rows":job["menu_rows"],
                  "function":job["function"],"parameters":job["parameters"],
                  "received_at":run["finished_at"],"records":len(result["records"]),
                  "verified_empty_window":empty_window,"live_sample_present":bool(result["records"]),
                  "current_implementation":job.get("implementation_sha256")==current,
                  "offline_fault_injection":True,"deployed":False}
            try:
                from ashare_data.providers.free_update_manual import REVIEWED_ROWS, reparse
                if set(job["menu_rows"]) <= REVIEWED_ROWS:
                    assert reparse(root, result, job, run) == result["records"]
                    item["raw_protocol_reparsed"] = True
                from ashare_data.providers.free_update_shareholders import REPORTS, collect as parse_holders
                from ashare_data.providers.free_update_period_tables import TABLES, collect as parse_tables
                direct = {"stock_value_em", "macro_china_fx_gold", "stock_profit_forecast_em", "stock_zh_a_disclosure_report_cninfo", "macro_china_shibor_all", "macro_china_hgjck", "energy_oil_hist", "energy_oil_detail_latest", "bond_zh_cov", "bond_zh_cov_info", "futures_main_sina", "futures_resample", "macro_china_mobile_number", "stock_financial_analysis_indicator_em"}
                direct |= {"stock_zt_pool_em", "stock_zt_pool_dtgc_em", "stock_zt_pool_zbgc_em", "stock_zygc_em", "szse_margin_summary", "futures_zh_minute_sina", "macro_china_stock_market_cap"}
                direct |= {"energy_carbon_hb", "macro_china_new_house_price", "stock_zh_a_st_em", "sina_native_minutes"}
                if job["function"] in REPORTS or job["function"] in TABLES or job["function"] in direct | {"news_cctv"}:
                    from ashare_data.providers.free_update_coverage import CapturedResponses
                    captured=CapturedResponses(root,result,datetime.fromisoformat(run["started_at"]),
                                               datetime.fromisoformat(run["finished_at"]))
                    if job["function"] == "news_cctv":
                        from ashare_data.providers.free_update_broadcast import collect as parse_broadcast
                        parsed = parse_broadcast(captured, job["parameters"])
                    elif job["function"] in direct:
                        from ashare_data.providers.free_update_worker import direct_records
                        parsed = direct_records(captured, job["function"], job["parameters"])
                    else:
                        parser = parse_holders if job["function"] in REPORTS else parse_tables
                        parsed=parser(captured,job["function"],job["parameters"])
                    assert parsed == result["records"] and captured.dataset_metadata == result["coverage"]
                    assert captured.index == len(result["receipts"])
                    item["raw_protocol_reparsed"]=True
                if job["function"] in {"stock_profile_cninfo", "stock_new_ipo_cninfo"}:
                    from types import SimpleNamespace
                    from ashare_data.providers.free_update_reference import captured_cninfo
                    from ashare_data.providers.free_update_coverage import CapturedResponses
                    captured = CapturedResponses(root, result, datetime.fromisoformat(run["started_at"]), datetime.fromisoformat(run["finished_at"]))
                    for receipt in result["receipts"]:
                        captured.get(receipt["url"])
                    replay_capture = SimpleNamespace(root=root/"raw", receipts=result["receipts"])
                    assert captured_cninfo(replay_capture, job["function"], job["parameters"]) == result["records"]
                    assert replay_capture.dataset_metadata == result["coverage"]
                    item["raw_protocol_reparsed"] = True
                if job["function"] == "spot_hog_year_trend_soozhu":
                    from types import SimpleNamespace
                    from ashare_data.providers.free_update_spot_contracts import enrich
                    from ashare_data.providers.free_update_coverage import CapturedResponses
                    captured = CapturedResponses(root, result, datetime.fromisoformat(run["started_at"]), datetime.fromisoformat(run["finished_at"]))
                    for receipt in result["receipts"]:
                        captured.get(receipt["url"])
                    replay_capture = SimpleNamespace(root=root/"raw", receipts=result["receipts"])
                    assert enrich(replay_capture, job["function"]) == result["records"]
                    assert replay_capture.dataset_metadata == result["coverage"]
                    item["raw_protocol_reparsed"] = True
                if job["function"] == "soozhu_province_rank":
                    from ashare_data.providers.free_update_coverage import CapturedResponses
                    from ashare_data.providers.free_update_province_prices import replay as province_replay
                    captured = CapturedResponses(root, result, datetime.fromisoformat(run["started_at"]), datetime.fromisoformat(run["finished_at"]))
                    assert province_replay(captured) == result["records"] and captured.dataset_metadata == result["coverage"]
                    assert captured.index == len(result["receipts"])
                    item["raw_protocol_reparsed"] = True
                from ashare_data.providers.free_update_dates import require_dates
                audit = require_dates(result["records"],job,result.get("observation_as_of",run["as_of"]))
                item["date_audit"] = {k: audit[k] for k in ("checked_fields", "violation_count", "allowed_future_count", "unparsed_count", "policy")}
                if empty_window:
                    assert job["function"] == "stock_zh_a_disclosure_report_cninfo" and item["raw_protocol_reparsed"] and not result["records"]
                    item["semantics"] = {"contract_reviewed":True,"empty_source_window_verified":True,"gaps":[],
                        "sample_data_present":False,"limitation":"zero announcements does not independently pass a menu row"}
                else:
                    item["semantics"]=validate_semantics(result["records"],job,result.get("observation_as_of",run["as_of"]))
                with TemporaryDirectory(prefix="ashare-free-replay-") as directory:
                    target=Path(directory)/"observations"
                    connect(target).close()
                    (target/"raw").mkdir()
                    for receipt in result["receipts"]:
                        file=receipt["sha256"]+".gz"
                        shutil.copyfile(root/"raw"/file,target/"raw"/file)
                    selected=[i for i in evidence["items"] if i["excel_row"] in job["menu_rows"]]
                    plan={"as_of":run["as_of"],"items":selected,"jobs":[job]}
                    with patch("ashare_data.storage.free_updates.subprocess.run",side_effect=subprocess.TimeoutExpired("offline-fault",1)):
                        failed=collect(target,plan,timeout=1,force=True)[0]
                    assert failed["status"]=="timeout" and not failed["inserted"]
                    def replay(args,**kwargs):
                        Path(args[-1]).write_bytes(encoded(result))
                        return subprocess.CompletedProcess(args,0,b"",b"")
                    with patch("ashare_data.storage.free_updates.subprocess.run",side_effect=replay):
                        recovered=collect(target,plan,force=True)[0]
                        repeated=collect(target,plan,force=True)[0]
                    expected_status = "empty_source_window" if empty_window else "received"
                    assert recovered["status"]==expected_status and (recovered["inserted"]==0 if empty_window else recovered["inserted"]>0)
                    assert repeated["status"]==expected_status and repeated["inserted"]==0
                    assert bool(query(target,job["menu_rows"][0],limit=1)) == bool(result["records"])
                    item.update(raw_hashes_checked=True,recovery_passed=True,idempotence_passed=True,
                                query_passed=True,replay_inserted=recovered["inserted"],repeat_inserted=0)
                    # Exercise overlapping batches using subsets of the same
                    # immutable real response. This is an offline boundary
                    # test, never evidence that a future source release ran.
                    if len(result["records"]) > 1:
                        incremental = Path(directory)/"incremental"
                        connect(incremental).close()
                        (incremental/"raw").mkdir()
                        for receipt in result["receipts"]:
                            file=receipt["sha256"]+".gz"
                            shutil.copyfile(root/"raw"/file,incremental/"raw"/file)
                        partial = dict(result, records=result["records"][:-1])
                        def replay_partial(args, **kwargs):
                            Path(args[-1]).write_bytes(encoded(partial))
                            return subprocess.CompletedProcess(args,0,b"",b"")
                        with patch("ashare_data.storage.free_updates.subprocess.run",side_effect=replay_partial):
                            initial=collect(incremental,plan,force=True)[0]
                        with patch("ashare_data.storage.free_updates.subprocess.run",side_effect=replay):
                            grown=collect(incremental,plan,force=True)[0]
                            stable=collect(incremental,plan,force=True)[0]
                        assert initial["status"]==grown["status"]==stable["status"]=="received"
                        expected_new=len({encoded(r) for r in result["records"]}-{encoded(r) for r in partial["records"]})
                        assert grown["inserted"]==expected_new and stable["inserted"]==0
                        assert initial["inserted"]+grown["inserted"]==recovered["inserted"]
                        item.update(incremental_overlap_passed=True,incremental_new_records=expected_new,
                                    incremental_evidence="offline subsets of one real capture, not a future release")
                    else:
                        item.update(incremental_overlap_passed=True,
                                    incremental_evidence="verified empty source window; checkpoint/no-write replay" if empty_window else "single observation snapshot; exact repeat has zero new rows")
                item["implemented_verified"]=(item["current_implementation"]
                    and item.get("incremental_overlap_passed",False)
                    and item["semantics"].get("contract_reviewed",False) and not item["semantics"].get("gaps"))
            except (ValueError, AssertionError, OSError, KeyError) as exc:
                item.update(implemented_verified=False,error=type(exc).__name__+": "+str(exc))
            output.append(item)
    verified=[]
    for item in evidence["items"]:
        if rows is not None and item["excel_row"] not in rows:
            continue
        active={r["run_id"] for r in item["sample_runs"]}
        if current_only:
            active &= current_run_ids
        checks=[r for r in output if r["run_id"] in active]
        if (current_only or item["status"]=="真实小样本通过") and active and active=={r["run_id"] for r in checks} and all(r["implemented_verified"] for r in checks) and any(r["live_sample_present"] for r in checks):
            verified.append({"excel_row":item["excel_row"],"run_ids":sorted(active)})
    return {"schema":"ad.free-update-verification.v1","verified_at":datetime.now(timezone.utc).isoformat(),
            "implementation_sha256":current,"mode":"offline real-response persistence recovery replay",
            "scope":"explicit contexts only; no all-market coverage claim", "deployed":False,
            "verified_menu_rows":verified,"jobs":output}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--rows",type=int,nargs="+")
    p.add_argument("--current-only",action="store_true")
    args=p.parse_args()
    result=verify(args.root,rows=args.rows,current_only=args.current_only)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"jobs":len(result["jobs"]),"implemented_verified":len(result["verified_menu_rows"]),
                      "replayed":sum(r.get("idempotence_passed",False) for r in result["jobs"])},ensure_ascii=False))


if __name__=="__main__":
    main()
