"""Decision-relevant panel, publication, missingness and ranking contracts."""

from copy import deepcopy
from fractions import Fraction as F

import pytest

from ashare_data.f1_batch import (
    BatchInputUnavailable,
    DEFINITION,
    MODE,
    UNITS,
    SnapshotFinancials,
    _effective,
    _nearest_nonmidpoint,
    compare_panels,
    compute_panel,
    market_value,
    rank_metrics,
)


def test_no_compressed_calendar_window_or_zero_trade_fill():
    sessions = [f"2024-04-{i:02}" for i in range(1, 7)]
    bar = dict(open="10", close="10", high="11", low="9", volume="100", amount="1000")
    item = {"sina": {d: dict(bar) for d in sessions}}
    del item["sina"][sessions[2]]
    with pytest.raises(BatchInputUnavailable, match="SOURCE_SESSION_MISSING"):
        market_value(item, sessions, sessions[-1], "TVMA6")
    item["sina"][sessions[2]] = {**bar, "volume": "0"}
    with pytest.raises(BatchInputUnavailable, match="NORMAL_TRADING_ONLY"):
        market_value(item, sessions, sessions[-1], "TVMA6")


@pytest.mark.parametrize(
    "change",
    [
        {"units": {**UNITS, "volume": "lots"}},
        {"raw_price_adjustment": "post"},
        {"input_contains_JQ_values": True},
    ],
)
def test_incompatible_units_or_source_are_rejected(change):
    dataset = {
        "schema": "ad.f1-batch-inputs.v1",
        "units": UNITS,
        "raw_price_adjustment": "none",
        "input_contains_JQ_values": False,
        "items": [],
        "sessions": ["2024-04-19"],
        **change,
    }
    with pytest.raises(ValueError, match="input schema"):
        compute_panel(dataset, ["2024-04-19"], definition=DEFINITION)


def test_conflicting_effective_shares_fail_only_relevant_interval():
    shares = [
        {"date": "2020-01-01", "value": "100"},
        {"date": "2020-01-01", "value": "101"},
        {"date": "2020-02-01", "value": "200"},
    ]
    with pytest.raises(BatchInputUnavailable, match="AMBIGUOUS_EFFECTIVE"):
        _effective(shares, "2020-01-20", "value")
    assert _effective(shares, "2020-02-10", "value") == 200


def test_unproven_halfway_rounding_rejected():
    with pytest.raises(BatchInputUnavailable, match="MIDPOINT"):
        _nearest_nonmidpoint(F(1, 20000), 4)


def financial_rows():
    records = []
    for year in (2022, 2023):
        for q, end in enumerate(("03-31", "06-30", "09-30", "12-31"), 1):
            records.append(
                {
                    "kind": "income",
                    "period": f"{year}-{end}",
                    "notice_date": f"{year + 1}-01-01" if q == 4 else f"{year}-{q * 3 + 1:02}-10",
                    "update_date": "2026-01-01",
                    "currency": "CNY",
                    "values": {
                        "NETPROFIT": str(q * 10),
                        "OPERATE_INCOME": str(q * 100),
                        "TOTAL_OPERATE_INCOME": str(q * 110),
                    },
                }
            )
    return records


def test_visible_selection_next_day_and_strict_pit_fail_closed():
    ledger = SnapshotFinancials(financial_rows())
    assert ledger.latest("NETPROFIT", "2023-04-10") == "2022-12-31"
    assert ledger.latest("NETPROFIT", "2023-04-11") == "2023-03-31"
    with pytest.raises(BatchInputUnavailable, match="STRICT_PIT"):
        ledger.factor("2024-01-02", "operating_revenue_ttm", mode="strict")
    value, sources, period = ledger.factor("2024-01-02", "operating_revenue_ttm", mode=MODE)
    assert value == 400 and period == "2023-12-31"
    assert all(s["update_date"] > "2024-01-02" for s in sources)


def test_missing_quarter_rejected_even_annual_value_exists():
    rows = [r for r in financial_rows() if r["period"] != "2023-06-30"]
    with pytest.raises(BatchInputUnavailable, match="VISIBLE_COMPONENT_MISSING"):
        SnapshotFinancials(rows).factor("2024-01-02", "operating_revenue_ttm", mode=MODE)


def test_duplicate_financial_version_is_not_silently_selected():
    rows = financial_rows()
    with pytest.raises(ValueError, match="ambiguous"):
        SnapshotFinancials(rows + [deepcopy(rows[-1])])


def test_average_ties_and_deterministic_selection():
    result = rank_metrics(["d", "c", "b", "a"], [2, 2, 1, 0], [2, 2, 1, 0], k=1)
    assert result["spearman"] == pytest.approx(1)
    assert result["reference_tied_pairs"] == 1
    assert result["top"]["local"] == ["c"]
    assert result["bottom"]["local"] == ["a"]


def test_selection_can_fail_despite_small_numeric_error():
    result = rank_metrics(["a", "b", "c", "d"], [1, 2, 3.001, 3], [1, 2, 3, 3.001], k=1)
    assert result["top"]["overlap"] == 0
    assert result["top"]["replacement_count"] == 1


def test_coverage_uses_requested_panel_not_only_paired_subset():
    scope = {
        "securities": [{"security": str(i)} for i in range(12)],
        "reference_dates": ["2024-04-19"],
        "fields": ["ROC120"],
        "thresholds": {"ROC120": {"atol": 0, "rtol": 0}},
        "coverage_min": 0.95,
        "minimum_panel_size": 10,
        "rank_rule": {
            "top_k": 3,
            "spearman_min": 0.98,
            "pair_concordance_min": 0.97,
            "top_overlap_min": 1,
            "bottom_overlap_min": 1,
        },
    }
    rows = [
        {"security": str(i), "date": "2024-04-19", "field": "ROC120", "value": str(i)}
        for i in range(11)
    ]
    result = compare_panels(rows, rows, scope, comparison_kind="test")
    assert result["panels"][0]["rank_pass"] is True
    assert result["fields"]["ROC120"]["numeric_pass_rate"] == 1
    assert result["fields"]["ROC120"]["coverage"] == 11 / 12
    assert result["fields"]["ROC120"]["rank_usable"] is False
    with pytest.raises(ValueError, match="duplicate"):
        compare_panels(rows + rows[:1], rows, scope, comparison_kind="test")
