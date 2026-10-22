"""Independent small mathematical cases; these synthetic prices are not market evidence."""

from dataclasses import replace
import importlib.util
import json
import hashlib
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from ashare_data.factors.evaluation import EvaluationConfig, evaluate

pytestmark = pytest.mark.skipif(importlib.util.find_spec("alphalens") is None,
                                reason="optional factor-evaluation extra not installed")


@pytest.fixture
def sample():
    # Irregular actual-session labels test positional rather than calendar-day shifts.
    dates = pd.DatetimeIndex(["2024-02-05", "2024-02-06", "2024-02-07", "2024-02-08",
                              "2024-02-19", "2024-02-20", "2024-02-21", "2024-02-22",
                              "2024-02-23", "2024-02-26", "2024-02-27", "2024-02-28"])
    assets = [f"asset{i}" for i in range(10)]
    values = pd.DataFrame(np.tile(np.arange(1., 11.), (len(dates), 1)), index=dates, columns=assets)
    rates = np.arange(1., 11.) / 1000
    prices = pd.DataFrame(100 * np.power(1 + rates, np.arange(len(dates))[:, None]),
                          index=dates, columns=assets)
    membership = pd.DataFrame(True, index=dates, columns=assets)
    config = EvaluationConfig(input_id="synthetic-unit-test", universe_label="ten synthetic assets",
                              price_basis="synthetic close", horizons=(1, 3), rolling_window=3)
    return values, prices, membership, config


def run(sample, **changes):
    v, p, m, c = sample
    return evaluate(v, p, membership=m, config=replace(c, **changes))


def cell(result, table, day="2024-02-05", horizon=1, **extra):
    return next(row for row in result[table] if row.get("date") == day
                and row.get("horizon", horizon) == horizon
                and all(row.get(k) == v for k, v in extra.items()))


def test_known_ic_quantiles_raw_spread_and_costs(sample):
    result = run(sample)
    assert cell(result, "ic")["value"] == pytest.approx(1)
    assert cell(result, "rank_ic")["value"] == pytest.approx(1)
    assert cell(result, "quantile_returns", quantile=5)["mean_return"] == pytest.approx(.0095)
    assert cell(result, "quantile_returns", quantile=1)["mean_return"] == pytest.approx(.0015)
    assert cell(result, "spread")["value"] == pytest.approx(.008)  # not Alphalens 0.5 half-scale
    cost = next(x for x in result["cost_sensitivity"] if x["horizon"] == 1 and x["cost_bps"] == 10)
    assert cost["top_mean"] == pytest.approx(.0075)
    assert cost["bottom_mean"] == pytest.approx(-.0005)
    assert cost["spread_mean"] == pytest.approx(.004)
    assert cost["n"] == 10
    assert result["assumptions"]["execution_simulated"] is False
    json.dumps(result, allow_nan=False)


def test_lag_excludes_same_day_price_and_uses_session_grid(sample):
    first = run(sample)
    values, prices, membership, config = sample
    prices.iloc[0] *= np.arange(1, 11)  # signal-day close must not be the entry price
    second = run(sample)
    assert first["ic"] == second["ic"]
    assert first["quantile_returns"] == second["quantile_returns"]
    row = cell(second, "quantile_returns", day="2024-02-08", quantile=1)
    assert row["mean_return"] == pytest.approx(.0015)  # holiday does not consume a horizon
    third = run(sample, lag=2)
    assert cell(third, "quantile_returns", quantile=1)["mean_return"] == pytest.approx(.0015)
    assert third["coverage"]["by_horizon"][0]["paired"] == 90


def test_horizons_have_separate_pair_samples_and_compounded_labels(sample):
    result = run(sample, horizons=(1, 10))
    assert cell(result, "ic", day="2024-02-08", horizon=1)["n"] == 10
    assert cell(result, "ic", day="2024-02-08", horizon=10)["n"] == 0
    expected = (((1.009 ** 10) - 1) + ((1.01 ** 10) - 1)) / 2
    assert cell(result, "quantile_returns", horizon=10, quantile=5)["mean_return"] == pytest.approx(expected)
    assert result["coverage"]["by_horizon"][1]["excluded"] == {"insufficient_label_buffer": 110}


