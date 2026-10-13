"""Verify local evidence bindings and replay only the integrated finite F1 scope."""

import argparse
import hashlib
import json
from pathlib import Path

from ashare_data.f1_confirmation import observe_frozen_confirmation, replay_inputs
from ashare_data.f1_price_volume import RESEARCH_ROLE
from ashare_data.f1_tvma6_bounded import bounded_prediction
from ashare_data.f1_tvma6_composite import (
    compare_composite_reference,
    summarize_composite_comparisons,
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_file(root, relative):
    relative = Path(relative)
    require(not relative.is_absolute() and ".." not in relative.parts, "unsafe evidence path")
    path = root / relative
    require(path.resolve().is_relative_to(root.resolve()), "evidence escapes root")
    require(
        not any(p.is_symlink() for p in [path, *path.parents] if p != root.parent),
        "symlink in evidence path",
    )
    require(path.is_file(), f"missing evidence: {relative}")
    return path


def verify(repo, evidence):
    registry = read(repo / "docs/f1-integration.json")
    manifest_path = local_file(evidence, "manifest.json")
    require(
        sha(manifest_path) == registry["evidence"]["manifest_sha256"], "evidence manifest changed"
    )
    manifest = read(manifest_path)
    for name, expected in manifest["files"].items():
        path = local_file(evidence, name)
        require(
            path.stat().st_size == expected["bytes"] and sha(path) == expected["sha256"],
            f"evidence changed: {name}",
        )
    for source in registry["source_map"]:
        require(
            sha(local_file(repo, source["path"])) == source["integrated_sha256"],
            f"integrated source changed: {source['path']}",
        )

    fi = evidence / "fi"
    data = fi / "fi-confirmation-round12"
    pack = read(fi / "ashare-data-f1-contracts/docs/f1-inovance-inputs.json")
    lock = read(data / "prediction-lock.json")
    plan, exposure = read(data / "sample-plan.json"), read(data / "exposure.json")
    for artifact in lock["payload"]["artifacts"]:
        require(
            sha(local_file(fi, artifact["path"])) == artifact["sha256"],
            "FI frozen artifact changed",
        )
    result = {
        "inputs": replay_inputs(pack, data, lock, plan, exposure),
        "lock_sha256": lock["sha256"],
        "observation": observe_frozen_confirmation(
            lock, read(data / "reference-records.json"), read(data / "execution-authorization.json")
        ),
    }
    require(
        result == read(fi / "evidence/confirmation-results-round12.json"),
        "FI replay differs from frozen result",
    )
    verdict = read(evidence / "reviews/fi-scoped-verdict.json")
    require(
        verdict["formal_finite_confirmation"] is True
        and verdict["source_commit"] == registry["sources"]["fi"],
        "FI independent verdict differs",
    )
    require(
        verdict["prediction_lock_payload_sha256"] == lock["sha256"],
        "FI review does not bind prediction lock",
    )
    keys = [
        {k: row[k] for k in ("security", "date", "slot", "period_end")}
        for row in lock["payload"]["rows"]
    ]
    require(keys == registry["financial"]["keys"], "FI integrated scope differs")
    for reviewed, predicted in zip(verdict["scope_rows"], lock["payload"]["rows"], strict=True):
        require(reviewed["prediction"] == predicted["prediction"], "FI verdict prediction differs")
    require(result["observation"]["passed_cells"] == 3, "FI finite numeric result changed")
    require(
        not result["observation"]["legacy_pre_read_review_certificate_issued"],
        "legacy review was backdated",
    )

    pv = evidence / "pv/pv-tvma6-p2-correction"
    prior = pv / "prior-round5"
    predictions = read(prior / "evidence/frozen-predictions.json")["selected_predictions"]
    items = {
        item["security"]: item for item in read(prior / "frozen-round4/native/inputs.json")["items"]
    }
    references = read(prior / "results/tvma6-reference.json")["records"]
    by_key = {(row["security"], row["date"]): row for row in references}
    require(len(by_key) == len(references) == len(predictions) == 4, "four TVMA6 keys required")
    rows = []
    for predicted in predictions:
        computed = bounded_prediction(
            items[predicted["security"]], predicted["date"], role=RESEARCH_ROLE
        )
        audit_only = {"separately_recorded_postAmt_sum_CNY_not_added"}
        require(set(predicted) == set(computed) | audit_only, "TVMA6 prediction fields changed")
        require(
            computed == {key: predicted[key] for key in computed},
            "TVMA6 prediction differs from frozen native-input output",
        )
        reference = by_key[predicted["security"], predicted["date"]]
        rows.append(
            compare_composite_reference(computed, reference_float_hex=reference["float_hex"])
        )
    require(
        {"comparisons": rows} == read(pv / "results/four-observation-comparisons-v2.json"),
        "TVMA6 comparisons changed",
    )
    summary = summarize_composite_comparisons(rows)
    require(summary == read(pv / "results/criteria-summary-v2.json"), "TVMA6 summary changed")
    require(
        summary["composite_model"]["all_compatible"]
        and summary["composite_model"]["compatible_count"] == 4,
        "TVMA6 conditional result changed",
    )
    require(
        summary["original_v1_checks"]["pass_counts"]["original_strict"] == 0,
        "TVMA6 historical failure changed",
    )
    require(not summary["JQ_equivalence_or_production_acceptance"], "TVMA6 certification promoted")
    return {
        "status": "verified",
        "evidence_files_verified": len(manifest["files"]),
        "integrated_sources_verified": len(registry["source_map"]),
        "financial_complete_prediction_and_observation_equal": True,
        "financial_real_cells_zero_ULP": 3,
        "financial_independent_review_completed": True,
        "legacy_pre_read_review_certificate_issued": False,
        "tvma6_predictions_and_v1_diagnostics_equal": True,
        "tvma6_composite_compatible": 4,
        "tvma6_original_strict_passed": 0,
        "tvma6_frozen_point_matches": 0,
        "full_original_twelve_factor_certifications": 0,
        "network_requests": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    evidence = args.evidence or repo / ".local-evidence/f1-integration"
    print(json.dumps(verify(repo, evidence.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
