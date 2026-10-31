"""Independent arithmetic anchors, causality, contracts and dependency behavior."""

import json
from importlib.resources import files

import numpy as np
import pandas as pd
import pytest

from ashare_data.factors.alpha191 import Alpha191, Alpha191Stream, STATUS, VERSION, coverage
from ashare_data.factors.alpha191._engine import (
    DOMAIN,
    EXCLUDED,
    MISSING,
    OK,
    RANK_INCOMPLETE,
    WARMUP,
    Engine,
    panel,
)
from ashare_data.factors.alpha191._formulas import AMBIGUOUS


def sample(n=640, s=5):
    rng = np.random.default_rng(191)
    ix, cols = pd.bdate_range("2020-01-01", periods=n), [f"S{i}" for i in range(s)]
    c = np.exp(rng.normal(0, 0.08, (n, s))) * 20
    o = c * np.exp(rng.normal(0, 0.02, (n, s)))
    high, low = np.maximum(o, c) * 1.03, np.minimum(o, c) * 0.97
    v = rng.integers(10000, 1000000, (n, s)).astype(float)
    p = (high + low + c) / 3
    data = {
        k: pd.DataFrame(a, index=ix, columns=cols)
        for k, a in zip(
            ["open", "close", "high", "low", "volume", "vwap", "amount"],
            [o, c, high, low, v, p, v * p],
            strict=True,
        )
    }
    pool = pd.DataFrame(True, index=ix, columns=cols)
    benchmark = pd.DataFrame(
        {"open": rng.uniform(90, 110, n), "close": rng.uniform(90, 110, n)}, index=ix
    )
    ff = pd.DataFrame(rng.normal(0, 0.01, (n, 3)), index=ix, columns=["mkt", "smb", "hml"])
    return data, pool, benchmark, ff


def engine(a, pool=None, rank_policy="strict"):
    a = np.asarray(a, dtype=float)
    if a.ndim == 1:
        a = a[:, None]
    x = panel(a, np.where(np.isnan(a), MISSING, OK))
    return Engine({"close": x}, np.ones(a.shape, bool) if pool is None else pool, rank_policy, None)


@pytest.mark.parametrize(
    "name,n,expected",
    [
        ("SUM", 3, 12),
        ("MEAN", 3, 4),
        ("STD", 3, 2),
        ("TSMIN", 3, 2),
        ("TSMAX", 3, 6),
        ("TSRANK", 3, 1),
        ("DECAY", 3, 28 / 6),
        ("WMA", 3, (2 * 0.81 + 4 * 0.9 + 6) / 2.71),
        ("SLOPE", 3, 2),
        ("HIGHDAY", 3, 0),
        ("LOWDAY", 3, 2),
    ],
)
def test_window_operators_hand_calculated(name, n, expected):
    e = engine([100, 2, 4, 6])
    result = e.evaluate(f"{name}(C,{n})")
    assert result.values[-1, 0] == pytest.approx(expected)
    assert np.all(result.reasons[:2] == WARMUP)


def test_rank_is_cross_section_ties_pool_and_missing_are_explicit():
    e = engine([[3, 1, 3, 8], [9, 8, 9, 1]])
    np.testing.assert_allclose(e.evaluate("R(C)").values[0], [0.625, 0.25, 0.625, 1])
    t = e.evaluate("TSRANK(C,2)").values[-1]
    np.testing.assert_allclose(t, [1, 1, 1, 0.5])
    a = [[3, 1, np.nan, 8]]
    r = engine(a).evaluate("R(C)")
    assert np.all(r.reasons == RANK_INCOMPLETE)
    r = engine(a, rank_policy="available").evaluate("R(C)")
    np.testing.assert_allclose(r.values, [[2 / 3, 1 / 3, np.nan, 1]], equal_nan=True)
    r = engine(a, pool=np.array([[True, True, False, True]])).evaluate("R(C)")
    assert r.reasons[0, 2] == EXCLUDED
    assert r.values[0, 0] == pytest.approx(2 / 3)


