"""Exact-input references for ordinary statistics before a discontinuous rank."""

from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np
import pandas as pd
import pytest

from ashare_data.factors.alpha191 import Alpha191, Alpha191Stream, EXPLICIT_VERSION, VERSION
from ashare_data.factors.alpha191._engine import DOMAIN, OK, Engine, panel


def exact_statistic(x, y, name):
    """Exact binary64 observations, integer moments, then one high precision sqrt."""
    x, y = ([Fraction(float(z)) for z in a] for a in [x, y])
    n = len(x)
    num = n * sum(a * b for a, b in zip(x, y)) - sum(x) * sum(y)
    if name == "COV":
        return float(num / (n * (n - 1)))
    den = (n * sum(a * a for a in x) - sum(x) ** 2) * (n * sum(a * a for a in y) - sum(y) ** 2)
    if not den:
        return np.nan
    squared = num * num / den
    with localcontext() as ctx:
        ctx.prec = 80
        return float(
            (1 if num >= 0 else -1)
            * (Decimal(squared.numerator) / Decimal(squared.denominator)).sqrt()
        )


def pair_engine(x, y):
    x, y = (np.asarray(a, dtype=float) for a in [x, y])
    if x.ndim == 1:
        x, y = x[:, None], y[:, None]
    return Engine({"close": panel(x), "volume": panel(y)}, np.ones(x.shape, bool), "strict", None)


@pytest.mark.parametrize("version", [VERSION, EXPLICIT_VERSION])
def test_alpha045_integer_translations_have_exact_analytic_rank(version):
    t = np.arange(164)[:, None]
    dates, stocks = pd.date_range("2024-01-01", periods=len(t)), ["A", "B", "C"]
    prices = np.array([16, 32, 64]) + t % 7
    volumes = np.array([15000, 30000, 60000]) + 150 * (t % 11)
    frames = {
        k: pd.DataFrame(v, index=dates, columns=stocks)
        for k, v in {"open": prices, "close": prices, "vwap": prices, "volume": volumes}.items()
    }
    pool = frames["close"].notna()
    options = dict(adjustment="raw", version=version)
    actual = Alpha191(frames, universe=pool, **options).compute([45])
    np.testing.assert_array_equal(actual.values.iloc[-3:, 0], [4 / 9] * 3)
    np.testing.assert_array_equal(actual.status.iloc[-3:, 0], [OK] * 3)
    # Check both incremental reconstruction and order independence at the API boundary.
    stream = Alpha191Stream(ids=[45], **options)
    stream.append({k: v.iloc[:155] for k, v in frames.items()}, universe=pool.iloc[:155])
    appended = stream.append({k: v.iloc[155:] for k, v in frames.items()}, universe=pool.iloc[155:])
    pd.testing.assert_frame_equal(appended.values, actual.between(dates[155]).values)
    shuffled = Alpha191(
        {k: v.loc[:, stocks[::-1]] for k, v in frames.items()},
        universe=pool.loc[:, stocks[::-1]],
        **options,
    ).compute([45])
    pd.testing.assert_frame_equal(shuffled.values.sort_index(), actual.values.sort_index())
    pd.testing.assert_frame_equal(shuffled.status.sort_index(), actual.status.sort_index())


@pytest.mark.parametrize("bases", [[10.0, 20.0, 30.0], [1e8, 2e8, 3e8], [1e12, 2e12, 3e12]])
@pytest.mark.parametrize("name", ["CORR", "COV"])
def test_translation_preserves_exact_ties_before_ranking(bases, name):
    x = np.array([0.0, 1, 4, 2, 7])[:, None] + bases
    y = np.array([0.0, 3, 2, 9, 5])[:, None] + bases
    e = pair_engine(x, y)
    raw = e.evaluate(f"{name}(C,V,5)").values[-1]
    reference = [exact_statistic(x[:, j], y[:, j], name) for j in range(3)]
    assert len(set(reference)) == 1
    assert len(set(raw)) == 1
    np.testing.assert_allclose(raw, reference, atol=1e-15, rtol=4e-15)
    np.testing.assert_array_equal(e.evaluate(f"R({name}(C,V,5))").values[-1], [2 / 3] * 3)


@pytest.mark.parametrize("exponent", [-700, -500, 0, 500, 700])
def test_scale_extremes_and_negative_affine_relations(exponent):
    x = np.array([0.0, 1, 4, 2, 7, -3, 8])
    y = np.array([0.0, 3, 2, 9, 5, 1, 6])
    x, y = np.ldexp(x, exponent), np.ldexp(y, -exponent)
    for name in ["CORR", "COV"]:
        e = pair_engine(x, y)
        assert e.evaluate(f"{name}(C,V,7)").values[-1, 0] == pytest.approx(
            exact_statistic(x, y, name), rel=4e-15, abs=1e-15
        )
    e = pair_engine(x, -x)
    assert e.evaluate("CORR(C,V,7)").values[-1, 0] == -1


