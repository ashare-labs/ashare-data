"""Explicit research-only price semantics, separate from vendor data compatibility.

No data acquisition, provider fallback, security-specific scaling, or production
registration. A declared native basis is sufficient for source-specific research;
it never proves compatibility with JoinQuant's historical factor input basis.
"""

from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction as F
import hashlib
import json
import math
import re

DEFINITION = "post_price_q2_sample250_smaTR_ema60_q6.research.v1"
CONTRACT_SCHEMA = "ad.pv.source-basis.v1"
BUNDLE_SCHEMA = "ad.pv.price-bundle.v1"
SOURCE_ISSUE = "ADJ-INPUT-001"
FIELDS = ("Variance20", "ATR6", "ATR14", "EMA5")
MINIMUM = {"Variance20": 21, "ATR6": 7, "ATR14": 15, "EMA5": 60}
RULE = {
    "definition": DEFINITION,
    "price_processing": "post-adjusted H/L/C, nearest 0.01, half-even hypothesis",
    "Variance20": "250 * sample variance (ddof=1) of 20 simple close ratios minus 1",
    "ATR6": "simple mean of last6 max(H-L, abs(H-prevC), abs(L-prevC))",
    "ATR14": "simple mean of last14 max(H-L, abs(H-prevC), abs(L-prevC))",
    "EMA5": "alpha=1/3; exactly60 closes; first close seed; divide by last close",
    "output_processing": "nearest 0.000001, half-even hypothesis, then binary64",
    "calendar_policy": "explicit sessions; no skipping, filling or compression",
    "scope": "normal trading only; no zero, missing, pause or short-history recovery",
    "vendor_generation_rule_certified": False,
}


class ContractError(ValueError):
    """A missing or incompatible declaration must not become a numeric answer."""


def require(condition, reason):
    if not condition:
        raise ContractError(reason)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) and value != "0" * 64


def _named(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().lower() not in {"unknown", "unspecified", "latest", "none", "unversioned"}
    )


def _day(value):
    require(
        isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
        "CANONICAL_DATE_REQUIRED",
    )
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError("INVALID_DATE") from exc
    return value


def exact_decimal(value):
    require(isinstance(value, str), "EXACT_DECIMAL_TEXT_REQUIRED")
    try:
        parsed = Decimal(value)
        require(parsed.is_finite(), "NONFINITE_INPUT")
        return F(parsed)
    except (ValueError, ArithmeticError) as exc:
        raise ContractError("INVALID_DECIMAL_INPUT") from exc


def machine_price(value):
    require(isinstance(value, str), "BINARY64_HEX_REQUIRED")
    try:
        parsed = float.fromhex(value)
    except ValueError as exc:
        raise ContractError("INVALID_MACHINE_INPUT") from exc
    require(math.isfinite(parsed) and parsed.hex() == value, "CANONICAL_FINITE_BINARY64_REQUIRED")
    return F.from_float(parsed)


def quantize(value, places):
    return F(round(value * 10**places), 10**places)


def text(value):
    with localcontext() as context:
        context.prec = 60
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def point(value):
    return {
        "decimal": text(value),
        "exact_ratio": [value.numerator, value.denominator],
        "binary64_hex": float(value).hex(),
    }


def payload(bundle):
    return {key: bundle[key] for key in ("schema", "security", "sessions", "rows", "factors")}


