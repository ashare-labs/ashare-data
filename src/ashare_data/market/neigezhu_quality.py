"""Conditional diagnostics for raw neigezhu bars; no correction or admission.

Storage envelopes match the pinned full audit. One-yuan and +/-99-share
calculations are named hypotheses, never acceptance tolerances.
"""

import numpy as np


def representation_error(values):
    a = np.asarray(values, dtype=np.float64)
    with np.errstate(invalid="ignore", over="ignore"):
        f = a.astype(np.float32)
        embedded = f.astype(np.float64) == a
        up = np.nextafter(f, np.float32(np.inf)).astype(np.float64) - a
        down = a - np.nextafter(f, np.float32(-np.inf)).astype(np.float64)
        half64 = np.abs(np.spacing(a)) * 0.5
        error = np.where(embedded, np.maximum(up, down) * 0.5, half64)
        error += 4 * np.abs(np.spacing(a))
    return error, embedded


def diagnostics(prices, volume, amount):
    """Return overlapping factual facets and explicitly conditional hypotheses."""
    prices = np.asarray(prices, dtype=np.float64)
    v, a = np.asarray(volume, dtype=np.float64), np.asarray(amount, dtype=np.float64)
    errors, embedded = representation_error(prices)
    ae, amount32 = representation_error(a)
    oe, he, le, ce = errors.T
    o, h, low, c = prices.T
    ae = np.maximum(0.005, ae)
    price_finite = np.isfinite(prices).all(axis=1)
    finite = price_finite & np.isfinite(v) & np.isfinite(a)
    eligible = finite & (v > 0) & (v < 2**53) & (low > 0) & (h >= low) & (a >= 0)
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        outside = eligible & ((a + ae < v * (low - le)) | (a - ae > v * (h + he)))
        yuan_outside = eligible & (
            (a + np.maximum(1.0, ae) < v * (low - le)) | (a - np.maximum(1.0, ae) > v * (h + he))
        )
        minimum = np.ceil(np.nextafter((a - ae) / (h + he), -np.inf))
        maximum = np.floor(np.nextafter((a + ae) / (low - le), np.inf))
        feasible99 = np.maximum(0, np.maximum(minimum, v - 99)) <= np.minimum(maximum, v + 99)
    lattice = np.remainder(v, 100) == 0
    flags = {
        "rows": np.ones(len(v), dtype=bool),
        "nonfinite_price": ~price_finite,
        "nonfinite_volume": ~np.isfinite(v),
        "nonfinite_amount": ~np.isfinite(a),
        "negative_volume": v < 0,
        "negative_amount": a < 0,
        "noninteger_volume": np.isfinite(v) & (v != np.trunc(v)),
        "eligible_positive_volume": eligible,
        "zero_volume": v == 0,
        "amount_integer": a == np.trunc(a),
        "all_prices_float32_compatible": embedded.all(axis=1),
        "amount_float32_compatible": amount32,
        "volume_multiple_100": lattice,
        "vwap_beyond_storage_envelope": outside,
        "vwap_beyond_candidate_yuan_rounding": yuan_outside,
        "outside_but_one_yuan_candidate_fits": outside & ~yuan_outside,
        "outside_with_float32_compatible_prices": outside & embedded.all(axis=1),
        "outside_with_float64_only_price": outside & ~embedded.all(axis=1),
        "outside_with_integer_amount": outside & (a == np.trunc(a)),
        "outside_with_volume_multiple_100": outside & lattice,
        "outside_with_non_lot_volume": outside & ~lattice,
        "outside_fixed_99_share_interval_feasible": outside & feasible99,
        "outside_cumulative_100_local_candidate": outside & feasible99 & lattice,
        "outside_99_share_candidate_wrong_lattice": outside & feasible99 & ~lattice,
        "outside_not_even_99_share_feasible": outside & ~feasible99,
        "nonpositive_price": np.any(prices <= 0, axis=1),
        # A missing quantity/amount cannot hide a contradiction in finite prices.
        "ohlc_envelope_error": price_finite
        & (
            (h + he < np.maximum.reduce([o - oe, c - ce, low - le]))
            | (low - le > np.minimum(o + oe, c + ce))
        ),
        "all_ohlc_zero": np.all(prices == 0, axis=1),
        "zero_volume_nonzero_amount": (v == 0) & (a != 0),
        "positive_volume_zero_amount": (v > 0) & (a == 0),
    }
    # Candidate ratios from CNY/thousand-CNY/ten-thousand-CNY and shares/lots.
    for ratio in (0.01, 10.0, 100.0, 1000.0, 10000.0):
        fits = (ratio * (a + ae) >= v * (low - le)) & (ratio * (a - ae) <= v * (h + he))
        flags[f"outside_unit_ratio_{ratio:g}_candidate"] = outside & fits
    # Same conditional rule as the separately sealed all-row extrema scan.
    # This never reclassifies prices as a certain error or changes admission.
    distinct = h - he > low + le
    extrema = eligible & (
        (
            (v >= 2)
            & ((a + ae < h - he + (v - 1) * (low - le)) | (a - ae > low + le + (v - 1) * (h + he)))
        )
        | ((v == 1) & distinct)
    )
    lower_v = np.maximum(np.where(distinct, 2, 1), v - 99)
    upper_v = v + 99
    yuan = np.maximum(1.0, ae)
    beyond_hypotheses = eligible & (
        (a + yuan < h - he + (lower_v - 1) * (low - le))
        | (a - yuan > low + le + (upper_v - 1) * (h + he))
    )
    flags["actual_extrema_exact_volume_conditional"] = extrema
    flags["new_extrema_inside_vwap_envelope"] = extrema & ~outside
    flags["new_extrema_beyond_99_share_and_one_yuan_hypotheses"] = (
        extrema & ~outside & beyond_hypotheses
    )
    return flags, dict(
        price_error=errors, amount_error=ae, true_volume_min=minimum, true_volume_max=maximum
    )