def test_unknown_comparisons_do_not_become_false_or_zero():
    e = engine([1, np.nan, 2, 3, 4])
    for formula in ["C > 0", "IF(C > 0, 1, 0)", "IF((C > 0) | (C < 5), 1, 0)", "SUM(C > 0,2)"]:
        r = e.evaluate(formula)
        assert np.isnan(r.values[1, 0])
        assert r.reasons[1, 0] == MISSING
    r = e.evaluate("SUM(C,2)")
    assert r.reasons[2, 0] == MISSING
    assert r.values[3, 0] == 5
    assert e.evaluate("IF(C > 0, 2, 1 / (C-C))").values[-1, 0] == 2


def test_undefined_math_and_constant_correlation_are_nan():
    e = engine([2, 2, 2, 2])
    for expr in ["C / (C-C)", "LOG(C-C)", "CORR(C,C,3)", "(-C) ** .5"]:
        r = e.evaluate(expr)
        assert np.isnan(r.values[-1, 0])
        assert r.reasons[-1, 0] == DOMAIN


def test_sma_seed_reset_adjust_false_and_extreme_ties():
    r = engine([3, 6, 9, np.nan, 30, 60]).evaluate("SMA(C,3,1)")
    np.testing.assert_allclose(r.values[:, 0], [3, 4, 17 / 3, np.nan, 30, 40], equal_nan=True)
    e = engine([5, 1, 5, 1])
    assert e.evaluate("HIGHDAY(C,4)").values[-1, 0] == 1
    assert e.evaluate("LOWDAY(C,4)").values[-1, 0] == 0
    assert e.evaluate("TSRANK(C,4)").values[-1, 0] == 0.375


def test_registry_and_dependency_inventory_cover_all_191():
    c = coverage()
    assert len(c) == 191 and c.index.is_unique
    assert c.availability.value_counts().to_dict() == {
        "IMPLEMENTED": 178,
        "REQUIRES_BENCHMARK": 3,
        "AMBIGUOUS_FORMULA": 8,
        "REQUIRES_EXTERNAL_FACTORS": 1,
        "REQUIRES_EXPLICIT_SEED": 1,
    }
    assert c.loc["alpha025", "full_window_rows"] == 251
    assert set(c.loc["alpha030", "dependencies"]) == {"close", "mkt", "smb", "hml"}
    assert c.loc["alpha039", "cross_section_rank"]
    assert c.loc["alpha081", "recursive_or_event_history"]
    d, u, b, f = sample(640, 16)
    r = Alpha191(
        d,
        universe=u,
        adjustment="raw",
        benchmark=b,
        factors=f,
        alpha143_seed=1,
        rank_policy="available",
    ).compute()
    assert r.values.shape == (len(u) * len(u.columns), 191)
    for i in range(1, 192):
        name = f"alpha{i:03d}"
        if i in AMBIGUOUS:
            assert set(r.status_labels()[name]) == {"AMBIGUOUS_FORMULA"}
        else:
            assert r.report.loc[name, "valid_cells"] > 0, name
    assert not np.isinf(r.values.to_numpy()).any()
    assert np.array_equal(r.values.notna().to_numpy(), r.status.to_numpy() == OK)
    src = json.loads(files("ashare_data.factors.alpha191").joinpath("sources.json").read_text())
    assert len(src["factors"]) == 191 and len(src["commit"]) == 40
    assert (
        "Apache License"
        in files("ashare_data.factors.alpha191").joinpath("LICENSE.dolphindb").read_text()
    )


def test_missing_dependencies_and_seed_never_fill_zero():
    d, u, _, _ = sample(80)
    del d["vwap"]
    r = Alpha191(d, universe=u, adjustment="raw").compute([13, 30, 75, 143, 149, 182])
    assert r.values.isna().all().all()
    assert set(r.status_labels().to_numpy().ravel()) == {"MISSING_DEPENDENCY"}
    assert r.metadata["missing_dependencies"]["alpha013"] == ["vwap"]
    assert r.metadata["missing_dependencies"]["alpha143"] == ["alpha143_seed"]


def rank(values):
    """Slow independent average rank; denominator is the complete row length."""
    return np.array(
        [
            (sum(x < value for x in values) + (sum(x == value for x in values) + 1) / 2)
            / len(values)
            for value in values
        ]
    )


