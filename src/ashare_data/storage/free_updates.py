"""Isolated, resumable observations for free-source menu acceptance.

This store has no production publisher and never touches CURRENT or existing
CMES releases. A whole-market rollout remains an explicit integration step.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from ashare_data.providers.free_updates import encoded, digest, catalog, assess_records, implementation_identity

MARKER = "ad.free-update-observations.v1"


def now():
    return datetime.now(timezone.utc).isoformat()


def execution_day(timestamp=None):
    return datetime.fromisoformat(timestamp or now()).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def connect(root, *, readonly=False):
    root = Path(root).resolve()
    marker = root / "sample-store.json"
    if not marker.exists():
        if readonly or (root.exists() and any(root.iterdir())):
            raise ValueError("use a new empty observation directory; existing data roots are refused")
        root.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(encoded({"schema": MARKER, "purpose":"isolated acceptance samples",
                                    "production_publication":False}))
    if json.loads(marker.read_text()).get("schema") != MARKER:
        raise ValueError("not a free-update observation store")
    if any((root/name).exists() for name in ("CURRENT","CURRENT.json","releases")):
        raise ValueError("production release roots are forbidden")
    db_path = root / "observations.sqlite"
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) if readonly else sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    if not readonly:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS runs(
            run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, as_of TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            inserted INTEGER NOT NULL DEFAULT 0, returned INTEGER NOT NULL DEFAULT 0,
            job_json TEXT NOT NULL, result_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations(
            series_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL, first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL, payload_json TEXT NOT NULL, run_id TEXT NOT NULL,
            PRIMARY KEY(series_id,payload_sha256));
        CREATE TABLE IF NOT EXISTS run_records(
            run_id TEXT NOT NULL, series_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
            PRIMARY KEY(run_id,series_id,payload_sha256));
        CREATE TABLE IF NOT EXISTS menu_runs(
            excel_row INTEGER NOT NULL, run_id TEXT NOT NULL,
            PRIMARY KEY(excel_row,run_id));
        CREATE TABLE IF NOT EXISTS cooldowns(
            host TEXT PRIMARY KEY, until_epoch REAL NOT NULL, reason TEXT NOT NULL);
        """)
        db.commit()
    return db