def cumulative_100_feasible(low, high, reported):
    """Existence test, not estimation of true volumes or vendor-algorithm proof.

    Integer true cumulative S must round half-up to reported cumulative R:
    R-50 <= S <= R+49. Per-bar integer-volume bounds derive from fixed OHLC/A
    envelopes. Prefix extrema implement interval propagation in linear time.
    """
    low, high, reported = (np.asarray(x, dtype=np.float64) for x in (low, high, reported))
    if not (
        low.ndim == high.ndim == reported.ndim == 1
        and len(low) == len(high) == len(reported)
        and len(low)
    ):
        return {"eligible": False, "reason": "nonempty_aligned_vectors_required"}
    low = np.maximum(0, low)
    if not (
        np.isfinite(low).all()
        and np.isfinite(high).all()
        and np.isfinite(reported).all()
        and (reported >= 0).all()
        and (low == np.floor(low)).all()
        and (high == np.floor(high)).all()
        and (np.remainder(reported, 100) == 0).all()
        and max(np.max(np.abs(np.cumsum(low))), np.max(np.abs(np.cumsum(high)))) < 2**53
    ):
        return {
            "eligible": False,
            "reason": "invalid_numeric_bounds_or_non100_lattice_or_float_integer_range",
        }
    lower, upper, r = np.cumsum(low), np.cumsum(high), np.cumsum(reported)
    possible_low = lower + np.maximum.accumulate(np.r_[0.0, r - 50 - lower])[1:]
    possible_high = upper + np.minimum.accumulate(np.r_[0.0, r + 49 - upper])[1:]
    # An empty local integer interval is a proof of infeasibility, not missing
    # applicability. A prefix can already fail before the first empty interval.
    empty = low > high
    bad = np.flatnonzero((possible_low > possible_high) | empty)
    return {
        "eligible": True,
        "feasible": not len(bad),
        "reason": (
            "empty_integer_volume_interval" if empty[bad[0]] else "incompatible_cumulative_prefix"
        )
        if len(bad)
        else "feasible_conditional_model",
        "first_infeasible_offset": int(bad[0]) if len(bad) else None,
        "terminal_true_volume_min": float(possible_low[-1]) if not len(bad) else None,
        "terminal_true_volume_max": float(possible_high[-1]) if not len(bad) else None,
    }