def sma(values, n, m):
    out = []
    last = None
    for value in values:
        last = value if last is None else (m * value + (n - m) * last) / n
        out.append(last)
    return np.array(out)


def decay_last(values):
    return sum((i + 1) * x for i, x in enumerate(values)) / sum(range(1, len(values) + 1))


def test_formula_anchors_against_separate_scalar_implementations():
    d, u, b, _ = sample(280)
    ids = [7, 12, 21, 27, 28, 33, 35, 39, 55, 75, 81, 89, 104, 137, 155, 159, 173, 182, 190]
    r = (
        Alpha191(d, universe=u, adjustment="raw", benchmark=b)
        .compute(ids)
        .values.iloc[-len(u.columns) :]
    )
    a = {k: v.to_numpy() for k, v in d.items()}
    c, o, h, low, v, p = [a[k] for k in ["close", "open", "high", "low", "volume", "vwap"]]
    ret = c[1:] / c[:-1] - 1
    np.testing.assert_allclose(
        r.alpha007, (rank((p - c)[-3:].max(0)) + rank((p - c)[-3:].min(0))) * rank(v[-1] - v[-4])
    )
    np.testing.assert_allclose(
        r.alpha012, -rank(o[-1] - p[-10:].mean(0)) * rank(abs(c[-1] - p[-1]))
    )
    np.testing.assert_allclose(
        r.alpha033,
        (-low[-5:].min(0) + low[-10:-5].min(0))
        * rank((ret[-240:].sum(0) - ret[-20:].sum(0)) / 220)
        * np.array([rank(v[-5:, j])[-1] for j in range(c.shape[1])]),
    )
    d_open = np.diff(o, axis=0)
    part1 = rank(np.array([decay_last(d_open[-15:, j]) for j in range(c.shape[1])]))
    corr = np.array(
        [
            [
                np.corrcoef(v[i - 16 : i + 1, j], o[i - 16 : i + 1, j])[0, 1]
                for j in range(c.shape[1])
            ]
            for i in range(len(c) - 7, len(c))
        ]
    )
    part2 = rank(np.array([decay_last(corr[:, j]) for j in range(c.shape[1])]))
    np.testing.assert_allclose(r.alpha035, -np.minimum(part1, part2))
    # Long nested correlation and negative sign in 39, independently looped.
    left = rank(np.array([decay_last((c[2:] - c[:-2])[-8:, j]) for j in range(c.shape[1])]))
    vol_mean = np.array([v[i - 179 : i + 1].mean(0) for i in range(179, len(v))])
    vol_sum = np.array([vol_mean[i - 36 : i + 1].sum(0) for i in range(36, len(vol_mean))])
    pv = p * 0.3 + o * 0.7
    correlations = np.array(
        [
            [
                np.corrcoef(pv[i - 13 : i + 1, j], vol_sum[i - 215 - 13 : i - 215 + 1, j])[0, 1]
                for j in range(c.shape[1])
            ]
            for i in range(len(c) - 12, len(c))
        ]
    )
    right = rank(np.array([decay_last(correlations[:, j]) for j in range(c.shape[1])]))
    np.testing.assert_allclose(r.alpha039, -(left - right))
    for j in range(c.shape[1]):
        avg6 = [np.mean(c[t - 5 : t + 1, j]) for t in range(len(c) - 6, len(c))]
        expected = np.linalg.lstsq(
            np.column_stack([np.ones(6), np.arange(1, 7)]), avg6, rcond=None
        )[0][1]
        assert r.alpha021.iloc[j] == pytest.approx(expected)
        changes = [
            (c[t, j] / c[t - 3, j] - 1) * 100 + (c[t, j] / c[t - 6, j] - 1) * 100
            for t in range(len(c) - 12, len(c))
        ]
        weights = [0.9**k for k in range(11, -1, -1)]
        assert r.alpha027.iloc[j] == pytest.approx(np.dot(changes, weights) / sum(weights))
        k1 = np.array(
            [
                (c[t, j] - min(low[t - 8 : t + 1, j]))
                / (max(h[t - 8 : t + 1, j]) - min(low[t - 8 : t + 1, j]))
                * 100
                for t in range(8, len(c))
            ]
        )
        k2 = np.array(
            [
                (c[t, j] - min(low[t - 8 : t + 1, j]))
                / (max(h[t - 8 : t + 1, j]) - max(low[t - 8 : t + 1, j]))
                * 100
                for t in range(8, len(c))
            ]
        )
        assert r.alpha028.iloc[j] == pytest.approx(
            3 * sma(k1, 3, 1)[-1] - 2 * sma(sma(k2, 3, 1), 3, 1)[-1]
        )
        assert r.alpha081.iloc[j] == pytest.approx(sma(v[:, j], 21, 2)[-1])
        for field, series in [("alpha089", c[:, j]), ("alpha155", v[:, j])]:
            mac = sma(series, 13, 2) - sma(series, 27, 2)
            expected = (mac - sma(mac, 10, 2))[-1] * (2 if field == "alpha089" else 1)
            assert r[field].iloc[j] == pytest.approx(expected)
        assert r.alpha173.iloc[j] == pytest.approx(
            (
                3 * sma(c[:, j], 13, 2)
                - 2 * sma(sma(c[:, j], 13, 2), 13, 2)
                + sma(sma(sma(np.log(c[:, j]), 13, 2), 13, 2), 13, 2)
            )[-1]
        )
        # Both complicated branches in Alpha137 and its rolling sum Alpha55.
        swing = []
        for t in range(len(c) - 20, len(c)):
            aa, bb, cc = (
                abs(h[t, j] - c[t - 1, j]),
                abs(low[t, j] - c[t - 1, j]),
                abs(h[t, j] - low[t - 1, j]),
            )
            dd = abs(c[t - 1, j] - o[t - 1, j]) / 4
            denom = (
                aa + bb / 2 + dd
                if aa > bb and aa > cc
                else bb + aa / 2 + dd
                if bb > cc and bb > aa
                else cc + dd
            )
            swing.append(
                16
                * (c[t, j] - c[t - 1, j] + (c[t, j] - o[t, j]) / 2 + c[t - 1, j] - o[t - 1, j])
                / denom
                * max(aa, bb)
            )
        assert r.alpha137.iloc[j] == pytest.approx(swing[-1])
        assert r.alpha055.iloc[j] == pytest.approx(sum(swing))
        down = b.close.to_numpy() < b.open.to_numpy()
        assert r.alpha075.iloc[j] == pytest.approx(
            np.count_nonzero((c[-50:, j] > o[-50:, j]) & down[-50:]) / np.count_nonzero(down[-50:])
        )
        bc, bo = b.close.to_numpy()[-20:], b.open.to_numpy()[-20:]
        match = ((c[-20:, j] > o[-20:, j]) & (bc > bo)) | ((c[-20:, j] < o[-20:, j]) & (bc < bo))
        assert r.alpha182.iloc[j] == pytest.approx(sum(match) / 20)
        rs, gs = ret[-20:, j], (c[-20:, j] / c[-39:-19, j]) ** 0.05
        above, below = rs > gs - 1, rs < gs - 1
        expected = np.log(
            (sum(above) - 1)
            * sum((rs - gs - 1)[below] ** 2)
            / (sum(below) * sum((rs - (gs - 1))[above] ** 2))
        )
        assert r.alpha190.iloc[j] == pytest.approx(expected)
    corr_now = [np.corrcoef(h[-5:, j], v[-5:, j])[0, 1] for j in range(c.shape[1])]
    corr_before = [np.corrcoef(h[-10:-5, j], v[-10:-5, j])[0, 1] for j in range(c.shape[1])]
    np.testing.assert_allclose(
        r.alpha104, -(np.array(corr_now) - corr_before) * rank(c[-20:].std(0, ddof=1)), atol=1e-10
    )
    lower, upper = np.minimum(low[1:], c[:-1]), np.maximum(h[1:], c[:-1])
    literal = (
        sum(
            (c[-1] - lower[-n:].sum(0)) / (upper - lower)[-n:].sum(0) * w
            for n, w in [(6, 288), (12, 144), (24, 144)]
        )
        * 100
        / (72 + 144 + 288)
    )
    np.testing.assert_allclose(r.alpha159, literal)


