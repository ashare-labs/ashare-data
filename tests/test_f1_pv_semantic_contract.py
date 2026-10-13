"""Synthetic contract tests; no observation, label, credential or network access."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction as F

import pytest

from ashare_data.f1_pv_semantic_contract import (
    BUNDLE_SCHEMA, CONTRACT_SCHEMA, DEFINITION, FIELDS, ContractError,
    compare_labels, digest, evaluate, payload, quantize,
)


def fixture_bundle():
    days = [(date(2000, 1, 1) + timedelta(days=k)).isoformat() for k in range(60)]
    snapshot, basis = "Sina:" + "a"*64, "synthetic-basis-v1"
    bundle = {"schema": BUNDLE_SCHEMA, "security": "000002.XSHE", "sessions": days,
              "rows": [{"date": d, "source_ref": snapshot, "basis_ref": basis,
                        "trading_state": "observed_trade",
                        "prices": {"close": str(Decimal(10) + Decimal((k*7) % 13)/100),
                                   "high": "10.23", "low": "9.94"}}
                       for k, d in enumerate(days)],
              "factors": [{"date": days[0], "value": "1.123456", "basis_ref": basis}]}
    bundle["contract"] = {
        "schema": CONTRACT_SCHEMA, "provider": "Sina", "source_role": "independent",
        "snapshot_id": snapshot, "snapshot_version": "sha256:" + "a"*64,
        "snapshot_sha256": "a"*64, "basis_id": basis, "anchor_version": "b"*64,
        "anchor_kind": "source_factor_table_sha256", "construction": "raw_times_native_hfq",
        "input_precision": "source_decimal_no_added_rounding", "stored_price_unit": "CNY_per_share",
        "stored_adjustment": "none", "price_unit": "CNY_per_post_adjusted_share", "currency": "CNY",
        "adjustment": "post", "calendar_id": "synthetic-only-calendar-v1",
        "calendar_sha256": "c"*64, "normal_state_sha256": "d"*64, "timezone": "Asia/Shanghai",
        "session_policy": "explicit_no_fill_no_skip", "generation_status": "supplier_native_snapshot_not_JQ_certified",
        "quantity_relationship": "not_asserted", "evidence_sha256": [v*64 for v in "abcd"],
        "payload_sha256": digest(payload(bundle))}
    return bundle


def seal(bundle):
    bundle["contract"]["payload_sha256"] = digest(payload(bundle))
    return bundle


def run(bundle, **kwargs):
    params = dict(definition=DEFINITION, mode="independent_source", end_date=bundle["sessions"][-1], fields=list(FIELDS))
    params.update(kwargs)
    return evaluate(bundle, **params)


def exact(row, field="value"):
    return F(*row[field]["exact_ratio"])


def test_decimal_variance_oracle_and_closed_form_ema():
    bundle = fixture_bundle()
    result = {r["field"]: r for r in run(bundle)["rows"]}
    with localcontext() as context:
        context.prec = 100
        closes = [(Decimal(r["prices"]["close"])*Decimal("1.123456")).quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN)
                  for r in bundle["rows"]]
        returns = [b/a-1 for a, b in zip(closes[-21:-1], closes[-20:], strict=True)]
        # Independent E[x²]-E[x]² identity, rather than the engine's centered squares.
        variance = (sum(r*r for r in returns)/20 - (sum(returns)/20)**2)*Decimal(20)/19*250
        beta = Decimal(2)/3
        ema = (closes[0]*beta**59 + sum(closes[k]*beta**(59-k)/3 for k in range(1, 60)))/closes[-1]
        for field, expected in [("Variance20", variance), ("EMA5", ema)]:
            assert exact(result[field]) == F(expected.quantize(Decimal(".000001"), rounding=ROUND_HALF_EVEN))
            assert abs(Decimal(result[field]["pre_output"]["decimal"])-expected) < Decimal("1e-58")
    assert result["EMA5"]["seed"]["unknown_vendor_seed_covered"] is False


def test_atr_uses_previous_trade_close_and_adjusts_before_rounding():
    bundle = fixture_bundle()
    bundle["factors"][0]["value"] = "2.5"
    for row in bundle["rows"]:
        row["prices"] = {"close": "10.005", "high": "10.009", "low": "10.001"}
    result = run(seal(bundle), fields=["ATR6"])["rows"][0]
    assert exact(result) == F(".02")  # Q2(25.0225)-Q2(25.0025), not raw rounding then scaling.
    bundle["rows"][-1]["prices"] = {"close": "11", "high": "11.1", "low": "10.9"}
    result = run(seal(bundle), fields=["ATR6"])["rows"][0]
    assert exact(result, "pre_output") == F("2.84")/6  # Five .02 ranges, then 27.75-25.01.


def test_declared_half_even_ties_are_hypothesis_not_vendor_evidence():
    assert quantize(F("1.005"), 2) == F(1)
    assert quantize(F("1.015"), 2) == F("1.02")
    assert quantize(F(".0000005"), 6) == 0
    assert quantize(F(".0000015"), 6) == F(".000002")
    assert run(fixture_bundle())["rule"]["vendor_generation_rule_certified"] is False


@pytest.mark.parametrize("key,value,reason", [
    ("provider", "unknown", "NAMED_VERSIONED_BASIS_REQUIRED"),
    ("snapshot_version", "latest", "NAMED_VERSIONED_BASIS_REQUIRED"),
    ("snapshot_version", "version1", "SNAPSHOT_IDENTITY_MISMATCH"),
    ("anchor_version", "unknown", "SHA256_REQUIRED"),
    ("anchor_kind", "opaque_post_price_snapshot_sha256", "INDEPENDENT_NATIVE_SOURCE_REQUIRED"),
    ("currency", "USD", "POST_CNY_PRICE_BASIS_REQUIRED"),
    ("stored_price_unit", "unknown", "INDEPENDENT_NATIVE_SOURCE_REQUIRED"),
    ("stored_adjustment", "post", "INDEPENDENT_NATIVE_SOURCE_REQUIRED"),
    ("adjustment", "pre", "POST_CNY_PRICE_BASIS_REQUIRED"),
    ("session_policy", "fill_previous", "EXPLICIT_LOCAL_SESSION_POLICY_REQUIRED"),
    ("quantity_relationship", "divide_volume_by_price_factor", "UNSUPPORTED_COMPATIBILITY_OR_QUANTITY_CLAIM"),
    ("evidence_sha256", ["a"*64], "BASIS_EVIDENCE_NOT_BOUND"),
])
def test_unknown_or_incompatible_contract_refused(key, value, reason):
    bundle = fixture_bundle()
    bundle["contract"][key] = value
    with pytest.raises(ContractError, match=reason):
        run(bundle)


@pytest.mark.parametrize("fault,reason", [
    ("tamper", "PAYLOAD_CHANGED"), ("missing", "SOURCE_SESSION_MISSING"),
    ("paused", "NORMAL_TRADING_ONLY"), ("mixed", "MIXED_SOURCE_OR_BASIS"),
    ("future_factor", "PRIOR_EFFECTIVE_FACTOR_MISSING"), ("zero_factor", "POSITIVE_FACTOR_REQUIRED"),
    ("duplicate", "INVALID_ROW_GRID"), ("negative", "POSITIVE_PRICE_REQUIRED"),
    ("nan", "INVALID_DECIMAL_INPUT"), ("bad_hlc", "INCONSISTENT_HLC"),
    ("missing_hl", "REQUIRED_HIGH_LOW_MISSING"), ("short", "HISTORY_INSUFFICIENT"),
])
def test_data_failures_never_fill_or_guess(fault, reason):
    bundle = fixture_bundle()
    if fault == "tamper":
        bundle["rows"][-1]["prices"]["close"] = "10.11"
    elif fault == "missing":
        bundle["rows"].pop(-2)
    elif fault == "paused":
        bundle["rows"][-2]["trading_state"] = "paused"
    elif fault == "mixed":
        bundle["rows"][-2]["basis_ref"] = "other-version"
    elif fault == "future_factor":
        bundle["factors"][0]["date"] = bundle["sessions"][1]
    elif fault == "zero_factor":
        bundle["factors"][0]["value"] = "0"
    elif fault == "duplicate":
        bundle["rows"].append(deepcopy(bundle["rows"][-1]))
    elif fault in {"negative", "nan", "bad_hlc"}:
        bundle["rows"][-1]["prices"]["close"] = {"negative": "-1", "nan": "NaN", "bad_hlc": "99"}[fault]
    elif fault == "missing_hl":
        bundle["rows"][-1]["prices"] = {"close": "10.1"}
    elif fault == "short":
        bundle["sessions"].pop(0)
        bundle["rows"].pop(0)
    if fault != "tamper":
        seal(bundle)
    with pytest.raises(ContractError, match=reason):
        run(bundle)


def test_mode_is_mandatory_and_reference_can_never_be_an_independent_default():
    bundle = fixture_bundle()
    with pytest.raises(TypeError, match="mode"):
        evaluate(bundle, definition=DEFINITION, end_date=bundle["sessions"][-1], fields=list(FIELDS))
    with pytest.raises(ContractError, match="EXPLICIT_JQ_REFERENCE_INPUT_REQUIRED"):
        run(bundle, mode="reference_semantics")
    with pytest.raises(ContractError, match="VERSIONED_RESEARCH_DEFINITION_REQUIRED"):
        run(bundle, definition="vendor-equivalent")


def test_even_all_matches_leave_source_contract_blocked_and_not_production():
    result = run(fixture_bundle())
    labels = {r["field"]: r["value"]["binary64_hex"] for r in result["rows"]}
    compared = compare_labels(result, labels=labels, security=result["security"],
                              end_date=result["date"], label_snapshot_sha256="e"*64)
    assert compared["matched"] == 4
    assert compared["comparison_scope"] == "native_source_research"
    assert compared["source_compatibility"] == {"status": "blocked", "issue": "ADJ-INPUT-001"}
    assert compared["independent_JQ_parity_certified"] is False
    assert compared["production_eligible"] is False
