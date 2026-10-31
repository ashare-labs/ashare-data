"""Local projections of previously captured public responses, without requests.

Source observation times remain unchanged. Processing time is separate evidence;
replaying a source table is never reported as a fresh remote observation.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from ashare_data.providers.free_updates.free_update_coverage import CapturedResponses, require
from ashare_data.providers.free_updates.free_update_manual import checked_snapshot, reparse, write_snapshot
from ashare_data.providers.free_updates import digest, encoded, implementation_identity, plan
from ashare_data.storage.free_updates import connect, ingest, writer
from ashare_data.storage.immutable import immutable_write

BASES = {133: "macro_china_cpi", 134: "macro_china_cpi", 138: "macro_china_ppi",
         145: "macro_china_pmi", 146: "macro_china_pmi", 168: "macro_china_money_supply"}


def source_run(root, function, parameters=None):
    """Read and reparse the last received matching request, including old versions."""
    root = Path(root).resolve()
    with connect(root, readonly=True) as db:
        candidates = db.execute("SELECT * FROM runs WHERE status='received' "
                                "AND json_extract(job_json,'$.function')=? "
                                "ORDER BY finished_at DESC,run_id DESC", (function,))
        for candidate in candidates:
            job = json.loads(candidate["job_json"])
            if parameters is None or job["parameters"] == parameters:
                run = dict(candidate)
                break
        else:
            return None
    result = json.loads((root / "runs" / (run["run_id"] + ".json")).read_text())
    require(result.get("implementation_sha256") == job["implementation_sha256"]
            and len(result.get("records", [])) == run["returned"], "source run identity/count changed")
    records = reparse(root, result, job, run)
    from ashare_data.providers.free_updates.free_update_semantics import validate_semantics
    semantics = validate_semantics(records, job, result["observation_as_of"])
    require(semantics.get("contract_reviewed") and not semantics.get("gaps"),
            "cached source semantics incomplete")
    return run, job, result


def store_projection(root, source_root, source, job, records, coverage, as_of):
    """Idempotently persist a derived run while retaining original receipt times."""
    run, _, original = source
    root, source_root = Path(root).resolve(), Path(source_root).resolve()
    require(root != source_root, "projection may not write its source store")
    provenance = {"source_root": str(source_root), "run_id": run["run_id"],
                  "result_sha256": digest(original)}
    identity = "derived-" + digest([provenance, job["job_id"], as_of])
    result = dict(original, records=records, coverage=coverage, status="received",
                  implementation_sha256=implementation_identity(), derived_from=provenance,
                  execution_kind="cached_native_projection",
                  processed_at=datetime.now(timezone.utc).isoformat())
    with writer(root) as db:
        previous = db.execute("SELECT status FROM runs WHERE run_id=?", (identity,)).fetchone()
        if not previous or previous["status"] == "running":
            (root / "raw").mkdir(exist_ok=True)
            (root / "runs").mkdir(exist_ok=True)
            for receipt in original["receipts"]:
                origin = source_root / "raw" / (receipt["sha256"] + ".gz")
                target = root / "raw" / origin.name
                if not target.exists():
                    shutil.copyfile(origin, target)
            target = root / "runs" / (identity + ".json")
            if target.exists():
                # Recover a crash after the immutable result but before DB commit.
                saved = json.loads(target.read_text())
                require(saved["derived_from"] == provenance and saved["records"] == records
                        and saved["coverage"] == coverage, "incomplete projection differs")
                result = saved
            else:
                immutable_write(target, encoded(result))
            with db:
                db.execute("INSERT OR IGNORE INTO runs(run_id,job_id,as_of,started_at,status,job_json,result_json) "
                           "VALUES(?,?,?,?,?,?,?)", (identity, job["job_id"], as_of, run["started_at"],
                            "running", encoded(job).decode(), "{}"))
            ingest(db, root, identity, job, result, as_of=as_of)
        records, check = checked_snapshot(root, db, job, as_of)
        return [write_snapshot(root, row, job, records, check) for row in job["menu_rows"]]


def run_cached_macro(profile, *, as_of, execute=False):
    config = json.loads(Path(profile).read_text())
    require(config.get("schema") == "ad.cached-macro.v1" and len(config["rows"]) == 1
            and config["rows"][0] in BASES, "one reviewed macro projection required")
    row = config["rows"][0]
    summary = dict(dry_run=not execute, rows=[row], attempted=0, remote_requests=0,
                   all_requested_jobs_received=False, formal_published=False)
    if not execute:
        return summary
    source = source_run(config["source_root"], BASES[row], {})
    if source is None:
        return summary | {"deferred": True, "reason": "source table not yet captured"}
    run, _, result = source
    job = plan(rows=[row], as_of=as_of)["jobs"][0]
    from ashare_data.providers.free_updates.free_update_worker import direct_records
    captured = CapturedResponses(Path(config["source_root"]), result,
                                 datetime.fromisoformat(run["started_at"]),
                                 datetime.fromisoformat(run["finished_at"]))
    records = direct_records(captured, job["function"], job["parameters"])
    require(captured.index == len(result["receipts"]), "unused cached source receipts")
    items = store_projection(config["root"], config["source_root"], source, job, records,
                             captured.dataset_metadata, as_of)
    return summary | dict(all_requested_jobs_received=True, items=items,
                           source_run_id=run["run_id"], source_observed_at=run["finished_at"],
                           execution_kind="cached_native_projection")
