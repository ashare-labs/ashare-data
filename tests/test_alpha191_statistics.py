"""Public regressions for analytic ties without merging truly distinct values."""

import numpy as np
import pandas as pd
import pytest

from ashare_data.factors.alpha191 import Alpha191, Alpha191Stream, EXPLICIT_VERSION
from ashare_data.factors.alpha191._engine import DOMAIN, OK
from test_alpha191 import engine, sample


# Include every formula with CORR/COV, including calls reached through an alias.
STATISTIC_CONSUMERS = [
    1,
    5,
    16,
    26,
    32,
    35,
    36,
    39,
    42,
    44,
    45,
    54,
    56,
    61,
    62,
    64,
    73,
    74,
    77,
    83,
    90,
    91,
    92,
    99,
    101,
    104,
    105,
    108,
    113,
    115,
    119,
    121,
    123,
    125,
    130,
    131,
    136,
    138,
    139,
    140,
    141,
    148,
    154,
    176,
    179,
    184,
    191,
]


def test_alpha113_two_observation_correlations_have_exact_sign_and_ties():
    dates = pd.date_range("2024-01-01", periods=40)
    c = np.arange(40)[:, None] * 0.01 + np.array([10, 20, 30])[None, :]
    v = np.arange(40)[:, None] * 10 + np.array([1000, 2000, 3000])[None, :]
    frames = {
        k: pd.DataFrame(a, index=dates, columns=["A", "B", "C"])
        for k, a in [("close", c), ("volume", v)]
    }
    pool = frames["close"].notna()
    for scale in [1, 10]:
        data = {**frames, "close": frames["close"] * scale}
        r = Alpha191(data, universe=pool, adjustment="raw").compute([113])
        np.testing.assert_array_equal(r.values.iloc[-3:, 0], [-2 / 9, -4 / 9, -2 / 3])
        assert r.status.iloc[-3:].eq(OK).all().all()


def test_constant_covariance_preserves_alpha099_and_083_ties():
    ix = pd.date_range("2024-01-01", periods=5)
    c = np.tile([10.0, 20.0, 30.0], (5, 1))
    v = [[100, 200, 300], [200, 300, 100], [300, 100, 200], [100, 300, 200], [300, 200, 100]]
    d = {
        k: pd.DataFrame(a, index=ix, columns=["A", "B", "C"])
        for k, a in [("close", c), ("high", c), ("volume", v)]
    }
    r = Alpha191(d, universe=d["close"].notna(), adjustment="raw").compute([83, 99])
    np.testing.assert_array_equal(r.values.iloc[-3:], np.full((3, 2), -2 / 3))
    assert r.status.iloc[-3:].eq(OK).all().all()


def test_statistics_keep_true_near_ties_distinct_and_constants_undefined():
    e = engine([[1, 1, 1], [2, 2, 2], [4, 4 + 1e-4, 4 + 2e-4], [8, 8, 8]])
    # Every column is compared to the same explicit series, bypassing any formula.
    x = e.fields["close"]
    from ashare_data.factors.alpha191._engine import panel

    y = panel(np.repeat(np.array([1.0, 2.0, 4.0, 8.0])[:, None], 3, axis=1))
    corr = e.call("CORR", x, y, 4)
    assert corr.values[-1, 0] > corr.values[-1, 1] > corr.values[-1, 2]
    ranked = e.call("R", corr)
    np.testing.assert_array_equal(ranked.values[-1], [1, 2 / 3, 1 / 3])
    cov = e.call("COV", x, y, 4)
    assert len(set(cov.values[-1])) == 3
    flat = engine([2.0, 2.0, 2.0, 2.0])
    assert flat.evaluate("CORR(C,C,3)").reasons[-1, 0] == DOMAIN
    assert flat.evaluate("COV(C,C,3)").values[-1, 0] == 0


def test_two_point_sign_does_not_underflow_and_large_levels_stay_centered():
    e = engine([[1e-200], [2e-200], [1e-200]])
    np.testing.assert_array_equal(e.evaluate("CORR(C,-C,2)").values[1:], [[-1], [-1]])
    e = engine([[1e12], [1e12 + 1], [1e12 + 2], [1e12 + 3]])
    assert e.evaluate("COV(C,C,4)").values[-1, 0] == pytest.approx(5 / 3)
    assert e.evaluate("CORR(C,C,4)").values[-1, 0] == 1


