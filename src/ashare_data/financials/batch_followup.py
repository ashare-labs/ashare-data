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
import os
from pathlib import Path
import time

from ashare_data.financials import expansion as e
from ashare_data.financials import limit_expansion as limits
from ashare_data.financials import official_expansion as official
from ashare_data.financials import tushare_reference as ts
from ashare_data.financials.official_access import reviewed_ledger, verified_source

FINISHED = "source_pass_finished_with_explicit_gaps"
HANDOFF_SCHEMA = "ad.reference-handoff-stop.v1"
TERMINAL_FOLLOWUP = {"verified_with_explicit_gaps", "integrity_failed", "followup_failed",
                     "user_stop_preserved", "source_blocked", "source_stopped", "source_exited_incomplete",
                     "dependency_ended_before_start"}


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
    if read(record, {}).get("state") == "stop_requested":
        return {"state": "owned_stop_removed; no_recreation"}
    if generic_ready(legacy_root):
        return {"state": "legacy_already_finished; no_stop_needed"}
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
        return bool(handoff_record and owned_handoff(root, handoff_record) and supervisor.get("state") in {"stopped", FINISHED})
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


def follow_batch(root, kind, *, handoff_record=None, cache_repair=None):
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
        repaired = None
        if cache_repair:
            from ashare_data.financials.source_repair import apply_idle
            repair_root = Path(cache_repair["root"])
            if e.digest((repair_root / "repair-manifest.json").read_bytes()) != cache_repair["manifest_sha256"]:
                raise ValueError("Cache repair manifest changed before adoption")
            repaired = apply_idle(repair_root, root, _writer_owned=True)
        if kind == "official":
            result = official_integrity(root)
            return dict(state="verified_with_explicit_gaps" if not result["integrity_errors"] else "integrity_failed",
                        cache_repair=repaired, result=result)
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
                cache_repair=repaired,
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
        worker = read(tsroot / "worker.json", {})
        final = read(tsroot / "completion.json", {})
        if final.get("pid") != worker.get("pid") or final.get("at", "") < worker.get("started_at", ""):
            final = {}
        result["tushare_runtime"] = dict(worker_metadata_exists=(tsroot / "worker.json").exists(),
            worker_active=lock_held(tsroot / "worker.lock"), credential_values_read=False,
            progress=read(tsroot / "progress.json", {}), final=final,
            phases={name: dict(progress=read(tsroot / name / "progress.json", {}),
                               supervisor=read(tsroot / name / "supervisor.json", {}))
                    for name in ("calendar", "limits", "capped-targets", "fundamentals")})
    jobs = result.setdefault("jobs", {})
    transitioned = False
    for job in spec["batches"]:
        prior = jobs.get(job["id"], {})
        if prior.get("state") in TERMINAL_FOLLOWUP:
            continue
        try:
            current = follow_batch(job["root"], job["kind"], handoff_record=handoff if job["kind"] == "limits" else None,
                                   cache_repair=job.get("cache_repair"))
        except Exception as error:
            current = dict(state="followup_failed", error=f"{type(error).__name__}: {error}"[:500])
        if spec.get("finish_policy") == "current_batch_only" and current["state"] in {"waiting_for_natural_source_boundary", "waiting_for_pending_requests"}:
            current = classify_inactive(job, jobs) or current
        jobs[job["id"]] = dict(at=e.now(), **current)
        transitioned |= current["state"] == "verified_with_explicit_gaps"
    if spec.get("daily_validation") and ("daily_validation" not in result or transitioned):
        try:
            validation = validate_daily(spec["daily_validation"])
            e.atomic(output / "historical-query-validation.json", e.encoded(validation))
            result["daily_validation"] = dict(at=validation["at"], all_matched=validation["all_matched"], queries=len(validation["queries"]))
        except Exception as error:
            result["daily_validation"] = dict(at=e.now(), all_matched=False, error=f"{type(error).__name__}: {error}"[:500])
    if spec.get("gap_preview") and "closed_source_gap_preview" not in result:
        config = spec["gap_preview"]
        if jobs.get(config["after"], {}).get("state") == "verified_with_explicit_gaps":
            from ashare_data.financials import tushare_gaps
            try:
                preview = tushare_gaps.preview(config["baseline"], config["root"], config.get("factor_inventory"))
                result["closed_source_gap_preview"] = dict(state="prepared_offline", at=preview["at"],
                    requests_by_api=preview["requests_by_api"], summary=str(Path(config["root"]) / "preview-summary.json"))
            except Exception as error:
                result["closed_source_gap_preview"] = dict(state="failed", error=f"{type(error).__name__}: {error}"[:300])
    result.update(at=e.now(), schema="ad.reference-followup-state.v1", no_network_requests=True)
    if spec.get("finish_policy") == "current_batch_only":
        result["completion"] = completion(spec, result)
    e.atomic(output / "progress.json", e.encoded(result))
    return result


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def classify_inactive(job, jobs):
    root = Path(job["root"])
    progress, supervisor = read(root / "progress.json", {}), read(root / "supervisor.json", {})
    if (lock_held(root / "writer.lock") or process_alive(progress.get("pid"))
            or process_alive(supervisor.get("pid")) or process_alive(job.get("owner_pid"))):
        return None  # Includes an existing collector's cooldown, not just its write section.
    dependency = jobs.get(job.get("after"), {})
    if job.get("after") and dependency.get("state") not in TERMINAL_FOLLOWUP:
        return None
    state = supervisor.get("state")
    if state == "remaining_sources_blocked_access_or_payment":
        return dict(state="source_blocked", reason=state, source_state=read(root / "source-state.json", {}))
    if (root / "STOP").exists() or read(root / "worker-exit.json", {}).get("stop_requested"):
        return dict(state="source_stopped", reason="existing_collector_stopped; no_restart")
    if job.get("after") and not progress.get("pid"):
        return dict(state="dependency_ended_before_start", reason="sequential_source_not_started; no_restart")
    if progress.get("pid"):
        return dict(state="source_exited_incomplete", reason="source_process_exited_without_completed_boundary")
    return None


def completion(spec, state):
    jobs = state.get("jobs", {})
    pending = [job["id"] for job in spec["batches"] if jobs.get(job["id"], {}).get("state") not in TERMINAL_FOLLOWUP]
    # Do not exit while a collector still holds its writer lock, including STOP drain.
    active = [job["id"] for job in spec["batches"] if lock_held(Path(job["root"]) / "writer.lock") or process_alive(job.get("owner_pid"))]
    runtime = state.get("tushare_runtime", {})
    if pending or active or runtime.get("worker_active"):
        return dict(exit_ready=False, pending_jobs=pending, active_writers=active,
                    tushare_active=runtime.get("worker_active", False))
    tsstate = (runtime.get("final", {}).get("state") or
               ("credentials_pending_not_started" if not runtime.get("worker_metadata_exists") else "worker_exited_without_final_receipt"))
    return dict(exit_ready=True, state="current_batch_closed_with_explicit_gaps", tushare_state=tsstate,
                jobs={name: job["state"] for name, job in jobs.items()}, full_history_complete=False,
                stop_condition="all_current_source_writers_closed_and_tushare_inactive",
                future_credentials_watched=False, source_collectors_restarted=False, system_service_installed=False)