def test_affine_scalings_share_statistic_ties_and_ignore_column_order():
    x = np.array([0.0, 1, 4, 2, 7])[:, None]
    y = np.array([0.0, 3, 2, 9, 5])[:, None]
    exponents = np.array([-700, 0, 700])
    x = np.ldexp(x, exponents) * [1, 1, -1]
    y = np.ldexp(y, -exponents) * [1, 1, -1]
    e, reordered = pair_engine(x, y), pair_engine(x[:, ::-1], y[:, ::-1])
    for name in ["CORR", "COV"]:
        expression = f"{name}(C,V,5)"
        actual = e.evaluate(expression)
        reference = [exact_statistic(x[:, j], y[:, j], name) for j in range(3)]
        assert len(set(reference)) == 1
        np.testing.assert_allclose(actual.values[-1], reference, atol=1e-15, rtol=4e-15)
        np.testing.assert_array_equal(e.evaluate(f"R({expression})").values[-1], [2 / 3] * 3)
        np.testing.assert_array_equal(actual.values[:, ::-1], reordered.evaluate(expression).values)


def test_anchor_subtraction_overflow_and_small_normalized_variations():
    x = np.ldexp(np.array([-1.0, 1.0, -0.5, 0.5]), 1023)
    y = np.array([1.0, 0.0, 2.0, 4.0])
    e = pair_engine(x, y)
    assert e.evaluate("CORR(C,V,4)").values[-1, 0] == pytest.approx(
        exact_statistic(x, y, "CORR"), abs=1e-15
    )
    for exponent in [-500, 0, 500]:
        base = np.ldexp(1.0, exponent)
        unit = np.spacing(base)
        x = base + unit * np.array([0.0, 1, 4, 2, 7])
        y = base + unit * np.array([0.0, 3, 2, 9, 5])
        e = pair_engine(x, y)
        assert e.evaluate("CORR(C,V,5)").values[-1, 0] == pytest.approx(
            exact_statistic(x, y, "CORR"), abs=1e-15
        )
    e = pair_engine([1e200] * 5, np.arange(5.0))
    assert e.evaluate("COV(C,V,5)").values[-1, 0] == 0
    assert e.evaluate("CORR(C,V,5)").reasons[-1, 0] == DOMAIN


@pytest.mark.parametrize(
    "name,epsilon,rank",
    [
        ("CORR", 1e-6, [1.0, 2 / 3, 1 / 3]),
        ("COV", 1e-12, [1 / 3, 2 / 3, 1.0]),
    ],
)
def test_real_differences_below_coarse_rounding_threshold_remain_distinct(name, epsilon, rank):
    y = np.tile(np.array([1.0, 2.0, 4.0, 8.0])[:, None], (1, 3))
    x = y.copy()
    x[2] += np.array([0, epsilon, 2 * epsilon])
    expected = [exact_statistic(x[:, j], y[:, j], name) for j in range(3)]
    assert len(set(expected)) == 3
    e = pair_engine(x, y)
    raw = e.evaluate(f"{name}(C,V,4)").values[-1]
    np.testing.assert_allclose(raw, expected, rtol=4e-15, atol=1e-15)
    np.testing.assert_array_equal(e.evaluate(f"R({name}(C,V,4))").values[-1], rank)


@pytest.mark.parametrize("n", [3, 5, 15, 17, 230])
def test_raw_statistics_match_exact_input_and_ignore_future_rows(n):
    rng = np.random.default_rng(4500 + n)
    x = rng.integers(-100, 100, size=(n + 10, 5)).astype(float) + 1e12
    y = rng.integers(-100, 100, size=x.shape).astype(float) + 2e12
    full, prefix = pair_engine(x, y), pair_engine(x[:-5], y[:-5])
    for name in ["CORR", "COV"]:
        result, partial = full.evaluate(f"{name}(C,V,{n})"), prefix.evaluate(f"{name}(C,V,{n})")
        np.testing.assert_array_equal(result.values[:-5], partial.values)
        np.testing.assert_array_equal(result.reasons[:-5], partial.reasons)
        for t in [n - 1, len(x) - 1]:
            expected = [
                exact_statistic(x[t - n + 1 : t + 1, j], y[t - n + 1 : t + 1, j], name)
                for j in range(5)
            ]
            if name == "CORR":
                np.testing.assert_allclose(result.values[t], expected, rtol=4e-15, atol=1e-14)
            else:
                # Covariance near zero is ill-conditioned in relative units. Its
                # absolute error is measured against the product of dispersions,
                # not the (possibly cancelling) covariance itself.
                for j, value in enumerate(expected):
                    a, b = x[t - n + 1 : t + 1, j], y[t - n + 1 : t + 1, j]
                    scale = np.sqrt(exact_statistic(a, a, "COV") * exact_statistic(b, b, "COV"))
                    assert abs(result.values[t, j] - value) <= 4e-15 * scale + 1e-14
