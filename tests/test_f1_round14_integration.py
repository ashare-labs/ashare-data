"""Public API and historical-state regression for the additive research scope."""

import json
import math
from pathlib import Path
import struct

import pytest

from ashare_data import f1
import test_f1_fi_sv_scoped as synthetic_cases

pack = synthetic_cases.pack
sv = synthetic_cases.sv


def test_explicit_public_fi02_entry_keeps_original_dispatch_restricted(pack):
    value = f1.fi02_lagged_candidate(
        f1.FinancialLedger(pack), "000001.XSHE", "2024-04-29", mode=f1.RESEARCH_MODE
    )
    assert value["exact_ratio"] == {"numerator": 4, "denominator": 9}
    assert value["eight_quarter_chain_complete"]
    assert not value["vendor_version_policy_proven"] and not value["jq_equivalent"]
    with pytest.raises(f1.AcceptanceError, match="unsupported"):
        f1.financial_factor_candidate(
            f1.FinancialLedger(pack),
            "FI02",
            "000001.XSHE",
            "2024-04-29T15:00:00+08:00",
            mode=f1.RESEARCH_MODE,
        )


def test_public_sv_entry_preserves_failed_candidate_and_explicit_mode(sv):
    value = f1.sv_stage_candidate(*sv, "2024-01-08", mode=f1.SHARE_MODE)
    assert value["SV02"]["candidate_status"] == "failed_round14_named_key_zero_ULP_rule_preserved"
    assert not value["historical_pit_certified"] and not value["jq_equivalent"]
    with pytest.raises(f1.VisibilityUnproven):
        f1.sv_stage_candidate(*sv, "2024-01-08", mode="strict")


def test_public_observation_cannot_turn_one_ulp_failure_into_batch_pass():
    lock, records, authorization = synthetic_cases.synthetic_batch()
    value = math.nextafter(1.0, 0.0)
    records[1].update(
        value_17g=format(value, ".17g"), float64_bits_be=struct.pack(">d", value).hex()
    )
    observed = f1.observe_fi_sv_batch(lock, records, authorization)
    assert observed["passed_cells"] == 10
    assert not observed["finite_numeric_batch_passed"]
    assert not observed["independent_review_completed"] and not observed["all_twelve_slots_passed"]


def test_review_is_additive_and_prior_blocks_remain():
    root = Path(__file__).parents[1]
    contract = json.loads((root / "docs/f1-fi-sv-round14.json").read_text())
    assert contract["status"]["passed_cells"] == 10
    assert contract["status"]["finite_batch_passed"] is False
    assert contract["status"]["independent_review_completed"] is False
    review = contract["independent_review"]
    assert review["completed"] and review["SV02_finite_numeric_equivalence"]
    assert not review["original_experiment_reclassified_as_pass"]
    assert not review["supplier_conversion_order_identified"]
    assert not review["strict_PIT_certified"] and not review["all_twelve_slots_passed"]
    assert contract["unchanged_blocks"] == ["FI03", "PV05", "ROC"]
    assert not any(
        hasattr(f1, name)
        for name in ("net_profit_growth_rate", "VOL5", "circulating_market_cap", "ROC120")
    )
