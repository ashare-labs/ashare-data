"""Explicit FI02 and SV research stages, without a JoinQuant certification alias.

Ported from the frozen round13 candidate arithmetic onto the c2b98e3 integration
baseline. The round14 read confirms FI02 and VOL5 at one key each and eight raw
valuation keys. The named SV02 candidate FAILED by one ULP and stays unchanged.
Calculations never promote arbitrary inputs or these observations to a certificate.
"""

from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
import math
import struct

from .f1_acceptance import instant, number
from .f1_financial import InputUnavailable, RESEARCH_MODE, VisibilityUnproven
from .f1_followup import annual_bridge, digest
from .f1_precision import require, six_decimal_profiles

SHARE_MODE = "bounded_official_share_structure_chain"
FI02_RULE = "current_cutoff_TTM_over_prior_calendar_year_cutoff_TTM_nearest6_nonmidpoint"
SV01_RULE = "daily_percent_nearest4_nonmidpoint_then_exact_five_session_mean"
SV02_RULE = "cap_1e8_CNY_nearest4_nonmidpoint_float64_then_multiply_1e8"


def _float(value):
    value = float(value)
    require(math.isfinite(value), "nonfinite float64 candidate")
    return {"float64_17g": format(value, ".17g"), "float64_bits_be": struct.pack(">d", value).hex()}


def _decimal(value):
    with localcontext() as context:
        context.prec = 60
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _nearest4(value):
    units, remainder = divmod(abs(value).numerator * 10_000, value.denominator)
    require(2 * remainder != value.denominator, "midpoint outside bounded research scope")
    units += 2 * remainder > value.denominator
    return Fraction((-1 if value < 0 else 1) * units, 10_000)


def fi02_lagged_candidate(ledger, security, day, *, mode):
    """Require eight quarters and exact cents; select the prior vintage explicitly.

    This is a version candidate, not a claim about the vendor's unique policy.
    Feb29's prior calendar-year date is Feb28. Date-only reports remain subject
    to the existing next-natural-day Shanghai visibility convention.
    """
    require(mode == RESEARCH_MODE, "explicit date-only research mode required")
    today = date.fromisoformat(day)
    require(today.isoformat() == day, "canonical ISO date required")
    try:
        previous = today.replace(year=today.year - 1)
    except ValueError:
        previous = today.replace(year=today.year - 1, day=28)
    cutoff, prior_cutoff = day + "T15:00:00+08:00", previous.isoformat() + "T15:00:00+08:00"
    period = ledger.latest_period(security, "net_profit", as_of=cutoff, mode=mode)
    prior_period = f"{int(period[:4]) - 1}{period[4:]}"
    current = ledger.quarter_ttm(security, "net_profit", period, as_of=cutoff, mode=mode)
    prior = ledger.quarter_ttm(security, "net_profit", prior_period, as_of=prior_cutoff, mode=mode)
    current_exact, current_oracle = annual_bridge(ledger, security, "net_profit", period, cutoff)
    prior_exact, prior_oracle = annual_bridge(
        ledger, security, "net_profit", prior_period, prior_cutoff
    )
    for chain, exact in ((current, current_exact), (prior, prior_exact)):
        require(
            len(chain["quarters"]) == 4
            and sum((Fraction(q["value"]) for q in chain["quarters"]), Fraction()) == exact,
            "complete quarter chain must agree with exact-cent annual bridge",
        )
        for quarter in chain["quarters"]:
            require(
                all((Fraction(t["value"]) * 100).denominator == 1 for t in quarter["terms"]),
                "every quarterly source amount must be exact CNY cents",
            )
    require(prior_exact > 0, "positive prior TTM required")
    ratio = (current_exact - prior_exact) / prior_exact
    profiles = six_decimal_profiles(ratio)
    return {
        "slot": "FI02",
        "factor_id": "net_profit_growth_rate",
        "security": security,
        "date": day,
        "as_of": cutoff,
        "prior_as_of": prior_cutoff,
        "period_end": period,
        "unit": "ratio",
        "selected_rule": FI02_RULE,
        "visibility_mode": mode,
        "current_ttm": current,
        "prior_ttm": prior,
        "annual_bridge_oracle": [current_oracle, prior_oracle],
        "exact_ratio": {"numerator": ratio.numerator, "denominator": ratio.denominator},
        "prediction": profiles["nearest6_nonmidpoint"],
        "truncate6": profiles["truncate6_toward_zero"],
        "distinguishes_truncation": profiles["nearest6_nonmidpoint"]["float64_bits_be"]
        != profiles["truncate6_toward_zero"]["float64_bits_be"],
        "eight_quarter_chain_complete": True,
        "vendor_version_policy_proven": False,
        "independent_review_completed": False,
        "jq_equivalent": False,
        "historical_pit_certified": False,
    }


