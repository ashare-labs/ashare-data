"""Public API and historical-state regression for the additive research scope."""

import pytest

from ashare_data.factors import joinquant as joinquant
import test_share_financial as synthetic_cases

pack = synthetic_cases.pack
sv = synthetic_cases.sv


def test_explicit_public_fi02_entry_keeps_original_dispatch_restricted(pack):
    value = joinquant.net_profit_growth_candidate(
        joinquant.FinancialLedger(pack), "000001.XSHE", "2024-04-29", mode=joinquant.RESEARCH_MODE
    )
    assert value["exact_ratio"] == {"numerator": 4, "denominator": 9}
    assert value["eight_quarter_chain_complete"]
    assert not value["vendor_version_policy_proven"] and not value["jq_equivalent"]
    with pytest.raises(joinquant.AcceptanceError, match="unsupported"):
        joinquant.financial_factor_candidate(
            joinquant.FinancialLedger(pack),
            "net_profit_growth_rate",
            "000001.XSHE",
            "2024-04-29T15:00:00+08:00",
            mode=joinquant.RESEARCH_MODE,
        )


def test_public_sv_entry_preserves_failed_candidate_and_explicit_mode(sv):
    value = joinquant.share_metrics(*sv, "2024-01-08", mode=joinquant.SHARE_MODE)
    assert (
        value["circulating_market_cap"]["candidate_status"]
        == "explicit_float_conversion_model_unverified"
    )
    assert not value["historical_pit_certified"] and not value["jq_equivalent"]
    with pytest.raises(joinquant.VisibilityUnproven):
        joinquant.share_metrics(*sv, "2024-01-08", mode="strict")
