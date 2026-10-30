"""Bounded manual collection, native snapshots and stable paginated readback.

Only reviewed, explicitly scoped recipes are admitted. Each row succeeds or
fails independently. Rolling windows may lose old keys in their current view;
the observation store retains those rows and every previously received revision.
This module never installs timers, consumes production releases or publishes one.
"""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ashare_data.providers.free_update_coverage import CapturedResponses, require
from ashare_data.providers.free_updates import catalog, digest, encoded, implementation_identity, plan
from ashare_data.storage.immutable import fsync_dir

# Menu rows remain separate even where a source request can be shared.
REVIEWED_ROWS = frozenset({137, 139, 143, 150, 151, 152, 153, 154, 163, 167,
                           178, 179, 182, 209, 218, 219, 224, 233, 234,
                           114, 115, 116, 133, 134, 138, 144, 145, 146, 168,
                           183, 196, 235, 236, 237, 238, 239, 240,
                           12, 13, 14, 15, 16, 33, 34, 35, 36, 37, 43, 44, 45, 46, 47, 79, 80,
                           11, 21, 32, 78, 93, 95, 96, 100, 136, 169, 197, 198, 199, 200,
                           222, 230, 231, 232, 241, 242, 243, 245, 113, 25, 26, 118, 120, 122,
                           147, 148, 149, 158, 159, 160, 164, 165, 175, 177, 180, 181,
                           110, 173, 190, 191, 193, 194, 228, 66, 67, 192, 28, 68, 69, 70,
                           248, 249, 250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260,
                           89, 188, 217, 220, 221, 246, 247, 270, 140, 226, 227, 73, 75, 76})
OBSERVATION_KEYS = frozenset({"_snapshot_date", "observation_snapshot_date"})


def change_key(semantics):
    """Observation dates live in snapshot metadata, never invented source fields."""
    fields = [k for k in semantics["business_key"] if k not in OBSERVATION_KEYS]
    require(fields, "snapshot needs a source entity or statistical-period key")
    return fields


