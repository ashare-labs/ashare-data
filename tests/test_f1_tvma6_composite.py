from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction as F
import math

import pytest

from ashare_data.f1_acceptance import AcceptanceError
from ashare_data.f1_price_volume import RESEARCH_ROLE
from ashare_data.f1_tvma6_bounded import bounded_prediction, compare_reference
from ashare_data.f1_tvma6_composite import (
    compare_composite_reference,
    summarize_composite_comparisons,
)


def prediction(amounts):
    days = ["2024-04-12", "2024-04-15", "2024-04-16", "2024-04-17", "2024-04-18", "2024-04-19"]
    item = {
        "security": "300124.XSHE",
        "sessions": days,
        "source_family": "Sina",
        "currency": "CNY",
        "raw_price_adjustment": "none",
        "amount_unit": "CNY",
        "amount_policy": "native",
        "rows": [
            {"date": day, "amount": str(amount), "trading_state": "observed_trade"}
            for day, amount in zip(days, amounts, strict=True)
        ],
    }
    return bounded_prediction(item, days[-1], role=RESEARCH_ROLE)


@pytest.mark.parametrize(
    "last,latent_delta,expected",
    [
        (700000007, "0.4999", "700000004.167"),
        (700000005, "-0.4999", "700000002.833"),
    ],
)
def test_legitimate_rounded_edge_is_not_rejected_by_unrounded_diagnostic(
    last, latent_delta, expected
):
    amounts = [700000001, 700000002, 700000003, 700000004, 700000005, last]
    p = prediction(amounts)
    before = deepcopy(p)
    with localcontext() as context:
        context.prec = 80
        latent = [Decimal(amount) + Decimal(latent_delta) for amount in amounts]
        assert [x.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN) for x in latent] == amounts
        assert all(abs(x - amount) < Decimal("0.5") for x, amount in zip(latent, amounts))
        output = (sum(latent) / 6).quantize(Decimal(".001"), rounding=ROUND_HALF_EVEN)
        assert output == Decimal(expected)
    result = compare_composite_reference(p, reference_float_hex=float(output).hex())
    original = result["original_v1_comparison"]
    assert not original["input_precision_comparison"][
        "inside_conditional_source_mean_interval_up_to_representation"
    ]
    assert not original["original_strict_comparison"]["within_original_arithmetic_bound"]
    assert not result["original_preregistered_bounded_checks_all_satisfied"]
    assert result["composite_model_compatible"]
    summary = summarize_composite_comparisons([result])
    assert not summary["original_v1_checks"]["bounded_consistency_met"]
    assert summary["composite_model"]["all_compatible"]
    assert original == compare_reference(p, reference_float_hex=float(output).hex())
    assert p == before


@pytest.mark.parametrize(
    "target,raw,lattice,compatible",
    [
        ("700000003.167", True, True, True),
        ("700000004.167", False, True, True),
        ("700000003.166", False, True, False),
        ("700000004.168", False, True, False),
        ("700000004.1664", True, False, False),
    ],
)
def test_fixed_interval_and_lattice_still_reject_outside_or_off_grid(
    target, raw, lattice, compatible
):
    p = prediction([700000001, 700000002, 700000003, 700000004, 700000005, 700000007])
    result = compare_composite_reference(p, reference_float_hex=float(target).hex())
    original = result["original_v1_comparison"]
    assert (
        original["input_precision_comparison"][
            "inside_conditional_source_mean_interval_up_to_representation"
        ]
        is raw
    )
    assert original["output_rounding_comparison"]["fixed_milliyuan_lattice_compatible"] is lattice
    assert result["composite_model_compatible"] is compatible


@pytest.mark.parametrize("base", [1, 700000000, 2**43, 2**52, 2**53 - 8])
@pytest.mark.parametrize("residue", range(6))
def test_serialized_image_membership_against_independent_enumerated_decimal_oracle(base, residue):
    # All six integer-input sum residues; also cover multiple grid nodes sharing
    # one binary64 value at large supported magnitudes. This is synthetic only.
    amounts = [base] * 5 + [base + residue]
    p = prediction(amounts)
    with localcontext() as context:
        context.prec = 80
        mean = Decimal(sum(amounts)) / 6
        low = (mean - Decimal(".5")).quantize(Decimal(".001"), rounding=ROUND_HALF_EVEN)
        high = (mean + Decimal(".5")).quantize(Decimal(".001"), rounding=ROUND_HALF_EVEN)
        image = {
            float(Decimal(k) / 1000).hex() for k in range(int(low * 1000), int(high * 1000) + 1)
        }
        center = float(mean.quantize(Decimal(".001"), rounding=ROUND_HALF_EVEN))
        probes = {
            float(low),
            float(high),
            float(low - Decimal(".001")),
            float(high + Decimal(".001")),
            center,
            math.nextafter(float(low), -math.inf),
            math.nextafter(float(high), math.inf),
            math.nextafter(center, math.inf),
        }
    for observed in probes:
        result = compare_composite_reference(p, reference_float_hex=observed.hex())
        assert result["composite_model_compatible"] == (observed.hex() in image)


def test_summary_keeps_strict_failure_historical_checks_and_conditional_claims_separate():
    result = compare_composite_reference(
        prediction([1, 2, 3, 4, 5, 6]), reference_float_hex=float("3.7").hex()
    )
    summary = summarize_composite_comparisons([result])
    assert summary["original_v1_checks"]["pass_counts"] == {
        "original_strict": 0,
        "conditional_input_interval": 1,
        "fixed_output_lattice": 1,
        "conditional_output_interval": 1,
        "frozen_point_binary64": 0,
    }
    assert summary["original_v1_checks"]["bounded_consistency_met"]
    assert summary["composite_model"]["all_compatible"]
    assert not summary["original_v1_checks"]["strict_acceptance_met"]
    assert not summary["original_v1_checks"]["point_replication_met"]
    assert not summary["source_error_bound_certified"]
    assert not summary["rounding_rule_identified"]
    assert not summary["JQ_equivalence_or_production_acceptance"]


def test_empty_unversioned_or_duplicate_comparisons_cannot_be_reported_as_acceptance():
    result = compare_composite_reference(
        prediction([1, 2, 3, 4, 5, 6]), reference_float_hex=float(F(7, 2)).hex()
    )
    for records in [[], [result["original_v1_comparison"]], [result, result]]:
        with pytest.raises(AcceptanceError):
            summarize_composite_comparisons(records)