def validate(bundle, *, mode):
    require(
        mode in {"independent_source", "reference_semantics"}, "EXPLICIT_RESEARCH_MODE_REQUIRED"
    )
    require(
        set(bundle) == {"schema", "security", "sessions", "rows", "factors", "contract"},
        "EXACT_BUNDLE_SCHEMA_REQUIRED",
    )
    require(bundle["schema"] == BUNDLE_SCHEMA, "WRONG_BUNDLE_VERSION")
    require(
        isinstance(bundle["security"], str)
        and re.fullmatch(r"\d{6}\.X(?:SHG|SHE)", bundle["security"]),
        "INVALID_SECURITY",
    )
    c = bundle["contract"]
    required = {
        "schema",
        "provider",
        "source_role",
        "snapshot_id",
        "snapshot_version",
        "snapshot_sha256",
        "basis_id",
        "anchor_version",
        "anchor_kind",
        "construction",
        "input_precision",
        "stored_price_unit",
        "stored_adjustment",
        "price_unit",
        "currency",
        "adjustment",
        "calendar_id",
        "calendar_sha256",
        "normal_state_sha256",
        "timezone",
        "session_policy",
        "generation_status",
        "quantity_relationship",
        "evidence_sha256",
        "payload_sha256",
    }
    require(
        set(c) == required and c["schema"] == CONTRACT_SCHEMA,
        "EXACT_SOURCE_BASIS_CONTRACT_REQUIRED",
    )
    require(
        c["currency"] == "CNY"
        and c["price_unit"] == "CNY_per_post_adjusted_share"
        and c["adjustment"] == "post",
        "POST_CNY_PRICE_BASIS_REQUIRED",
    )
    require(
        c["timezone"] == "Asia/Shanghai" and c["session_policy"] == "explicit_no_fill_no_skip",
        "EXPLICIT_LOCAL_SESSION_POLICY_REQUIRED",
    )
    require(
        c["generation_status"] == "supplier_native_snapshot_not_JQ_certified"
        and c["quantity_relationship"] == "not_asserted",
        "UNSUPPORTED_COMPATIBILITY_OR_QUANTITY_CLAIM",
    )
    for key in ["provider", "snapshot_id", "snapshot_version", "basis_id", "calendar_id"]:
        require(_named(c[key]), "NAMED_VERSIONED_BASIS_REQUIRED:" + key)
    for key in [
        "snapshot_sha256",
        "anchor_version",
        "calendar_sha256",
        "normal_state_sha256",
        "payload_sha256",
    ]:
        require(_sha(c[key]), "SHA256_REQUIRED:" + key)
    require(
        c["snapshot_version"] == "sha256:" + c["snapshot_sha256"]
        and c["snapshot_id"] == c["provider"] + ":" + c["snapshot_sha256"],
        "SNAPSHOT_IDENTITY_MISMATCH",
    )
    require(
        isinstance(c["evidence_sha256"], list)
        and c["evidence_sha256"]
        and len(set(c["evidence_sha256"])) == len(c["evidence_sha256"])
        and all(_sha(v) for v in c["evidence_sha256"]),
        "EVIDENCE_HASHES_REQUIRED",
    )
    require(
        {
            c[key]
            for key in [
                "snapshot_sha256",
                "anchor_version",
                "calendar_sha256",
                "normal_state_sha256",
            ]
        }
        <= set(c["evidence_sha256"]),
        "BASIS_EVIDENCE_NOT_BOUND",
    )
    require(c["payload_sha256"] == digest(payload(bundle)), "PAYLOAD_CHANGED")
    if mode == "independent_source":
        require(
            c["source_role"] == "independent"
            and c["provider"] == "Sina"
            and c["construction"] == "raw_times_native_hfq"
            and c["stored_price_unit"] == "CNY_per_share"
            and c["stored_adjustment"] == "none"
            and c["anchor_kind"] == "source_factor_table_sha256"
            and c["input_precision"] == "source_decimal_no_added_rounding",
            "INDEPENDENT_NATIVE_SOURCE_REQUIRED",
        )
    else:
        require(
            c["source_role"] == "reference"
            and c["provider"] == "JQ_public_price"
            and c["construction"] == "materialized_post"
            and c["stored_price_unit"] == "CNY_per_post_adjusted_share"
            and c["stored_adjustment"] == "post"
            and c["anchor_kind"] == "opaque_post_price_snapshot_sha256"
            and c["input_precision"] == "binary64_api_post_round_false",
            "EXPLICIT_JQ_REFERENCE_INPUT_REQUIRED",
        )
    sessions = [_day(d) for d in bundle["sessions"]]
    require(sessions and sessions == sorted(set(sessions)), "ORDERED_UNIQUE_SESSIONS_REQUIRED")
    rows = bundle["rows"]
    labels = [_day(r["date"]) for r in rows]
    require(labels == sorted(set(labels)) and set(labels) <= set(sessions), "INVALID_ROW_GRID")
    for row in rows:
        require(
            set(row) == {"date", "source_ref", "basis_ref", "trading_state", "prices"},
            "EXACT_ROW_SCHEMA_REQUIRED",
        )
        require(
            row["source_ref"] == c["snapshot_id"] and row["basis_ref"] == c["basis_id"],
            "MIXED_SOURCE_OR_BASIS",
        )
        require(
            set(row["prices"]) in ({"close"}, {"close", "high", "low"}),
            "EXPLICIT_HLC_FIELDS_REQUIRED",
        )
    factors = bundle["factors"]
    if c["construction"] == "materialized_post":
        require(factors == [], "MATERIALIZED_PRICE_MUST_NOT_BE_ADJUSTED_AGAIN")
    else:
        require(isinstance(factors, list) and factors, "NATIVE_FACTOR_SERIES_REQUIRED")
        fdates = [_day(f["date"]) for f in factors]
        require(fdates == sorted(set(fdates)), "ORDERED_UNIQUE_FACTOR_DATES_REQUIRED")
        for factor in factors:
            require(
                set(factor) == {"date", "value", "basis_ref"}
                and factor["basis_ref"] == c["basis_id"],
                "FACTOR_BASIS_MISMATCH",
            )
            require(exact_decimal(factor["value"]) > 0, "POSITIVE_FACTOR_REQUIRED")
    return c, sessions, dict(zip(labels, rows, strict=True))


