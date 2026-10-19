"""Coalesce pending dividend dates without replacing the original frozen queue.

Every network response belongs to a separate source batch. Original date tasks
receive explicit local projections only after coverage and event checks pass;
unmatched or capped responses leave the original network fallback untouched.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import fcntl
import gzip
import json
import os
from pathlib import Path

from ashare_data.financials import expansion as e
from ashare_data.financials import tushare_fundamentals as f
from ashare_data.financials import tushare_gaps as g
from ashare_data.financials import tushare_reference as ts


def pending(receipt):
    # A captured response, even after parse failure, must be reused in place.
    return receipt.get("state") not in e.TERMINAL and not receipt.get("raw")


def link_artifact(source, target, relative, artifact):
    """Share bytes using a hard link, with both immutable checksum identities."""
    destination = target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        os.link(source / artifact["path"], destination)
    result = artifact | {"path": relative}
    e.verify_receipt(target, {"raw": result})
    return result


def prepare(root, source):
    root, source = Path(root).resolve(), Path(source).resolve()
    with (source / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source, manifest, tasks = e.load_batch(source)
        if (root / "manifest.json").exists():
            _, prior, captures = e.load_batch(root)
            if prior.get("original_source") != str(source) or prior.get("original_tasks_sha256") != manifest["tasks_sha256"]:
                raise ValueError("Dividend coalescing source identity changed")
            snapshot = (root / "original-receipts.json.gz").read_bytes()
            if e.digest(snapshot) != prior["original_receipts_sha256"]:
                raise ValueError("Dividend checkpoint receipt snapshot changed")
            adopt_existing(root, source, captures, json.loads(gzip.decompress(snapshot)))
            return prior
        groups, receipts, originals = defaultdict(list), {}, {}
        for task in tasks:
            if task["kind"] != "tushare_dividend":
                continue
            _, params = ts.request_identity(task["url"])
            groups[params["ts_code"]].append(task)
            path = source / "receipts" / (task["id"] + ".json")
            if path.exists():
                blob = path.read_bytes()
                receipt = json.loads(blob)
                e.verify_receipt(source, receipt)
                receipts[task["id"]] = receipt
                originals[task["id"]] = dict(sha256=e.digest(blob), receipt=receipt)
        planned = []
        for owned in groups.values():
            remaining = [t for t in owned if pending(receipts.get(t["id"], {}))]
            if len(remaining) < 2:
                continue
            fields = sorted({name for t in remaining for name in ts.request_fields(t["url"]).split(",")})
            # Existing successful whole-code responses take priority over a new call.
            candidates = [t for t in owned if set(ts.request_identity(t["url"])[1]) == {"ts_code"}
                and receipts.get(t["id"], {}).get("state") == "complete"
                and not receipts[t["id"]].get("coverage", {}).get("row_cap_reached")
                and set(fields) <= set(ts.request_fields(t["url"]).split(","))]
            capture = (candidates[0].copy() if candidates else
                g.make_task("dividend", remaining[0], {}, fields, manifest,
                            reason="coalesced_pending_dividend_dates", gaps=[], matching_events=[]))
            capture["coalesces"] = [t["id"] for t in remaining]
            planned.append(capture)
            if candidates:
                capture["existing_response_task_id"] = capture["id"]
        snapshot = gzip.compress(e.encoded(originals), mtime=0)
        result = ts._prepare(root, source, planned, dict(original_source=str(source),
            original_tasks_sha256=manifest["tasks_sha256"], original_receipts_sha256=e.digest(snapshot),
            original_pending_dates=sum(len(t["coalesces"]) for t in planned),
            retained_existing_broad_responses=sum("existing_response_task_id" in t for t in planned), source_only=True,
            coverage_policy="all_versions; cap_or_unmatched_event_uses_original_date_fallback"))
        e.immutable(root / "original-receipts.json.gz", snapshot)
        adopt_existing(root, source, planned, originals)
        return result


def adopt_existing(root, source, planned, originals):
    for task in planned:
        task_id = task.get("existing_response_task_id")
        if task_id:
            path = root / "receipts" / (task_id + ".json")
            if path.exists():
                e.verify_receipt(root, g.read(path))
                continue
            receipt = originals[task_id]["receipt"]
            e.verify_receipt(source, receipt)
            copied = receipt | {"reused_from": dict(batch=str(source), task_id=task_id,
                                                     receipt_sha256=originals[task_id]["sha256"])}
            for key in ("raw", "normalized"):
                if receipt.get(key):
                    copied[key] = link_artifact(source, root, receipt[key]["path"], receipt[key])
            e.immutable(root / "receipts" / (task_id + ".json"), e.encoded(copied))


def source_rows(raw, task, receipt):
    fields = set(json.loads(raw).get("data", {}).get("fields", []))
    if fields != set(ts.request_fields(task["url"]).split(",")):
        raise ValueError("Coalesced response field projection mismatch")
    if set(ts.request_identity(task["url"])[1]) != {"ts_code"}:
        raise ValueError("Coalesced response must bind exactly one code")
    rows, coverage = f.observations(raw, task)
    for row in rows:
        row.update(observed_at=receipt["observed_at"], response_sha256=e.digest(raw),
                   source_task_id=task["id"], historical_pit_certified=False)
    return rows, coverage


def select_rows(task, source_task, rows, coverage):
    if coverage["row_cap_reached"]:
        return [], "source_truncated"
    api, params = ts.request_identity(task["url"])
    _, origin = ts.request_identity(source_task["url"])
    if (api != "dividend" or params.get("ts_code") != origin["ts_code"]
            or not set(params) <= {"ts_code", "ex_date"}
            or any(task.get(k) != source_task.get(k) for k in ("symbol", "out", "target_start", "target_end"))
            or not set(ts.request_fields(task["url"]).split(",")) <= set(ts.request_fields(source_task["url"]).split(","))):
        return [], "incompatible_scope_or_fields"
    selected = [row for row in rows if not params.get("ex_date") or row["fields"]["ex_date"]["value"] == e.day(params["ex_date"])]
    if not selected or any(not any(g.dividend_matches(row, event) for row in selected) for event in task["matching_events"]):
        return [], "event_not_matched"
    # Keep every returned version on the date, including ambiguous matches.
    return selected, None


def normalize_reused(raw, task, receipt):
    proof = receipt["source_reuse"]
    if e.digest(raw) != proof["response_sha256"]:
        raise ValueError("Reused dividend response checksum changed")
    rows, coverage = source_rows(raw, proof["source_task"], receipt)
    selected, failure = select_rows(task, proof["source_task"], rows, coverage)
    if failure:
        raise ValueError("Reused dividend coverage failed: " + failure)
    for row in selected:
        row.update(source_batch=proof["batch"], original_task_id=task["id"],
                   local_projection=True, source_receipt_sha256=proof["receipt_sha256"])
    return selected, coverage | dict(target_rows=len(selected), reused_from_whole_code=True,
        original_query_executed=False, original_task_id=task["id"], source_task_id=proof["source_task"]["id"],
        source_batch=proof["batch"], matched_event_count=len(task["matching_events"]))


def apply(root):
    root, manifest, captures = e.load_batch(root)
    source = Path(manifest["original_source"])
    counts, ledger, network = Counter(), [], 0
    with (source / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, original, tasks = e.load_batch(source)
        if original["tasks_sha256"] != manifest["original_tasks_sha256"]:
            raise ValueError("Original dividend queue changed")
        tasks = {t["id"]: t for t in tasks}
        for capture in captures:
            receipt_path = root / "receipts" / (capture["id"] + ".json")
            blob = receipt_path.read_bytes() if receipt_path.exists() else b"{}"
            receipt = json.loads(blob)
            network += len(receipt.get("attempts", [])) if not receipt.get("reused_from") else 0
            rows, coverage, raw = None, None, None
            for identity in capture["coalesces"]:
                task = tasks[identity]
                path = source / "receipts" / (identity + ".json")
                prior = g.read(path, {})
                item = dict(original_task_id=identity, source_task_id=capture["id"])
                if not pending(prior):
                    e.verify_receipt(source, prior)
                    item["state"] = "reused" if prior.get("source_reuse", {}).get("batch") == str(root) else "original_response_preserved"
                elif receipt.get("state") not in {"complete", "empty_unknown", "partial_source"}:
                    item["state"] = "source_pending" if receipt.get("state") not in e.TERMINAL else "fallback_source_failed"
                else:
                    if rows is None:
                        e.verify_receipt(root, receipt)
                        raw = gzip.decompress((root / receipt["raw"]["path"]).read_bytes())
                        try:
                            rows, coverage = source_rows(raw, capture, receipt)
                        except ValueError:
                            rows, coverage = [], {"row_cap_reached": False}
                            item["invalid_source_projection"] = True
                    selected, failure = select_rows(task, capture, rows, coverage)
                    if failure:
                        item["state"] = "fallback_" + failure
                    else:
                        origin = {k: capture[k] for k in ("id", "kind", "url", "symbol", "out", "target_start", "target_end")}
                        result = prior | dict(task_id=identity, attempts=prior.get("attempts", []),
                            observed_at=receipt["observed_at"], state="captured", source_reuse=dict(
                                schema="ad.tushare-dividend-reuse.v1", batch=str(root), source_task=origin,
                                receipt_sha256=e.digest(blob), response_sha256=e.digest(raw),
                                original_tasks_sha256=original["tasks_sha256"], network_request_saved=True))
                        result["raw"] = link_artifact(root, source, "raw/shared-" + capture["id"] + ".json.gz", receipt["raw"])
                        ts.normalize(source, original, task, result, raw)
                        if result["state"] != "complete":
                            raise ValueError("Validated dividend projection did not normalize")
                        e.atomic(path, e.encoded(result))
                        item["state"] = "reused"
                counts[item["state"]] += 1
                ledger.append(item)
        result = dict(schema="ad.tushare-dividend-coalescing.v1", at=e.now(), source=str(source),
            planned_capture_requests=len(captures), original_pending_dates=manifest["original_pending_dates"],
            states=dict(counts), capture_network_attempts=network,
            original_network_requests_avoided=counts["reused"],
            net_network_requests_saved_so_far=counts["reused"] - network,
            ledger=e.artifact(root, "reuse-ledger.jsonl.gz", gzip.compress(b"".join(e.encoded(row) for row in ledger), mtime=0)),
            originals_preserved=True, full_history_complete=False, database_imported=False)
        e.atomic(root / "reuse-summary.json", e.encoded(result))
        e.audit(source, verify=False, write=True, _writer_owned=True)
    return result
