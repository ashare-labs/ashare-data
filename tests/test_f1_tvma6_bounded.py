from copy import deepcopy


from datetime import date, timedelta


from fractions import Fraction as F


import pytest


from ashare_data.f1_acceptance import AcceptanceError


from ashare_data.f1_price_volume import InputUnavailable, RESEARCH_ROLE


from ashare_data.f1_tvma6_bounded import (
    bounded_prediction,
    compare_reference,
    round_milliyuan_even,
)


def series(count):
    days = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(count)]
    return {
        "security": "600519.XSHG",
        "sessions": days,
        "source_family": "Sina",
        "currency": "CNY",
        "raw_price_adjustment": "none",
        "amount_unit": "CNY",
        "amount_policy": "native",
        "price_unit": "CNY_per_share",
        "price_policy": "raw_times_native_hfq_no_rounding",
        "factors": [{"date": days[0], "value": "1"}],
        "rows": [
            {
                "date": day,
                "trading_state": "observed_trade",
                "amount": str(i + 1),
                "high": "11",
                "low": "9",
                "close": "10",
            }
            for i, day in enumerate(days)
        ],
    }


def money(item, day=None):
    return bounded_prediction(item, day or item["sessions"][-1], role=RESEARCH_ROLE)


def test_first_valid_six_day_window_includes_today_and_five_prior():
    item = series(7)
    with pytest.raises(InputUnavailable, match="WARMUP_INSUFFICIENT"):
        money(item, item["sessions"][4])
    assert money(item, item["sessions"][5])["unrounded_mean_exact_ratio"] == [7, 2]
    assert money(item)["unrounded_mean_exact_ratio"] == [9, 2]


def test_source_uncertainty_does_not_shrink_with_six_correlated_errors():
    p = money(series(6))
    low, high = (F(*v) for v in p["source_precision"]["mean_interval_exact_ratios"])
    assert (low, high, high - low) == (F(3), F(4), F(1))
    assert p["source_precision"]["supplier_truth_bound_certified"] is False


def test_formula_is_independent_of_post_amount_prices_and_factors():
    item = series(6)
    before = money(item)
    item["factors"] = [{"date": "bad", "value": "NaN"}]
    for row in item["rows"]:
        row.update(close="999999", volume="42", postAmt="700000")
    assert money(item) == before


@pytest.mark.parametrize(
    "number,expected",
    [
        ("1.0005", "1.000"),
        ("1.0015", "1.002"),
        ("-1.0005", "-1.000"),
        ("-1.0015", "-1.002"),
        ("1234567890.123499999", "1234567890.123"),
        ("1234567890.123500001", "1234567890.124"),
    ],
)
def test_rounding_is_final_exact_half_even_at_fixed_absolute_quantum(number, expected):
    assert round_milliyuan_even(F(number)) == F(expected)


def test_rounding_does_not_round_each_input_before_averaging():
    item = series(6)
    item["rows"][-1]["amount"] = "7"
    p = money(item)
    assert p["unrounded_mean_exact_ratio"] == [11, 3]
    assert p["output_rounding_candidate"]["point_CNY"] == "3.667"


def test_three_goals_can_disagree_without_relabelling_original_failure():
    p = money(series(6))
    unchanged = deepcopy(p)
    result = compare_reference(p, reference_float_hex=float("3.7").hex())
    assert result["original_strict_comparison"]["within_original_arithmetic_bound"] is False
    assert (
        result["input_precision_comparison"][
            "inside_conditional_source_mean_interval_up_to_representation"
        ]
        is True
    )
    assert (
        result["output_rounding_comparison"]["conditional_rounded_output_interval_compatible"]
        is True
    )
    assert result["output_rounding_comparison"]["frozen_candidate_point_matches_binary64"] is False
    assert result["jq_equivalent_certified"] is False and p == unchanged


def test_output_lattice_can_be_rejected_even_when_input_interval_contains_value():
    result = compare_reference(money(series(6)), reference_float_hex=float("3.7004").hex())
    assert result["input_precision_comparison"][
        "inside_conditional_source_mean_interval_up_to_representation"
    ]
    assert not result["output_rounding_comparison"]["fixed_milliyuan_lattice_compatible"]


def test_outside_source_and_output_interval_rejected():
    result = compare_reference(money(series(6)), reference_float_hex=float("4.001").hex())
    assert not result["input_precision_comparison"][
        "inside_conditional_source_mean_interval_up_to_representation"
    ]
    assert not result["output_rounding_comparison"][
        "conditional_rounded_output_interval_compatible"
    ]


def test_interval_endpoint_is_compared_after_its_binary64_serialization():
    item = series(6)
    amount = 2**52 + 1
    for row in item["rows"]:
        row["amount"] = str(amount)
    # A possible +0.5-yuan endpoint is a valid milliyuan-grid output. Binary64
    # rounds this half-way value to the next even integer at this magnitude.
    observed = float(F(amount) + F(1, 2))
    assert observed == amount + 1
    result = compare_reference(money(item), reference_float_hex=observed.hex())
    assert result["output_rounding_comparison"]["conditional_rounded_output_interval_compatible"]


@pytest.mark.parametrize("bad", [1.0, True, "NaN", "Infinity", "1.1", str(2**53)])
def test_money_precision_contract_rejects_unsupported_values(bad):
    item = series(6)
    item["rows"][1]["amount"] = bad
    with pytest.raises(AcceptanceError):
        money(item)


@pytest.mark.parametrize("state", ["paused", "unknown", "verified_zero_trade"])
def test_normal_scope_does_not_inherit_any_zero_or_pause_semantics(state):
    item = series(6)
    item["rows"][1]["trading_state"] = state
    with pytest.raises(InputUnavailable):
        money(item)


def test_missing_session_is_not_compressed_and_zero_is_not_normal():
    item = series(7)
    item["rows"].pop(3)
    with pytest.raises(InputUnavailable, match="SOURCE_SESSION_MISSING"):
        money(item)
    item = series(6)
    item["rows"][2]["amount"] = "0"
    with pytest.raises(InputUnavailable, match="NORMAL_POSITIVE_AMOUNT_REQUIRED"):
        money(item)