def test_alpha30_external_ols_and_geometric_wma_independent():
    d, u, _, ff = sample(140, 3)
    r = Alpha191(d, universe=u, adjustment="raw", factors=ff).compute([30])
    y = d["close"].pct_change(fill_method=None).to_numpy()
    residuals = []
    for t in range(120, 140):
        design = np.column_stack([np.ones(60), ff.iloc[t - 59 : t + 1]])
        betas = np.linalg.lstsq(design, y[t - 59 : t + 1], rcond=None)[0]
        residuals.append(y[t] - design[-1] @ betas)
    expected = np.average(np.square(residuals), axis=0, weights=0.9 ** np.arange(19, -1, -1))
    np.testing.assert_allclose(r.values.iloc[-3:, 0], expected, rtol=1e-11)
    assert r.status.iloc[78 * 3 : 79 * 3, 0].eq(WARMUP).all()
    assert r.status.iloc[79 * 3 : 80 * 3, 0].eq(OK).all()
    ff["hml"] = ff["smb"]
    bad = Alpha191(d, universe=u, adjustment="raw", factors=ff).compute([30])
    assert bad.status.iloc[-3:, 0].eq(DOMAIN).all()


def test_alpha143_requires_seed_and_missing_poisoning():
    ix = pd.bdate_range("2020-01-01", periods=6)
    d = {"close": pd.DataFrame({"S0": [10, 11, 10, 12, np.nan, 14]}, index=ix)}
    u = pd.DataFrame(True, index=ix, columns=["S0"])
    r = Alpha191(d, universe=u, adjustment="raw", alpha143_seed=2).compute([143])
    np.testing.assert_allclose(
        r.values.iloc[:, 0], [2, 0.2, 0.2, 0.04, np.nan, np.nan], atol=1e-14, equal_nan=True
    )
    assert r.status.iloc[-1, 0] == MISSING


