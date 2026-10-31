"""Synthetic integration tests; none of these amounts are issuer observations."""

from fractions import Fraction

import pytest

from ashare_data.factors import joinquant as joinquant
from ashare_data.factors.joinquant.precision import six_decimal_profiles


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
    return {"schema": "ad.financial-ledger.v1", "reports": reports, "observations": rows}


def candidate(pack, slot="net_profit_to_total_operate_revenue_ttm", day="2024-04-11"):
    return joinquant.financial_factor_candidate(
        joinquant.FinancialLedger(pack),
        slot,
        "000001.XSHE",
        day + "T15:00:00+08:00",
        mode=joinquant.RESEARCH_MODE,
    )


def test_public_financial_chain_is_exact_and_unquantized(financial_pack):
    row = candidate(financial_pack)
    assert Fraction(row["value"]) == Fraction(5_000_008, 10_000_000)
    assert row["period_end"] == "2023-09-30"
    assert len(row["components"]["net_profit"]["quarters"]) == 4
    assert not row["jq_equivalent"] and not row["historical_pit_certified"]
    growth = candidate(financial_pack, "net_operate_cashflow_growth_rate")
    assert Fraction(growth["value"]) == Fraction(12, 20_000_000)
    assert len(growth["prior_ttm"]["quarters"]) == 4


@pytest.mark.parametrize(
    "slot",
    [
        "net_profit_growth_rate",
        "operating_revenue_ttm",
        "VOL5",
        "circulating_market_cap",
        "ROC120",
        "Variance20",
        "ATR6",
        "ATR14",
        "EMA5",
        "VROC6",
    ],
)
def test_unreviewed_slots_are_unsupported(financial_pack, slot):
    with pytest.raises(joinquant.AcceptanceError, match="unsupported"):
        candidate(financial_pack, slot)
    assert not hasattr(joinquant, slot)


def test_future_input_change_and_publication_boundary(financial_pack):
    before = candidate(financial_pack)
    for row in financial_pack["observations"]:
        if row["period_end"] in {"2023-12-31", "2024-03-31"}:
            row["value"] = "999999999"
    assert candidate(financial_pack) == before
    ledger = joinquant.FinancialLedger(financial_pack)
    assert (
        ledger.latest_period(
            "000001.XSHE",
            "net_profit",
            as_of="2024-04-23T23:59:59+08:00",
            mode=joinquant.RESEARCH_MODE,
        )
        == "2023-09-30"
    )
    assert (
        ledger.latest_period(
            "000001.XSHE",
            "net_profit",
            as_of="2024-04-24T00:00:00+08:00",
            mode=joinquant.RESEARCH_MODE,
        )
        == "2024-03-31"
    )
    with pytest.raises(joinquant.VisibilityUnproven):
        joinquant.financial_factor_candidate(
            ledger,
            "net_profit_to_total_operate_revenue_ttm",
            "000001.XSHE",
            "2024-04-11T15:00:00+08:00",
            mode="strict",
        )


def test_missing_quarter_is_not_shortened_to_annual_bridge(financial_pack):
    financial_pack["observations"] = [
        r
        for r in financial_pack["observations"]
        if not (r["field"] == "net_profit" and r["period_end"] == "2023-06-30")
    ]
    with pytest.raises(joinquant.FinancialInputUnavailable):
        candidate(financial_pack)


@pytest.mark.parametrize("value", [Fraction(1, 2_000_000), Fraction(-1, 2_000_000)])
def test_midpoint_policy_remains_unsupported(value):
    with pytest.raises(joinquant.AcceptanceError, match="midpoint"):
        six_decimal_profiles(value)
