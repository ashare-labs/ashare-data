"""Verify two live whole-window reads of bounded native public tables.

The second read exercises the actual daily overlap/idempotence path with zero
new values. No future release or history before the source window is inferred.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from ashare_data.providers.free_update_coverage import CapturedResponses, require
from ashare_data.providers.free_update_period_tables import TABLES, collect as parse_table
from ashare_data.providers.free_update_semantics import validate_semantics
from ashare_data.providers.free_update_spot_contracts import YEAR_TREND, enrich
from ashare_data.providers.free_updates import digest, implementation_identity, source_date
from ashare_data.storage.free_updates import query


def verify(root, rows):
    root = Path(root).resolve()
    proof = json.loads((root/"verification.json").read_text())
    require(proof["implementation_sha256"] == implementation_identity(), "core proof implementation changed")
    accepted = {r["excel_row"]: set(r["run_ids"]) for r in proof["verified_menu_rows"]}
    output = []
    with sqlite3.connect(f"file:{root/'observations.sqlite'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for number in rows:
            item = {"excel_row": number, "coverage_verified": False}
            try:
                require(len(accepted.get(number, [])) == 1, "whole-window contract needs one core-verified job")
                selected = db.execute("SELECT * FROM runs WHERE run_id=?", (next(iter(accepted[number])),)).fetchone()
                runs = db.execute("SELECT * FROM runs WHERE job_id=? AND as_of=? ORDER BY started_at DESC LIMIT 2", (selected["job_id"], selected["as_of"])).fetchall()[::-1]
                require(len(runs) == 2 and runs[-1]["run_id"] == selected["run_id"], "two latest live reads absent")
                parsed_runs = []
                for run in runs:
                    require(run["status"] == "received", "latest read failed")
                    job = json.loads(run["job_json"])
                    result = json.loads((root/"runs"/(run["run_id"]+".json")).read_text())
                    require(job["implementation_sha256"] == result["implementation_sha256"] == implementation_identity(), "live implementation mismatch")
                    captured = CapturedResponses(root, result, datetime.fromisoformat(run["started_at"]), datetime.fromisoformat(run["finished_at"]))
                    if job["function"] in TABLES:
                        records = parse_table(captured, job["function"], job["parameters"])
                    elif job["function"] in {"stock_profit_forecast_em", "macro_china_shibor_all", "energy_oil_hist", "energy_oil_detail_latest", "energy_carbon_hb", "macro_china_new_house_price", "stock_zh_a_st_em"}:
                        from ashare_data.providers.free_update_worker import direct_records
                        records = direct_records(captured, job["function"], job["parameters"])
                    elif job["function"] == "soozhu_province_rank":
                        from ashare_data.providers.free_update_province_prices import replay as province_replay
                        records = province_replay(captured)
                    elif job["function"] == YEAR_TREND:
                        for receipt in result["receipts"]:
                            captured.get(receipt["url"])
                        local = SimpleNamespace(root=root/"raw", receipts=result["receipts"])
                        records = enrich(local, job["function"])
                        captured.dataset_metadata = local.dataset_metadata
                    else:
                        raise ValueError("function lacks a reviewed whole-window contract")
                    require(captured.index == len(result["receipts"]) and records == result["records"] and captured.dataset_metadata == result["coverage"], "raw protocol or coverage differs from result")
                    meta = result["coverage"]
                    require(meta.get("complete_returned_window") is True and len(records) == meta.get("source_count", meta.get("source_total")), "whole returned window count does not reconcile")
                    semantic = validate_semantics(records, job, result["observation_as_of"])
                    require(semantic.get("contract_reviewed") and not semantic.get("gaps"), "semantic checks incomplete")
                    from zoneinfo import ZoneInfo
                    snapshot = datetime.fromisoformat(result["finished_at"]).astimezone(ZoneInfo(job["timezone"])).date().isoformat()
                    payloads = {digest(r | {"_snapshot_date": snapshot}) if job["mode"] == "snapshot" else digest(r) for r in records}
                    links = {r[0] for r in db.execute("SELECT payload_sha256 FROM run_records WHERE run_id=?", (run["run_id"],))}
                    require(payloads == links, "persisted run records differ from raw source")
                    parsed_runs.append((records, payloads, meta, semantic))
                require(parsed_runs[0][1] == parsed_runs[1][1] and runs[-1]["inserted"] == 0, "live second read changed; zero-change proof not established")
                visible = {r["payload_sha256"] for r in query(root, number, limit=100000)}
                require(parsed_runs[-1][1] <= visible, "full table query readback incomplete")
                records, _, meta, semantic = parsed_runs[-1]
                from ashare_data.providers.free_updates import DATE_FIELDS
                days = [source_date(v) for r in records for k, v in r.items() if k in DATE_FIELDS and source_date(v)]
                item.update(coverage_verified=True, source_records=len(records), first_source_period=min(days, default=None), last_source_period=max(days, default=None),
                    complete_history_as_declared_by_source=meta["complete_history"], coverage_scope=meta.get("scope", meta.get("history_limit")),
                    raw_reparsed=True, count_reconciled=True, full_query_readback=True, live_run_ids=[r["run_id"] for r in runs],
                    live_read_times=[r["finished_at"] for r in runs], live_repeat_inserted=0, daily_update_path="reread bounded source window and persist only new content/revisions",
                    offline_overlap_recovery_proof="verification.json", real_cross_publication_cycle_observed=False,
                    missing_field_observations=semantic.get("missing_field_observations", {}),
                    missing_chart_dates=semantic.get("missing_chart_dates", []),
                    source_cities=semantic.get("source_cities", []),
                    field_latest_nonnull_periods=semantic.get("field_latest_nonnull_periods", {}),
                    daily_deployed=False, formally_published=False)
            except (ValueError, KeyError, OSError, TypeError) as exc:
                item["error"] = type(exc).__name__+": "+str(exc)
            output.append(item)
    return {"schema": "ad.free-update-native-window-verification.v1", "verified_at": datetime.now(timezone.utc).isoformat(),
            "implementation_sha256": implementation_identity(), "coverage_verified_count": sum(i["coverage_verified"] for i in output), "items": output}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--rows", nargs="+", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.root, args.rows)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({"coverage_verified_count": result["coverage_verified_count"], "errors": [i for i in result["items"] if not i["coverage_verified"]]}, ensure_ascii=False))