def prepare(config_path, *, as_of=None):
    path = Path(config_path).resolve()
    config = json.loads(path.read_text())
    require(config.get("schema") == "ad.free-update-manual.v1", "unsupported manual profile")
    rows = config.get("rows", [])
    require(rows and len(set(rows)) == len(rows) and set(rows) <= REVIEWED_ROWS,
            "manual profile requires distinct reviewed menu rows")
    root = (path.parent / config["root"]).resolve()
    require(not any((root / name).exists() for name in ("CURRENT", "CURRENT.json", "releases")),
            "production roots are forbidden")
    prepared = plan(rows=rows, context=config.get("context"), as_of=as_of,
                    lookback_days=config.get("lookback_days", 14))
    require(all(not item["missing_context"] and len(item["job_ids"]) == {25: 2, 26: 2, 67: 3, 68: 2, 69: 3, 73: 2, 75: 2, 76: 2, 220: 3}.get(item["excel_row"], 1)
                for item in prepared["items"]), "manual profile requires all reviewed full-window source jobs per row")
    for job in prepared["jobs"]:
        if job['function'] in {'stock_board_concept_cons_em','stock_board_industry_cons_em'}:
            from ashare_data.providers.free_update_board_members import parameters
            parameters(job['function'], job['parameters'])
        elif job['function'] in {'stock_board_concept_index_ths','stock_board_industry_index_ths'}:
            from ashare_data.providers.free_update_ths_bars import parameters
            _, end = parameters(job['function'], job['parameters'])
            require(end == prepared['as_of'], 'THS static current window must end on the observation day')
        elif job['function'] in {'stock_board_concept_hist_em','stock_board_industry_hist_em'}:
            from ashare_data.providers.free_update_board_bars import parameters
            _, end = parameters(job['function'], job['parameters'])
            require(end <= prepared['as_of'], 'future board date requested')
        elif job["function"] == "pbc_social_financing_flow":
            require(job["parameters"] == {"year": prepared["as_of"][:4]},
                    "PBOC flow profile needs the as-of calendar year")
        elif job["function"] == "spot_goods":
            from ashare_data.providers.free_update_spot_contracts import GOODS
            require(set(job["parameters"]) == {"symbol"} and job["parameters"]["symbol"] in GOODS,
                    "reviewed native goods identity required")
        elif job["function"] == "spot_price_qh":
            require(job["parameters"] == {"symbol": "螺纹钢"}, "only metadata-bound rebar is reviewed")
        elif job["function"] == "stock_fund_flow_industry":
            require(job["parameters"] == {"symbol": "即时"}, "current industry ranking required")
        elif job["function"] == "futures_zh_minute_sina":
            from ashare_data.providers.free_update_futures_minutes import request_identity
            from ashare_data.providers.free_update_futures import PRODUCTS
            product, _ = request_identity(job["parameters"])
            require(product in PRODUCTS, "manual futures profile requires a reviewed product unit")
        elif job["function"] in {"futures_main_sina", "futures_resample"}:
            from ashare_data.providers.free_update_futures import PRODUCTS, exact_day
            params = job["parameters"]
            expected = {"symbol", "start_date", "end_date"} | ({"period"} if job["function"] == "futures_resample" else set())
            require(set(params) == expected and params["symbol"] in PRODUCTS, "explicit reviewed futures product required")
            start, end = exact_day(params["start_date"]), exact_day(params["end_date"])
            require(start <= end <= prepared["as_of"] and (datetime.fromisoformat(end)-datetime.fromisoformat(start)).days <= 366,
                    "manual daily futures window exceeds one year")
            require(params.get("period") in {None, "W-FRI", "ME"}, "unreviewed futures aggregation")
        elif job["function"] in {"stock_zt_pool_em", "stock_zt_pool_dtgc_em", "stock_zt_pool_zbgc_em"}:
            require(job["parameters"] == {"date": prepared["as_of"].replace("-", "")}
                    and job["mode"] == "snapshot", "limit pools are observed snapshots with explicit request date")
        elif job["function"] == "szse_margin_latest":
            require(job["parameters"] == {"as_of": prepared["as_of"].replace("-", "")},
                    "latest SZSE report requires actual observation day")
        elif job["function"] in {"news_cctv", "szse_margin_summary"}:
            from ashare_data.providers.free_updates import source_date
            params = job["parameters"]
            day = source_date(str(params.get("date", "")))
            require(set(params) == {"date"} and day and "2016-02-03" < day <= prepared["as_of"],
                    "explicit past or current source date required")
        elif job["function"] == "china_government_yield":
            from ashare_data.providers.free_update_market_windows import curve_bounds
            start, end = curve_bounds(job["parameters"])
            require(end <= prepared["as_of"], "future curve date requested")
        elif job["function"] == "energy_carbon_bj":
            require(job["parameters"] == {"page": 1}, "current Beijing carbon entry requires first source page")
        elif job["function"] == "sina_macro_page":
            require(job["parameters"] == {"dataset": "macro_china_passenger_load_factor", "offset": 0, "limit": 1000},
                    "reviewed complete civil-aviation source window required")
        elif job["function"] == "fund_etf_scale_sse":
            require(set(job["parameters"]) == {"date"}
                    and job["parameters"]["date"] == prepared["as_of"].replace("-",""),
                    "SSE ETF snapshot requires the actual collection day")
        elif job["function"] in {"sw_index_first_cons", "sw_index_second_cons", "sw_index_third_cons"}:
            import re
            require(set(job["parameters"]) == {"symbol"}
                    and re.fullmatch(r"\d{6}\.SI",str(job["parameters"]["symbol"])),
                    "explicit SW industry code required")
        elif job["function"] == "index_stock_cons_csindex":
            import re
            require(set(job["parameters"]) == {"symbol"}
                    and re.fullmatch(r"\d{6}", str(job["parameters"]["symbol"])),
                    "explicit CSI index code required")
        elif job["function"] == "tdx_members":
            from ashare_data.providers.tdx_blocks import exchange_code
            require(set(job["parameters"]) == {"block_code"}, "explicit single board required")
            exchange_code(job["parameters"]["block_code"])
        elif job["function"] == "security_events_em":
            params=job["parameters"]
            require(set(params) == {"dataset","start_date","end_date"} and params["dataset"] in {"block_trades", "billboard"},
                    "manual event profile requires reviewed event and explicit date bounds")
            start,end=(datetime.strptime(params[k],"%Y%m%d").date() for k in ("start_date","end_date"))
            require(0 <= (end-start).days <= 31, "manual market event window must be at most 31 days")
        elif job["function"] in {"stock_lhb_jgmmtj_em", "stock_lhb_stock_detail_em"}:
            from ashare_data.providers.free_update_billboard import query
            query(job["function"], job["parameters"])
            params = job["parameters"]
            day = datetime.strptime(params.get("end_date", params.get("date")), "%Y%m%d").date().isoformat()
            require(day <= prepared["as_of"], "future billboard date requested")
        elif job["function"] == "safe_rmb_window":
            from ashare_data.providers.free_update_fx import bounds
            start, end = bounds(job["parameters"])
            require((datetime.fromisoformat(end)-datetime.fromisoformat(start)).days <= 31
                    and end <= prepared["as_of"], "manual SAFE window exceeds recent date bounds")
        elif job["function"] == "treasury_rates":
            params=job["parameters"]
            require(set(params)=={"kind","year"} and params["kind"] in {
                "daily_treasury_yield_curve","daily_treasury_real_yield_curve",
                "daily_treasury_bill_rates","daily_treasury_long_term_rate"}, "reviewed Treasury table required")
            require(str(params["year"])==prepared["as_of"][:4], "manual Treasury scope is the current requested year")
        elif job["function"] == "sina_native_minutes":
            from ashare_data.providers.free_update_sina_minutes import parameters
            parameters(job["parameters"])
        elif job["function"] == "macro_china_new_house_price":
            require(job["parameters"] == {"city_first": "全部", "city_second": "全部"},
                    "house-price profile requires the entire source city universe")
        elif job["function"] in {"cpi_rate_em", "macro_indicator_em"}:
            from ashare_data.providers.free_update_macro_contracts import INDICATORS
            allowed = {"mom", "yoy"} if job["function"] == "cpi_rate_em" else set(INDICATORS)
            require(set(job["parameters"]) == {"indicator"} and job["parameters"]["indicator"] in allowed,
                    "indicator profile requires one reviewed native field and its complete source window")
        else:
            require(not job["parameters"], "bounded whole-window profile cannot limit source dimensions")
    limits = {"timeout": config.get("timeout", 75), "workers": config.get("workers", 1),
              "max_jobs": config.get("max_jobs", len(prepared["jobs"])),
              "attempts_per_day": config.get("attempts_per_day", 2)}
    require(1 <= limits["timeout"] <= 75 and limits["workers"] == 1
            and 1 <= limits["max_jobs"] <= 18 and 1 <= limits["attempts_per_day"] <= 2,
            "manual budget exceeds reviewed bounds")
    return root, prepared, limits