def test_alpha149_filter_then_252_observations_not_252_sessions():
    d, u, b, _ = sample(640, 2)
    r = Alpha191(d, universe=u, adjustment="raw", benchmark=b).compute([149])
    bx = b.close.pct_change(fill_method=None).to_numpy()
    selected = np.flatnonzero(bx < 0)[-252:]
    x = bx[selected]
    y = d["close"].pct_change(fill_method=None).to_numpy()[selected]
    expected = np.linalg.lstsq(np.column_stack([np.ones(252), x]), y, rcond=None)[0][1]
    np.testing.assert_allclose(r.values.iloc[-2:, 0], expected, rtol=1e-11)
    assert selected[0] < 640 - 252
    b.iloc[-2, 1] = np.nan
    r = Alpha191(d, universe=u, adjustment="raw", benchmark=b).compute([149])
    assert r.status.iloc[-2:, 0].eq(MISSING).all()


def test_full_prefix_chunks_future_changes_and_rank_order():
    d, u, b, f = sample(560, 4)
    # Include an explicit membership change and an actual missing observation.
    u.iloc[170:185, 1] = False
    d["volume"].iloc[203, 2] = np.nan
    options = {"adjustment": "raw", "alpha143_seed": 1}
    full = Alpha191(d, universe=u, benchmark=b, factors=f, **options).compute()
    stream = Alpha191Stream(**options)
    parts = []
    codes = []
    for a, z in [(0, 73), (73, 210), (210, 397), (397, 560)]:
        result = stream.append(
            {k: v.iloc[a:z] for k, v in d.items()},
            universe=u.iloc[a:z],
            benchmark=b.iloc[a:z],
            factors=f.iloc[a:z],
        )
        parts.append(result.values)
        codes.append(result.status)
    pd.testing.assert_frame_equal(pd.concat(parts), full.values)
    pd.testing.assert_frame_equal(pd.concat(codes), full.status)
    prefix = Alpha191(
        {k: v.iloc[:397] for k, v in d.items()},
        universe=u.iloc[:397],
        benchmark=b.iloc[:397],
        factors=f.iloc[:397],
        **options,
    ).compute()
    pd.testing.assert_frame_equal(prefix.values, full.values.loc[: u.index[396]])
    changed = {k: v.copy() for k, v in d.items()}
    for k in ["open", "high", "low", "close", "vwap"]:
        changed[k].iloc[397:] *= 8
    edited = Alpha191(changed, universe=u, benchmark=b, factors=f, **options).compute()
    pd.testing.assert_frame_equal(edited.values.loc[: u.index[396]], prefix.values)
    cols = list(reversed(u.columns))
    permuted = Alpha191(
        {k: v[cols] for k, v in d.items()}, universe=u[cols], benchmark=b, factors=f, **options
    ).compute()
    pd.testing.assert_frame_equal(permuted.values.sort_index(), full.values.sort_index())


