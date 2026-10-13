"""Synthetic regression inputs only; none are external provider observations."""

from copy import deepcopy
from fractions import Fraction
import math
import struct

import pytest

from ashare_data.f1_acceptance import AcceptanceError
from ashare_data.f1_financial import (
    FinancialLedger,
    InputUnavailable,
    RESEARCH_MODE,
    VisibilityUnproven,
)
from ashare_data.f1_fi_sv_scoped import (
    SHARE_MODE,
    fi02_lagged_candidate,
    observe_fi_sv_batch,
    sv_stage_candidate,
)
from ashare_data.f1_followup import digest


@pytest.fixture
def pack():
    reports, observations = {}, []
    for year in (2021, 2022, 2023, 2024):
        for quarter, suffix in enumerate(("03-31", "06-30", "09-30", "12-31"), 1):
            if year == 2024 and quarter > 1:
                continue
            period = f"{year}-{suffix}"
            publication = (
                f"{year + 1}-04-20"
                if quarter == 4
                else f"{year}-" + {1: "04-20", 2: "08-20", 3: "10-20"}[quarter]
            )
            reports[period] = {
                "published_date": publication,
                "publication_precision": "date_only_in_current_issuer_index",
                "sha256": "a" * 64,
            }
            observations.append(
                {
                    "record_id": period,
                    "report_id": period,
                    "security": "000001.XSHE",
                    "field": "net_profit",
                    "basis": "YTD",
                    "scope": "consolidated",
                    "currency": "CNY",
                    "unit": "CNY",
                    "period_start": f"{year}-01-01",
                    "period_end": period,
                    "page": 1,
                    "value": str((year - 2020) * 1000 * quarter),
                    "replaces_in_this_pack": None,
                }
            )
    return {"schema": "ad.f1-small-pack.v1", "reports": reports, "observations": observations}


def fi(pack, day="2024-04-29", mode=RESEARCH_MODE):
    return fi02_lagged_candidate(FinancialLedger(pack), "000001.XSHE", day, mode=mode)


def test_fi02_complete_quarters_and_exact_cent_oracle(pack):
    row = fi(pack)
    assert row["exact_ratio"] == {"numerator": 4, "denominator": 9}
    assert row["prediction"]["decimal"] == "0.444444"
    assert row["current_ttm"]["value"] == "13000"
    assert row["prior_ttm"]["value"] == "9000"
    assert len(row["current_ttm"]["quarters"]) == len(row["prior_ttm"]["quarters"]) == 4
    assert not row["vendor_version_policy_proven"] and not row["jq_equivalent"]
    assert not row["distinguishes_truncation"]


def test_new_current_cutoff_restatement_does_not_replace_prior_vintage(pack):
    before = fi(pack)
    old = next(r for r in pack["observations"] if r["period_end"] == "2022-12-31")
    correction = {
        **old,
        "record_id": "correction",
        "report_id": "correction",
        "value": "8001",
        "replaces_in_this_pack": old["record_id"],
    }
    pack["reports"]["correction"] = {"published_date": "2024-04-25", "sha256": "b" * 64}
    pack["observations"].append(correction)
    assert fi(pack)["prediction"] == before["prediction"]
    selected = FinancialLedger(pack).select(
        "000001.XSHE",
        "net_profit",
        "2022-12-31",
        as_of="2024-04-29T15:00:00+08:00",
        mode=RESEARCH_MODE,
    )
    assert selected["record_id"] == "correction"


def test_late_current_report_cannot_leak_before_release(pack):
    before = fi(pack, "2024-04-19")
    for row in pack["observations"]:
        if row["period_end"] in {"2023-12-31", "2024-03-31"}:
            row["value"] = "999999999"
    assert fi(pack, "2024-04-19") == before


def test_missing_old_quarter_rejected_even_when_annual_bridge_available(pack):
    pack["observations"] = [r for r in pack["observations"] if r["period_end"] != "2022-06-30"]
    with pytest.raises(InputUnavailable):
        fi(pack)