def test_future_missing_does_not_change_ranking_turnover_or_reweight_group(sample):
    before = run(sample)
    sample[1].iloc[2, -1] = np.nan
    result = run(sample)
    assert result["quantile_membership"] == before["quantile_membership"]
    assert result["turnover"] == before["turnover"]
    assert cell(result, "ic")["n"] == 9
    top = cell(result, "quantile_returns", quantile=5)
    assert top["selected_n"] == 2 and top["n"] == 1 and top["mean_return"] is None
    assert cell(result, "spread")["value"] is None
    assert cell(result, "quantile_returns", quantile=1)["mean_return"] == pytest.approx(.0015)


def test_missing_interior_price_invalidates_path_without_fill(sample):
    sample[1].iloc[2, 0] = np.nan
    result = run(sample)
    assert cell(result, "ic", horizon=3)["n"] == 9
    assert result["coverage"]["by_horizon"][1]["excluded"]["missing_path_price"] >= 1
    assert sample[1].iloc[2, 0] is np.nan or np.isnan(sample[1].iloc[2, 0])


def test_signal_membership_not_future_membership_controls_selection(sample):
    v, p, m, c = sample
    m.iloc[0, -1] = False
    m.iloc[1:] = False
    result = run(sample)
    assert cell(result, "ic")["n"] == 9
    selected = [x["security"] for x in result["quantile_membership"] if x["date"] == "2024-02-05"]
    assert len(selected) == 9 and "asset9" not in selected
    assert cell(result, "quantile_returns", quantile=5)["mean_return"] == pytest.approx(.0085)


def test_constant_factor_and_constant_returns_are_undefined(sample):
    sample[0].iloc[0] = 7
    result = run(sample)
    assert cell(result, "ic")["value"] is None
    assert cell(result, "rank_ic")["value"] is None
    assert result["coverage"]["rejected_quantile_dates"][0]["reason"] == "tied_quantile_edges"
    sample[1].iloc[2] = sample[1].iloc[1]  # all forward returns exactly zero
    result = run(sample)
    assert cell(result, "ic")["value"] is None
    assert cell(result, "rank_ic")["value"] is None


def test_ties_not_split_by_security_order(sample):
    sample[0].iloc[0] = [1, 1, 1, 1, 1, 1, 1, 2, 3, 4]
    result = run(sample)
    assert cell(result, "ic")["value"] is not None  # IC independent from quantile admission
    assert not [r for r in result["quantile_membership"] if r["date"] == "2024-02-05"]
    v, p, m, c = sample
    reverse = evaluate(v.iloc[:, ::-1], p.iloc[:, ::-1], membership=m.iloc[:, ::-1], config=c)
    assert result["coverage"]["rejected_quantile_dates"] == reverse["coverage"]["rejected_quantile_dates"]


def test_turnover_uses_preceding_session_and_never_bridges_missing_factor_day(sample):
    v, _, _, _ = sample
    v.iloc[1] = v.iloc[1].to_numpy()[::-1]
    v.iloc[2] = np.nan
    result = run(sample)
    assert cell(result, "turnover", quantile=5)["value"] is None
    assert cell(result, "turnover", day="2024-02-06", quantile=5)["value"] == 1
    assert cell(result, "turnover", day="2024-02-07", quantile=5)["value"] is None
    assert cell(result, "turnover", day="2024-02-08", quantile=5)["value"] is None
    assert cell(result, "turnover", day="2024-02-19", quantile=5)["value"] == 0


def test_direction_is_explicit_and_flips_spread(sample):
    result = run(sample, direction=-1)
    assert cell(result, "ic")["value"] == pytest.approx(-1)
    assert cell(result, "rank_ic")["value"] == pytest.approx(-1)
    assert cell(result, "spread")["value"] == pytest.approx(-.008)


def test_empty_membership_and_all_nan_factors_return_json_nulls(sample):
    sample[2].iloc[:] = False
    result = run(sample)
    assert result["coverage"]["finite_factor_cells"] == 0
    assert result["summary"][0]["ic"]["mean"] is None
    assert all(r["value"] is None for r in result["turnover"])
    sample[2].iloc[:] = True
    sample[0].iloc[:] = np.nan
    result = run(sample)
    assert result["coverage"]["missing_factor_cells"] == 120
    json.dumps(result, allow_nan=False)


def test_window_and_no_future_append_changes_earlier_metrics(sample):
    result = run(sample, end_date="2024-02-08")
    assert result["coverage"]["sessions"] == 4
    assert len(result["ic"]) == 8
    assert result["coverage"]["by_horizon"][1]["paired"] == 40
    sample[1].iloc[8:] *= 123
    changed = run(sample, end_date="2024-02-08")
    assert result["ic"] == changed["ic"]
    assert result["quantile_returns"] == changed["quantile_returns"]


