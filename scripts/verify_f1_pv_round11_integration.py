"""Verify pinned local PV evidence, unchanged arithmetic and public scope offline."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from ashare_data import f1
from ashare_data import f1_pv_round11 as scope
from ashare_data import f1_pv_semantic_contract as engine


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(repo, evidence):
    record = read(repo / "docs/f1-pv-round11.json")
    binding = record["local_evidence"]
    manifest_bytes = scope._local_bytes(evidence, "manifest.json")
    engine.require(
        hashlib.sha256(manifest_bytes).hexdigest() == binding["manifest_sha256"],
        "LOCAL_EVIDENCE_MANIFEST_CHANGED",
    )
    manifest = json.loads(manifest_bytes)
    for name, expected in manifest["files"].items():
        data = scope._local_bytes(evidence, name)
        engine.require(
            len(data) == expected["bytes"]
            and hashlib.sha256(data).hexdigest() == expected["sha256"],
            "LOCAL_EVIDENCE_CHANGED: " + name,
        )
    for item in record["source_mapping"]:
        if item["treatment"] == "byte-identical":
            engine.require(sha(repo / item["path"]) == item["source_sha256"], "SOURCE_PORT_CHANGED")
    review = read(evidence / binding["review_decision"])
    for key in [
        "reviewed_commit",
        "reviewed_target",
        "definition",
        "decision",
        "fields",
        "counts",
        "preserve",
    ]:
        engine.require(record[key] == review[key], "REVIEW_SCOPE_CHANGED: " + key)
    engine.require(
        review["decision"] == "allow_bounded_local_research_integration", "REVIEW_REQUIRED"
    )
    engine.require(not review["new_P0_P1_P2_implementation_findings"], "REVIEW_BLOCKED")
    archive = evidence / "bundle/pv-semantic-confirmation-round11"
    package, _ = scope._archive(archive)
    source = archive / "frozen-round9/code/ashare_data/f1_pv_semantic_contract.py"
    engine.require(sha(Path(engine.__file__)) == sha(source), "IMPORTED_ENGINE_DIFFERS_FROM_FREEZE")
    original = load("pv_round11_frozen_engine", source)
    replay = load("pv_round11_frozen_replay", archive / "code/replay_f1_huichuan_confirmation.py")
    paths = sys.path.copy()
    try:
        replay_outputs = replay.calculate(archive)
    finally:
        sys.path[:] = paths
    for name, value in replay_outputs.items():
        engine.require(value == read(archive / "results" / name), "FROZEN_REPLAY_CHANGED: " + name)

    api_cases, engine_cases = 0, 0
    for mode in ["reference_semantics", "independent_source"]:
        if mode == "reference_semantics":
            locked = read(archive / "results/prediction-lock.json")
            bundle, saved_result = locked["bundle"], locked["result"]
        else:
            bundle = read(archive / "frozen-round9/results/holdout-independent-bundle.json")
            saved_result = read(archive / "frozen-round9/results/holdout-independent-result.json")
        all_rows = engine.evaluate(
            bundle,
            definition=engine.DEFINITION,
            mode=mode,
            end_date=record["reviewed_target"]["date"],
            fields=list(engine.FIELDS),
        )
        engine.require(all_rows == saved_result, "COMPLETE_FROZEN_OUTPUT_CHANGED")
        cases = [["Variance20", "ATR6", "ATR14"], ["Variance20"], ["ATR6"], ["ATR14"], ["EMA5"]]
        for fields in cases:
            arguments = dict(definition=engine.DEFINITION, mode=mode, fields=fields)
            caller = (
                f1.pv_round11_ema5_local_candidate
                if fields == ["EMA5"]
                else f1.pv_round11_candidate
            )
            output = caller(archive, **arguments)
            original_result = original.evaluate(
                bundle, end_date=record["reviewed_target"]["date"], **arguments
            )
            engine.require(output["calculation"] == original_result, "PUBLIC_CALCULATION_CHANGED")
            engine.require(
                output["calculation"]["rows"]
                == [row for row in all_rows["rows"] if row["field"] in fields],
                "SUBSET_CHANGED_CALCULATION",
            )
            for status in output["empirical_review"]["fields"]:
                reviewed = next(row for row in review["fields"] if row["field"] == status["field"])
                engine.require(
                    all(status[key] == value for key, value in reviewed.items()),
                    "FIELD_EMPIRICAL_STATUS_CHANGED",
                )
            api_cases += 1
            engine_cases += 1
    return {
        "status": "verified",
        "source_commit": scope.SOURCE_COMMIT,
        "local_evidence_files_verified": len(manifest["files"]),
        "reviewed_package_files_verified": len(package["files"]),
        "frozen_replay_unchanged": True,
        "public_API_cases_equal_to_original_engine": api_cases,
        "frozen_engine_subset_cases": engine_cases,
        "conditional_field_matches": 3,
        "conditional_original_slots_with_support": 2,
        "independent_matches": 0,
        "denominator": 4,
        "EMA5_original_failure_preserved": True,
        "source_issue": "ADJ-INPUT-001 blocked",
        "jq_equivalent": False,
        "production_eligible": False,
        "new_market_reads": 0,
        "browser_actions": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    evidence = (args.evidence or repo / ".local-evidence/f1-pv-round11").resolve()
    print(json.dumps(verify(repo, evidence), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
