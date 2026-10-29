"""Protocol fixtures test missing cells, native units and page completeness."""
import json
from math import ceil
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data.providers import free_update_regional_prices as regional
from ashare_data.providers.free_update_semantics import numeric


def chart(values, days):
    return ("<div class='threeLeft'><script>name: '湖北'; '成交量：吨 涨跌：'; riqi = '"+json.dumps(days)+"'; cjj = '"+json.dumps(values)+"';</script></div>").encode()


def carbon(day="2026-10-23", **changes):
    return {"riqi": day, "cjj": "90.00", "cjl": "1,713", "zx": "0", "zd": "-1.1", "value": "90.00"} | changes


def hb_validate(rows):
    return regional.validate(rows, {"function": "energy_carbon_hb"}, "2026-10-29", numeric)


def test_carbon_thousands_and_absent_chart_cells_retain_both_raw_and_nulls():
    raw = carbon()
    rows = regional.parse_hubei(chart([{"value": ""}, raw], ["2026-10-22", "2026-10-23"]))
    result = hb_validate(rows)
    assert rows[1]["成交量"] == "1713" and rows[1]["source_native"]["cjl"] == "1,713"
    assert rows[0]["成交价"] is None and result["priced_observations"] == 1
    assert result["missing_chart_dates"] == ["2026-10-22"] and not result["gaps"]
    newer_missing = regional.parse_hubei(chart([raw, {"value": ""}], ["2026-10-23", "2026-10-26"]))
    assert hb_validate(newer_missing)["gaps"]


@pytest.mark.parametrize("change", [{"cjl": "1,71"}, {"value": "91"}, {"riqi": "2026-10-22"}, {"surprise": 1}])
def test_chart_grouping_axis_and_price_projection_are_strict(change):
    with pytest.raises(ValueError):
        regional.parse_hubei(chart([carbon(**change)], ["2026-10-23"]))


@pytest.mark.parametrize("change", [{"成交量": "1714"}, {"涨跌": "10"}, {"最新": "5"}, {"日期": "2027-10-23"}, {"market": "全国"}])
def test_native_projection_date_market_cannot_be_reinterpreted(change):
    rows = regional.parse_hubei(chart([carbon()], ["2026-10-23"]))
    with pytest.raises(ValueError):
        hb_validate([rows[0] | change])


def test_missing_chart_is_not_a_zero_trade_or_duplicate_valid_price():
    rows = regional.parse_hubei(chart([{"value": ""}], ["2026-10-23"]))
    with pytest.raises(ValueError):
        hb_validate(rows)
    rows = regional.parse_hubei(chart([carbon()], ["2026-10-23"]))
    with pytest.raises(ValueError):
        hb_validate(rows*2)
    with pytest.raises(ValueError):
        hb_validate(regional.parse_hubei(chart([carbon(cjj="0", value="0")], ["2026-10-23"])))


def house(day="2026-09-01", city="北京"):
    return dict.fromkeys(regional.HOUSE_FIELDS, 101) | {"REPORT_DATE": day, "CITY": city,
        "REPORT_DAY": day, "FIRST_COMHOUSE_BASE": None, "SECOND_HOUSE_BASE": None}


class HousingCapture:
    def __init__(self, rows):
        self.rows = rows
        self.requests = []

    def get(self, url):
        q = parse_qs(urlsplit(url).query)
        self.requests.append(q)
        page, size = int(q["pageNumber"][0]), int(q["pageSize"][0])
        return json.dumps({"success": True, "result": {"pages": ceil(len(self.rows)/size), "count": len(self.rows), "data": self.rows[(page-1)*size:page*size]}}).encode()


ALL = {"city_first": "全部", "city_second": "全部"}


def test_all_source_cities_pages_reconcile_and_null_fixed_base_is_not_filled():
    source = [house(f"{year}-{month:02d}-01", city) for year in range(2011, 2026) for month in range(1, 13) for city in ["北京", "上海", "广州"]]
    c = HousingCapture(source)
    rows = regional.collect(c, regional.HOUSE, ALL)
    result = regional.validate(rows, {"function": regional.HOUSE, "parameters": ALL}, "2026-10-29", numeric)
    assert c.dataset_metadata["pages_received"] == 2 and c.dataset_metadata["complete_source_city_universe"]
    assert all("filter" not in q for q in c.requests) and result["source_cities"] == ["上海", "北京", "广州"]
    assert rows[-1]["fixed_base_year"] is None and not result["gaps"]
    assert "定基" not in result["field_latest_nonnull_periods"]


@pytest.mark.parametrize("day,expected", [("2011-01-01", 2010), ("2015-12-01", 2010), ("2016-01-01", 2015), ("2020-12-01", 2015), ("2021-01-01", 2020), ("2025-12-01", 2020), ("2026-09-01", 2025)])
def test_fixed_base_years_follow_reviewed_official_methodology(day, expected):
    raw = house(day) | {"FIRST_COMHOUSE_BASE": 110}
    assert regional.base_year(raw, day) == expected
    with pytest.raises(ValueError):
        regional.base_year(raw, "2027-01-01")


def test_source_city_filter_missing_index_and_duplicate_month_are_not_accepted():
    params = {"city_first": "北京", "city_second": "上海"}
    with pytest.raises(ValueError):
        regional.collect_housing(HousingCapture([house()]), params)
    c = HousingCapture([house() | {"FIRST_COMHOUSE_SAME": None}])
    rows = regional.collect_housing(c, ALL)
    assert regional.validate(rows, {"function": regional.HOUSE, "parameters": ALL}, "2026-10-29", numeric)["gaps"]
    with pytest.raises(ValueError):
        regional.collect_housing(HousingCapture([house()]*2), ALL)
    with pytest.raises(ValueError):
        regional.cities({"city_first": "全部", "city_second": "北京"})


def test_concurrent_revision_and_page_count_mismatch_stop_collection():
    c = SimpleNamespace(get=lambda url: json.dumps({"result": {"pages": 2, "count": 1, "data": [house()]}}).encode())
    with pytest.raises(ValueError, match="count/page"):
        regional.collect_housing(c, ALL)