def reparse(root, result, job, run):
    """Verify actual HTTP bytes, the native projection and source count contract."""
    from ashare_data.providers.free_update_worker import direct_records
    from ashare_data.providers.free_update_spot_contracts import PRODUCTS, YEAR_TREND, enrich
    from ashare_data.providers.free_update_province_prices import replay as province_replay
    start = datetime.fromisoformat(run["started_at"])
    if result.get("receipt_reuse"):
        require(job["function"] == "news_cctv", "cached receipt reuse is limited to broadcast transcript repairs")
        from ashare_data.providers.free_update_broadcast import receipt_window_start
        start = receipt_window_start(root, result, run)
    capture = CapturedResponses(root, result, start,
                                datetime.fromisoformat(run["finished_at"]))
    if job["function"] == YEAR_TREND or job["function"] in PRODUCTS:
        for receipt in result["receipts"]:
            capture.get(receipt["url"])
        local = SimpleNamespace(root=root / "raw", receipts=result["receipts"])
        records = enrich(local, job["function"])
        capture.dataset_metadata = local.dataset_metadata
    elif job["function"] in {"stock_profile_cninfo", "stock_new_ipo_cninfo"}:
        from urllib.parse import urlencode
        from ashare_data.providers.free_update_reference import CNINFO, parse_cninfo
        name,params=job["function"],job["parameters"]
        query={"scode":params["symbol"]} if name == "stock_profile_cninfo" else {"timetype":"36","market":"ALL"}
        body=capture.post("https://webapi.cninfo.com.cn/api/sysapi/"+CNINFO[name]+"?"+urlencode(query),{})
        records,capture.dataset_metadata=parse_cninfo(body,name,params)
    elif job["function"] == "soozhu_province_rank":
        records = province_replay(capture)
    else:
        records = direct_records(capture, job["function"], job["parameters"])
    require(capture.index == len(result["receipts"]) and records == result["records"]
            and capture.dataset_metadata == result["coverage"], "raw/native source projection differs")
    coverage = result["coverage"]
    require(coverage.get("complete_returned_window") or coverage.get("complete_history"),
            "complete source-visible window was not verified")
    expected = coverage.get("unique_observations", coverage.get("source_count", coverage.get("source_total")))
    require(len(records) == expected, "source count and native rows disagree")
    return records


