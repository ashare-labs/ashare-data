"""Frozen FI01/FI04 prediction and comparison protocol, without network access.

The legacy comparison still requires review before exposure. Integration evidence
records the later independent review separately and does not backdate it.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
import math
import struct

from .f1_acceptance import instant, number
from .f1_financial import FINANCIAL_IDS, FinancialLedger, RESEARCH_MODE, financial_factor_candidate
from .f1_precision import require, six_decimal_profiles

RULE = "nearest6_nonmidpoint"


TOLERANCE = {"float64_ULP": 0, "atol": "0", "rtol": "0"}


EXCLUSIONS = [
    "official midpoint or negative-base policy",
    "all-market or all-date equivalence",
    "historical vendor intake clock and complete revision history",
    "strict PIT or universal availability of a structurally ambiguous ledger",
    "other original slots or complete twelve-slot certification",
]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def regime(row):
    return (row["security"], row["slot"], row["period_end"])


def annual_bridge(ledger, security, field, period, cutoff):
    """An exact integer-cent oracle independent of quarter-difference accumulation."""
    periods = [(1, period)]
    if period[5:] != "12-31":
        prior_year = int(period[:4]) - 1
        periods += [(1, f"{prior_year}-12-31"), (-1, f"{prior_year}{period[4:]}")]
    amount, terms = Fraction(0), []
    for sign, end in periods:
        row = ledger.select(security, field, end, as_of=cutoff, mode=RESEARCH_MODE)
        cents = Fraction(row["value"]) * 100
        require(cents.denominator == 1, "source amount is not exact CNY cents")
        amount += sign * cents / 100
        terms.append({"coefficient": sign, "record_id": row["record_id"], "cents": int(cents)})
    return amount, {"field": field, "period_end": period, "terms": terms, "value": str(amount)}


def confirmation_prediction(ledger, case, exposed_regimes):
    """Compute one fixed-rule prediction, rejecting re-dated or nondiscriminating cases."""
    slot, security, day = case["slot"], case["security"], case["date"]
    require(slot in {"FI01", "FI04"}, "confirmation is limited to FI01/FI04")
    cutoff = day + "T15:00:00+08:00"
    native = financial_factor_candidate(ledger, slot, security, cutoff, mode=RESEARCH_MODE)
    require(regime(native) not in {tuple(r) for r in exposed_regimes}, "exposed financial regime")
    period = native["period_end"]
    if slot == "FI01":
        numerator, a = annual_bridge(ledger, security, "net_profit", period, cutoff)
        denominator, b = annual_bridge(ledger, security, "total_operating_revenue", period, cutoff)
    else:
        current, a = annual_bridge(ledger, security, "net_operate_cash_flow", period, cutoff)
        prior_period = f"{int(period[:4]) - 1}{period[4:]}"
        denominator, b = annual_bridge(
            ledger, security, "net_operate_cash_flow", prior_period, cutoff
        )
        numerator = current - denominator
    require(denominator > 0, "confirmation scope requires a positive denominator")
    ratio = numerator / denominator
    with localcontext() as ctx:
        ctx.prec = 50
        require(
            Decimal(ratio.numerator) / Decimal(ratio.denominator) == number(native["value"]),
            "annual bridge disagrees with quarter chain",
        )
    profiles = six_decimal_profiles(ratio)
    require(
        profiles[RULE]["float64_bits_be"] != profiles["truncate6_toward_zero"]["float64_bits_be"],
        "nearest and truncation coincide; not an eligible final confirmation cell",
    )
    return {
        **native,
        "date": day,
        "exact_ratio": {"numerator": ratio.numerator, "denominator": ratio.denominator},
        "annual_bridge_oracle": [a, b],
        "selected_rule": RULE,
        "prediction": profiles[RULE],
        "discriminates_from_rejected_truncation": True,
    }


def freeze_confirmation(pack, cases, sample_plan, exposure, artifacts, frozen_at):
    """Prepare 2-4 complete cases. No partial lock or synthetic fallback is emitted."""
    instant(frozen_at)
    require(2 <= len(cases) <= 4, "two to four confirmation cells required")
    require({c["slot"] for c in cases} == {"FI01", "FI04"}, "both original slots required")
    keys = [(c["security"], c["date"], c["slot"]) for c in cases]
    require(len(keys) == len(set(keys)), "duplicate confirmation key")
    groups = list(dict.fromkeys((c["security"], c["date"]) for c in cases))
    require(len(groups) <= 2, "at most two bounded single-date calls")
    grouped = [
        c
        for security, day in groups
        for c in cases
        if (c["security"], c["date"]) == (security, day)
    ]
    require(cases == grouped, "cases must follow frozen call and factor order")
    require(
        exposure["scope"] == "local_observed_FI_regimes_global_review_pending",
        "explicit exposure scope required",
    )
    required_roles = {"input", "source_provenance", "calendar", "candidate_code", "oracle_code"}
    require(required_roles <= {a["role"] for a in artifacts}, "complete artifact bindings required")
    for artifact in artifacts:
        h = artifact["sha256"]
        require(len(h) == 64 and all(c in "0123456789abcdef" for c in h), "artifact hash required")
    ledger, rows, regimes = FinancialLedger(pack), [], set()
    for case in cases:
        day = date.fromisoformat(case["date"])
        require(case["security"] in sample_plan["securities"], "security outside original sample")
        require(
            any(
                date.fromisoformat(a) <= day <= date.fromisoformat(b)
                for a, b in sample_plan["windows"]
            ),
            "date outside original windows",
        )
        require(case["date"] in sample_plan["verified_sessions"], "unverified exchange session")
        require(
            (case["security"], case["date"], FINANCIAL_IDS[case["slot"]])
            not in {tuple(k) for k in exposure["keys"]},
            "exposed reference key",
        )
        row = confirmation_prediction(ledger, case, exposure["regimes"])
        require(regime(row) not in regimes, "repeated financial regime within confirmation")
        regimes.add(regime(row))
        rows.append(row)
    payload = {
        "schema": "ad.f1-final-confirmation-lock.v1",
        "frozen_at": frozen_at,
        "selected_rule": RULE,
        "tolerance": deepcopy(TOLERANCE),
        "rows": rows,
        "input_digest": digest(pack),
        "exposure_digest": digest(exposure),
        "sample_plan_digest": digest(sample_plan),
        "artifacts": deepcopy(artifacts),
        "cross_thread_exposure_review_required_before_read": True,
        "certification_if_all_pass": "finite_external_confirmation_of_locked_FI01_FI04_on_exact_keys",
        "additional_holdout_required_for_that_exact_scope_after_pass": False,
        "exclusions": EXCLUSIONS,
        "on_failure": "record failure; do not alter rule, tolerance, inputs, or substitute keys",
        "max_reference_values": len(rows),
        "reference_request": {
            "status": "frozen_not_authorized_or_executed",
            "max_values": len(rows),
            "max_calls": len(groups),
            "calls": [
                {
                    "api": "jqfactor.get_factor_values",
                    "securities": [security],
                    "start_date": day,
                    "end_date": day,
                    "factors": [
                        r["factor_id"]
                        for r in rows
                        if (r["security"], r["date"]) == (security, day)
                    ],
                    "max_values": sum((r["security"], r["date"]) == (security, day) for r in rows),
                }
                for security, day in groups
            ],
        },
        "no_reference_calls_executed": True,
    }
    return {"payload": payload, "sha256": digest(payload)}


def compare_confirmation(lock, records, exposure_review):
    """A finite pass closes the specified external confirmation; no moving goalpost."""
    payload = lock["payload"]
    require(lock["sha256"] == digest(payload), "confirmation lock changed")
    require(
        payload["schema"] == "ad.f1-final-confirmation-lock.v1"
        and payload["selected_rule"] == RULE
        and payload["tolerance"] == TOLERANCE,
        "locked rule or tolerance differs",
    )
    require(
        2 <= len(payload["rows"]) <= 4
        and {r["slot"] for r in payload["rows"]} == {"FI01", "FI04"}
        and len({regime(r) for r in payload["rows"]}) == len(payload["rows"]),
        "complete nonrepeated confirmation scope required",
    )
    require(
        exposure_review["lock_sha256"] == lock["sha256"]
        and exposure_review["cross_thread_unexposed"] is True
        and exposure_review["input_provenance_independently_reviewed"] is True
        and exposure_review["artifact_hashes_verified"] is True
        and exposure_review["reviewer"]
        and exposure_review["evidence_sha256"],
        "independent exposure/provenance review required",
    )
    reviewed_at = instant(exposure_review["reviewed_at"])
    require(instant(payload["frozen_at"]) <= reviewed_at, "review must bind the completed freeze")
    require(len(records) == len(payload["rows"]), "no missing or additional reference rows")
    rows = []
    for expected, actual in zip(payload["rows"], records, strict=True):
        require(
            (actual["security"], actual["date"], actual["factor_id"])
            == (expected["security"], expected["date"], expected["factor_id"]),
            "reference key or order differs",
        )
        require(actual["dtype"] == "float64", "float64 transport required")
        require(actual["provider"] == "JoinQuant", "unexpected reference provider")
        require(reviewed_at < instant(actual["first_seen_at"]), "reference exposed before review")
        require(
            instant(actual["first_seen_at"]) <= instant(actual["observed_at"]),
            "reference observation chronology",
        )
        matches = False
        if actual["missing_kind"] == "none":
            value = float(actual["value_17g"])
            require(
                math.isfinite(value)
                and format(value, ".17g") == actual["value_17g"]
                and struct.pack(">d", value).hex() == actual["float64_bits_be"],
                "reference raw representation conflicts",
            )
            matches = actual["float64_bits_be"] == expected["prediction"]["float64_bits_be"]
        else:
            require(
                actual["value_17g"] is None and actual["float64_bits_be"] is None,
                "missing value metadata conflicts",
            )
        rows.append({**actual, "matches_locked_rule": matches})
    passed = all(r["matches_locked_rule"] for r in rows)
    return {
        "status": "finite_external_confirmation_passed" if passed else "confirmation_failed",
        "scope": [(r["security"], r["date"], r["factor_id"]) for r in rows],
        "rows": rows,
        "finite_confirmation_complete": passed,
        "another_holdout_required_for_this_exact_scope": False,
        "on_failure": payload["on_failure"],
        "exclusions": EXCLUSIONS,
        "strict_PIT_certified": False,
        "all_twelve_slots_passed": False,
    }