def sv_stage_candidate(market, sessions, shares, day, *, mode):
    """Preserve the frozen daily-percent and cap conversion stages.

    ``shares`` must be a caller-verified bounded disclosure reconstruction with
    explicit security, shares unit, same-day 09:00 cutoff and research mode.
    This function validates input shape and arithmetic; it does not authenticate
    a share registry. Missing sessions are never filled or compressed.
    """
    if mode == "strict":
        raise VisibilityUnproven("bounded disclosures are not daily registrar proof")
    require(mode == SHARE_MODE, "explicit bounded share mode required")
    require(
        market["price_adjustment"] == "none"
        and market["price_unit"] == "CNY/share"
        and market["volume_unit"] == "shares",
        "unadjusted CNY/share and volume shares required",
    )
    require(
        sessions == sorted(set(sessions)) and day in sessions, "ordered session calendar required"
    )
    for session in sessions:
        require(
            date.fromisoformat(session).isoformat() == session, "canonical session date required"
        )
    index = sessions.index(day)
    if index < 4:
        raise InputUnavailable("five sessions including today required")
    bars, counts = {r["date"]: r for r in market["rows"]}, {r["date"]: r for r in shares}
    require(
        len(bars) == len(market["rows"]) and len(counts) == len(shares), "duplicate daily input"
    )
    window, daily, rounded = sessions[index - 4 : index + 1], [], []
    for session in window:
        if session not in bars or session not in counts:
            raise InputUnavailable("missing session; no fill or shortened window")
        count = counts[session]
        require(
            count["security"] == market["security"]
            and count["unit"] == "shares"
            and count["mode"] == mode
            and count["as_of"] == session + "T09:00:00+08:00",
            "same-session explicitly sourced share denominator required",
        )
        instant(count["as_of"])
        volume, denominator = number(bars[session]["volume"]), number(count["value"])
        require(
            volume > 0 and volume == volume.to_integral_value(),
            "positive integer traded shares required",
        )
        require(
            denominator > 0 and denominator == denominator.to_integral_value(),
            "positive integer share denominator required",
        )
        raw = Fraction(volume) * 100 / Fraction(denominator)
        value = _nearest4(raw)
        rounded.append(value)
        daily.append(
            {
                "date": session,
                "native_percent": _decimal(raw),
                "nearest4_percent": _decimal(value),
                "prediction": _float(value),
            }
        )
    close = number(bars[day]["close"])
    require(close > 0, "positive unadjusted close required")
    exact_cny = Fraction(close) * Fraction(number(counts[day]["value"]))
    raw_cap4 = _nearest4(exact_cny / 100_000_000)
    return {
        "security": market["security"],
        "date": day,
        "window": window,
        "daily_inputs": daily,
        "SV01": {
            "unit": "percent",
            "selected_rule": SV01_RULE,
            "prediction": _float(sum(rounded, Fraction()) / 5),
        },
        "SV02": {
            "unit": "CNY",
            "selected_rule": SV02_RULE,
            "native_CNY": _decimal(exact_cny),
            "raw_cap4_decimal": _decimal(raw_cap4),
            "raw_cap_prediction": _float(raw_cap4),
            "raw_cap_unit": "1e8_CNY",
            "prediction": _float(float(raw_cap4) * 1e8),
            "candidate_status": "failed_round14_named_key_zero_ULP_rule_preserved",
        },
        "source_authentication_performed": False,
        "independent_review_completed": False,
        "jq_equivalent": False,
        "historical_pit_certified": False,
    }