def checked_snapshot(root, db, job, as_of):
    from ashare_data.providers.free_update_dates import require_dates
    from ashare_data.providers.free_update_semantics import validate_semantics
    run = db.execute("SELECT * FROM runs WHERE job_id=? AND as_of=? ORDER BY started_at DESC LIMIT 1",
                     (job["job_id"], as_of)).fetchone()
    empty = bool(run and run["status"] == "empty_source_window"
                 and job["function"] == "stock_zh_a_disclosure_report_cninfo")
    require(run and (run["status"] == "received" or empty), "current job is incomplete; older data is not a new success")
    saved_job = json.loads(run['job_json'])
    # menu_rows is packaging/accounting, deliberately excluded from job_id.
    # One actual request can serve several original menu entries without a new
    # download. All data-bearing scope and implementation fields must match.
    require({k:v for k,v in saved_job.items() if k!='menu_rows'} ==
            {k:v for k,v in job.items() if k!='menu_rows'},
            "saved job differs from the requested scope")
    result = json.loads((root / "runs" / (run["run_id"] + ".json")).read_text())
    require(result.get("implementation_sha256") == job["implementation_sha256"] == implementation_identity(),
            "capture uses another implementation")
    records = reparse(root, result, job, run)
    if job['function'] in {'stock_zt_pool_em', 'stock_zt_pool_dtgc_em', 'stock_zt_pool_zbgc_em'}:
        require(result['coverage'].get('reported_qdate') == job['parameters']['date'],
                'limit pool source date differs from request; raw response retained, no dated acceptance')
    require_dates(records, job, result["observation_as_of"])
    if empty:
        require(not records and result["coverage"].get("complete_requested_security_window")
                and result["coverage"].get("source_count") == 0, "empty source window not verified")
        semantic = {"contract_reviewed": True, "business_key": ["orgId", "secCode", "announcementId"],
                    "gaps": [], "source_rows": 0, "unique_keys": 0, "latest_period": None,
                    "latest_announcement": None, "verified_empty_source_window": True}
    else:
        semantic = validate_semantics(records, job, result["observation_as_of"])
    require(semantic.get("contract_reviewed") and not semantic.get("gaps"), "native semantics incomplete")
    virtual = set(semantic["business_key"]) & OBSERVATION_KEYS
    require(not virtual or job["mode"] == "snapshot", "observation key is only valid for a snapshot")
    keys = [digest([record[k] for k in change_key(semantic)]) for record in records]
    require((records or empty) and len(set(keys)) == len(records), "empty or duplicate business keys")
    local_day = datetime.fromisoformat(run["finished_at"]).astimezone(ZoneInfo(job["timezone"])).date().isoformat()
    payloads = {digest(r | {"_snapshot_date": local_day}) if job["mode"] == "snapshot" else digest(r)
                for r in records}
    saved = db.execute("""SELECT rr.payload_sha256,o.payload_json FROM run_records rr
        JOIN observations o USING(series_id,payload_sha256) WHERE rr.run_id=?""", (run["run_id"],)).fetchall()
    require(len(saved) == len(records) and {r["payload_sha256"] for r in saved} == payloads
            and all(digest(json.loads(r["payload_json"])) == r["payload_sha256"] for r in saved),
            "persisted complete snapshot does not match raw response")
    return records, {"run_id": run["run_id"], "job_id": job["job_id"], "function": job["function"],
                     "execution_kind": result.get("execution_kind", "source_collection"),
                     "derived_from": result.get("derived_from"),
                     "observed_at": run["finished_at"], "records": len(records),
                     "coverage": result["coverage"], "semantics": semantic,
                     "snapshot_observation_date": local_day if job["mode"] == "snapshot" else None,
                     "raw_protocol_reparsed": True, "full_query_readback": True,
                     "source_urls": sorted({r["url"] for r in result["receipts"]})}


