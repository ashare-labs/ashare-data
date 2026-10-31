"""Synthetic source classification diagnostics cannot authorize corrected shares."""

from decimal import Decimal

import pytest

from ashare_data.financials.share_resolution import diagnose, printed_shares


def gap(total="100 万股", restricted="100 万股", executive="30 万股", value="-30 万股"):
    return dict(field="float_a_shares", source_value=value, date="2010-01-01",
                native_fields={"总股本": total, "限售A股": restricted,
                               "高管股": executive, "流通A股": value})


def series(value, day="2010-01-01"):
    return dict(date=day, locator="source/row/1", fields=dict(float_a_shares=value),
                source_quantum_shares=dict(float_a_shares="1"))


@pytest.mark.parametrize("cell,expected", [("1,000.12 万股", (Decimal("10001200"), Decimal("100"))),
    ("-1.234 万股", (Decimal("-12340"), Decimal("10"))), ("--", None), ("12 股", None)])
def test_printed_precision_and_signed_evidence(cell, expected):
    assert printed_shares(cell) == expected


def test_classification_evidence_and_exact_date_series_do_not_supply_replacement():
    result = diagnose(gap(), dict(ipo="2012-01-01"), [series("0")])
    assert result["group"] == "restricted_equals_total"
    assert result["arithmetic"]["exact"]
    assert result["before_source_code_ipo"]
    assert result["replacement_value"] is None and result["cause_is_inferred"]
    assert result["series_comparison"]["matches_total_minus_restricted"]
    assert not result["series_comparison"]["independent_provider_confirmation"]


def test_rounding_is_bounded_and_different_dates_or_duplicate_series_cannot_confirm():
    source = gap("100.000 万股", "80.000 万股", "30.000 万股", "-10.001 万股")
    result = diagnose(source, dict(ipo="2000-01-01"), [series("200000", "2010-01-02")])
    assert result["group"] == "overlapping_share_categories"
    assert result["arithmetic"]["within_printed_precision"] and not result["arithmetic"]["exact"]
    assert result["series_comparison"]["matches_total_minus_restricted"] is None
    result = diagnose(source, {}, [series("200000"), series("199999")])
    assert result["series_comparison"]["matches_total_minus_restricted"] is None
    source["native_fields"]["流通A股"] = "-10.010 万股"
    assert diagnose(source, {}, [])["group"] == "unexplained_negative"