def test_benchmark_close_only_supports_actual_dependency_inventory():
    d, u, b, _ = sample(560, 2)
    full = Alpha191(d, universe=u, adjustment="raw", version=EXPLICIT_VERSION, benchmark=b).compute(
        [75, 149, 181, 182]
    )
    close_only = Alpha191(
        d, universe=u, adjustment="raw", version=EXPLICIT_VERSION, benchmark=b[["close"]]
    ).compute([75, 149, 181, 182])
    pd.testing.assert_frame_equal(
        full.values[["alpha149", "alpha181"]], close_only.values[["alpha149", "alpha181"]]
    )
    assert close_only.metadata["missing_dependencies"] == {
        "alpha075": ["benchmark_open"],
        "alpha182": ["benchmark_open"],
    }


def test_stream_does_not_backfill_new_benchmark_fields_into_old_blocks():
    d, u, b, _ = sample(100, 2)
    stream = Alpha191Stream(ids=[181], adjustment="raw", version=EXPLICIT_VERSION)
    stream.append(
        {k: v.iloc[:50] for k, v in d.items()},
        universe=u.iloc[:50],
        benchmark=b[["close"]].iloc[:50],
    )
    with pytest.raises(ValueError, match="dependency fields"):
        stream.append(
            {k: v.iloc[50:] for k, v in d.items()}, universe=u.iloc[50:], benchmark=b.iloc[50:]
        )
    appended = stream.append(
        {k: v.iloc[50:] for k, v in d.items()},
        universe=u.iloc[50:],
        benchmark=b[["close"]].iloc[50:],
    )
    full = Alpha191(
        d, universe=u, adjustment="raw", version=EXPLICIT_VERSION, benchmark=b[["close"]]
    ).compute([181])
    pd.testing.assert_frame_equal(appended.values, full.between(u.index[50]).values)


def test_statistic_consumer_inventory_is_exhaustive():
    import ast

    from ashare_data.factors.alpha191._formulas import ALIASES, EXPLICIT_FORMULAS, FORMULAS

    def calls(expression):
        node = ast.parse(expression, mode="eval")
        result = {x.func.id for x in ast.walk(node) if isinstance(x, ast.Call)}
        for x in ast.walk(node):
            if isinstance(x, ast.Name) and x.id in ALIASES:
                result |= calls(ALIASES[x.id])
        return result

    assert [
        i for i, f in sorted((FORMULAS | EXPLICIT_FORMULAS).items()) if calls(f) & {"CORR", "COV"}
    ] == STATISTIC_CONSUMERS


@pytest.mark.parametrize("i", STATISTIC_CONSUMERS)
def test_shared_rank_statistic_consumers_against_fraction_decimal_oracle(i):
    from _alpha191_precision_oracle import PrecisionOracle
    from ashare_data.factors.alpha191._formulas import ALIASES, EXPLICIT_FORMULAS, FORMULAS

    d, u, _, _ = sample(320, 5)
    names = dict(O="open", C="close", H="high", L="low", V="volume", P="vwap", A="amount")
    oracle = PrecisionOracle({k: d[v].to_numpy() for k, v in names.items()}, u.to_numpy(), ALIASES)
    result = Alpha191(d, universe=u, adjustment="raw", version=EXPLICIT_VERSION).compute([i]).values
    for t in [100, 137, 250, 319]:
        expected = [oracle.compute((FORMULAS | EXPLICIT_FORMULAS)[i], t, j) for j in range(5)]
        np.testing.assert_allclose(
            result.loc[u.index[t]].iloc[:, 0],
            [np.nan if x is None else float(x) for x in expected],
            rtol=1e-10,
            atol=1e-12,
            equal_nan=True,
        )
    oracle.value.cache_clear()


def test_rank_rational_paths_are_local_to_each_window():
    from ashare_data.factors.alpha191._engine import Engine, panel

    rng = np.random.default_rng(888)
    values = rng.uniform(size=(100, 11))
    pool = np.ones_like(values, dtype=bool)
    # Future membership changes introduce coprime denominator choices.
    for t in range(60, 100):
        pool[t, (t % 10) + 1 :] = False
    full = Engine({"close": panel(values)}, pool, "strict", None)
    prefix = Engine({"close": panel(values[:60])}, pool[:60], "strict", None)
    for expr in ["R(CORR(R(C),R(-C),3))", "R(COV(R(C),R(-C),5))", "R(DECAY(R(C),17))"]:
        a, b = full.evaluate(expr), prefix.evaluate(expr)
        np.testing.assert_array_equal(a.values[:60], b.values)
        np.testing.assert_array_equal(a.reasons[:60], b.reasons)
