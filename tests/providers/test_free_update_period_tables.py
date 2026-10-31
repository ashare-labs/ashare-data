"""Small synthetic contracts, never actual collected macro data or transcripts."""
from copy import deepcopy

import pytest

from ashare_data.providers.free_updates.free_update_period_tables import SINA, collect, financing_observed_months, parse_sina, parse_ths, validate
from ashare_data.providers.free_updates.free_update_semantics import numeric
from ashare_data.providers.free_updates.free_update_broadcast import index, article
from ashare_data.providers.free_updates.free_update_spot_contracts import parse_year_trend


def source(name):
    payload={"config": {"all": deepcopy(SINA[name]["columns"])}, "count": 2,
            "data": [[day, *["1" for _ in SINA[name]["columns"][1:]]] for day in ("2026.8", "2026.9")]}
    if name in {'macro_china_insurance','macro_china_society_traffic_volume'}:
        for row in payload['data']:
            row[1]='全国' if name=='macro_china_insurance' else '民航'
    if name in {'macro_china_postal_telecommunicational','macro_china_society_traffic_volume'}:
        payload['data']={'非累计':payload['data']}
    return payload


@pytest.mark.parametrize("name", SINA)
def test_native_schema_units_and_missing_values_remain_distinct(name):
    payload = source(name)
    values=next(iter(payload['data'].values())) if isinstance(payload['data'],dict) else payload['data']
    col=next(i for i,c in enumerate(payload['config']['all']) if len(c)==3)
    values[0][col] = None
    rows, coverage = parse_sina(payload, name)
    checked = validate(rows, {"function": name}, "2026-10-29", numeric)
    assert checked["contract_reviewed"] and not checked["gaps"]
    assert rows[0][payload["config"]["all"][col][1]] is None
    assert coverage["complete_history"] and checked["latest_announcement"] is None
    assert checked["missing_field_observations"]


@pytest.mark.parametrize("name", SINA)
def test_reject_unit_drift_even_with_same_numeric_response(name):
    payload = source(name)
    next(c for c in payload['config']['all'] if len(c)==3)[2] = "元"
    with pytest.raises(ValueError, match="schema changed"):
        parse_sina(payload, name)


def test_insurance_only_explicit_national_rows_no_duplicate_province_guessing():
    name='macro_china_insurance'
    payload=source(name)
    provincial=deepcopy(payload['data'][-1])
    provincial[1]='广东'
    payload['data'] += [provincial,deepcopy(provincial)]
    payload['count']=4
    rows,coverage=parse_sina(payload,name)
    assert len(rows)==2 and coverage['raw_returned_rows']==4
    assert coverage['selection']=={'省市地区':'全国'}
    result=validate(rows,{'function':name},'2026-10-29',numeric)
    assert result['business_key']==['统计时间','source_section','省市地区']
    assert result['units']['原保险保费收入']=='万元'


def test_transport_modes_and_sections_distinct_quantity_scale_unclaimed():
    name='macro_china_society_traffic_volume'
    payload=source(name)
    one=deepcopy(payload['data']['非累计'][-1])
    one[1]='公路'
    payload['data']['非累计'].append(one)
    payload['data']['累计']=deepcopy(payload['data']['非累计'])
    payload['count']=3
    rows,coverage=parse_sina(payload,name)
    result=validate(rows,{'function':name},'2026-10-29',numeric)
    assert len(rows)==6 and result['unique_keys']==6
    assert result['units']['货运量']=='source_native_scale_per_transport_mode_unverified'
    assert result['units']['货运量同比增长']=='percent'
    assert rows[0]['source_units']['货运量']=='亿吨'
    assert coverage['complete_returned_window']


@pytest.mark.parametrize("change", ["count", "duplicate", "future", "nonfinite", "balance"])
def test_incomplete_or_invalid_months_do_not_pass(change):
    name = "macro_china_central_bank_balance"
    payload = source(name)
    if change == "count":
        payload["count"] = 3
    elif change == "duplicate":
        payload["data"][1][0] = payload["data"][0][0]
    elif change == "future":
        payload["data"][1][0] = "2027.1"
    elif change == "nonfinite":
        payload["data"][1][1] = "NaN"
    else:
        labels = [c[1] for c in payload["config"]["all"]]
        payload["data"][1][labels.index("总资产")] = "2"
    with pytest.raises(ValueError):
        rows, _ = parse_sina(payload, name)
        validate(rows, {"function": name}, "2026-10-29", numeric)


def finance_html(unit="亿元"):
    return ("<html><meta charset='utf-8'><h1>股票筹资</h1><table><thead><tr><th>月份</th>"+
            "".join(f"<th>{k}({unit})</th>" for k in ("募集资金", "首发募集资金", "增发募集资金", "配股募集资金"))+
            "</tr></thead><tbody><tr><td>2026-10</td><td>30</td><td>10</td><td>20</td><td>--</td></tr></tbody></table></html>").encode()


def test_financing_current_month_and_missing_component_preserved():
    pytest.importorskip("bs4", reason="free-updates extra required for HTML contracts")
    rows, coverage = parse_ths(finance_html(), "macro_stock_finance")
    result = validate(rows, {"function": "macro_stock_finance"}, "2026-10-10", numeric)
    assert rows[0]["配股募集资金(亿元)"] is None
    assert not coverage["complete_history"] and not result["gaps"]
    assert any("当前月" in note for note in result["limitations"])
    with pytest.raises(ValueError, match="headings/units"):
        parse_ths(finance_html("万元"), "macro_stock_finance")


