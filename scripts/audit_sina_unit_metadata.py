"""Offline, resumable unit metadata audit of a frozen Sina table candidate.

Reads only the candidate's original closed batch. No network or collector control.
Each checkpoint binds receipt/raw hashes and counts field metadata, never guesses
units or writes financial values. Run from the isolated worktree with PYTHONPATH.
"""

from collections import Counter
import argparse
import gzip
import hashlib
import json
from pathlib import Path

from ashare_data.core.model import require
from ashare_data.financials import expansion as e, tables as t
from ashare_data.storage.financial_release import checked_path, closed_batch, immutable_json
from ashare_data.storage.market_release import canonical, sha_file


def audit(candidate_root, output):
    candidate_root, output = Path(candidate_root), Path(output)
    candidate = json.loads((candidate_root / "candidate.json").read_bytes())
    require(len(candidate["batches"]) == 1, "UNIT_SCOPE", "Select one frozen public batch")
    batch = candidate["batches"][0]
    root = Path(batch["source_root"])
    output.mkdir(parents=True, exist_ok=True)
    immutable_json(output / "input.json", {
        "candidate_id": candidate["candidate_id"],
        "candidate_sha256": sha_file(candidate_root / "candidate.json"),
        "batch_key": batch["batch_key"],
        "receipt_inventory_sha256": batch["receipt_inventory_sha256"],
        "audit_implementation_sha256": sha_file(__file__),
    })
    with closed_batch(root):
        _, manifest, tasks = e.load_batch(root)
        seen = [(task["id"], sha_file(root / "receipts" / (task["id"] + ".json"))) for task in tasks]
        require(t.identity("receipts", seen) == batch["receipt_inventory_sha256"],
                "UNIT_SCOPE", "Frozen receipt inventory changed")
        selected = [(task, checksum) for task, (_, checksum) in zip(tasks, seen, strict=True)
                    if task["kind"] in e.STATEMENTS]
        all_counts, captures = Counter(), []
        examples = {}
        for first in range(0, len(selected), 100):
            checkpoint = output / f"part-{first:05d}.json"
            part = selected[first:first + 100]
            if checkpoint.exists():
                saved = json.loads(checkpoint.read_bytes())
                require(saved["tasks"] == [[task["id"], checksum] for task, checksum in part],
                        "UNIT_SCOPE", "Checkpoint input changed")
            else:
                counts, bindings, samples = Counter(), [], {}
                for task, checksum in part:
                    receipt = json.loads((root / "receipts" / (task["id"] + ".json")).read_bytes())
                    if not receipt.get("raw"):
                        continue
                    raw = checked_path(root, receipt["raw"]["path"])
                    packed = raw.read_bytes()
                    require(hashlib.sha256(packed).hexdigest() == receipt["raw"]["sha256"],
                            "UNIT_SOURCE", "Original compressed response changed")
                    body = gzip.decompress(packed)
                    response_sha = hashlib.sha256(body).hexdigest()
                    require(response_sha == receipt["attempts"][-1]["response_sha256"],
                            "UNIT_SOURCE", "Original response differs from receipt")
                    payload = json.loads(body, parse_float=str)
                    reports = payload.get("result", {}).get("data", {}).get("report_list", {})
                    require(isinstance(reports, dict) or reports == [], "UNIT_SOURCE", "Invalid report map")
                    capture_id = t.identity("capture", batch["batch_key"], task["id"], checksum)
                    bindings.append({
                        "capture_id": capture_id, "task_id": task["id"],
                        "receipt_sha256": checksum, "raw_sha256": receipt["raw"]["sha256"],
                        "response_sha256": response_sha, "normalized_sha256": receipt["normalized"]["sha256"],
                        "observed_at": receipt["observed_at"],
                    })
                    for period, report in (reports or {}).items():
                        day = e.day(period)
                        if not manifest["target_start"] <= day <= manifest["target_end"]:
                            continue
                        for ordinal, field in enumerate(report["data"]):
                            value, state = e.numeric(field.get("item_value"))
                            source_field, title = field.get("item_field"), field.get("item_title")
                            known = source_field in e.METRICS and report.get("rCurrency") == "CNY"
                            key = (
                                e.STATEMENTS[task["kind"]], source_field, title,
                                field.get("item_precision"), field.get("item_source"),
                                report.get("rCurrency"), report.get("rType"),
                                "existing_explicit" if known else "unverified",
                                state, value is not None,
                            )
                            counts[key] += 1
                            if key not in samples:
                                samples[key] = {
                                    "task_id": task["id"], "period": period, "ordinal": ordinal,
                                    "capture_id": capture_id, "response_sha256": response_sha,
                                    "raw_value": field.get("item_value"),
                                }
                saved = {
                    "tasks": [[task["id"], checksum] for task, checksum in part],
                    "captures": bindings,
                    "groups": [{"key": list(k), "rows": n, "example": samples[k]}
                               for k, n in sorted(counts.items(), key=lambda p: canonical(p[0]))],
                }
                immutable_json(checkpoint, saved)
            captures.extend(saved["captures"])
            for group in saved["groups"]:
                key = tuple(group["key"])
                all_counts[key] += group["rows"]
                examples.setdefault(key, group["example"])
            print(json.dumps({"tasks": min(first + 100, len(selected)), "total": len(selected),
                              "fields": sum(all_counts.values())}), flush=True)
        result = {
            "schema": "ad.sina-unit-metadata-audit.v1",
            "candidate_id": candidate["candidate_id"], "mapping_sha256": candidate["mapping_sha256"],
            "batch_key": batch["batch_key"], "source_root": str(root),
            "target_start": manifest["target_start"], "target_end": manifest["target_end"],
            "key_columns": ["statement", "source_field", "source_title", "precision", "item_source",
                            "currency", "report_type", "existing_unit_state", "value_state", "numeric"],
            "fields": sum(all_counts.values()), "captures": captures,
            "groups": [{"key": list(k), "rows": n, "example": examples[k]}
                       for k, n in sorted(all_counts.items(), key=lambda p: canonical(p[0]))],
            "unit_decisions": None, "historical_visibility_added": False,
        }
        require(result["fields"] == candidate["validation"]["table_rows"]["financial.observations"],
                "UNIT_RECONCILIATION", "Raw/target field counts differ")
        immutable_json(output / "audit.json", result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    audit(a.candidate, a.output)