def observe_fi_sv_batch(lock, records, authorization):
    """Compare all frozen keys, preserving failures and separating review from arithmetic."""
    payload = lock["payload"]
    require(digest(payload) == lock["sha256"], "changed prediction lock")
    require(
        payload["schema"] == "ad.f1-fi-sv-batch-lock.v1"
        and payload["tolerance"] == {"float64_ULP": 0, "atol": "0", "rtol": "0"}
        and payload["max_calls"] == 4
        and payload["max_values"] == 11,
        "sealed four-call eleven-cell zero-ULP experiment required",
    )
    require(
        authorization["frozen_lock_sha256"] == lock["sha256"]
        and authorization["reference_read_authorized"] is True
        and authorization["max_calls"] == 4
        and authorization["max_values"] == 11,
        "explicit bounded acquisition authorization required",
    )
    authorized_at = instant(authorization["recorded_at"])
    require(instant(payload["frozen_at"]) <= authorized_at, "authorization must follow frozen lock")
    expected = payload["rows"]
    require(len(expected) == len(records) == 11, "missing or extra observations")

    def key(row):
        return tuple(row[k] for k in ("api", "security", "date", "field"))

    require(len({key(r) for r in expected}) == 11, "duplicate frozen key")
    require(
        [r["role"] for r in expected] == ["named_confirmation"] * 3 + ["raw_stage_diagnostic"] * 8,
        "changed named/raw roles or order",
    )
    rows = []
    for predicted, observed in zip(expected, records, strict=True):
        require(key(predicted) == key(observed), "changed key or order")
        require(authorized_at < instant(observed["first_seen_at"]), "target predates authorization")
        require(
            observed["provider"] == "JoinQuant" and observed["dtype"] == "float64",
            "native JoinQuant float64 required",
        )
        require(observed["missing_kind"] in {"none", "NaN", "absent"}, "unknown missing mask")
        predicted_value = _float(predicted["prediction"]["float64_17g"])
        require(
            predicted_value == {k: predicted["prediction"][k] for k in predicted_value},
            "prediction float64 transport mismatch",
        )
        passed, difference = False, None
        if observed["missing_kind"] == "none":
            actual = _float(observed["value_17g"])
            require(
                actual["float64_17g"] == observed["value_17g"]
                and actual["float64_bits_be"] == observed["float64_bits_be"],
                "observation float64 transport mismatch",
            )
            passed = actual == predicted_value
            # Exact dyadic difference is diagnostic; it never relaxes the acceptance rule.
            difference = str(
                Fraction(float(observed["value_17g"]))
                - Fraction(float(predicted_value["float64_17g"]))
            )
        else:
            require(
                observed["value_17g"] is None and observed["float64_bits_be"] is None,
                "inconsistent missing representation",
            )
        rows.append(
            {
                **observed,
                "role": predicted["role"],
                "expected": predicted["prediction"],
                "zero_ULP_pass": passed,
                "actual_minus_expected_exact": difference,
            }
        )
    return {
        "schema": "ad.f1-fi-sv-observation.v1",
        "lock_sha256": lock["sha256"],
        "rows": rows,
        "cells": 11,
        "passed_cells": sum(r["zero_ULP_pass"] for r in rows),
        "named_passed_cells": sum(r["zero_ULP_pass"] for r in rows[:3]),
        "raw_passed_cells": sum(r["zero_ULP_pass"] for r in rows[3:]),
        "finite_numeric_batch_passed": all(r["zero_ULP_pass"] for r in rows),
        "independent_review_completed": False,
        "global_unexposed_proven": False,
        "strict_PIT_certified": False,
        "all_twelve_slots_passed": False,
        "another_holdout_required_for_passed_exact_keys": False,
        "failed_keys_may_be_silently_repaired": False,
    }
