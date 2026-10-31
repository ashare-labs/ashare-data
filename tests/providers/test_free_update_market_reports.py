"""Native dates, units, precision and incomplete months must not be inferred."""

import copy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data.providers.free_updates import free_update_market_reports as reports
from ashare_data.providers.free_updates.free_update_semantics import numeric, validate_semantics


def source(day="2026-10-28"):
    return [
        {
            "metadata": {
                "tabkey": "tab1",
                "name": "融资融券交易总量",
                "catalogid": "1837_xxpl",
                "subname": day,
                "cols": reports.MARGIN_COLUMNS.copy(),
                "pagesize": -1,
                "pagecount": 0,
            },
            "error": None,
            "data": [
                {
                    "jrrzmr": "620.00",
                    "jrrzye": "11,400.00",
                    "jrrjmc": "0.30",
                    "jrrjyl": "12.00",
                    "jrrjye": "100.00",
                    "jrrzrjye": "11,500.00",
                }
            ],
        },
        {
            "metadata": {"tabkey": "tab2", "name": "融资融券交易明细"},
            "data": [{"证券代码": "000001"}],
        },
    ]


def collect(body):
    urls = []

    def get(url):
        urls.append(url)
        return json.dumps(body).encode()

    capture = SimpleNamespace(get=get)
    rows = reports.collect(capture, {"date": "20261028"})
    return rows, capture.dataset_metadata, parse_qs(urlsplit(urls[0]).query)


def check(rows):
    return validate_semantics(
        rows, {"function": reports.MARGIN, "parameters": {"date": "20261028"}}, "2026-10-29"
    )


def test_official_aggregate_date_units_and_precision_are_preserved():
    rows, meta, q = collect(source())
    assert len(rows) == 1 and q["txtDate"] == ["2026-10-28"]
    assert rows[0]["融资余额"] == "11400.00" and rows[0]["source_native"]["jrrzye"] == "11,400.00"
    assert rows[0]["source_units"]["融券余量"] == "100000000 shares_or_fund_units"
    assert (
        meta["complete_requested_window"]
        and not meta["complete_security_details"]
        and not meta["complete_history"]
    )
    assert check(rows)["gaps"] == [] and rows[0]["publication_at"] is None


@pytest.mark.parametrize(
    "damage",
    ["date", "unit", "page", "name", "empty", "duplicate", "missing", "rounding", "grouping"],
)
def test_changed_schema_or_unavailable_date_never_becomes_a_valid_aggregate(damage):
    body = source()
    meta, row = body[0]["metadata"], body[0]["data"][0]
    if damage == "date":
        meta["subname"] = "2026-10-29"
    if damage == "unit":
        meta["cols"]["jrrjyl"] = "融券余量<br>(股)"
    if damage == "page":
        meta["pagecount"] = 2
    if damage == "name":
        meta["name"] = "融资融券交易明细"
    if damage == "empty":
        body[0]["data"] = []
    if damage == "duplicate":
        body.append(copy.deepcopy(body[0]))
    if damage == "missing":
        row.pop("jrrjyl")
    if damage == "rounding":
        row["jrrzye"] = "11400.001"
    if damage == "grouping":
        row["jrrzye"] = "11,40.00"
    with pytest.raises(ValueError):
        collect(body)


def test_native_total_precision_and_no_inferred_projection():
    body = source()
    body[0]["data"][0]["jrrzrjye"] = "11,500.02"
    assert not check(collect(body)[0])["gaps"]
    body[0]["data"][0]["jrrzrjye"] = "11,500.03"
    with pytest.raises(ValueError, match="precision"):
        check(collect(body)[0])
    rows = collect(source())[0]
    rows[0]["source_units"] = dict.fromkeys(reports.MARGIN_UNITS, "CNY")
    with pytest.raises(ValueError, match="projection"):
        check(rows)


def test_latest_margin_skips_unpublished_date_but_keeps_actual_source_day():
    urls = []
    def get(url):
        day = parse_qs(urlsplit(url).query)["txtDate"][0]
        urls.append(day)
        body = source(day)
        if day == "2026-10-30":
            body[0]["metadata"]["subname"] = ""
            body[0]["data"] = []
        return json.dumps(body).encode()
    capture = SimpleNamespace(get=get)
    rows = reports.collect_latest(capture, {"as_of": "20261030"})
    assert urls == ["2026-10-30", "2026-10-29"]
    assert rows[0]["日期"] == "2026-10-29"
    assert capture.dataset_metadata["dates_without_published_rows"] == ["2026-10-30"]
    check = reports.validate(rows, {"function": reports.LATEST_MARGIN,
        "parameters": {"as_of": "20261030"}}, "2026-10-30", numeric)
    assert check["latest_period"] == "2026-10-29"


def test_latest_margin_never_uses_default_undated_table_or_bypasses_refusal():
    def bad(url):
        return json.dumps(source("2010-03-31")).encode()
    with pytest.raises(ValueError, match="mismatch"):
        reports.collect_latest(SimpleNamespace(get=bad), {"as_of": "20261030"})
    def refused(url):
        raise RuntimeError("HTTP 403")
    with pytest.raises(RuntimeError, match="403"):
        reports.collect_latest(SimpleNamespace(get=refused), {"as_of": "20261030"})


def month(day="2026-09-01", partial=False):
    fields = "TOTAL_SHARES_SH TOTAL_SZARES_SZ TOTAL_MARKE_SH TOTAL_MARKE_SZ DEAL_AMOUNT_SH DEAL_AMOUNT_SZ VOLUME_SH VOLUME_SZ HIGH_INDEX_SH HIGH_INDEX_SZ LOW_INDEX_SH LOW_INDEX_SZ".split()
    return (
        dict.fromkeys(fields, 1)
        | {"REPORT_DATE": day, "TIME": day[:4] + "年" + day[5:7] + "月"}
        | ({"TOTAL_SHARES_SH": None} if partial else {})
    )


def test_current_partial_month_is_explicit_but_does_not_supply_missing_units():
    result = reports.stock_statistics([month(), month("2026-10-01", True)], "2026-10-29", numeric)
    assert result["latest_complete_period"] == "2026-09-01"
    assert result["incomplete_periods"][0]["current_month_in_progress"]
    assert result["contract_reviewed"] and result["gaps"] == []
    assert result["units"]["TOTAL_SHARES_SH"] == "source_native_scale_unverified"
    assert reports.stock_statistics([month(partial=True)], "2026-10-29", numeric)["incomplete_periods"]


@pytest.mark.parametrize(
    "rows",
    [
        [month(), month()],
        [month("2027-01-01")],
        [month() | {"LOW_INDEX_SH": 2}],
        [month() | {"TIME": "2026年08月"}],
    ],
)
def test_stock_month_duplicate_future_label_and_high_low_mismatch(rows):
    with pytest.raises(ValueError):
        reports.stock_statistics(rows, "2026-10-29", numeric)


def test_enterprise_yoy_labels_are_not_accepted_as_a_reviewed_growth_definition():
    from ashare_data.providers.free_updates.free_update_macro_contracts import CONTRACTS
    row = dict.fromkeys(CONTRACTS["macro_china_enterprise_boom_index"], 1)
    row.update(REPORT_DATE="2026-06-01", TIME="2026年第2季度", BOOM_INDEX=109.5, BOOM_INDEX_SAME=9.5)
    result = validate_semantics([row], {"function":"macro_china_enterprise_boom_index","parameters":{}}, "2026-10-29")
    assert not result["gaps"] and any("同比" in note for note in result["field_availability_notes"])
    assert "unverified" in result["units"]["BOOM_INDEX_SAME"]
