"""Independent arithmetic and invariants for every opt-in ambiguity resolution."""

import json
from importlib.resources import files

import numpy as np
import pandas as pd
import pytest

from ashare_data.factors.alpha191 import (
    Alpha191,
    Alpha191Stream,
    EXPLICIT_VERSION,
    VERSION,
    coverage,
)
from ashare_data.factors.alpha191._engine import DOMAIN, MISSING, OK, WARMUP
from ashare_data.factors.catalog import catalog
from test_alpha191 import sample, rank

IDS = [36, 54, 127, 146, 165, 166, 181, 183]


def explicit(data, universe, **kwargs):
    return Alpha191(data, universe=universe, adjustment="raw", version=EXPLICIT_VERSION, **kwargs)


def scalar_ema(values, n, m):
    previous = None
    result = []
    for x in values:
        if np.isnan(x):
            previous = None
            result.append(np.nan)
        else:
            previous = x if previous is None else (m * x + (n - m) * previous) / n
            result.append(previous)
    return np.array(result)


def scalar_cumulative_range(close, t, n):
    prefix, path = 0.0, []
    for j in range(t - n + 1, t + 1):
        mean_j = sum(close[j - n + 1 : j + 1]) / n
        prefix += close[j] - mean_j
        path.append(prefix)
    mean_now = sum(close[t - n + 1 : t + 1]) / n
    std_now = (sum((x - mean_now) ** 2 for x in close[t - n + 1 : t + 1]) / (n - 1)) ** 0.5
    return (max(path) - min(path)) / std_now


def test_all_191_paths_with_explicit_inputs_and_unchanged_legacy_numbers():
    d, u, b, f = sample(640, 16)
    kwargs = dict(benchmark=b, factors=f, alpha143_seed=1, rank_policy="available")
    new = explicit(d, u, alpha146_m=1, **kwargs).compute()
    old = Alpha191(d, universe=u, adjustment="raw", **kwargs).compute()
    assert new.report.availability.value_counts().to_dict() == {
        "IMPLEMENTED": 184,
        "REQUIRES_BENCHMARK": 4,
        "REQUIRES_EXTERNAL_FACTORS": 1,
        "REQUIRES_EXPLICIT_SEED": 1,
        "REQUIRES_EXPLICIT_PARAMETER": 1,
    }
    assert new.report.valid_cells.gt(0).all()
    same = [f"alpha{i:03}" for i in range(1, 192) if i not in IDS]
    pd.testing.assert_frame_equal(new.values[same], old.values[same])
    pd.testing.assert_frame_equal(new.status[same], old.status[same])
    assert old.metadata["version"] == VERSION
    assert new.metadata["input_sha256"] != old.metadata["input_sha256"]
    assert (new.values.notna().to_numpy() == (new.status.to_numpy() == OK)).all()
    assert not np.isinf(new.values.to_numpy()).any()
    assert set(coverage(version=EXPLICIT_VERSION).expression.dropna().index) == set(new.values)
    assert (
        catalog("alpha191", alpha191_version=EXPLICIT_VERSION).definition.eq(EXPLICIT_VERSION).all()
    )
    assert coverage().availability.eq("AMBIGUOUS_FORMULA").sum() == 8


