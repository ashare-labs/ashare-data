"""Replay the completed reference batch without querying or altering frozen inputs."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from ashare_data.f1_batch import compare_panels
from ashare_data.f1_batch_reference import candidate_selection_impact, validate_captures


def load(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def run(local_root, reference_root, scope_path):
    scope = load(scope_path)
    frozen = scope_path.with_suffix(".sha256").read_text().split()[0]
    if sha(scope_path) != frozen:
        raise ValueError("frozen scope changed")
    request = load(reference_root / "pre-reference-request.json")
    ledger = load(reference_root / "query-ledger.json")
    local_path = local_root / "results/local-panel.json"
    if (
        ledger["scope_sha256"] != frozen
        or request["scope_sha256"] != frozen
        or sha(local_path) != ledger["prediction_sha256"]
        or sha(local_path) != request["predictions_sha256"]
        or sha(local_root / "derived/dataset.json") != request["input_sha256"]
    ):
        raise ValueError("reference/prediction/input lock mismatch")
    captures, proof = [], []
    for day in scope["reference_dates"]:
        capture_path = reference_root / f"{day}.json"
        capture = load(capture_path)
        output_path = reference_root / f"{day}.output.txt"
        lines = output_path.read_text().strip().splitlines()
        if (
            len(lines) != 2
            or lines[0] != f"AD_F1_BATCH_RETURNED {day} {capture['returned_values']}"
            or not lines[1].startswith("AD_F1_BATCH_RESULT ")
            or json.loads(lines[1].removeprefix("AD_F1_BATCH_RESULT ")) != capture
        ):
            raise ValueError("raw rendered output differs from captured JSON")
        editor_path = reference_root / f"{day}.pre-run-editor.json"
        editor = "\n".join(load(editor_path)).replace("\u200b", "").rstrip()
        prepared = local_root / f"reference-preparation/{day}.py.txt"
        if editor != prepared.read_text().rstrip():
            raise ValueError("executed editor differs from frozen query")
        captures.append(capture)
        proof.append(
            {
                "date": day,
                "output_sha256": sha(output_path),
                "capture_sha256": sha(capture_path),
                "query_sha256": sha(prepared),
                "account_dom_sha256": sha(reference_root / f"account-after-{day}.dom.txt"),
                "account_png_sha256": sha(reference_root / f"account-after-{day}.png"),
            }
        )
    reference = validate_captures(captures, ledger, scope)
    local = load(local_path)
    comparison = compare_panels(
        local, reference, scope, comparison_kind="frozen_four_date_JQ_reference"
    )
    selection = candidate_selection_impact(local, reference, scope)
    output = reference_root / "results"
    output.mkdir(exist_ok=True)
    dump(output / "reference-rows.json", reference)
    dump(output / "jq-comparison.json", comparison)
    dump(output / "candidate-selection-impact.json", selection)
    summary = {
        "schema": "ad.f1-batch-reference-review.v1",
        "scope_sha256": frozen,
        "prediction_sha256": sha(local_path),
        "input_sha256": request["input_sha256"],
        "reference_sha256": sha(output / "reference-rows.json"),
        "ledger_sha256": sha(reference_root / "query-ledger.json"),
        "actual_new_returned_values": len(reference),
        "reference_finite_values": sum(row["value"] is not None for row in reference),
        "actual_total_spend": ledger["actual_total_spend"],
        "remaining_values": ledger["unspent_values"],
        "points_before": ledger["account_precheck"]["points"],
        "points_after": ledger["points_after"],
        "paired_cells": sum(row["paired"] for row in comparison["cells"]),
        "numeric_passed_cells": sum(row.get("numeric_pass", False) for row in comparison["cells"]),
        "rank_panels": dict(Counter(str(p["rank_pass"]) for p in comparison["panels"])),
        "fields": comparison["fields"],
        "proof": proof,
        "candidate_pool_tail_changes": [
            p for p in selection if any(p[side]["replacement_count"] for side in ("top", "bottom"))
        ],
        "original_exact_status_preserved": scope["original_status_preserved"],
        "production_eligible": False,
        "historical_pit_certified": False,
        "reference_scope": "four frozen diagnostic dates; not all 59 local output dates",
        "independent_reference_review_completed": False,
    }
    dump(output / "summary.json", summary)
    print(
        json.dumps(
            {
                k: v
                for k, v in summary.items()
                if k not in {"fields", "proof", "candidate_pool_tail_changes"}
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("local_root", type=Path)
    parser.add_argument("reference_root", type=Path)
    parser.add_argument("--scope", type=Path, default=Path("docs/f1-batch-scope.json"))
    args = parser.parse_args()
    run(args.local_root, args.reference_root, args.scope)
