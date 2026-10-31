"""Native commodity contracts: units, identity, malformed rows and complete pages."""
import json
from copy import deepcopy
from urllib.parse import urlencode

import pytest

from ashare_data.providers.free_updates import free_update_commodity_reports as commodity
from ashare_data.providers.free_updates import free_update_industry_flow as industry
from ashare_data.providers.free_updates.free_update_dates import require_dates, DateContractError
from ashare_data.providers.free_updates.free_update_semantics import validate_semantics
from ashare_data.providers.free_updates.free_update_spot_contracts import collect_goods


class OneResponse:
    def __init__(self, value):
        self.value = value
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return json.dumps(self.value).encode()


def sge_response():
    base = ["Au99.99", 860, 870, 850, 865, 5, .58, 863, 10, 8630000, None, "", None]
    return {"keys": [{"name": k, "unit": "元" if k == "涨跌" else ""} for k in commodity.SGE_FIELDS],
            "values": {"2026-10-28": [base, ["Ag(T+D)", *base[1:]], ["Au9999", *base[1:]],
                                      ["Au(T+N1)", *([None]*10), "", None], ["", *base[1:]]]}}


def test_sge_contract_units_aliases_null_prices_and_explicit_quarantine():
    capture = OneResponse(sge_response())
    rows = commodity.sge(capture, {})
    assert capture.urls == [commodity.SGE_URL]
    assert [r["source_price_unit"] for r in rows] == ["CNY/g", "CNY/kg", None, None]
    assert rows[0]["成交量"] == 10 and rows[2]["商品"] == "Au9999"
    assert rows[3]["收盘价"] is None
    assert capture.dataset_metadata["native_source_count"] == 5
    assert capture.dataset_metadata["quarantined_rows"][0]["native"]["商品"] == ""
    checked = validate_semantics(rows, {"function": "macro_china_au_report", "parameters": {}}, "2026-10-29")
    assert checked["unique_keys"] == 4 and not checked["gaps"]
    rows[1]["source_price_unit"] = "CNY/g"
    with pytest.raises(ValueError, match="quotation unit"):
        validate_semantics(rows, {"function": "macro_china_au_report", "parameters": {}}, "2026-10-29")


@pytest.mark.parametrize("url", [commodity.SGE_URL+"?symbol=Au99.99", commodity.SGE_URL+"?_=not-a-number",
                                  "https://other.invalid/sge.json", commodity.SGE_URL+"#fragment"])
def test_sge_legacy_replay_only_allows_numeric_cachebuster(url):
    capture = OneResponse(sge_response())
    capture.result = {"receipts": [{"url": url}]}
    with pytest.raises(ValueError, match="receipt identity"):
        commodity.sge(capture, {})
    assert not capture.urls


def test_sge_duplicate_and_future_native_dates_are_rejected_not_repaired():
    data = sge_response()
    data["values"]["2026-10-28"].append(data["values"]["2026-10-28"][0])
    with pytest.raises(ValueError, match="duplicate"):
        validate_semantics(commodity.sge(OneResponse(data), {}), {"function": "macro_china_au_report", "parameters": {}}, "2026-10-29")
    data = sge_response()
    data["values"]["2027-01-01"] = data["values"].pop("2026-10-28")
    with pytest.raises(DateContractError):
        require_dates(commodity.sge(OneResponse(data), {}), {"function": "macro_china_au_report", "parameters": {}}, "2026-10-29")


def qh_fixture():
    product = {"code": "rb", "name": "螺纹钢", "priceUnit": "元（人民币）/吨", "productId": 11,
               "qhExchangeName": "上海期货交易所", "quoteCode": "RBSH00"}
    page = {"props": {"pageProps": {"data": {"varietyListData": [{"productList": [product]}]}}}}
    body = {"code": 0, "data": {"hasNextPage": False, "isLastPage": True, "pageNum": 1, "endRow": 1, "total": 1,
                                "list": [{"date": "2026-10-28", "fp": "3043", "sp": "3122"}]}}
    return page, body


def qh_parse(page, body):
    return commodity.qh_parse('<script id="__NEXT_DATA__">'+json.dumps(page)+'</script>', json.dumps(body))


