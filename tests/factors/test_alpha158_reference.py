"""Offline fixture and comparator regressions; Qlib stays outside runtime dependencies."""
import csv
import gzip
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("alpha158_reference", ROOT / "scripts/alpha158_reference.py")
reference = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reference)
BUNDLE = ROOT / "tests/fixtures/alpha158-reference"


def matrix(values, *, key=("SYN", "2024-01-02"), names=None):
    return (["instrument", "datetime", *(names or [f"F{i}" for i in range(len(values))])],
            {key: values})


def test_official_bundle_integrity_and_default_contract():
    manifest, header, rows = reference.verify_bundle(BUNDLE)
    snapshot = json.loads((BUNDLE / "features.json").read_text())
    assert manifest["qlib_commit"] == "da920b7f954f48ab1bb64117c976710de198373e"
    assert manifest["qlib_version"] == "0.9.7"
    assert manifest["synthetic_only"] is True
    assert manifest["contract"]["processors"] == []
    assert manifest["input_rows"] == 641
    assert len(rows) == 643
    assert len(header) == 160
    assert header[2:15] == ["KMID", "KLEN", "KMID2", "KUP", "KUP2", "KLOW", "KLOW2",
                             "KSFT", "KSFT2", "OPEN0", "HIGH0", "LOW0", "VWAP0"]
    assert header[-1] == "VSUMD60"
    features = {value["name"]: value for value in snapshot["features"]}
    assert features["ROC5"]["expression"] == "Ref($close, 5)/$close"
    assert features["ROC60"]["lookback"] == 60
    assert features["MA60"]["lookback"] == 59
    assert features["CORD60"]["lookback"] == 60
    assert all(item["lookahead"] == 0 for item in features.values())
    assert all(manifest["checks"].values())
    assert manifest["output_value_counts"]["nan"] > 0
    assert manifest["output_value_counts"]["positive_inf"] > 0


def test_original_synthetic_input_is_exactly_reproducible():
    calendar, instruments, rows = reference.synthetic_inputs()
    assert (BUNDLE / "input.csv").read_bytes() == reference.input_bytes(rows)
    assert (BUNDLE / "calendar.txt").read_text() == "\n".join(calendar) + "\n"
    assert json.loads((BUNDLE / "instruments.json").read_text()) == instruments
    lookup = {(r["instrument"], r["datetime"]): r for r in rows}
    assert ("SYN_MISSING", calendar[20]) not in lookup
    assert math.isnan(lookup["SYN_ZERO_VOLUME", calendar[20]]["vwap"])
    assert lookup["SYN_ZERO_VOLUME", calendar[20]]["volume"] == 0
    assert sum(r["instrument"] == "SYN_SHORT" for r in rows) == 3


def test_selected_official_edge_semantics():
    _, header, rows = reference.verify_bundle(BUNDLE)
    calendar = (BUNDLE / "calendar.txt").read_text().splitlines()

    def value(case, index, feature):
        return rows[case, calendar[index]][header.index(feature) - 2]

    assert value("SYN_CONSTANT", 79, "RANK5") == pytest.approx(0.6)
    assert value("SYN_CONSTANT", 79, "IMAX5") == pytest.approx(0.2)
    assert value("SYN_CONSTANT", 79, "SUMP5") == 0
    assert value("SYN_CONSTANT", 79, "SUMN5") == 0
    assert math.isnan(value("SYN_NEAR_CONSTANT", 79, "RSQR5"))
    assert value("SYN_ALL_MISSING", 79, "CNTP5") == 0
    assert value("SYN_ZERO_PRICE", 10, "OPEN0") == math.inf
    assert math.isnan(value("SYN_MISSING", 33, "VWAP0"))
    assert math.isnan(value("SYN_NORMAL", 59, "ROC60"))
    assert math.isfinite(value("SYN_NORMAL", 60, "ROC60"))
    assert value("SYN_SHORT", 77, "MA60") == 1


def test_comparator_accepts_equal_ieee_values_and_reordered_rows():
    expected = matrix([math.nan, math.inf, -math.inf, 0.0, 2.0])
    expected[1]["SECOND", "2024-01-03"] = [1, 2, 3, 4, 5]
    candidate = (expected[0], dict(reversed(list(expected[1].items()))))
    result = reference.compare_matrices(expected, candidate)
    assert result["passed"]
    assert result["compared_cells"] == 10