def read_snapshot(path):
    path = Path(path).resolve()
    manifest = json.loads((path / "manifest.json").read_text())
    require(manifest.get("schema") == "ad.free-source-manual-snapshot.v1", "not a manual snapshot")
    require(digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) == manifest["snapshot_id"]
            and path.name == manifest["snapshot_id"], "manual snapshot identity mismatch")
    for name, sha in manifest["files"].items():
        file = (path / name).resolve()
        require(file.parent == path and hashlib.sha256(file.read_bytes()).hexdigest() == sha,
                "manual snapshot file hash mismatch")
    records = json.loads((path / "records.json").read_text())
    require(len(records) == manifest["records"] and len(records) <= 100000, "snapshot record count differs")
    keys = [digest([r[k] for k in change_key(manifest["semantics"])]) for r in records]
    require(len(set(keys)) == len(keys), "snapshot business keys are not unique")
    return manifest, records


def write_snapshot(root, row, job, records, check):
    key_fields = change_key(check["semantics"])
    virtual = set(check["semantics"]["business_key"]) & OBSERVATION_KEYS
    require(not virtual or check.get("snapshot_observation_date"), "snapshot observation date is absent")
    require((records or check["semantics"].get("verified_empty_source_window"))
            and len(records) == check["records"] and len(records) <= 100000,
            "snapshot record count differs")
    require(len({digest([r[k] for k in key_fields]) for r in records}) == len(records),
            "snapshot business keys are not unique")
    folder = root / "manual-snapshots"
    folder.mkdir(exist_ok=True)
    # Menu packaging is not a data-bearing scope. Collecting one row alone and
    # later sharing the same job across wrappers must retain its checkpoint.
    scope_job = {k: v for k, v in job.items() if k != "menu_rows"}
    if job.get("function") in {"stock_board_concept_index_ths", "stock_board_industry_index_ths"}:
        # A rolling request date is not a new index series. Keep the last
        # snapshot across daily windows, while retaining every immutable run.
        scope_job = {k: v for k, v in scope_job.items() if k != "job_id"}
        scope_job["parameters"] = {k: v for k, v in job["parameters"].items()
                                   if k not in {"start_date", "end_date"}}
    if job.get("function") == "szse_margin_latest":
        scope_job = {k: v for k, v in scope_job.items() if k != "job_id"}
        scope_job["parameters"] = {}
    scope = digest([row, scope_job])
    checkpoint = folder / ("latest-" + scope + ".json")
    prior, previous = None, []
    if checkpoint.exists():
        identity = json.loads(checkpoint.read_text())["snapshot_id"]
        require(isinstance(identity, str) and len(identity) == 64
                and all(c in "0123456789abcdef" for c in identity), "invalid snapshot checkpoint")
        prior, previous = read_snapshot(folder / identity)
        require(prior["scope_id"] == scope and prior["excel_row"] == row, "previous snapshot scope differs")
        if prior["run_id"] == check["run_id"]:
            return {"excel_row": row, "status": "可读取", "snapshot": str(folder / identity),
                    "snapshot_id": identity, "records": len(records), "new_records": 0,
                    "revised_records": 0, "left_current_window": 0, "checkpoint_reused": True}
    key = key_fields
    def index(values):
        return {digest([r[k] for k in key]): digest(r) for r in values}
    before, after = index(previous), index(records)
    new = len(after.keys() - before.keys())
    revised = sum(after[k] != before[k] for k in after.keys() & before.keys())
    left = len(before.keys() - after.keys())
    require(not (left and check["coverage"].get("complete_history")),
            "complete-history source dropped previous keys; review before accepting")
    gap_possible = bool(prior and not (before.keys() & after.keys())
                        and not check["coverage"].get("complete_history"))
    # Source-visible snapshots are not deletion instructions. Historical values
    # and corrections stay in observations.sqlite, even after a rolling cutoff.
    content = io.StringIO(newline="")
    fields = sorted({k for r in records for k in r})
    writer = csv.DictWriter(content, fieldnames=fields)
    writer.writeheader()
    writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                     for k, v in record.items()} for record in records)
    bodies = {"records.json": encoded(records), "data.csv": content.getvalue().encode("utf-8-sig")}
    manifest = {"schema": "ad.free-source-manual-snapshot.v1", "excel_row": row, "scope_id": scope,
                "implementation_sha256": implementation_identity(), "workbook_sha256": catalog()["workbook_sha256"],
                "previous_snapshot": prior["snapshot_id"] if prior else None,
                **check, "files": {k: hashlib.sha256(v).hexdigest() for k, v in bodies.items()},
                "change_key": key_fields, "observation_key_fields": sorted(virtual),
                "new_records": new, "revised_records": revised, "left_current_window": left,
                "history_gap_possible": gap_possible,
                "history_deleted": False, "formal_published": False,
                "scope": check["coverage"].get("source_window", "complete source-visible window at receipt time; not CMES full history"),
                "cross_publication_cycle_observed": False}
    manifest["snapshot_id"] = digest(manifest)
    target = folder / manifest["snapshot_id"]
    pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=folder))
    try:
        for name, body in {**bodies, "manifest.json": encoded(manifest)}.items():
            with (pending / name).open("wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        if target.exists():
            read_snapshot(target)
        else:
            os.replace(pending, target)
        fsync_dir(folder)
        read_snapshot(target)
        pending_pointer = checkpoint.with_suffix(".next.json")
        with pending_pointer.open("wb") as stream:
            stream.write(encoded({"snapshot_id": manifest["snapshot_id"], "excel_row": row}))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending_pointer, checkpoint)
        fsync_dir(folder)
    finally:
        if pending.exists():
            shutil.rmtree(pending)
    return {"excel_row": row, "status": "可读取", "snapshot": str(target),
            "snapshot_id": manifest["snapshot_id"], "records": len(records), "new_records": new,
            "revised_records": revised, "left_current_window": left,
            "history_gap_possible": gap_possible, "checkpoint_reused": False}


