"""A bounded TVMA6 research implementation with separate comparison goals.

The old mean-six definition and its failed observations remain unchanged.
Nearest-yuan source error and final milliyuan rounding are explicit hypotheses,
not supplier promises. This module is not registered as a production factor.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction
import math

from .f1_acceptance import AcceptanceError
from .f1_price_volume import InputUnavailable, NativeSeries, RESEARCH_ROLE

DEFINITION = "native_money6_nearest_yuan_final_milliyuan_even.research.v1"
SPEC = {
    "definition": DEFINITION,
    "catalog_candidate": "TVMA6",
    "official_source": "https://www.joinquant.com/help/api/help#name:factor_values",
    "official_confirmed_part": "six-day moving average of transaction amount",
    "formula": "exact rational sum of six native CNY amounts including today / 6",
    "input_adjustment": "none; no price, volume, factor or postAmt used",
    "normal_boundary": "six explicit consecutive market sessions, observed_trade, amount > 0",
    "warmup_prior_sessions": 5,
    "missing_pause_zero_short_history": "unavailable; never fill, compress, or assume zero",
    "source_encoding_quantum_CNY": "1",
    "maximum_native_amount_CNY": "9007199254740991",
    "source_truth_error_bound_certified": False,
    "source_error_hypothesis": "each integer amount represents an unknown amount within +/-0.5 CNY",
    "aggregate_error": "arithmetic mean of six intervals; errors need not be independent",
    "output_rounding_candidate": "nearest 0.001 CNY, ties to even, once after exact mean",
    "output_rounding_officially_confirmed": False,
    "serialization_candidate": "one correctly rounded IEEE754 binary64 conversion of final decimal",
    "strict_legacy_rule": "50 * binary64 epsilon * abs(unrounded native mean); unchanged arithmetic-only rule",
    "scope": "retrospective native snapshot; normal trading only",
    "formal_id": None,
    "jq_equivalent": False,
    "production_eligible": False,
}


def decimal_text(value: Fraction, precision: int = 60) -> str:
    with localcontext() as context:
        context.prec = precision
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def exact_number(value) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if type(value) not in (str, int, Decimal):
        raise AcceptanceError("exact decimal text or integer required")
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise AcceptanceError("invalid decimal") from exc
    if not parsed.is_finite():
        raise AcceptanceError("finite decimal required")
    return Fraction(parsed)


def round_milliyuan_even(value: Fraction) -> Fraction:
    """Integer arithmetic avoids an intermediate decimal or binary rounding."""
    scaled = value * 1000
    floor, remainder = divmod(scaled.numerator, scaled.denominator)
    if 2 * remainder > scaled.denominator or (2 * remainder == scaled.denominator and floor % 2):
        floor += 1
    return Fraction(floor, 1000)


def money3(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 80
        return format(Decimal(value.numerator) / Decimal(value.denominator), ".3f")


def bounded_prediction(item: dict, output_date: str, *, role: str) -> dict:
    if role != RESEARCH_ROLE:
        raise AcceptanceError("research role required")
    if item.get("amount_unit") != "CNY" or item.get("amount_policy") != "native":
        raise AcceptanceError("native CNY amount required")
    series = NativeSeries(item)
    rows = series.window(output_date, 6)
    if any(r.get("trading_state") != "observed_trade" for r in rows):
        raise InputUnavailable("NORMAL_TRADING_ONLY")
    amounts = []
    for row in rows:
        if row.get("amount") is None:
            raise InputUnavailable("NATIVE_AMOUNT_MISSING", "source_missing")
        value = exact_number(row["amount"])
        if value <= 0:
            raise InputUnavailable("NORMAL_POSITIVE_AMOUNT_REQUIRED")
        if value.denominator != 1:
            raise AcceptanceError("this candidate requires verified integer-yuan encoding")
        if value > 2**53 - 1:
            raise AcceptanceError("native amount exceeds exact JavaScript integer boundary")
        amounts.append(value)
    mean = sum(amounts, Fraction()) / 6
    low, high = mean - Fraction(1, 2), mean + Fraction(1, 2)
    rounded = round_milliyuan_even(mean)
    rounded_low, rounded_high = round_milliyuan_even(low), round_milliyuan_even(high)
    arithmetic_bound = 50 * Fraction(1, 2**52) * abs(mean)
    return {
        "security": series.security,
        "date": output_date,
        "definition": DEFINITION,
        "window_dates": [r["date"] for r in rows],
        "warmup_prior_sessions": 5,
        "native_amounts_CNY": [str(x.numerator) for x in amounts],
        "unrounded_mean_exact_ratio": [mean.numerator, mean.denominator],
        "legacy_unrounded_point_CNY": decimal_text(mean, 50),
        "strict_legacy_arithmetic_bound_CNY": decimal_text(arithmetic_bound),
        "source_precision": {
            "encoded_quantum_CNY": "1",
            "supplier_truth_bound_certified": False,
            "conditional_assumption": "nearest yuan for each input; closed conservative +/-0.5 CNY",
            "mean_interval_CNY": [decimal_text(low), decimal_text(high)],
            "mean_interval_exact_ratios": [
                [low.numerator, low.denominator],
                [high.numerator, high.denominator],
            ],
            "mean_half_width_CNY": "0.5",
            "stochastic_independence_assumed": False,
        },
        "output_rounding_candidate": {
            "quantum_CNY": "0.001",
            "ties": "half_even",
            "stage": "final_only",
            "point_CNY": money3(rounded),
            "point_binary64_hex": float(rounded).hex(),
            "conditional_output_interval_CNY": [money3(rounded_low), money3(rounded_high)],
            "officially_confirmed": False,
        },
        "normal_trading_only": True,
        "formal_id": None,
        "jq_equivalent": False,
        "production_eligible": False,
    }


def compare_reference(prediction: dict, *, reference_float_hex: str) -> dict:
    """Consume a revealed target separately; never revise the input prediction."""
    if prediction.get("definition") != DEFINITION:
        raise AcceptanceError("locked definition required")
    try:
        observed_float = float.fromhex(reference_float_hex)
    except (TypeError, ValueError) as exc:
        raise AcceptanceError("binary64 reference required") from exc
    if not math.isfinite(observed_float) or observed_float.hex() != reference_float_hex:
        raise AcceptanceError("canonical finite binary64 required")
    observed = Fraction.from_float(observed_float)
    mean = Fraction(*prediction["unrounded_mean_exact_ratio"])
    source_low, source_high = [
        Fraction(*r) for r in prediction["source_precision"]["mean_interval_exact_ratios"]
    ]
    half_ulp = Fraction.from_float(math.ulp(observed_float)) / 2
    bound = 50 * Fraction(1, 2**52) * abs(mean)
    rounded = prediction["output_rounding_candidate"]
    rounded_low, rounded_high = [
        exact_number(s) for s in rounded["conditional_output_interval_CNY"]
    ]
    nearest_lattice_value = round_milliyuan_even(observed)
    lattice_compatible = float(nearest_lattice_value).hex() == reference_float_hex
    # The source interval and candidate output interval are distinct checks.
    economic_compatible = observed + half_ulp >= source_low and observed - half_ulp <= source_high
    # Serialize interval endpoints too: at large supported amounts, several
    # milliyuan grid points can share one binary64 value. Comparing the observed
    # float to unconverted decimal endpoints would incorrectly reject that edge.
    output_compatible = lattice_compatible and float(rounded_low) <= observed_float <= float(
        rounded_high
    )
    return {
        "security": prediction["security"],
        "date": prediction["date"],
        "original_strict_comparison": {
            "target_minus_native_mean_CNY": decimal_text(observed - mean),
            "arithmetic_bound_CNY": decimal_text(bound),
            "within_original_arithmetic_bound": abs(observed - mean) <= bound,
            "old_observations_or_tolerance_modified": False,
        },
        "input_precision_comparison": {
            "target_representation_half_ULP_CNY": decimal_text(half_ulp),
            "inside_conditional_source_mean_interval_up_to_representation": economic_compatible,
            "absolute_relative_error": decimal_text(abs(observed - mean) / abs(mean)),
            "meaning": "limited numerical consistency conditional on source precision assumption",
            "supplier_error_guaranteed": False,
        },
        "output_rounding_comparison": {
            "fixed_milliyuan_lattice_compatible": lattice_compatible,
            "conditional_rounded_output_interval_compatible": output_compatible,
            "frozen_candidate_point_matches_binary64": rounded["point_binary64_hex"]
            == reference_float_hex,
            "target_minus_candidate_point_CNY": decimal_text(
                observed - exact_number(rounded["point_CNY"])
            ),
            "rounding_rule_identified": False,
            "limitation": "integer-yuan inputs cannot identify exact output rounding from interval containment alone",
        },
        "jq_equivalent_certified": False,
        "production_eligible": False,
    }