@contextmanager
def writer(root):
    db = connect(root)
    with (Path(root)/"writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            db.close()
            raise RuntimeError("observation writer already running") from exc
        try:
            yield db
        finally:
            db.close()
            fcntl.flock(lock, fcntl.LOCK_UN)


def ingest(db, root, run_id, job, result, *, as_of):
    """Verify evidence before atomic insertion; retries retain all observed revisions."""
    rows = result.get("records", [])
    if result["status"] != "received" and rows:
        raise ValueError("failed worker results must not commit records")
    if result["status"] in {"received", "empty_source_window"}:
        # The frozen request day is a checkpoint key. A resumed current-source
        # request may publish newer observations; validation uses the actual
        # receipt day, without rewriting the requested window or old evidence.
        observed_day = result.get("observation_as_of") or (
            datetime.fromisoformat(result["finished_at"]).astimezone(ZoneInfo(job["timezone"])).date().isoformat()
            if result.get("finished_at") else as_of)
        empty_window = result["status"] == "empty_source_window"
        assessment = {"freshness": "verified_empty_source_window", "live_sample_acceptance": False} if empty_window else assess_records(rows, mode=job["mode"], as_of=observed_day)
        if not result.get("receipts"):
            raise ValueError("a live result must have HTTP receipts")
        for receipt in result["receipts"]:
            path = Path(root)/"raw"/(receipt["sha256"]+".gz")
            body = gzip.decompress(path.read_bytes())
            if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
                raise ValueError("raw response hash mismatch")
        from ashare_data.providers.free_update_dates import require_dates
        date_audit = require_dates(rows, job, observed_day)
        if empty_window:
            if rows or job["function"] != "stock_zh_a_disclosure_report_cninfo":
                raise ValueError("unreviewed empty-source success")
            from ashare_data.providers.free_update_coverage import CapturedResponses
            from ashare_data.providers.free_update_disclosures import collect as reparse
            captured = CapturedResponses(Path(root), result, datetime.fromisoformat(result["started_at"]), datetime.fromisoformat(result["finished_at"]))
            if reparse(captured, job["parameters"]) or captured.dataset_metadata != result["coverage"] or captured.index != len(result["receipts"]):
                raise ValueError("empty announcement window lacks matching full raw evidence")
            semantics = {"contract_reviewed": False, "empty_window_checked": True, "gaps": ["empty window is not live sample acceptance"]}
        else:
            from ashare_data.providers.free_update_semantics import validate_semantics
            semantics = validate_semantics(rows, job, observed_day)
        result = dict(result, assessment=assessment, semantics=semantics, date_audit=date_audit)
        if assessment["freshness"] in {"stale", "unverified_date", "release_verification_pending"}:
            if (assessment['freshness']=='release_verification_pending'
                    and job['function']=='macro_china_reserve_requirement_ratio'
                    and semantics.get('contract_reviewed') and not semantics.get('gaps')):
                result['collection_note']='policy source records received; latest official policy publication remains independently unverified'
            else:
                result["status"] = assessment["freshness"]
    series_params = {k:v for k,v in job["parameters"].items()
                     if k not in {"start_date","end_date","start_time","end_time","year","page_size"}}
    if job["function"] in {"sina_macro_page", "stock_fund_flow_individual", "energy_carbon_bj"}:
        series_params={k:v for k,v in series_params.items() if k not in {"offset","limit","page"}}
    if job["function"] == "szse_margin_latest":
        series_params = {}
    series_id = digest({"function":job["function"],"parameters":series_params,
                        "mode":job["mode"],"timezone":job["timezone"]})
    observed = result.get("finished_at", now())
    inserted = 0
    with db:
        # Stale records remain useful evidence, but never become a live acceptance pass.
        for row in rows:
            payload = dict(row)
            if job["mode"] == "snapshot":
                payload["_snapshot_date"] = datetime.fromisoformat(observed).astimezone(
                    ZoneInfo(job["timezone"])).date().isoformat()
            sha = digest(payload)
            cursor = db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)",
                                (series_id,sha,observed,observed,encoded(payload).decode(),run_id))
            inserted += cursor.rowcount
            db.execute("UPDATE observations SET last_seen=? WHERE series_id=? AND payload_sha256=?",
                       (observed,series_id,sha))
            db.execute("INSERT OR IGNORE INTO run_records VALUES(?,?,?)",(run_id,series_id,sha))
        for receipt in result.get("receipts", []):
            status = receipt["status"]
            if status == "transport_error" or (isinstance(status, int) and (status in {401,403,429} or 500 <= status < 600)):
                delay = 3600 if status in {401,403,429} else 300
                try:
                    delay = max(delay, min(float(receipt.get("retry_after") or 0),86400))
                except ValueError:
                    pass
                db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?,?)",
                           (receipt["host"],time.time()+delay,f"HTTP {receipt['status']}"))
        summary = {k:v for k,v in result.items() if k!="records"}
        db.execute("UPDATE runs SET finished_at=?,status=?,inserted=?,returned=?,result_json=? WHERE run_id=?",
                   (observed,result["status"],inserted,len(rows),encoded(summary).decode(),run_id))
        for row in job["menu_rows"]:
            db.execute("INSERT OR IGNORE INTO menu_runs VALUES(?,?)",(row,run_id))
    return dict(summary,run_id=run_id,inserted=inserted,returned=len(rows))


def _worker_result(root, request_path, output_path, journal_path, timeout):
    try:
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for key in ("ASHARE_DEPLOYMENT","ASHARE_DATA_ROOT","ASHARE_QUANT_DB"):
            environment.pop(key,None)
        child = subprocess.run([sys.executable,"-B","-m","ashare_data.providers.free_update_worker",
                                str(request_path),str(output_path)], env=environment,
                               timeout=timeout+2,capture_output=True,check=False)
        if child.returncode==124:
            raise subprocess.TimeoutExpired("provider deadline",timeout)
        if child.returncode or not output_path.exists():
            raise RuntimeError("worker failed: " + child.stderr.decode(errors="replace")[-500:])
        if output_path.stat().st_size > 64*1024*1024:
            raise ValueError("worker result exceeds 64 MiB")
        return json.loads(output_path.read_text())
    except Exception as exc:
        receipts=[]
        if journal_path.exists() and journal_path.stat().st_size <= 2*1024*1024:
            for line in journal_path.read_bytes().splitlines():
                try:
                    receipts.append(json.loads(line))
                except ValueError:
                    break
        return {"status":"timeout" if isinstance(exc,subprocess.TimeoutExpired) else "failed",
                "error":type(exc).__name__+": "+str(exc)[:1000],"records":[],
                "receipts":receipts,"finished_at":now()}