def evaluate(bundle, *, definition, mode, end_date, fields):
    """Evaluate one fixed rule with an explicit source role; never pick a provider."""
    require(definition == DEFINITION, "VERSIONED_RESEARCH_DEFINITION_REQUIRED")
    require(
        isinstance(fields, (list, tuple))
        and fields
        and len(set(fields)) == len(fields)
        and set(fields) <= set(FIELDS),
        "EXPLICIT_SUPPORTED_FIELDS_REQUIRED",
    )
    c, sessions, by_day = validate(bundle, mode=mode)
    _day(end_date)
    require(end_date in sessions, "OUTPUT_SESSION_NOT_IN_CALENDAR")
    end = sessions.index(end_date)
    need = max(MINIMUM[field] for field in fields)
    require(end + 1 >= need, "HISTORY_INSUFFICIENT")
    dates = sessions[end + 1 - need : end + 1]
    source_prices, prices = {}, {}
    decoder = exact_decimal if mode == "independent_source" else machine_price
    for day in dates:
        require(day in by_day, "SOURCE_SESSION_MISSING")
        row = by_day[day]
        require(row["trading_state"] == "observed_trade", "NORMAL_TRADING_ONLY")
        factor = F(1)
        if c["construction"] == "raw_times_native_hfq":
            available = [f for f in bundle["factors"] if f["date"] <= day]
            require(available, "PRIOR_EFFECTIVE_FACTOR_MISSING")
            factor = exact_decimal(available[-1]["value"])
        for field, encoded in row["prices"].items():
            value = decoder(encoded) * factor
            require(value > 0, "POSITIVE_PRICE_REQUIRED")
            source_prices[day, field] = value
            rounded = quantize(value, 2)
            require(rounded > 0, "Q2_PRICE_BECAME_ZERO")
            prices[day, field] = rounded
        if "high" in row["prices"]:
            for grid in [source_prices, prices]:
                require(
                    grid[day, "low"] <= grid[day, "close"] <= grid[day, "high"], "INCONSISTENT_HLC"
                )
    answers = []
    for field in fields:
        window = dates[-MINIMUM[field] :]
        closes = [prices[d, "close"] for d in window]
        seed = None
        if field == "Variance20":
            returns = [b / a - 1 for a, b in zip(closes[:-1], closes[1:], strict=True)]
            mean = sum(returns, F()) / 20
            value = sum(((r - mean) ** 2 for r in returns), F()) * F(250, 19)
            unit = "ratio_squared_per_assumed_250_session_year"
        elif field.startswith("ATR"):
            tr = []
            for previous, day in zip(window[:-1], window[1:], strict=True):
                require(
                    (day, "high") in prices and (day, "low") in prices, "REQUIRED_HIGH_LOW_MISSING"
                )
                h, low, prev = prices[day, "high"], prices[day, "low"], prices[previous, "close"]
                tr.append(max(h - low, abs(h - prev), abs(low - prev)))
            value, unit = sum(tr, F()) / len(tr), "source_basis_post_adjusted_CNY"
        else:
            state = closes[0]
            for close in closes[1:]:
                state = (state * 2 + close) / 3
            value, unit = state / closes[-1], "ratio"
            # Bound only the two registered local initializations, not an arbitrary vendor seed.
            alternate = sum(closes[:5], F()) / 5
            for close in closes[5:]:
                alternate = (alternate * 2 + close) / 3
            alternate /= closes[-1]
            width = (max(closes[:5]) - min(closes[:5])) * F(2, 3) ** 55 / closes[-1]
            seed = {
                "history_start": window[0],
                "bars": 60,
                "seed": "first_close",
                "alternative_first5_mean_value": point(alternate),
                "local_seed_width": text(width),
                "local_seeds_same_Q6": quantize(value, 6) == quantize(alternate, 6),
                "unknown_vendor_seed_covered": False,
            }
        answers.append(
            {
                "field": field,
                "unit": unit,
                "window_dates": window,
                "pre_output": point(value),
                "value": point(quantize(value, 6)),
                "seed": seed,
            }
        )
    result = {
        "schema": "ad.pv.price-semantics-result.v1",
        "definition": definition,
        "rule": RULE,
        "security": bundle["security"],
        "date": end_date,
        "mode": mode,
        "input_contract_sha256": digest(c),
        "input_payload_sha256": c["payload_sha256"],
        "provider": c["provider"],
        "snapshot_version": c["snapshot_version"],
        "basis_id": c["basis_id"],
        "anchor_version": c["anchor_version"],
        "rows": answers,
        "semantics_status": "computed_under_fixed_candidate",
        "comparison_scope": "given_JQ_public_prices_only"
        if mode == "reference_semantics"
        else "native_source_research",
        "source_compatibility": {"status": "blocked", "issue": SOURCE_ISSUE},
        "jq_equivalent": False,
        "strategy_visible": False,
        "production_eligible": False,
    }
    return result