def test_eight_definitions_against_independent_scalar_calculations():
    d, u, b, _ = sample(140, 7)
    # Smooth trending benchmark avoids a near-zero cubic denominator by construction.
    b["close"] = 90 + np.arange(len(u)) ** 1.25
    got = explicit(d, u, benchmark=b, alpha146_m=2).compute(IDS).values
    c, o, v, p = [d[k].to_numpy() for k in ["close", "open", "volume", "vwap"]]
    rvol = np.array([rank(row) for row in v])
    rprice = np.array([rank(row) for row in p])
    bc = b.close.to_numpy()
    for t in range(95, 140, 7):
        corr_sums = []
        for j in range(len(u.columns)):
            corr_sums.append(
                sum(
                    np.corrcoef(rvol[k - 5 : k + 1, j], rprice[k - 5 : k + 1, j])[0, 1]
                    for k in [t - 1, t]
                )
            )
        np.testing.assert_allclose(got.loc[u.index[t], "alpha036"], rank(corr_sums))
        inner54 = [
            np.std(abs(c[t - 9 : t + 1, j] - o[t - 9 : t + 1, j]), ddof=1)
            + c[t, j]
            - o[t, j]
            + np.corrcoef(c[t - 9 : t + 1, j], o[t - 9 : t + 1, j])[0, 1]
            for j in range(len(u.columns))
        ]
        np.testing.assert_allclose(got.loc[u.index[t], "alpha054"], -rank(inner54))
        for j, security in enumerate(u.columns):
            row = got.loc[(u.index[t], security)]
            price = c[:, j]
            expected127 = (
                sum(
                    (100 * (price[k] / max(price[k - 11 : k + 1]) - 1)) ** 2
                    for k in range(t - 11, t + 1)
                )
                / 12
            ) ** 0.5
            assert row.alpha127 == pytest.approx(expected127)
            returns = np.r_[np.nan, price[1:] / price[:-1] - 1]
            base = scalar_ema(returns, 61, 2)
            denominator = scalar_ema(base**2, 60, 2)
            deviations = returns - base
            assert row.alpha146 == pytest.approx(
                sum(deviations[t - 19 : t + 1]) / 20 * deviations[t] / denominator[t]
            )
            assert row.alpha165 == pytest.approx(scalar_cumulative_range(price, t, 48), rel=1e-11)
            assert row.alpha183 == pytest.approx(scalar_cumulative_range(price, t, 24), rel=1e-11)
            num = sum(returns[k] - sum(returns[k - 19 : k + 1]) / 20 for k in range(t - 19, t + 1))
            denom = (
                sum(
                    (sum(price[k - 19 : k + 1] / price[k - 20 : k]) / 20) ** 2
                    for k in range(t - 19, t + 1)
                )
                ** 1.5
            )
            assert row.alpha166 == pytest.approx(-20 * 19**1.5 * num / (19 * 18 * denom), abs=1e-13)
            deviations_b = [bc[k] - sum(bc[k - 19 : k + 1]) / 20 for k in range(t - 19, t + 1)]
            expected181 = (num - sum(x**2 for x in deviations_b)) / sum(x**3 for x in deviations_b)
            assert row.alpha181 == pytest.approx(expected181)


@pytest.mark.parametrize("m", [1, 2, 60])
def test_146_parameter_is_hashed_sma_is_recursive_and_gap_reseeds(m):
    d, u, _, _ = sample(140, 2)
    d = {"close": d["close"].copy()}
    d["close"].iloc[60, 0] = np.nan
    r = explicit(d, u, alpha146_m=m).compute([146])
    price = d["close"].iloc[:, 0].to_numpy()
    returns = np.r_[np.nan, price[1:] / price[:-1] - 1]
    base = scalar_ema(returns, 61, 2)
    den = scalar_ema(base**2, 60, m)
    diff = returns - base
    expected = np.array(
        [
            np.nan if t < 20 else sum(diff[t - 19 : t + 1]) / 20 * diff[t] / den[t]
            for t in range(140)
        ]
    )
    np.testing.assert_allclose(
        r.values.alpha146.to_numpy().reshape(140, 2)[:, 0], expected, equal_nan=True
    )
    assert r.metadata["alpha146_m"] == m
    alternate = explicit(d, u, alpha146_m=3 if m != 3 else 1).compute([146])
    assert r.metadata["input_sha256"] != alternate.metadata["input_sha256"]
    assert r.status.iloc[60 * 2, 0] == MISSING


def test_explicit_dependencies_and_parameter_validation():
    d, u, _, _ = sample(100, 2)
    r = explicit(d, u).compute([146, 181])
    assert r.metadata["missing_dependencies"] == {
        "alpha146": ["alpha146_m"],
        "alpha181": ["benchmark_close"],
    }
    assert r.values.isna().all().all()
    for m in [0, 61, 0.5, True, "1", np.nan, np.inf]:
        with pytest.raises(ValueError, match="alpha146_m"):
            explicit(d, u, alpha146_m=m)
    with pytest.raises(ValueError, match="version"):
        Alpha191(d, universe=u, adjustment="raw", alpha146_m=1)
    with pytest.raises(ValueError, match="version"):
        coverage(version="latest")