@pytest.mark.parametrize(("expected", "actual", "kind"), [
    ([1.0], [1.01], "finite_value"),
    ([math.nan], [0], "nan_mask"),
    ([0], [math.nan], "nan_mask"),
    ([math.inf], [-math.inf], "infinity_value_or_sign"),
    ([math.inf], [1e300], "infinity_value_or_sign"),
])
def test_comparator_detects_numeric_and_mask_errors(expected, actual, kind):
    result = reference.compare_matrices(matrix(expected), matrix(actual))
    assert not result["passed"]
    assert result["mismatch_counts"] == {kind: 1}
    assert result["examples"][0]["feature"] == "F0"
    json.dumps(result, allow_nan=False)


def test_comparator_rejects_missing_and_extra_keys_without_intersection_pass():
    result = reference.compare_matrices(matrix([1]), matrix([1], key=("WRONG", "2024-01-02")))
    assert not result["passed"]
    assert result["missing_row_count"] == result["extra_row_count"] == 1
    assert result["compared_cells"] == 0


def test_comparator_enforces_feature_order_and_explicit_tolerance():
    with pytest.raises(ValueError, match="column names/order"):
        reference.compare_matrices(matrix([1, 2]), matrix([2, 1], names=["F1", "F0"]))
    assert reference.compare_matrices(matrix([1]), matrix([1.000001]))["passed"]
    assert not reference.compare_matrices(matrix([1]), matrix([1.000001]), rtol=0, atol=0)["passed"]


@pytest.mark.parametrize("tolerance", [-1, math.nan, math.inf])
def test_comparator_rejects_invalid_tolerances(tolerance):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        reference.compare_matrices(matrix([1]), matrix([1]), rtol=tolerance)


@pytest.mark.parametrize("body", [
    "instrument,datetime,F,F\nX,2024-01-02,1,2\n",
    "instrument,datetime,F\nX,2024-01-02,1\nX,2024-01-02,2\n",
    "instrument,datetime,F\nX,2024-01-02,1,2\n",
    "instrument,datetime,F\nX,2024-01-02,no-value\n",
    "instrument,datetime,F\nX,2024-02-30,1\n",
    "instrument,datetime,F\nX,2024-01-02,1e999\n",
    "instrument,datetime,F\n",
])
def test_csv_reader_rejects_malformed_candidates(tmp_path, body):
    path = tmp_path / "bad.csv"
    path.write_text(body)
    with pytest.raises(ValueError):
        reference.read_matrix(path)


def test_csv_reader_accepts_empty_nan_from_pandas(tmp_path):
    path = tmp_path / "candidate.csv"
    path.write_text("instrument,datetime,F\nX,2024-01-02,\n")
    assert math.isnan(reference.read_matrix(path)[1]["X", "2024-01-02"][0])


def test_fixture_corruption_is_rejected_before_comparison(tmp_path):
    target = tmp_path / "bundle"
    shutil.copytree(BUNDLE, target)
    with (target / "input.csv").open("a") as stream:
        stream.write("corrupted\n")
    with pytest.raises(ValueError, match="checksum mismatch: input.csv"):
        reference.verify_bundle(target)


def test_generation_never_overwrites_existing_output(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError, match="new directory"):
        reference.generate(target, tmp_path / "unused-source")
    assert sentinel.read_text() == "keep"


def test_comparator_cli_works_without_qlib_imports():
    program = (
        "import runpy,sys; script,candidate=sys.argv[1:]; sys.modules['qlib']=None; "
        "sys.argv=[script,'compare','--candidate',candidate]; "
        "runpy.run_path(script,run_name='__main__')"
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program,
                             str(ROOT / "scripts/alpha158_reference.py"),
                             str(BUNDLE / "expected.csv.gz")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"]


def test_comparator_cli_reports_failure_with_nonzero_exit(tmp_path):
    with gzip.open(BUNDLE / "expected.csv.gz", "rt") as stream:
        records = list(csv.reader(stream))
    records[1][2] = "123456789"
    path = tmp_path / "candidate.csv"
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows(records)
    path.write_text(stream.getvalue())
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/alpha158_reference.py"),
                             "compare", "--candidate", str(path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert not json.loads(result.stdout)["passed"]
