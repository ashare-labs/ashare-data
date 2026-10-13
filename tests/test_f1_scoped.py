"""Synthetic integration tests; none of these amounts are issuer observations."""

from copy import deepcopy
from fractions import Fraction
import json
import math
from pathlib import Path
import struct

import pytest

from ashare_data import f1
from ashare_data.f1_confirmation import observe_frozen_confirmation
from ashare_data.f1_followup import compare_confirmation, digest, freeze_confirmation
from ashare_data.f1_precision import six_decimal_profiles


@pytest.fixture
def financial_pack():
    reports, rows = {}, []
    for year in range(2021, 2025):
        for quarter, end in enumerate(("03-31", "06-30", "09-30", "12-31"), 1):
            if year == 2024 and quarter > 1:
                continue
            period = f"{year}-{end}"
            published = (
                f"{year + 1}-04-23"
                if quarter == 4
                else f"{year}-" + {1: "04-23", 2: "08-22", 3: "10-27"}[quarter]
            )
            reports[period] = {
                "published_date": published,
                "publication_precision": "date_only_in_current_issuer_index",
                "sha256": "a" * 64,
            }
            for field, amount in {
                "net_profit": 6_000_008 if year == 2024 else 5_000_008,
                "total_operating_revenue": 10_000_000,
                "net_operate_cash_flow": 5_000_004 if year >= 2023 else 5_000_000,
            }.items():
                rows.append(
                    {
                        "record_id": period + ":" + field,
                        "report_id": period,
                        "security": "000001.XSHE",
                        "field": field,
                        "basis": "YTD",
                        "scope": "consolidated",
                        "currency": "CNY",
                        "unit": "CNY",
                        "period_start": f"{year}-01-01",
                        "period_end": period,
                        "page": 1,
                        "value": str(amount * quarter),
                        "replaces_in_this_pack": None,
                    }
                )
    return {"schema": "ad.f1-small-pack.v1", "reports": reports, "observations": rows}


def candidate(pack, slot="FI01", day="2024-04-11"):
    return f1.financial_factor_candidate(
        f1.FinancialLedger(pack),
        slot,
        "000001.XSHE",
        day + "T15:00:00+08:00",
        mode=f1.RESEARCH_MODE,
    )


def test_public_financial_chain_is_exact_and_unquantized(financial_pack):
    row = candidate(financial_pack)
    assert Fraction(row["value"]) == Fraction(5_000_008, 10_000_000)
    assert row["period_end"] == "2023-09-30"
    assert len(row["components"]["net_profit"]["quarters"]) == 4
    assert not row["jq_equivalent"] and not row["historical_pit_certified"]
    growth = candidate(financial_pack, "FI04")
    assert Fraction(growth["value"]) == Fraction(12, 20_000_000)
    assert len(growth["prior_ttm"]["quarters"]) == 4


@pytest.mark.parametrize(
    "slot",
    ["FI02", "FI03", "SV01", "SV02", "ROC120", "Variance20", "ATR6", "ATR14", "EMA5", "PV05"],
)
def test_unreviewed_slots_are_unsupported(financial_pack, slot):
    with pytest.raises(f1.AcceptanceError, match="unsupported"):
        candidate(financial_pack, slot)
    assert not hasattr(f1, slot)


def test_future_input_change_and_publication_boundary(financial_pack):
    before = candidate(financial_pack)
    for row in financial_pack["observations"]:
        if row["period_end"] in {"2023-12-31", "2024-03-31"}:
            row["value"] = "999999999"
    assert candidate(financial_pack) == before
    ledger = f1.FinancialLedger(financial_pack)
    assert (
        ledger.latest_period(
            "000001.XSHE", "net_profit", as_of="2024-04-23T23:59:59+08:00", mode=f1.RESEARCH_MODE
        )
        == "2023-09-30"
    )
    assert (
        ledger.latest_period(
            "000001.XSHE", "net_profit", as_of="2024-04-24T00:00:00+08:00", mode=f1.RESEARCH_MODE
        )
        == "2024-03-31"
    )
    with pytest.raises(f1.VisibilityUnproven):
        f1.financial_factor_candidate(
            ledger, "FI01", "000001.XSHE", "2024-04-11T15:00:00+08:00", mode="strict"
        )


def test_missing_quarter_is_not_shortened_to_annual_bridge(financial_pack):
    financial_pack["observations"] = [
        r
        for r in financial_pack["observations"]
        if not (r["field"] == "net_profit" and r["period_end"] == "2023-06-30")
    ]
    with pytest.raises(f1.FinancialInputUnavailable):
        candidate(financial_pack)


