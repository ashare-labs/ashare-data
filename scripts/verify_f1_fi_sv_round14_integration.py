"""Replay the unchanged round14 experiment and its separate finite review offline."""

import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path

from ashare_data import f1
from ashare_data.f1_precision import require

from verify_f1_fi_sv_round14 import verify as verify_original


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_file(root, name):
    relative = Path(name)
    require(not relative.is_absolute() and ".." not in relative.parts, "unsafe local path")
    path = root / relative
    require(path.resolve().is_relative_to(root.resolve()), "local path escapes root")
    require(not path.is_symlink() and path.is_file(), "missing or symlinked local file")
    return path


def verify(repo, evidence):
    contract = read(repo / "docs/f1-fi-sv-round14.json")
    binding = contract["local_integration"]
    manifest_path = local_file(evidence, "manifest.json")
    require(sha(manifest_path) == binding["manifest_sha256"], "local evidence manifest changed")
    manifest = read(manifest_path)
    for name, expected in manifest["files"].items():
        path = local_file(evidence, name)
        require(
            path.stat().st_size == expected["bytes"] and sha(path) == expected["sha256"],
            "local evidence changed: " + name,
        )
    for source in binding["unchanged_code"]:
        require(
            sha(local_file(repo, source["path"])) == source["source_sha256"],
            "frozen implementation changed: " + source["path"],
        )
    verdict = read(evidence / "reviews/outputs-round14/scoped-verdict.json")
    require(verdict["reviewed_commit"] == binding["source_commit"], "reviewed source differs")
    require(
        verdict["integrate_research_increment"] and not verdict["blocking_P0_to_P2_findings"],
        "finite integration review missing",
    )
    require(
        verdict["original_zero_ULP_rule"]
        == {"passed": 10, "cells": 11, "batch_pass": False, "unchanged": True},
        "original strict failure changed",
    )
    require(
        contract["status"]["finite_batch_passed"] is False
        and contract["status"]["independent_review_completed"] is False,
        "historical observation status changed",
    )
    review = contract["independent_review"]
    require(
        review["completed"] and review["original_experiment_reclassified_as_pass"] is False,
        "independent review classification changed",
    )
    require(
        review["exact_named_keys"] == verdict["exact_named_keys"]
        and review["exact_raw_keys"] == verdict["exact_raw_keys"],
        "exact review keys differ",
    )
    require(
        review["numeric_equivalence_key"] == verdict["numeric_equivalence_key"]
        and review["numeric_equivalence_api"] == verdict["numeric_equivalence_api"],
        "numeric-equivalence key differs",
    )

    bundle = evidence / "bundle"
    original = verify_original(bundle)
    inputs = read(bundle / "fi-sv-confirmation-round14/integration-inputs.json")
    security, day, field = verdict["numeric_equivalence_key"]
    require(inputs["market"]["security"] == security, "SV input security differs")
    result = f1.sv_stage_candidate(
        inputs["market"], inputs["sessions"], inputs["shares"], day, mode=f1.SHARE_MODE
    )
    cap = result["SV02"]
    require(
        cap["candidate_status"] == "failed_round14_named_key_zero_ULP_rule_preserved",
        "original failed candidate was promoted",
    )
    # Derive the arithmetic bound from the independent candidate before reading
    # the saved target. This is a post-experiment analysis, never a new tolerance.
    q, scale = Fraction(cap["raw_cap4_decimal"]), 100_000_000
    frozen_float = float(cap["prediction"]["float64_17g"])
    exact_cny = q * scale
    bound = (
        scale * Fraction.from_float(math.ulp(float(q))) / 2
        + Fraction.from_float(math.ulp(frozen_float)) / 2
    )
    require(
        Fraction(frozen_float) - exact_cny == Fraction(1, 4096), "frozen conversion error changed"
    )
    require(bound == Fraction(verdict["roundoff_bound_CNY"]), "independent roundoff bound differs")
    require(bound < 1, "one-yuan discrepancies cannot be hidden in this bound")
    records = read(bundle / "fi-sv-confirmation-round14/reference-records.json")
    matches = [
        row
        for row in records
        if (row["api"], row["security"], row["date"], row["field"])
        == (verdict["numeric_equivalence_api"], security, day, field)
    ]
    require(len(matches) == 1, "unique named SV02 reference required")
    observed = Fraction(float(matches[0]["value_17g"]))
    require(observed == exact_cny, "observed amount differs from exact unit conversion")
    require(
        observed - Fraction(frozen_float) == Fraction(-1, 4096),
        "original failed difference changed",
    )
    require(
        abs(observed - Fraction(frozen_float)) <= bound,
        "finite numeric equivalence no longer holds",
    )
    return {
        "status": "verified",
        "source_commit": binding["source_commit"],
        "local_evidence_files_verified": len(manifest["files"]),
        "unchanged_source_files_verified": len(binding["unchanged_code"]),
        "original_experiment": original,
        "independent_review_completed": True,
        "FI02_named_finite_agreement": True,
        "SV01_named_finite_agreement": True,
        "SV02_original_zero_ULP_passed": False,
        "SV02_fixed_key_numerically_equivalent": True,
        "SV02_error_bound_CNY": verdict["roundoff_bound_CNY"],
        "original_experiment_reclassified_as_pass": False,
        "supplier_conversion_order_identified": False,
        "strict_PIT_certified": False,
        "all_twelve_slots_passed": False,
        "new_JQ_calls": 0,
        "browser_actions": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    evidence = args.evidence or repo / ".local-evidence/f1-round14"
    print(json.dumps(verify(repo, evidence.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