def test_stability_and_identity(sample):
    result = run(sample)
    rolling = [r for r in result["stability"] if r["kind"] == "rolling" and r["horizon"] == 1]
    assert rolling[1]["rank_ic_mean"] is None
    assert rolling[2]["rank_ic_mean"] == pytest.approx(1)
    assert result["summary"][0]["rank_ic"]["mean_over_std"] is None
    digest = result["engine"]["input_sha256"]
    sample[2].iloc[0, 0] = False
    assert run(sample)["engine"]["input_sha256"] != digest


@pytest.mark.parametrize("field,value", [
    ("lag", 0), ("lag", True), ("horizons", [0]), ("horizons", [1, 1]),
    ("horizons", [True]), ("direction", 0), ("cost_bps", [float("nan")]),
    ("cost_bps", [-1]), ("min_assets", 1), ("universe_mode", "all_market"),
    ("input_id", ""), ("price_basis", ""), ("rolling_window", 1),
])
def test_invalid_config_rejected(sample, field, value):
    with pytest.raises(ValueError):
        run(sample, **{field: value})


@pytest.mark.parametrize("damage", ["mismatched_columns", "duplicate_date", "timezone", "infinity",
                                   "zero_price", "negative_price", "membership_nan", "membership_numeric"])
def test_invalid_frames_rejected(sample, damage):
    v, p, m, c = sample
    if damage == "mismatched_columns":
        p = p.iloc[:, ::-1]
    elif damage == "duplicate_date":
        v.index = pd.DatetimeIndex([v.index[0], *v.index[:-1]])
    elif damage == "timezone":
        v.index = v.index.tz_localize("Asia/Shanghai")
    elif damage == "infinity":
        v.iloc[0, 0] = np.inf
    elif damage in ("zero_price", "negative_price"):
        p.iloc[0, 0] = 0 if damage == "zero_price" else -1
    elif damage == "membership_nan":
        m = m.astype("boolean")
        m.iloc[0, 0] = pd.NA
    else:
        m = m.astype(int)
    with pytest.raises(ValueError):
        evaluate(v, p, membership=m, config=c)


def test_minimum_cross_section_and_pair_count(sample):
    sample[1].iloc[2, :6] = np.nan
    result = run(sample)
    assert cell(result, "ic")["n"] == 4
    assert cell(result, "ic")["value"] is None
    assert cell(result, "rank_ic")["value"] is None


def test_optional_backend_is_lazy_and_inputs_are_not_mutated(sample):
    code = "import sys; import ashare_data.factors.evaluation; assert 'alphalens' not in sys.modules"
    subprocess.run([sys.executable, "-B", "-c", code], check=True)
    copies = [frame.copy(deep=True) for frame in sample[:3]]
    run(sample)
    for before, after in zip(copies, sample[:3], strict=True):
        pd.testing.assert_frame_equal(before, after)


def test_frozen_cli_null_column_hash_and_no_overwrite(sample, tmp_path):
    v, p, m, c = sample
    v.iloc[:, 0] = np.nan
    payload = dict(schema="ad.factor-evaluation-input.v1", dates=v.index.strftime("%Y-%m-%d").tolist(),
                   securities=v.columns.tolist(), values=v.replace({np.nan: None}).to_numpy().tolist(),
                   prices=p.to_numpy().tolist(), membership=m.to_numpy().tolist(), config=c.to_dict())
    raw = json.dumps(payload, allow_nan=False).encode()
    source, output = tmp_path / "input.json", tmp_path / "output.json"
    source.write_bytes(raw)
    script = Path(__file__).parents[1] / "scripts/evaluate_factor.py"
    args = [sys.executable, "-B", str(script), "--input", str(source), "--sha256",
            hashlib.sha256(raw).hexdigest(), "--output", str(output)]
    subprocess.run(args, check=True, capture_output=True)
    report = json.loads(output.read_text())
    assert report["coverage"]["missing_factor_cells"] == 12
    before = output.read_bytes()
    assert subprocess.run(args, capture_output=True).returncode != 0
    assert output.read_bytes() == before
    source.write_bytes(raw + b" ")
    failed = subprocess.run(args, capture_output=True)
    assert failed.returncode != 0 and b"digest mismatch" in failed.stderr