@pytest.mark.parametrize("base", ["0", "-100"])
def test_nonpositive_base_is_not_a_confirmation(financial_pack, base):
    for row in financial_pack["observations"]:
        if row["field"] == "total_operating_revenue":
            row["value"] = str(int(base) * (int(row["period_end"][5:7]) // 3))
    with pytest.raises(f1.AcceptanceError):
        f1.confirmation_prediction(
            f1.FinancialLedger(financial_pack),
            {"security": "000001.XSHE", "slot": "FI01", "date": "2024-04-11"},
            [],
        )


@pytest.mark.parametrize("value", [Fraction(1, 2_000_000), Fraction(-1, 2_000_000)])
def test_midpoint_policy_remains_unsupported(value):
    with pytest.raises(f1.AcceptanceError, match="midpoint"):
        six_decimal_profiles(value)


@pytest.fixture
def confirmation(financial_pack):
    cases = [
        {"security": "000001.XSHE", "date": day, "slot": slot}
        for day, slot in [("2024-04-11", "FI01"), ("2024-04-11", "FI04"), ("2024-04-29", "FI01")]
    ]
    sample = {
        "securities": ["000001.XSHE"],
        "windows": [["2024-04-01", "2024-04-30"]],
        "verified_sessions": ["2024-04-11", "2024-04-29"],
    }
    exposure = {
        "scope": "local_observed_FI_regimes_global_review_pending",
        "regimes": [],
        "keys": [],
    }
    artifacts = [
        {"role": role, "sha256": "a" * 64}
        for role in ("input", "source_provenance", "calendar", "candidate_code", "oracle_code")
    ]
    lock = freeze_confirmation(
        financial_pack, cases, sample, exposure, artifacts, "2026-10-11T00:00:00Z"
    )
    auth = {
        "lock_sha256": lock["sha256"],
        "reference_read_authorized": True,
        "max_values": 3,
        "independent_review_completed": False,
        "recorded_at": "2026-10-11T00:00:01Z",
    }
    records = [
        {
            "security": row["security"],
            "date": row["date"],
            "factor_id": row["factor_id"],
            "provider": "JoinQuant",
            "dtype": "float64",
            "missing_kind": "none",
            "value_17g": row["prediction"]["float64_17g"],
            "float64_bits_be": row["prediction"]["float64_bits_be"],
            "first_seen_at": "2026-10-11T00:00:02Z",
            "observed_at": "2026-10-11T00:00:03Z",
        }
        for row in lock["payload"]["rows"]
    ]
    return lock, auth, records


def test_observation_is_not_a_backdated_review_certificate(confirmation):
    lock, auth, records = confirmation
    result = observe_frozen_confirmation(lock, records, auth)
    assert result["passed_cells"] == 3
    assert not result["legacy_pre_read_review_certificate_issued"]
    assert not result["independent_review_completed"]
    assert not result["strict_PIT_certified"]
    review = {
        "lock_sha256": lock["sha256"],
        "cross_thread_unexposed": True,
        "input_provenance_independently_reviewed": True,
        "artifact_hashes_verified": True,
        "reviewer": "synthetic",
        "evidence_sha256": "a" * 64,
        "reviewed_at": "2026-10-11T00:00:04Z",
    }
    with pytest.raises(f1.AcceptanceError, match="before review"):
        compare_confirmation(lock, records, review)


def test_one_ulp_and_missing_are_failures(confirmation):
    lock, auth, records = deepcopy(confirmation)
    value = math.nextafter(float(records[0]["value_17g"]), math.inf)
    records[0].update(
        value_17g=format(value, ".17g"), float64_bits_be=struct.pack(">d", value).hex()
    )
    records[1].update(missing_kind="NaN", value_17g=None, float64_bits_be=None)
    result = observe_frozen_confirmation(lock, records, auth)
    assert result["passed_cells"] == 1
    assert result["status"] == "fixed_sample_numeric_failure"


@pytest.mark.parametrize("damage", ["key", "order", "early", "missing", "tolerance"])
def test_confirmation_protocol_fails_closed(confirmation, damage):
    lock, auth, records = deepcopy(confirmation)
    if damage == "key":
        records[0]["date"] = "2024-04-12"
    elif damage == "order":
        records.reverse()
    elif damage == "early":
        records[0]["first_seen_at"] = lock["payload"]["frozen_at"]
    elif damage == "missing":
        records.pop()
    else:
        lock["payload"]["tolerance"]["float64_ULP"] = 1
        lock["sha256"] = digest(lock["payload"])
        auth["lock_sha256"] = lock["sha256"]
    with pytest.raises(f1.AcceptanceError):
        observe_frozen_confirmation(lock, records, auth)


def test_documented_registry_keeps_scope_and_failure_distinct():
    registry = json.loads((Path(__file__).parents[1] / "docs/f1-integration.json").read_text())
    assert registry["financial"]["confirmed_cells"] == 3
    assert registry["tvma6"]["composite_model_compatible"] == 4
    assert registry["tvma6"]["original_strict_passed"] == 0
    assert registry["pv05"]["status"] == "failed_not_integrated"
    assert registry["full_original_twelve_factor_certifications"] == 0