@pytest.mark.parametrize(
    "mutation",
    [
        "date_order",
        "duplicate_date",
        "column_order",
        "infinity",
        "negative",
        "ohlc",
        "unknown_pool",
        "units",
        "adjustment",
        "id",
    ],
)
def test_reject_invalid_or_ambiguous_inputs(mutation):
    d, u, _, _ = sample(10, 2)
    kw = {"universe": u, "adjustment": "raw"}
    ids = [1]
    if mutation == "date_order":
        kw["universe"] = u.iloc[::-1]
    elif mutation == "duplicate_date":
        kw["universe"] = pd.concat([u.iloc[:1], u])
    elif mutation == "column_order":
        d["close"] = d["close"][list(reversed(u.columns))]
    elif mutation == "infinity":
        d["volume"].iloc[0, 0] = np.inf
    elif mutation == "negative":
        d["volume"].iloc[0, 0] = -1
    elif mutation == "ohlc":
        d["high"].iloc[0, 0] = 1
    elif mutation == "unknown_pool":
        kw["universe"] = u.astype("boolean")
        kw["universe"].iloc[0, 0] = pd.NA
    elif mutation == "units":
        kw["units"] = {"volume": "lot"}
    elif mutation == "adjustment":
        kw["adjustment"] = "qfq"
    elif mutation == "id":
        ids = [192]
    with pytest.raises(ValueError):
        Alpha191(d, **kw).compute(ids)


def test_input_frozen_digest_seed_pool_and_failed_append_transaction():
    d, u, _, _ = sample(40, 2)
    calc = Alpha191(d, universe=u, adjustment="raw")
    before = calc.compute([81])
    d["volume"].iloc[:] = 1
    u.iloc[:] = False
    after = calc.compute([81])
    pd.testing.assert_frame_equal(before.values, after.values)
    assert before.metadata["input_sha256"] == after.metadata["input_sha256"]
    d, u, _, _ = sample(40, 2)
    stream = Alpha191Stream(ids=[81], adjustment="raw")
    stream.append({k: v.iloc[:20] for k, v in d.items()}, universe=u.iloc[:20])
    with pytest.raises(ValueError):
        stream.append({k: v.iloc[10:] for k, v in d.items()}, universe=u.iloc[10:])
    r = stream.append({k: v.iloc[20:] for k, v in d.items()}, universe=u.iloc[20:])
    pd.testing.assert_frame_equal(
        r.values, Alpha191(d, universe=u, adjustment="raw").compute([81]).values.loc[u.index[20] :]
    )
    assert r.metadata["version"] == VERSION and set(STATUS) == set(range(8))


def test_result_metadata_does_not_mutate_definition_or_stream_seed():
    d, u, _, _ = sample(20, 2)
    calc = Alpha191(d, universe=u, adjustment="raw")
    result = calc.compute([81])
    result.metadata["units"]["volume"] = "lots"
    result.metadata["status_codes"][0] = "ALTERED"
    fresh = calc.compute([81])
    assert fresh.metadata["units"]["volume"] == "share"
    assert fresh.metadata["status_codes"][0] == "OK"
    seed = {s: 1 for s in u.columns}
    stream = Alpha191Stream(ids=[143], adjustment="raw", alpha143_seed=seed)
    seed[u.columns[0]] = 999
    first = stream.append({k: v.iloc[:10] for k, v in d.items()}, universe=u.iloc[:10])
    assert first.values.iloc[0, 0] == 1
