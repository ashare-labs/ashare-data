"""Offline follow-up and reversible source handoff for explicit local batches.

This process never loads a credential or downloads data. It follows existing
writers, preserves user STOP files, and reparses only after a safe writer boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import gzip
import json
from pathlib import Path
import time

from ashare_data.financials import expansion as e
from ashare_data.financials import limit_expansion as limits
from ashare_data.financials import official_expansion as official
from ashare_data.financials import tushare_reference as ts
from ashare_data.financials.official_access import reviewed_ledger, verified_source

FINISHED = "source_pass_finished_with_explicit_gaps"
HANDOFF_SCHEMA = "ad.reference-handoff-stop.v1"


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


@contextmanager
def idle_writer(root):
    """Check an existing advisory lock without ever removing it."""
    with (root / "writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def lock_held(path):
    if not path.exists():
        return False
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def handoff_evidence(tushare_root, legacy_root):
    """Observe two ordinary batch results, never issue a validation request.

    Earliest and latest planned sessions establish bounded source usability,
    not complete history. The bulk legacy plan is paused; pending unique fields
    and missing dates remain explicit and resumable.
    """
    root = Path(tushare_root)
    finished = all(read(root / name / "supervisor.json", {}).get("state") == FINISHED
                   for name in ("calendar", "limits", "capped-targets"))
    if (not lock_held(root / "worker.lock") and not finished) or (root / "STOP").exists():
        return None
    target = root / "limits"
    if not (target / "manifest.json").exists():
        return None
    _, manifest, tasks = e.load_batch(target)
    if not tasks:
        return None
    _, prior, _ = e.load_batch(legacy_root)
    if prior["source_families"] != [limits.KIND] or manifest.get("source") != "tushare":
        raise ValueError("Handoff requires explicit Tushare and zzshare reference batches")
    if any(manifest[k] != prior[k] for k in ("universe_sha256", "target_start", "target_end")):
        raise ValueError("Handoff universe or historical bounds differ")
    host = read(target / "source-state.json", {}).get("api.tushare.pro", {})
    if host.get("blocked") or host.get("consecutive_errors") or host.get("cooldown_until", 0) > time.time():
        return None
    progress = read(target / "progress.json", {})
    if not finished and (not progress.get("updated_at") or
            (datetime.now(timezone.utc) - datetime.fromisoformat(progress["updated_at"])).total_seconds() > 600):
        return None
    if read(root / "calendar/supervisor.json", {}).get("state") != FINISHED:
        return None
    universe = read(target / "universe.json")
    evidence = []
    for task in {t["id"]: t for t in (min(tasks, key=lambda t: t["start"]), max(tasks, key=lambda t: t["start"]))}.values():
        path = target / "receipts" / (task["id"] + ".json")
        receipt = read(path, {})
        if receipt.get("state") not in {"complete", "partial_source"}:
            return None
        e.verify_receipt(target, receipt)
        rows, coverage = ts.limit_rows(gzip.decompress((target / receipt["raw"]["path"]).read_bytes()), task, universe)
        usable = [r for r in rows if all(Decimal(r["fields"][f]["value"] or "0") > 0 for f in ("up_limit", "down_limit"))]
        if {r["security"].split(".")[1] for r in usable} != {"XSHG", "XSHE"}:
            return None
        evidence.append(dict(task_id=task["id"], date=task["start"], usable_rows=len(usable),
                             receipt_sha256=e.digest(path.read_bytes()), coverage=coverage))
    return dict(tushare_batch=str(root), observed_at=e.now(), ordinary_request_evidence=evidence,
                complete_history_asserted=False, original_sources_preserved=True,
                unresolved_families=["ST", "suspension", "adjustment_factor", "missing_limit_dates_or_codes"],
                policy="pause duplicate bulk; reuse existing daily status/factors; derive explicit gap requests before any legacy resume")


def request_handoff(tushare_root, legacy_root, record):
    """Write an owned STOP once; never replace an existing user stop instruction."""
    legacy_root, record = Path(legacy_root), Path(record)
    stop = legacy_root / "STOP"
    if stop.exists():
        existing = read(record, {})
        return existing if owned_handoff(legacy_root, record) else {"state": "existing_stop_preserved"}
    proof = handoff_evidence(tushare_root, legacy_root)
    if proof is None:
        return {"state": "waiting_for_ordinary_tushare_results"}
    payload = dict(schema=HANDOFF_SCHEMA, at=e.now(), legacy_batch=str(legacy_root), evidence=proof)
    body = e.encoded(payload)
    e.atomic(record, e.encoded(dict(state="stop_requested", stop_sha256=e.digest(body), **payload)))
    try:
        with stop.open("xb") as output:
            output.write(body)
    except FileExistsError:
        return {"state": "existing_stop_preserved"}
    return read(record)


def owned_handoff(root, record):
    proof = read(record, {})
    stop = root / "STOP"
    return (proof.get("schema") == HANDOFF_SCHEMA and proof.get("legacy_batch") == str(root)
            and stop.exists() and e.digest(stop.read_bytes()) == proof.get("stop_sha256"))


def generic_ready(root, *, handoff_record=None):
    supervisor, exited = read(root / "supervisor.json", {}), read(root / "worker-exit.json", {})
    progress = read(root / "progress.json", {})
    if not exited.get("at") or exited.get("pid") != progress.get("pid"):
        return False
    if exited["at"] < progress.get("updated_at", "") or supervisor.get("at", "") < exited["at"]:
        return False
    if (root / "STOP").exists():
        return bool(handoff_record and owned_handoff(root, handoff_record) and supervisor.get("state") == "stopped")
    return supervisor.get("state") == FINISHED and not exited.get("stop_requested")


def official_ready(root):
    exited, progress = read(root / "worker-exit.json", {}), read(root / "progress.json", {})
    if ((root / "STOP").exists() or exited.get("stop_requested") or not exited.get("at")
            or exited.get("pid") != progress.get("pid") or exited["at"] < progress.get("updated_at", "")):
        return False
    scope = read(root / "scope.json")
    return all(read(root / "securities" / (symbol + ".json"), {}).get("state") in
               {"complete", "parse_failed", "source_exhausted"} for symbol in scope["symbols"])


def official_integrity(root):
    verified, errors = 0, []
    for path in (root / "receipts").glob("*.json"):
        receipt = read(path)
        if receipt.get("state") != "complete":
            continue
        try:
            verified_source(root, path.stem)
            verified += 1
        except (ValueError, OSError) as error:
            errors.append(dict(receipt=path.stem, error=str(error)))
    review_path = root / "field-reviews.json"
    if review_path.exists():
        reviewed_ledger(root, read(review_path))
    return dict(verified_originals=verified, integrity_errors=errors, coverage=official.audit(root),
                source_gaps={p.stem: read(p) for p in (root / "securities").glob("*.json")
                             if read(p).get("state") != "complete"})


def follow_batch(root, kind, *, handoff_record=None):
    """No source request, no restart, no all-captured derivative rewrite."""
    root = Path(root).resolve()
    if (root / "STOP").exists() and not (handoff_record and owned_handoff(root, handoff_record)):
        return {"state": "user_stop_preserved"}
    with idle_writer(root) as idle:
        if not idle:
            return {"state": "writer_active"}
        ready = official_ready(root) if kind == "official" else generic_ready(root, handoff_record=handoff_record)
        if not ready:
            return {"state": "waiting_for_natural_source_boundary"}
        if kind == "official":
            result = official_integrity(root)
            return dict(state="verified_with_explicit_gaps" if not result["integrity_errors"] else "integrity_failed", result=result)
        before = e.audit(root)
        if before["pending_or_retryable"] and not (handoff_record and owned_handoff(root, handoff_record)):
            return {"state": "waiting_for_pending_requests"}
    # replay acquires the same non-blocking writer lock itself; an intervening
    # writer causes a safe retry, never concurrent mutation.
    try:
        result = e.replay(root, normalizer=limits.normalize_saved if kind == "limits" else None)
    except BlockingIOError:
        return {"state": "writer_active"}
    return dict(state="verified_with_explicit_gaps" if not result["integrity_errors"] else "integrity_failed",
                network_requests=0, before=before, result=result,
                legacy_pending_preserved=bool(result["pending_or_retryable"]))


def validate_daily(spec):
    from ashare_data.financials.official_daily import reader
    root = Path(spec["official_root"])
    review = Path(spec["review"])
    if e.digest(review.read_bytes()) != spec["review_sha256"]:
        raise ValueError("Field review changed; do not inherit earlier query expectations")
    correction = read(Path(spec["correction_reviews"])) if spec.get("correction_reviews") else None
    view = reader(root, read(review), reference_root=spec["reference_root"], reference_tasks=spec["reference_tasks"],
                  correction_reviews=correction)
    results = []
    for case in spec["queries"]:
        query = view.query(**case["query"])
        rows = query["rows"]
        actual = [{k: row.get(k) for k in ("status", "value")} for row in rows]
        results.append(dict(query=case["query"], actual=actual, expected=case["expected"], matched=actual == case["expected"]))
    return dict(at=e.now(), source="real_captured_official_documents", queries=results,
                all_matched=all(r["matched"] for r in results), network_requests=0, database_imported=False)


def tick(spec, state, output):
    output = Path(output)
    result = dict(state)
    handoff = output / "handoff.json"
    if spec.get("handoff"):
        config = spec["handoff"]
        result["handoff"] = request_handoff(config["tushare_root"], config["legacy_root"], handoff)
        tsroot = Path(config["tushare_root"])
        result["tushare_runtime"] = dict(worker_metadata_exists=(tsroot / "worker.json").exists(),
            worker_active=lock_held(tsroot / "worker.lock"), credential_values_read=False,
            phases={name: dict(progress=read(tsroot / name / "progress.json", {}),
                               supervisor=read(tsroot / name / "supervisor.json", {}))
                    for name in ("calendar", "limits", "capped-targets")})
    jobs = result.setdefault("jobs", {})
    transitioned = False
    for job in spec["batches"]:
        prior = jobs.get(job["id"], {})
        if prior.get("state") in {"verified_with_explicit_gaps", "integrity_failed", "followup_failed"}:
            continue
        try:
            current = follow_batch(job["root"], job["kind"], handoff_record=handoff if job["kind"] == "limits" else None)
        except Exception as error:
            current = dict(state="followup_failed", error=f"{type(error).__name__}: {error}"[:500])
        jobs[job["id"]] = dict(at=e.now(), **current)
        transitioned |= current["state"] == "verified_with_explicit_gaps"
    if spec.get("daily_validation") and ("daily_validation" not in result or transitioned):
        try:
            validation = validate_daily(spec["daily_validation"])
            e.atomic(output / "historical-query-validation.json", e.encoded(validation))
            result["daily_validation"] = dict(at=validation["at"], all_matched=validation["all_matched"], queries=len(validation["queries"]))
        except Exception as error:
            result["daily_validation"] = dict(at=e.now(), all_matched=False, error=f"{type(error).__name__}: {error}"[:500])
    result.update(at=e.now(), schema="ad.reference-followup-state.v1", no_network_requests=True)
    e.atomic(output / "progress.json", e.encoded(result))
    return result