def test_rebar_units_and_identity_come_from_product_page():
    page, body = qh_fixture()
    rows, coverage = qh_parse(page, body)
    assert rows[0]["source_price_unit"] == "CNY/tonne" and rows[0]["现货价格"] == "3122"
    assert coverage["source_product"]["quoteCode"] == "RBSH00"
    checked = validate_semantics(rows, {"function": "spot_price_qh", "parameters": {}}, "2026-10-29")
    assert not checked["gaps"]
    page["props"]["pageProps"]["data"]["varietyListData"][0]["productList"][0]["priceUnit"] = "美元/吨"
    with pytest.raises(ValueError, match="quotation unit"):
        qh_parse(page, body)


@pytest.mark.parametrize("change", [{"total": 2}, {"hasNextPage": True}, {"endRow": 2}, {"pageNum": 2}])
def test_rebar_partial_pages_do_not_pass(change):
    page, body = qh_fixture()
    body["data"].update(change)
    with pytest.raises(ValueError, match="pagination"):
        qh_parse(page, body)


def test_rebar_replay_binds_product_id_and_all_evidence():
    receipts = [{"url": commodity.QH_PAGE}, {"url": commodity.QH_SESSION},
                {"url": commodity.QH_API+"?"+urlencode({k:v[0] for k,v in commodity.QH_QUERY.items()})}]
    assert commodity.qh_urls(receipts) == [r["url"] for r in receipts]
    receipts[-1]["url"] = receipts[-1]["url"].replace("productId=11", "productId=12")
    with pytest.raises(ValueError, match="identity"):
        commodity.qh_urls(receipts)


def test_goods_raw_values_no_invented_scale_and_bad_year_accounting():
    original = [{"symbol": "PB", "opendate": "0026-02-04", "price": "636", "zde": "0", "zdf": ".02"},
                {"symbol": "PB", "opendate": "2026-10-28", "price": "636", "zde": "0", "zdf": "2"}]
    data = {"result": {"status": {"code": 0}, "data": {"data": deepcopy(original)}}}
    capture = OneResponse(data)
    rows = collect_goods(capture, {"symbol": "澳大利亚粉矿价格"})
    assert capture.dataset_metadata["quarantined_rows"][0]["native"] == original[0]
    assert len(rows)+capture.dataset_metadata["quarantined_count"] == 2
    assert rows[0]["price"] == "636" and rows[0]["zdf"] == "2"
    assert "61.5%" in rows[0]["source_product"] and "not specified" in rows[0]["source_unit"]


def industry_page(page=1, total=1, code="881121", rank=1):
    cells = [str(rank), '<a href="http://q.10jqka.com.cn/thshy/detail/code/'+code+'/">合成行业</a>',
             "100", "1%", "675.90", "587.34", "88.56", "190",
             '<a href="http://stockpage.10jqka.com.cn/300613/">合成股票</a>', "20%", "72.48"]
    return ('<span class="page_info">'+str(page)+'/'+str(total)+'</span><table><thead><tr>'+
            ''.join('<th>'+h+'</th>' for h in industry.HEADERS)+'</tr></thead><tbody><tr>'+
            ''.join('<td>'+x+'</td>' for x in cells)+'</tr></tbody></table>').encode('gb18030')


def test_industry_native_headers_codes_and_rounded_fund_flows():
    class Capture:
        def __init__(self): self.urls = []
        def get(self, url):
            self.urls.append(url)
            return industry_page(1, 2) if len(self.urls) == 1 else industry_page(2, 2, code="881122", rank=2)
    capture = Capture()
    rows = industry.collect(capture, {"symbol": "即时"})
    assert len(capture.urls) == 2 and rows[0]["领涨股代码"] == "300613"
    assert capture.dataset_metadata["pages"] == 2
    checked = validate_semantics(rows, {"function": "stock_fund_flow_industry", "parameters": {}}, "2026-10-29")
    assert not checked["gaps"] and checked["latest_period"] is None
    rows[0]["净额"] = "88.6"
    with pytest.raises(ValueError, match="reconcile"):
        validate_semantics(rows, {"function": "stock_fund_flow_industry", "parameters": {}}, "2026-10-29")


def test_industry_headers_challenge_and_pagination_identity_not_guessed():
    with pytest.raises(ValueError, match="missing"):
        industry.parse(b'<html>captcha</html>')
    with pytest.raises(ValueError, match="headers"):
        industry.parse(industry_page().replace('流入资金(亿)'.encode('gb18030'), '流入资金(万)'.encode('gb18030')))
    class Capture:
        def get(self, url): return industry_page(1, 2)
    with pytest.raises(ValueError, match="page sequence"):
        industry.collect(Capture(), {"symbol": "即时"})