def test_explicit_warmup_and_zero_denominators_are_not_filled():
    d, u, b, _ = sample(120, 7)
    r = explicit(d, u, benchmark=b, alpha146_m=1).compute(IDS)
    windows = {36: 7, 54: 10, 127: 23, 146: 21, 165: 95, 166: 40, 181: 40, 183: 47}
    for i, n in windows.items():
        name = f"alpha{i:03}"
        assert r.report.loc[name, "full_window_rows"] == n
        assert r.status[name].iloc[: (n - 1) * 7].eq(WARMUP).all()
        assert r.status[name].iloc[(n - 1) * 7 : n * 7].eq(OK).all()
    flat = {"close": d["close"] * 0 + 10}
    b.loc[:, "close"] = 100
    constant = explicit(flat, u, benchmark=b, alpha146_m=1).compute([146, 165, 181, 183])
    assert constant.status.iloc[-7:].eq(DOMAIN).all().all()
    assert constant.values.iloc[-7:].isna().all().all()


def test_cumulative_range_is_time_series_bounded_and_scale_invariant():
    d, u, _, _ = sample(180, 3)
    kwargs = dict(ids=[165, 183])
    r = explicit(d, u).compute(**kwargs)
    changed = {"close": d["close"] * 17 + 900}
    scaled = explicit(changed, u).compute(**kwargs)
    np.testing.assert_allclose(r.values, scaled.values, rtol=1e-10, atol=1e-10, equal_nan=True)
    single = explicit({"close": d["close"].iloc[:, :1]}, u.iloc[:, :1]).compute(**kwargs)
    pd.testing.assert_frame_equal(single.values, r.values.loc[(slice(None), [u.columns[0]]), :])
    # A gap poisons exactly the finite, nested window; no dropping observations.
    d = {"close": d["close"].copy()}
    d["close"].iloc[80, 0] = np.nan
    missing = explicit(d, u).compute(**kwargs)
    for i, horizon in [(165, 95), (183, 47)]:
        codes = missing.status[f"alpha{i:03}"].to_numpy().reshape(180, 3)[:, 0]
        assert codes[80 : 80 + horizon].tolist() == [MISSING] * horizon
        assert codes[80 + horizon] == OK


def test_explicit_prefix_append_permutation_and_future_changes():
    d, u, b, _ = sample(180, 7)
    u.iloc[80:100, 0] = False
    d["volume"].iloc[100, 1] = np.nan
    d["close"].iloc[70, 2] = np.nan
    b.iloc[100, 1] = np.nan
    options = dict(version=EXPLICIT_VERSION, adjustment="raw", alpha146_m=1)
    full = Alpha191(d, universe=u, benchmark=b, **options).compute(IDS)
    stream = Alpha191Stream(ids=IDS, **options)
    chunks = []
    for a, z in [(0, 40), (40, 110), (110, 180)]:
        chunks.append(
            stream.append(
                {k: v.iloc[a:z] for k, v in d.items()}, universe=u.iloc[a:z], benchmark=b.iloc[a:z]
            )
        )
    pd.testing.assert_frame_equal(pd.concat([x.values for x in chunks]), full.values)
    pd.testing.assert_frame_equal(pd.concat([x.status for x in chunks]), full.status)
    prefix = Alpha191(
        {k: v.iloc[:110] for k, v in d.items()},
        universe=u.iloc[:110],
        benchmark=b.iloc[:110],
        **options,
    ).compute(IDS)
    pd.testing.assert_frame_equal(prefix.values, full.values.loc[: u.index[109]])
    pd.testing.assert_frame_equal(prefix.status, full.status.loc[: u.index[109]])
    cols = list(reversed(u.columns))
    permuted = Alpha191(
        {k: v[cols] for k, v in d.items()}, universe=u[cols], benchmark=b, **options
    ).compute(IDS)
    pd.testing.assert_frame_equal(permuted.values.sort_index(), full.values.sort_index())
    for k in ["open", "close", "high", "low", "vwap"]:
        d[k].iloc[110:] *= 2
    future = Alpha191(d, universe=u, benchmark=b, **options).compute(IDS)
    pd.testing.assert_frame_equal(prefix.values, future.values.loc[: u.index[109]])
    assert full.between(u.index[110]).report.version.eq(EXPLICIT_VERSION).all()


def test_per_factor_resolution_provenance_is_packaged():
    source = json.loads(files("ashare_data.factors.alpha191").joinpath("sources.json").read_text())
    for i in IDS:
        spec = source["factors"][str(i)]["explicit_definition"]
        assert spec["version"] == EXPLICIT_VERSION
        assert (
            spec["expression"]
            == coverage(version=EXPLICIT_VERSION).loc[f"alpha{i:03}", "expression"]
        )
        assert spec["report_is_underspecified"] is True
        assert spec["basis"] and spec["note"]
