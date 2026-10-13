"""Run the independent review's five unchanged offline counterexamples in CI."""

import importlib.util
import json
from pathlib import Path
import unittest

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REVIEW = load(
    "f2_reference_review_regressions",
    ROOT / "tests/fixtures/f2/reference_acceptance_regression.py",
)
REVIEW.COMPARATOR = load("f2_reference_comparator", ROOT / "scripts/compare_f2_reference.py")
SOURCE_CONTROL = load("f2_source_control_comparator", ROOT / "scripts/run_f2_batch.py")


@pytest.mark.parametrize(
    "method", unittest.defaultTestLoader.getTestCaseNames(REVIEW.AvailablePoolAcceptance)
)
def test_independent_reference_acceptance(method):
    result = unittest.TestResult()
    REVIEW.AvailablePoolAcceptance(method).run(result)
    assert result.wasSuccessful(), "\n".join(
        report for _, report in result.failures + result.errors
    )


@pytest.mark.parametrize(
    "missing,accepted",
    [
        ([], True),
        ([(REVIEW.DATES[0], REVIEW.SECURITIES[-1])], False),
        ([(REVIEW.DATES[0], REVIEW.SECURITIES[0])], False),
        ([(REVIEW.DATES[0], REVIEW.SECURITIES[5])], True),
        ([(day, REVIEW.SECURITIES[5]) for day in REVIEW.DATES[:3]], False),
    ],
    ids=["complete", "missing-top", "missing-bottom", "missing-middle", "low-coverage"],
)
def test_source_control_cannot_hide_selection_changes(tmp_path, missing, accepted):
    repo, reference = REVIEW.build_fixture(tmp_path, set(missing))
    scope = json.loads((repo / "docs/f2-batch-scope.json").read_text())
    scope["securities"] = [{"security": s} for s in REVIEW.SECURITIES]
    local = json.loads(
        (repo / ".local-evidence/f2-batch/results/local-panel.json").read_text()
    )["rows"]
    other = [
        row
        for index in range(1, 5)
        for row in json.loads((reference / f"raw/call-{index}.json").read_text())["rows"]
    ]
    result = SOURCE_CONTROL.compare(local, other, scope, REVIEW.DATES)["summary"]["PSY"]
    assert result["requested"] == 48
    assert result["paired"] == 48 - len(missing)
    assert result["source_control_pass"] is accepted