def collect(root, update_plan, *, timeout=90, max_jobs=None, force=False, progress=None,
            workers=1, attempts_per_day=2):
    """Run independent source families concurrently, committing through one writer.

    Successful jobs are checkpointed; max_jobs counts new attempts, so resumed
    bounded batches advance beyond previously completed jobs. No automatic retry
    occurs inside a job; failed attempts per execution day are bounded. Frozen
    queues can resume on later days without changing their target date or IDs.
    """
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    root = Path(root).resolve()
    if not 1 <= timeout <= 300 or not 1 <= workers <= 3 or not 1 <= attempts_per_day <= 3:
        raise ValueError("timeout 1..300, workers 1..3, attempts_per_day 1..3 required")
    if max_jobs is not None and max_jobs<1:
        raise ValueError("max_jobs must be positive")
    output = []
    with writer(root) as db:
        for prior in db.execute("SELECT run_id,started_at FROM runs WHERE status='running'").fetchall():
            path=root/"runs"/(prior["run_id"]+"-request.json")
            request=json.loads(path.read_text()) if path.exists() else {}
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(prior["started_at"])).total_seconds()
            if age<float(request.get("wall_timeout",300))+5:
                raise RuntimeError("prior worker may still be active; wait for its bounded deadline before resuming")
        db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'", (now(),))
        db.commit()
        (root/"runs").mkdir(exist_ok=True)
        (root/"raw").mkdir(exist_ok=True)
        old_plan=json.loads((root/"plan.json").read_text()) if (root/"plan.json").exists() else {"items":[]}
        merged={i["excel_row"]:i for i in old_plan["items"]}
        merged.update({i["excel_row"]:i for i in update_plan["items"]})
        temporary=root/"plan.next.json"
        temporary.write_bytes(encoded(dict(update_plan,items=list(merged.values()))))
        os.replace(temporary,root/"plan.json")
        pending=[]
        attempted_before=set()
        today=execution_day()
        for job in update_plan["jobs"]:
            previous=db.execute("SELECT * FROM runs WHERE job_id=? AND as_of=? ORDER BY started_at DESC",
                                (job["job_id"],update_plan["as_of"])).fetchall()
            received=next((r for r in previous if r["status"] in {"received", "empty_source_window"}),None)
            status=None
            if received and not force:
                status="already_checked_empty" if received["status"] == "empty_source_window" else "already_received"
                # A later menu wrapper can reuse the same source job. Register
                # its query mapping without changing the capture or timestamps.
                db.executemany('INSERT OR IGNORE INTO menu_runs VALUES(?,?)',
                               [(row,received['run_id']) for row in job['menu_rows']])
                db.commit()
            elif not received and sum(r["status"] != "interrupted" and execution_day(r["started_at"])==today for r in previous)>=attempts_per_day:
                status="daily_attempt_limit"
            if status:
                skipped={"job_id":job["job_id"],"status":status,"menu_rows":job["menu_rows"],"function":job["function"]}
                output.append(skipped)
                if progress:
                    progress(skipped)
            else:
                pending.append(job)
                if previous:
                    attempted_before.add(job["job_id"])
        # A failed prefix must not starve untried securities in bounded runs.
        pending.sort(key=lambda j:j["job_id"] in attempted_before)
        if max_jobs is not None:
            pending=pending[:max_jobs]
        active={}
        held=set()
        ready_at={}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while pending or active:
                if (root / "STOP").exists():
                    pending.clear()
                for job in list(pending):
                    groups=set(job.get("source_families",["unreviewed"]))
                    if len(active)>=workers:
                        break
                    if groups & held or any(ready_at.get(g,0)>time.monotonic() for g in groups):
                        continue
                    pending.remove(job)
                    run_id=uuid4().hex
                    journal_path=root/"runs"/(run_id+"-receipts.jsonl")
                    request={"job":job,"as_of":update_plan["as_of"],"observation_as_of":execution_day(),"raw_root":str(root/"raw"),
                             "wall_timeout":timeout,"journal_path":str(journal_path),
                             "blocked_hosts":[r[0] for r in db.execute("SELECT host FROM cooldowns WHERE until_epoch>?",(time.time(),))]}
                    request_path, output_path=root/"runs"/(run_id+"-request.json"),root/"runs"/(run_id+".json")
                    request_path.write_bytes(encoded(request))
                    with db:
                        db.execute("INSERT INTO runs(run_id,job_id,as_of,started_at,status,job_json,result_json) VALUES(?,?,?,?,?,?,?)",
                                   (run_id,job["job_id"],update_plan["as_of"],now(),"running",encoded(job).decode(),"{}"))
                    future=pool.submit(_worker_result,root,request_path,output_path,journal_path,timeout)
                    active[future]=(job,run_id,groups)
                    held.update(groups)
                if not active:
                    time.sleep(0.1)
                    continue
                completed,_=wait(active,timeout=0.5,return_when=FIRST_COMPLETED)
                for future in completed:
                    job,run_id,groups=active.pop(future)
                    result=future.result()
                    try:
                        summary=ingest(db,root,run_id,job,result,as_of=update_plan["as_of"])
                    except Exception as exc:
                        failure = {"status":"validation_failed","error":str(exc),"records":[],"receipts":result.get("receipts",[])}
                        if hasattr(exc, "date_audit"):
                            failure.update(date_audit=exc.date_audit, original_record_count=len(result.get("records", [])),
                                           original_result_file=run_id+".json")
                        summary=ingest(db,root,run_id,job,failure,as_of=update_plan["as_of"])
                    output.append(dict(summary,job_id=job["job_id"],menu_rows=job["menu_rows"],function=job["function"]))
                    held.difference_update(groups)
                    ready_at.update({g:time.monotonic()+1.0 for g in groups})
                    if progress:
                        progress(output[-1])
    return output


