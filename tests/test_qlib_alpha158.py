"""Candidate execution against the separately generated official Qlib oracle."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ashare_data.factors.catalog import catalog
from ashare_data.factors.qlib_alpha158 import Alpha158, coverage

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "tests/fixtures/alpha158-reference"
SPEC = importlib.util.spec_from_file_location("reference", ROOT / "scripts/alpha158_reference.py")
reference = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reference)


@pytest.fixture(scope="module")
def inputs():
    raw = pd.read_csv(BUNDLE / "input.csv")
    raw.datetime = pd.to_datetime(raw.datetime)
    calendar = pd.to_datetime((BUNDLE / "calendar.txt").read_text().splitlines())
    instruments = json.loads((BUNDLE / "instruments.json").read_text())
    names = [r["instrument"] for r in instruments]
    frames = {
        f: raw.pivot(index="datetime", columns="instrument", values=f).reindex(
            index=calendar, columns=names
        )
        for f in reference.INPUT_COLUMNS[2:]
    }
    options = dict(
        universe=pd.DataFrame(True, index=calendar, columns=names),
        adjustment="raw",
        volume_basis="synthetic shares",
        vwap_basis="explicit synthetic prices",
        instrument_ranges={r["instrument"]: (r["start_date"], r["end_date"]) for r in instruments},
    )
    return frames, options


def test_all_official_cells_names_order_and_ieee_masks(inputs):
    frames, options = inputs
    manifest, header, expected = reference.verify_bundle(BUNDLE)
    result = Alpha158(frames, **options).compute()
    actual = {
        (s, str(d.date())): [float(x) for x in row]
        for (d, s), row in result.values.iterrows()
        if (s, str(d.date())) in expected
    }
    report = reference.compare_matrices(
        (header, expected),
        (["instrument", "datetime", *result.values.columns], actual),
        rtol=0,
        atol=0,
    )
    assert report["passed"], report
    assert report["compared_cells"] == manifest["output_rows"] * 158
    assert result.values.shape == (720, 158)
    assert np.isnan(result.values.loc[(slice(None), "SYN_SHORT"), :].iloc[:77]).all().all()
    assert len(catalog("qlib_alpha158")) == 158
    assert len(catalog()) == 368
    assert coverage().loc["ROC60", "full_window_rows"] == 61
    assert coverage().loc["MA60", "full_window_rows"] == 60
    assert (
        result.status_labels().loc[(frames["close"].index[10], "SYN_ZERO_PRICE"), "OPEN0"]
        == "POSITIVE_INFINITY"
    )


def test_future_perturbation_and_subset_slicing(inputs):
    frames, options = inputs
    fields = ["ROC5", "MA60", "BETA30", "RSQR5", "CNTP5", "CORD20", "IMAX5"]
    before = Alpha158(frames, **options).compute(fields)
    changed = {k: v.copy() for k, v in frames.items()}
    for frame in changed.values():
        frame.iloc[66:] = frame.iloc[66:] * 17 + 123
    after = Alpha158(changed, **options).compute(fields)
    pd.testing.assert_frame_equal(
        before.between(end=frames["close"].index[65]).values,
        after.between(end=frames["close"].index[65]).values,
    )
    single = Alpha158(frames, **options).compute("CORD20").between(frames["close"].index[65])
    pd.testing.assert_frame_equal(
        single.values, before.between(frames["close"].index[65]).values[["CORD20"]]
    )


def test_missing_field_differs_from_nan_field_and_membership(inputs):
    frames, options = inputs
    frames = {k: v.copy() for k, v in frames.items() if k != "vwap"}
    options = dict(options, universe=options["universe"].copy())
    options["universe"].iloc[10, 4] = False
    result = Alpha158(frames, **options).compute(["VWAP0", "CNTP5"])
    assert result.metadata["missing_dependencies"] == {"VWAP0": ["vwap"]}
    labels = result.status_labels()
    assert labels.loc[(frames["close"].index[0], "SYN_NORMAL"), "VWAP0"] == "MISSING_DEPENDENCY"
    assert labels.loc[(frames["close"].index[10], "SYN_NORMAL"), "CNTP5"] == "OUT_OF_UNIVERSE"
    assert result.values.loc[(frames["close"].index[0], "SYN_ALL_MISSING"), "CNTP5"] == 0


@pytest.mark.parametrize("change", ["axes", "infinity", "overflow", "membership", "basis", "range"])
def test_input_contract_rejects_implicit_or_unsafe_alignment(inputs, change):
    frames, options = inputs
    frames = {k: v.copy() for k, v in frames.items()}
    options = dict(options)
    if change == "axes":
        frames["close"] = frames["close"].iloc[::-1]
    elif change == "infinity":
        frames["close"].iloc[0, 0] = np.inf
    elif change == "overflow":
        frames["close"].iloc[0, 0] = 1e100
    elif change == "membership":
        options["universe"] = options["universe"].astype(int)
    elif change == "basis":
        options["volume_basis"] = ""
    else:
        options["instrument_ranges"] = {}
    with pytest.raises(ValueError):
        Alpha158(frames, **options)


def test_constructor_freezes_inputs_and_rejects_arbitrary_fields(inputs):
    frames, options = inputs
    frames = {k: v.copy() for k, v in frames.items()}
    owner = Alpha158(frames, **options)
    expected = owner.compute("KMID")
    frames["close"].iloc[:] = 0
    pd.testing.assert_frame_equal(owner.compute("KMID").values, expected.values)
    for names in (["KMID", "KMID"], ["alpha158"], [], ["__import__('os')"]):
        with pytest.raises(ValueError):
            owner.compute(names)