def query_snapshot(path, *, offset=0, limit=100):
    require(isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0
            and isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 1000,
            "snapshot query needs offset>=0 and limit1..1000")
    manifest, rows = read_snapshot(path)
    from ashare_data.providers.free_update_macro_contracts import require_customs_units
    require_customs_units(manifest["function"], manifest["semantics"].get("units", {}))
    require(offset <= len(rows), "offset exceeds the pinned snapshot")
    end = min(offset + limit, len(rows))
    return {"snapshot_id": manifest["snapshot_id"], "excel_row": manifest["excel_row"],
            "total": len(rows), "offset": offset, "next_offset": end if end < len(rows) else None,
            "observed_at": manifest["observed_at"], "semantics": manifest["semantics"],
            "execution_kind": manifest.get("execution_kind", "source_collection"),
            "derived_from": manifest.get("derived_from"),
            "snapshot_observation_date": manifest.get("snapshot_observation_date"),
            "coverage": manifest["coverage"], "records": rows[offset:end]}


def run_manual(config_path, *, execute=False, as_of=None, progress=None):
    from ashare_data.storage.free_updates import collect, writer
    root, prepared, limits = prepare(config_path, as_of=as_of)
    summary = {"schema": "ad.free-update-manual-result.v1", "as_of": prepared["as_of"],
               "root": str(root), "rows": [i["excel_row"] for i in prepared["items"]],
               "jobs": len(prepared["jobs"]), "limits": limits, "dry_run": not execute,
               "timer_installed": False, "formal_published": False,
               "implementation_sha256": implementation_identity()}
    if not execute:
        return summary
    results = collect(root, prepared, progress=progress, **limits)
    summary.update(attempted=sum("run_id" in r for r in results),
                   resumed_without_download=sum(r["status"] == "already_received" for r in results))
    rows = []
    with writer(root) as db:
        for item in prepared["items"]:
            row = item["excel_row"]
            snapshots = []
            for job in (j for j in prepared["jobs"] if row in j["menu_rows"]):
                try:
                    records, check = checked_snapshot(root, db, job, prepared["as_of"])
                    snapshots.append(write_snapshot(root, row, job, records, check))
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    snapshots.append({"excel_row": row, "status": "未完成", "error": str(exc),
                                      "function": job["function"], "old_snapshot_exported_as_new": False})
            if len(snapshots) == 1:
                rows.append(snapshots[0])
            else:
                complete = all(s["status"] == "可读取" for s in snapshots)
                rows.append({"excel_row":row,"status":"可读取" if complete else "未完成",
                             "snapshots":snapshots,"records":sum(s.get("records",0) for s in snapshots),
                             "complete_requested_exchanges":complete if row in {25, 26} else None,
                             "complete_source_components":complete})
    summary.update(items=rows, ready_count=sum(r["status"] == "可读取" for r in rows),
                   all_requested_jobs_received=all(r["status"] == "可读取" for r in rows),
                   finished_at=datetime.now(timezone.utc).isoformat())
    return summary