def query(root, excel_row, *, limit=100, as_of=None, include_superseded=False):
    item = next((i for i in catalog()["items"] if i["excel_row"] == excel_row), None)
    if item is None:
        raise ValueError("row outside acceptance scope")
    if not isinstance(include_superseded, bool):
        raise ValueError("include_superseded must be boolean")
    excluded = item.get("exclude_superseded_functions", []) if not include_superseded else []
    condition = ""
    if excluded:
        condition = " AND json_extract(r.job_json,'$.function') NOT IN ("+",".join("?" for _ in excluded)+")"
    if not 1<=limit<=100000:
        raise ValueError("limit must be 1..100000")
    cutoff=datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc)
    if cutoff.tzinfo is None:
        raise ValueError("received-by cutoff must include a timezone")
    cutoff=cutoff.astimezone(timezone.utc).isoformat()
    with connect(root,readonly=True) as db:
        rows = db.execute("""WITH received AS (
            SELECT o.series_id,o.payload_sha256,o.payload_json,r.job_json,
            MIN(r.finished_at) OVER identity_window AS first_seen,
            MAX(r.finished_at) OVER identity_window AS last_seen,
            ROW_NUMBER() OVER (PARTITION BY o.series_id,o.payload_sha256
                ORDER BY r.finished_at DESC,r.rowid DESC) AS provenance_rank
            FROM observations o JOIN run_records rr USING(series_id,payload_sha256)
            JOIN runs r ON r.run_id=rr.run_id JOIN menu_runs mr ON mr.run_id=rr.run_id
            WHERE mr.excel_row=? AND r.finished_at<=?"""+condition+"""
            WINDOW identity_window AS (PARTITION BY o.series_id,o.payload_sha256))
            SELECT series_id,payload_sha256,first_seen,last_seen,payload_json,job_json
            FROM received WHERE provenance_rank=1
            ORDER BY first_seen DESC,payload_sha256 LIMIT ?""",
                          (excel_row,cutoff,*excluded,limit)).fetchall()
    output = [dict(series_id=r["series_id"],first_seen=r["first_seen"],last_seen=r["last_seen"],
                 source_context={k:v for k,v in json.loads(r["job_json"]).items() if k in {"function","parameters","timezone","implementation_sha256"}},
                 superseded_projection=json.loads(r["job_json"])["function"] in item.get("exclude_superseded_functions", []),
                 payload_sha256=r["payload_sha256"],data=json.loads(r["payload_json"])) for r in rows]
    from ashare_data.providers.free_update_macro_contracts import CONTRACTS
    for record in output:
        if record["source_context"]["function"] == "macro_china_hgjck":
            record["units"] = {k:v for k,v in CONTRACTS["macro_china_hgjck"].items() if k in record["data"]}
            record["unit_annotation"] = "Current reviewed unit contract; raw amounts are 1000 USD. Original payload/hash and receipt time are unchanged; old 100-million-USD raw labels are retired."
    return output