def test_financing_future_row_quarantined_without_losing_valid_months_or_raw_fields():
    rows, coverage = parse_ths(finance_html(), "macro_stock_finance")
    future = dict(rows[0], 月份="2026-11")
    native = [future, *rows]
    kept, scope = financing_observed_months(native, coverage, "2026-10-28T20:00:00+00:00")
    assert kept == rows and len(native) == 2
    assert scope["accepted_month_cutoff"] == "2026-10-29"
    assert scope["raw_source_count"] == 2 and scope["source_count"] == 1
    assert not scope["complete_source_table"] and scope["complete_returned_window"]
    assert scope["quarantined_records"][0]["record"] == future
    assert scope["quarantined_records"][0]["source_row_number"] == 1
    assert kept[0]["配股募集资金(亿元)"] is None
    assert not validate(kept, {"function": "macro_stock_finance"}, "2026-10-29", numeric)["gaps"]


def test_financing_replay_keeps_original_cutoff_but_new_receipt_can_admit_new_month():
    body = finance_html().replace(b"<tbody>", b"<tbody>" + b"<tr><td>2026-11</td><td>30</td><td>10</td><td>20</td><td>--</td></tr>")
    class Capture:
        receipts = [{"received_at": "2026-10-28T20:00:00+00:00"}]
        def get(self, url):
            assert url == "https://data.10jqka.com.cn/macro/finance/"
            return body
    old = Capture()
    records = collect(old, "macro_stock_finance", {})
    assert [r["月份"] for r in records] == ["2026-10"]
    assert collect(old, "macro_stock_finance", {}) == records
    fresh = Capture()
    fresh.receipts = [{"received_at": "2026-11-02T00:00:00+00:00"}]
    assert len(collect(fresh, "macro_stock_finance", {})) == 2
    assert fresh.dataset_metadata["quarantined_count"] == 0


@pytest.mark.parametrize("change", ["duplicate", "invalid", "all_future", "naive"])
def test_financing_quarantine_never_hides_malformed_or_duplicate_dates(change):
    rows, coverage = parse_ths(finance_html(), "macro_stock_finance")
    received = "2026-10-28T20:00:00+00:00"
    if change == "duplicate":
        rows += deepcopy(rows)
    elif change == "invalid":
        rows[0]["月份"] = "bad"
    elif change == "all_future":
        rows[0]["月份"] = "2027-01"
    else:
        received = "2026-10-28T20:00:00"
    with pytest.raises(ValueError):
        financing_observed_months(rows, coverage, received)


def test_loan_flow_can_be_negative_and_growth_below_minus_100():
    pytest.importorskip("bs4", reason="free-updates extra required for HTML contracts")
    body = """<meta charset='utf-8'><h1>新增人民币贷款</h1><table><thead>
    <tr><th rowspan='2'>月份</th><th colspan='3'>新增人民币贷款</th><th colspan='2'>累计人民币贷款</th></tr>
    <tr><th>总额(亿元)</th><th>同比</th><th>环比</th><th>总额(亿元)</th><th>同比</th></tr></thead>
    <tbody><tr><td>2026-09</td><td>-1</td><td>-110%</td><td>-120%</td><td>100</td><td>1%</td></tr></tbody></table>""".encode()
    rows, _ = parse_ths(body, "macro_rmb_loan")
    checked = validate(rows, {"function": "macro_rmb_loan"}, "2026-10-29", numeric)
    assert not checked["gaps"] and "outstanding" in checked["field_roles"]["累计人民币贷款/总额(亿元)"]
    rows[0]["新增人民币贷款/同比"] = "-1.1"
    with pytest.raises(ValueError, match="percent scale"):
        validate(rows, {"function": "macro_rmb_loan"}, "2026-10-29", numeric)


URL = "https://tv.cctv.com/2026/10/28/VIDETEST261028.shtml"


def broadcast_index(url=URL):
    return (f'<ul><li><a title="《新闻联播》 20261028 19:00" href="{URL.replace("TEST", "FULL")}">播出</a></li>'
            f'<li><a href="{url}">测试文字稿</a></li></ul>').encode()


def test_broadcast_index_requires_correct_dates_and_whole_article_identity():
    pytest.importorskip("bs4", reason="free-updates extra required for HTML contracts")
    assert index(broadcast_index(), "2026-10-28") == [URL]
    with pytest.raises(ValueError, match="date/host"):
        index(broadcast_index(URL.replace("/28/", "/27/")), "2026-10-28")
    with pytest.raises(ValueError, match="transcript/identity"):
        article(b"<h3>Missing transcript</h3>", URL, "2026-10-28")
    body = b'<meta name="contentid" content="VIDETEST261028"><h3>[video] test</h3><div class="content_area">text<script>bad()</script></div>'
    row = article(body, URL, "2026-10-28")
    assert row["content"] == "text" and row["publication_at"] is None


def test_soozhu_chart_unit_is_not_borrowed_from_another_product():
    pytest.importorskip("bs4", reason="free-updates extra required for HTML contracts")
    page = '<script>function drawline2() {title:"今年以来全国出栏均价走势",series:"全国出栏均价",unit:"元/公斤"}function thisyeartrend() {}</script>'.encode()
    data = {"res": "1", "nationlist": [["2026-10-28", 10]]}
    assert parse_year_trend(page, data)[0]["source_unit"] == "元/公斤"
    with pytest.raises(ValueError, match="identity/unit"):
        parse_year_trend(page.replace("元/公斤".encode(), "元/头".encode()), data)