def compare_labels(result, *, labels, security, end_date, label_snapshot_sha256):
    """A bit match is always scoped to the input role; it cannot grant source parity."""
    require(
        result["schema"] == "ad.pv.price-semantics-result.v1"
        and result["definition"] == DEFINITION,
        "FIXED_CANDIDATE_RESULT_REQUIRED",
    )
    require(
        result["mode"] in {"independent_source", "reference_semantics"},
        "EXPLICIT_RESEARCH_MODE_REQUIRED",
    )
    require(result["security"] == security and result["date"] == end_date, "LABEL_TARGET_MISMATCH")
    require(_sha(label_snapshot_sha256), "LABEL_EVIDENCE_HASH_REQUIRED")
    require(set(labels) == {r["field"] for r in result["rows"]}, "LABEL_FIELDS_DIFFER")
    comparisons = []
    for row in result["rows"]:
        observed = machine_price(labels[row["field"]])
        predicted = F(*row["value"]["exact_ratio"])
        comparisons.append(
            {
                "field": row["field"],
                "prediction": row["value"],
                "observed": point(observed),
                "target_minus_prediction": text(observed - predicted),
                "binary64_match": float(predicted).hex() == float(observed).hex(),
            }
        )
    return {
        "mode": result["mode"],
        "result_sha256": digest(result),
        "label_snapshot_sha256": label_snapshot_sha256,
        "comparison_scope": result["comparison_scope"],
        "comparisons": comparisons,
        "matched": sum(r["binary64_match"] for r in comparisons),
        "total": len(comparisons),
        "source_compatibility": {"status": "blocked", "issue": SOURCE_ISSUE},
        "general_vendor_semantics_certified": False,
        "independent_JQ_parity_certified": False,
        "production_eligible": False,
    }