@pytest.mark.parametrize("base", ["0", "-100"])
def test_fi02_nonpositive_prior_denominator_rejected(pack, base):
    for r in pack["observations"]:
        if r["period_end"] <= "2023-03-31":
            r["value"] = str(int(base) * (int(r["period_end"][5:7]) // 3))
    with pytest.raises(AcceptanceError, match="positive prior"):
        fi(pack)


def test_subcent_intermediate_quarter_is_not_hidden_by_ttm_telescoping(pack):
    next(r for r in pack["observations"] if r["period_end"] == "2023-06-30")["value"] = "6000.001"
    with pytest.raises(AcceptanceError, match="exact CNY cents"):
        fi(pack)


def test_leap_day_uses_calendar_year_and_strict_mode_not_silently_downgraded(pack):
    assert fi(pack, "2024-02-29")["prior_as_of"] == "2023-02-28T15:00:00+08:00"
    with pytest.raises(AcceptanceError, match="explicit"):
        fi(pack, mode="strict")


@pytest.fixture
def sv():
    sessions = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    market = {
        "security": "000001.XSHE",
        "price_adjustment": "none",
        "price_unit": "CNY/share",
        "volume_unit": "shares",
        "rows": [
            {"date": d, "volume": str(11230 + 101 * i), "close": "12.34567"}
            for i, d in enumerate(sessions)
        ],
    }
    shares = [
        {
            "date": d,
            "security": market["security"],
            "unit": "shares",
            "mode": SHARE_MODE,
            "as_of": d + "T09:00:00+08:00",
            "value": "9000000",
        }
        for d in sessions
    ]
    return market, sessions, shares


def sv_value(sv, day="2024-01-08", mode=SHARE_MODE):
    return sv_stage_candidate(*sv, day, mode=mode)


def test_sv_daily_percent_before_five_session_average(sv):
    result = sv_value(sv)
    # Independent integer arithmetic: nearest four decimals of percent is volume/9.
    units = [(11230 + 101 * i + 4) // 9 for i in range(5)]
    expected = Fraction(sum(units), 50000)
    assert result["SV01"]["prediction"]["float64_17g"] == format(float(expected), ".17g")
    assert result["SV02"]["raw_cap4_decimal"] == "1.1111"
    assert (
        result["SV02"]["prediction"]["float64_bits_be"]
        == struct.pack(">d", float("1.1111") * 1e8).hex()
    )
    assert result["SV02"]["candidate_status"].startswith("failed_round14")
    assert not result["jq_equivalent"] and not result["source_authentication_performed"]


@pytest.mark.parametrize(
    "change",
    [
        "volume_unit",
        "price_unit",
        "price_adjustment",
        "denominator_unit",
        "denominator_security",
        "denominator_cutoff",
        "denominator_mode",
    ],
)
def test_sv_rejects_wrong_unit_identity_or_asof(sv, change):
    if change.startswith("denominator"):
        field = {
            "denominator_unit": "unit",
            "denominator_security": "security",
            "denominator_cutoff": "as_of",
            "denominator_mode": "mode",
        }[change]
        sv[2][0][field] = "incorrect"
    else:
        sv[0][change] = "incorrect"
    with pytest.raises(AcceptanceError):
        sv_value(sv)


@pytest.mark.parametrize(
    "kind",
    [
        "missing_bar",
        "missing_shares",
        "duplicate_bar",
        "duplicate_shares",
        "unsorted_calendar",
        "warmup",
        "zero_volume",
        "fractional_volume",
        "negative_close",
        "zero_denominator",
    ],
)
def test_sv_boundary_inputs_fail_closed(sv, kind):
    day = "2024-01-08"
    if kind == "missing_bar":
        sv[0]["rows"].pop(1)
    elif kind == "missing_shares":
        sv[2].pop(1)
    elif kind == "duplicate_bar":
        sv[0]["rows"].append(deepcopy(sv[0]["rows"][0]))
    elif kind == "duplicate_shares":
        sv[2].append(deepcopy(sv[2][0]))
    elif kind == "unsorted_calendar":
        sv[1].reverse()
    elif kind == "warmup":
        day = sv[1][3]
    elif kind == "zero_volume":
        sv[0]["rows"][0]["volume"] = "0"
    elif kind == "fractional_volume":
        sv[0]["rows"][0]["volume"] = "1.2"
    elif kind == "negative_close":
        sv[0]["rows"][-1]["close"] = "-1"
    elif kind == "zero_denominator":
        sv[2][0]["value"] = "0"
    with pytest.raises(AcceptanceError):
        sv_value(sv, day)


def test_sv_midpoint_and_strict_registry_policy_remain_unsupported(sv):
    sv[0]["rows"][0]["volume"] = "1"
    sv[2][0]["value"] = "2000000"
    with pytest.raises(AcceptanceError, match="midpoint"):
        sv_value(sv)
    with pytest.raises(VisibilityUnproven):
        sv_value(sv, mode="strict")


def synthetic_batch():
    prediction = {"float64_17g": "1", "float64_bits_be": struct.pack(">d", 1.0).hex()}
    expected = [
        {
            "api": "synthetic_fixture",
            "security": "000001.XSHE",
            "date": "2024-01-08",
            "field": str(i),
            "role": "named_confirmation" if i < 3 else "raw_stage_diagnostic",
            "prediction": prediction,
        }
        for i in range(11)
    ]
    payload = {
        "schema": "ad.f1-fi-sv-batch-lock.v1",
        "frozen_at": "2026-01-01T00:00:00Z",
        "tolerance": {"float64_ULP": 0, "atol": "0", "rtol": "0"},
        "max_calls": 4,
        "max_values": 11,
        "rows": expected,
    }
    lock = {"payload": payload, "sha256": digest(payload)}
    auth = {
        "frozen_lock_sha256": lock["sha256"],
        "reference_read_authorized": True,
        "recorded_at": "2026-01-01T01:00:00Z",
        "max_calls": 4,
        "max_values": 11,
    }
    records = [
        {
            **{k: r[k] for k in ("api", "security", "date", "field")},
            "first_seen_at": "2026-01-01T02:00:00Z",
            "provider": "JoinQuant",
            "dtype": "float64",
            "missing_kind": "none",
            "value_17g": "1",
            "float64_bits_be": prediction["float64_bits_be"],
        }
        for r in expected
    ]
    return lock, records, auth


def test_observation_preserves_one_ulp_failure_without_changing_tolerance():
    lock, records, auth = synthetic_batch()
    value = math.nextafter(1.0, 0.0)
    records[1].update(
        value_17g=format(value, ".17g"), float64_bits_be=struct.pack(">d", value).hex()
    )
    result = observe_fi_sv_batch(lock, records, auth)
    assert (
        result["passed_cells"] == 10
        and result["named_passed_cells"] == 2
        and result["raw_passed_cells"] == 8
    )
    assert result["rows"][1]["actual_minus_expected_exact"] == "-1/9007199254740992"
    assert not result["finite_numeric_batch_passed"] and not result["independent_review_completed"]
    assert not result["another_holdout_required_for_passed_exact_keys"]


@pytest.mark.parametrize(
    "bad",
    [
        "key",
        "order",
        "dtype",
        "mask",
        "transport",
        "time",
        "count",
        "authorization",
        "lock",
        "role",
    ],
)
def test_batch_transport_and_authorization_guards(bad):
    lock, records, auth = synthetic_batch()
    if bad == "key":
        records[0]["security"] = "different"
    elif bad == "order":
        records.reverse()
    elif bad == "dtype":
        records[0]["dtype"] = "float32"
    elif bad == "mask":
        records[0]["missing_kind"] = "filled_zero"
    elif bad == "transport":
        records[0]["float64_bits_be"] = "0" * 16
    elif bad == "time":
        records[0]["first_seen_at"] = lock["payload"]["frozen_at"]
    elif bad == "count":
        records.pop()
    elif bad == "authorization":
        auth["reference_read_authorized"] = False
    elif bad == "lock":
        lock["sha256"] = "0" * 64
    elif bad == "role":
        lock["payload"]["rows"][0]["role"] = "raw_stage_diagnostic"
        lock["sha256"] = digest(lock["payload"])
        auth["frozen_lock_sha256"] = lock["sha256"]
    with pytest.raises(AcceptanceError):
        observe_fi_sv_batch(lock, records, auth)


def test_nan_keeps_mask_and_cannot_become_pass():
    lock, records, auth = synthetic_batch()
    records[0].update(missing_kind="NaN", value_17g=None, float64_bits_be=None)
    result = observe_fi_sv_batch(lock, records, auth)
    assert result["passed_cells"] == 10 and not result["rows"][0]["zero_ULP_pass"]