def report(root):
    root = Path(root)
    plan_path = root/"plan.json"
    selections = {i["excel_row"]:i for i in json.loads(plan_path.read_text())["items"]} if plan_path.exists() else {}
    with connect(root,readonly=True) as db:
        rows = db.execute("SELECT mr.excel_row,r.* FROM menu_runs mr JOIN runs r USING(run_id) ORDER BY r.started_at").fetchall()
    latest = {}
    for row in rows:
        latest[(row["excel_row"],row["job_id"])] = dict(row)
    output=[]
    verification_path=root/"verification.json"
    proof=json.loads(verification_path.read_text()) if verification_path.exists() else {}
    proof_rows={i["excel_row"]:set(i["run_ids"]) for i in proof.get("verified_menu_rows",[])} if proof.get("implementation_sha256")==implementation_identity() else {}
    from ashare_data.providers.free_update_coverage import registered_coverage
    coverage_rows,coverage_errors=registered_coverage(root)
    for item in catalog()["items"]:
        selection = selections.get(item["excel_row"], {})
        runs=[v for (r,j),v in latest.items() if r==item["excel_row"]
              and (not selection or j in selection.get("job_ids",[]))]
        if item.get("source_selection", {}).get("semantic_gap"):
            status="受阻"
        elif item.get("source_lifecycle", {}).get("status") == "discontinued":
            status="受阻"
        elif not item["calls"]:
            status="未开始"
        elif selection.get("missing_context"):
            status="等待明确采集范围"
        elif not runs:
            status="实现中"
        elif any(r["status"]=="release_verification_pending" for r in runs):
            status="待发布核实"
        elif any(r["status"]=="interrupted" for r in runs):
            status="中断待续"
        elif runs and all(r["status"] == "empty_source_window" for r in runs):
            status="无新公告（窗口已查，非样本通过）"
        elif any(r["status"] not in {"received", "empty_source_window"} for r in runs):
            status="受阻"
        elif selection and set(selection["job_ids"]) <= {r["job_id"] for r in runs}:
            status="真实小样本通过"
        else:
            status="部分小样本通过"
        implemented=bool(runs) and proof_rows.get(item["excel_row"])=={r["run_id"] for r in runs}
        coverage=coverage_rows.get(item["excel_row"],{})
        output.append(dict(item,status=status,sample_runs=[{
            k:r[k] for k in ("run_id","job_id","status","returned","inserted","finished_at")}
            | {"evidence":json.loads(r["result_json"])} for r in runs],
            missing_context=selection.get("missing_context",[]),
            implementation_verified=implemented,
            coverage_verified=bool(implemented and coverage),coverage_evidence=coverage,
            coverage_status="实现及覆盖验收通过，尚未日常部署" if implemented and coverage else "完整覆盖待验收",
            daily_deployment=False,
            acceptance_complete=False))
    from ashare_data.providers.free_update_acceptance import acceptance_dimensions
    for item in output:
        item["acceptance_dimensions"] = acceptance_dimensions(item)
    counts={s:sum(i["status"]==s for i in output) for s in sorted({i["status"] for i in output})}
    return {"scope_total":198,"completed":sum(i["implementation_verified"] for i in output),
            "completed_scope":"implemented and verified within explicit sample contexts",
            "coverage_verified_count":sum(i["coverage_verified"] for i in output),
            "coverage_verification_errors":coverage_errors,
            "daily_deployed":0,"status_counts":counts,"items":output,
            "release_status":"isolated samples only; production writer integration and coverage acceptance pending"}
