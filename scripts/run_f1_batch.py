"""Replay frozen local panels, source-layer comparisons and optional JQ labels."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import struct

from ashare_data.f1_batch import (
    DEFINITION,
    MINIMUM,
    compute_panel,
    compare_panels,
    coverage_summary,
)
from ashare_data.f1_batch_sources import read_captures


def dump(path, value):
    content = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def run(root, scope_path, reference_path=None):
    raw_scope = scope_path.read_bytes()
    frozen_sha = scope_path.with_suffix(".sha256").read_text().split()[0]
    if hashlib.sha256(raw_scope).hexdigest() != frozen_sha:
        raise ValueError("frozen scope changed")
    scope = json.loads(raw_scope)
    dataset = read_captures(root, scope)
    result = root / "results"
    result.mkdir(exist_ok=True)
    input_sha = dump(root / "derived/dataset.json", dataset)
    days = [
        d for d in dataset["sessions"] if any(a <= d <= b for a, b in scope["local_output_windows"])
    ]
    if not set(scope["reference_dates"]) <= set(days):
        raise ValueError("reference date absent from local output windows")
    local = compute_panel(dataset, days, definition=DEFINITION)
    other = compute_panel(dataset, days, definition=DEFINITION, provider="tencent")
    # Financial snapshots are the same input and cannot serve as independent comparators.
    for row in other:
        if row["field"] not in MINIMUM:
            row.update(
                value=None, status="missing", reason="NO_INDEPENDENT_BROAD_FINANCIAL_REFERENCE"
            )
    local_sha = dump(result / "local-panel.json", local)
    dump(result / "tencent-panel.json", other)
    full_scope = {**scope, "reference_dates": days}
    source_comparison = compare_panels(
        local,
        other,
        full_scope,
        comparison_kind="market_source_control_shared_Sina_factors_and_shares",
    )
    dump(result / "source-comparison.json", source_comparison)
    reference = json.loads(reference_path.read_text()) if reference_path else []
    if reference_path:
        expected = {
            (s["security"], day, field)
            for s in scope["securities"]
            for day in scope["reference_dates"]
            for field in scope["fields"]
        }
        seen = set()
        for row in reference:
            key = row["security"], row["date"], row["field"]
            if key not in expected or key in seen or row.get("provider") != "JoinQuant":
                raise ValueError("unexpected, duplicate or non-JQ reference cell")
            seen.add(key)
            if row["value"] is None:
                if (
                    row.get("missing_kind") not in {"NaN", "absent"}
                    or row.get("float64_bits_be") is not None
                ):
                    raise ValueError("inconsistent reference missing mask")
            elif (
                row.get("missing_kind") != "none"
                or not math.isfinite(float(row["value"]))
                or struct.pack(">d", float(row["value"])).hex() != row.get("float64_bits_be")
            ):
                raise ValueError("reference float64 transport mismatch")
    comparison = compare_panels(local, reference, scope, comparison_kind="JQ_named_factors")
    dump(result / "jq-comparison.json", comparison)
    request = {
        "scope_sha256": frozen_sha,
        "input_sha256": input_sha,
        "predictions_sha256": local_sha,
        "fields": scope["fields"],
        "dates": scope["reference_dates"],
        "securities": [s["security"] for s in scope["securities"]],
        "maximum_returned_values": 624,
        "existing_spend": 333,
        "authorized_total": 1000,
        "points_before_and_after_required": True,
        "stop_on_charge_or_points_decrease": True,
        "current_points": "not_checked",
        "requested_role": "practical_panel_validation; preserve exact failures",
        "browser_release_required": True,
        "minimum_calls": 4,
        "local_computable_cells": sum(
            r["value"] is not None and r["date"] in scope["reference_dates"] for r in local
        ),
        "reason_full_matrix": "One batch per frozen date preserves missing masks and common cross-section; no precision/input probes.",
    }
    dump(result / "reference-request.json", request)
    summary = {
        "scope_sha256": frozen_sha,
        "input_sha256": input_sha,
        "predictions_sha256": local_sha,
        "days": len(days),
        "securities": len(dataset["items"]),
        "cells": len(local),
        "coverage": coverage_summary(local),
        "source_comparison": source_comparison["fields"],
        "jq_comparison": comparison["fields"],
        "new_JQ_values": len(reference),
        "original_exact_alignment_unchanged": True,
        "independent_review_completed": False,
    }
    dump(result / "summary.json", summary)
    print(
        json.dumps(
            {"days": len(days), "cells": len(local), "coverage": summary["coverage"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--scope", type=Path, default=Path("docs/f1-batch-scope.json"))
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    run(args.root, args.scope, args.reference)
