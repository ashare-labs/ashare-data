import json
from pathlib import Path
import pytest
from ashare_data.providers.supplemental_updates import catalog, moving_averages, project


def test_separate_scope_is_exact_and_does_not_relabel_first_batch():
    from ashare_data.providers.free_updates import catalog as first

    second = catalog()
    assert len(second["items"]) == 19
    assert not {i["excel_row"] for i in second["items"]} & {
        i["excel_row"] for i in first()["items"]
    }
    assert second["workbook_sha256"] == first()["workbook_sha256"]


def test_indicators_have_explicit_warmup_no_future_data_and_fixed_basis():
    records = [
        {
            "security": "sh600000",
            "asset_type": "stock",
            "date": f"2026-10-{n:02d}",
            "close": str(n),
            "price_basis": "unadjusted",
        }
        for n in range(1, 12)
    ]
    rows = moving_averages(records)
    assert all(r["sma_5"] is None for r in rows[:4])
    assert rows[4]["sma_5"] == "3" and rows[9]["sma_10"] == "5.5"
    assert all(r["sma_20"] is None for r in rows)
    assert moving_averages(records[:6]) == rows[:6]
    with pytest.raises(ValueError, match="price basis"):
        moving_averages(records + [records[0] | {"price_basis": "qfq"}])


def source(function, rows):
    return (
        {"function": function, "snapshot_id": function, "observed_at": "2026-10-30T08:00:00+00:00"},
        rows,
    )


def test_constituent_join_does_not_apply_current_members_to_prior_dates():
    members = [
        {
            "指数代码": "000300",
            "成分券代码": "600000",
            "交易所": "上海证券交易所",
            "日期": "2026-10-29",
        }
    ]
    bar = {"security": "sh600000", "asset_type": "stock", "date": "2026-10-28", "close": "10"}
    assert (
        project(125, [source("index_stock_cons_csindex", members), source("sina_raw_daily", [bar])])
        is None
    )
    result = project(
        125,
        [
            source("index_stock_cons_csindex", members),
            source("sina_raw_daily", [bar, bar | {"date": "2026-10-29"}]),
        ],
    )
    assert result[0][0]["date"] == "2026-10-29" and result[4]["member_count"] == 1


def test_long_term_average_keeps_two_series_and_excludes_point_yield():
    result = project(
        201,
        [
            source(
                "treasury_rates",
                [
                    {"date": "2026-10-29", "RATE_TYPE": kind, "RATE": "2.5"}
                    for kind in ["BC_20year", "Over_10_Years", "Real_Rate"]
                ] + [{"date": "2026-10-29", "BC_20YEAR": "4.8", "BC_10YEAR": "4.1"}],
            )
        ],
    )
    assert {r["series"] for r in result[0]} == {"Over_10_Years", "Real_Rate"}


def test_missing_input_stays_pending_without_new_source_attempts(tmp_path):
    from ashare_data.providers.supplemental_updates import refresh

    path = tmp_path / "input.json"
    path.write_text(json.dumps({"manual_snapshot_queries": []}))
    result = refresh(path, tmp_path / "derived")
    assert (
        result["usable_count"] == 0
        and len(result["items"]) == 19
        and result["network_requests"] == 0
    )
    assert not any(i.get("daily_enabled") for i in result["items"])
    assert Path(tmp_path / "derived/latest.json").exists()


def test_overlapping_daily_windows_use_latest_observation_once():
    bars = [
        dict(
            security="sh600000",
            asset_type="stock",
            date=f"2026-10-{n:02d}",
            close=str(n),
            price_basis="unadjusted",
        )
        for n in range(1, 7)
    ]
    older = source("sina_raw_daily", bars[:5])
    newer = source("sina_raw_daily", bars[2:])
    newer[0]["observed_at"] = "2026-10-30T11:00:00+00:00"
    newer[0]["snapshot_id"] = "later-window"
    newer[1][0] = newer[1][0] | {"close": "30"}
    value = project(51, [newer, older, older])
    rows = value[0]
    assert len(rows) == 6 and rows[2]["close"] == "30"
    assert rows[4]["sma_5"] == "8.4" and rows[5]["sma_5"] == "9.4"
    assert {m["snapshot_id"] for m in value[5]} == {"sina_raw_daily", "later-window"}
    conflicting = (dict(newer[0]), [newer[1][0] | {"close": "31"}])
    with pytest.raises(ValueError, match="same-time daily observations conflict"):
        project(51, [newer, conflicting])


def test_revised_financial_factor_and_removed_topic_use_latest_observation():
    older = source("tdx_f10_valuation", [dict(security="sh600000", date="2026-10-30", pe_ttm="6", pb_mrq=".4")])
    newer = source("tdx_f10_valuation", [dict(security="sh600000", date="2026-10-30", pe_ttm="6.5", pb_mrq=".44")])
    newer[0].update(observed_at="2026-10-30T12:00:00+00:00", snapshot_id="revision")
    assert project(50, [older, newer])[0][0]["pe_ttm"] == "6.5"
    before = source("tdx_f10_hot_topics", [dict(security="sh600000", topic_id="1"), dict(security="sh600000", topic_id="2")])
    after = source("tdx_f10_hot_topics", [dict(security="sh600000", topic_id="2")])
    after[0].update(observed_at="2026-10-30T12:00:00+00:00", snapshot_id="current")
    assert project(214, [before, after])[0] == after[1]
    action = dict(security="sh600000", native_business_identity=["dividend", "2026-07-10"], value="1")
    a = source("sina_corporate_actions", [action])
    b = source("sina_corporate_actions", [action | {"value": "1.1"}])
    b[0].update(observed_at="2026-10-30T12:00:00+00:00", snapshot_id="revised-event")
    assert project(207, [a, b])[0] == b[1]
