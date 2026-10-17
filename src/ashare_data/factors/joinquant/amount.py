"""Bounded TVMA6 arithmetic with explicit source precision and output rounding hypotheses.

Calculation alone does not certify historical visibility or provider equivalence.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction

from ashare_data.financials.validation import AcceptanceError
from ashare_data.factors.joinquant.amount_inputs import (
    InputUnavailable,
    NativeSeries,
    RESEARCH_ROLE,
)

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
