"""Repair absent CCTV text using its proven original directory; at most five GETs.

Run from a frozen runtime with its existing public-request budget. No timer,
credentials, permissions or formal release are created. Failures preserve all
previous snapshots. The original full capture remains the retry base.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from ashare_data.providers.free_update_broadcast import refresh_missing
from ashare_data.providers.free_update_budget import from_environment
from ashare_data.providers.free_update_manual import checked_snapshot, read_snapshot, write_snapshot
from ashare_data.providers.free_update_worker import Capture
from ashare_data.providers.free_updates import digest, encoded, implementation_identity, plan
from ashare_data.storage.free_updates import connect, ingest, writer


def retry(root, snapshot, *, execute=False, cooldown_roots=()):
    root = Path(root).resolve()
    snapshot = Path(snapshot).resolve()
    if snapshot.parent != root / "manual-snapshots" or any((root/n).exists() for n in ("CURRENT", "CURRENT.json", "releases")):
        raise ValueError("retry requires its isolated existing snapshot root")
    manifest, original_rows = read_snapshot(snapshot)
    if manifest["excel_row"] != 110 or not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", str(manifest["run_id"])):
        raise ValueError("retry only supports the reviewed broadcast row")
    path = root / "runs" / (manifest["run_id"]+".json")
    source_bytes = path.read_bytes()
    source = json.loads(source_bytes)
    if source.get("receipt_reuse"):
        raise ValueError("retry from the original complete capture, not a repair chain")
    with connect(root, readonly=True) as db:
        run = dict(db.execute("SELECT * FROM runs WHERE run_id=?", (manifest["run_id"],)).fetchone())
    if run["status"] != "received" or source["records"] != original_rows:
        raise ValueError("source snapshot differs from accepted original capture")
    original_job = json.loads(run["job_json"])
    day = original_job["parameters"]["date"]
    prepared = plan(rows=[110], context={"broadcast_date": day})
    job = prepared["jobs"][0]
    pointer = root / "manual-snapshots" / ("latest-"+digest([110, job])+".json")
    if pointer.exists():
        current_id = json.loads(pointer.read_text())["snapshot_id"]
        if not re.fullmatch(r"[0-9a-f]{64}", str(current_id)):
            raise ValueError("invalid broadcast checkpoint")
        current, current_rows = read_snapshot(pointer.parent / current_id)
        if (current.get("receipt_reuse", {}).get("source_run_id") == manifest["run_id"]
                and [r["source_id"] for r in current_rows] == [r["source_id"] for r in original_rows]
                and all(r["content"] for r in current_rows)):
            return {"status": "already_filled", "execute": execute, "new_remote_requests": 0,
                    "snapshot": str(pointer.parent / current_id), "formal_published": False}
    blocked = set()
    for directory in {root, *(Path(p).resolve() for p in cooldown_roots)}:
        if (directory / "observations.sqlite").exists():
            with connect(directory, readonly=True) as db:
                blocked.update(r[0] for r in db.execute("SELECT host FROM cooldowns WHERE until_epoch>?", (time.time(),)))
    missing = [r["source_id"] for r in original_rows if not r["content"]]
    report = {"execute": execute, "original_run_id": manifest["run_id"], "missing_before": missing,
              "maximum_new_requests": len(missing), "reuses_original_index": True, "formal_published": False}
    if not execute or not missing:
        return report | {"new_remote_requests": 0}
    if from_environment() is None:
        raise ValueError("explicit persistent public-request budget required")
    if not 1 <= len(missing) <= 5:
        raise ValueError("split or review a broadcast with more than five absent transcripts")
    def now():
        return datetime.now(timezone.utc).isoformat()
    started, run_id = now(), "transcript-"+uuid.uuid4().hex
    with writer(root) as db:
        attempts = db.execute("SELECT count(*) FROM runs WHERE job_id=? AND as_of=? AND run_id LIKE 'transcript-%'",
                              (job["job_id"], prepared["as_of"])).fetchone()[0]
        if attempts >= 2:
            return report | {"status": "daily_retry_bound", "new_remote_requests": 0, "previous_snapshot_preserved": True}
        with db:
            db.execute("INSERT INTO runs(run_id,job_id,as_of,started_at,status,job_json,result_json) VALUES(?,?,?,?,?,?,?)",
                       (run_id, job["job_id"], prepared["as_of"], started, "running", encoded(job).decode(), "{}"))
        capture = Capture(root / "raw", blocked, interval=2, request_limit=len(missing),
                          journal_path=root / "runs" / (run_id+"-receipts.jsonl"))
        try:
            rows, coverage, receipts, reused = refresh_missing(capture, root, source, original_job["parameters"],
                datetime.fromisoformat(run["started_at"]), datetime.fromisoformat(run["finished_at"]))
            result = {"status": "received", "records": rows, "receipts": receipts, "coverage": coverage,
                "started_at": started, "finished_at": now(), "observation_as_of": prepared["as_of"],
                "implementation_sha256": implementation_identity(), "execution_kind": "cached_broadcast_transcript_repair",
                "new_remote_requests": len(capture.receipts), "receipt_reuse": {"source_run_id": manifest["run_id"],
                    "source_result_sha256": hashlib.sha256(source_bytes).hexdigest(), "indices": reused}}
        except Exception as exc:
            result = {"status": "failed", "records": [], "receipts": capture.receipts, "coverage": {},
                "started_at": started, "finished_at": now(), "observation_as_of": prepared["as_of"],
                "implementation_sha256": implementation_identity(), "error": type(exc).__name__+": "+str(exc)}
        saved = ingest(db, root, run_id, job, result, as_of=prepared["as_of"])
        (root / "runs" / (run_id+".json")).write_bytes(encoded(result))
        if saved["status"] != "received":
            return report | {"status": "failed", "run_id": run_id, "error": result.get("error"),
                             "new_remote_requests": len(capture.receipts), "previous_snapshot_preserved": True}
        rows, check = checked_snapshot(root, db, job, prepared["as_of"])
        check.update(execution_kind=result["execution_kind"], receipt_reuse=result["receipt_reuse"],
                     new_remote_requests=len(capture.receipts))
        snapshot_result = write_snapshot(root, 110, job, rows, check)
    return report | {"status": "received", "run_id": run_id, "snapshot": snapshot_result,
        "missing_after": coverage["missing_transcript_ids"], "available_transcripts": coverage["available_transcripts"],
        "new_remote_requests": len(capture.receipts), "database_inserted": saved["inserted"], "original_source_unchanged": path.read_bytes() == source_bytes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--cooldown-root", type=Path, action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    result = retry(args.root, args.snapshot, execute=args.execute, cooldown_roots=args.cooldown_root)
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
