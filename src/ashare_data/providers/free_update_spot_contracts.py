"""Preserve the source's product identity and per-head/per-kg quotations."""
import gzip
import json

PRODUCTS = {
    "spot_hog_lean_price_soozhu": (1, "瘦肉型肉猪(110kg左右)", "元/公斤"),
    "spot_hog_three_way_soozhu": (4, "三元仔猪(15kg左右)", "元/头"),
    "spot_hog_crossbred_soozhu": (6, "后备二元母猪", "元/头"),
    "spot_corn_price_soozhu": (8, "玉米(14%水分)", "元/公斤"),
    "spot_soybean_price_soozhu": (9, "豆粕(43%)", "元/公斤"),
    "spot_mixed_feed_soozhu": (11, "育肥猪配合料（含自配料）", "元/公斤"),
}
YEAR_TREND = "spot_hog_year_trend_soozhu"
GOODS = {"波罗的海干散货指数": "BDI", "钢坯价格指数": "GP", "澳大利亚粉矿价格": "PB"}
GOODS_NAMES = {"BDI": "波罗的海干散货指数", "GP": "钢坯价格指数", "PB": "澳大利亚粉矿价格(61.5%）"}
GOODS_PAGE = "http://stock.finance.sina.com.cn/futures/view/Goods_index.php"


def parse_year_trend(page, data):
    from bs4 import BeautifulSoup
    text = str(BeautifulSoup(page, "lxml"))
    # Verify the specific chart's product and axis rather than borrowing the
    # per-head units of the site's other products. Source JS is never executed.
    start, end = text.find("function drawline2("), text.find("function thisyeartrend(")
    chart = text[start:end] if 0 <= start < end else ""
    if any(s not in chart for s in ("今年以来全国出栏均价走势", "元/公斤", "全国出栏均价")) or str(data.get("res")) != "1":
        raise ValueError("Soozhu national chart identity/unit schema changed")
    values = data["nationlist"]
    if not isinstance(values, list) or not 1 <= len(values) <= 1000 or any(not isinstance(r, list) or len(r) != 2 for r in values):
        raise ValueError("Soozhu national chart row schema changed")
    return [{"日期": r[0], "价格": r[1], "source_product": "全国出栏均价", "source_unit": "元/公斤"} for r in values]


def parse(data, name):
    expected = PRODUCTS[name]
    info = data["ind"]
    actual = (int(info["id"]), info["name"], info["unit"])
    if actual != expected:
        raise ValueError("Soozhu source product specification or quotation unit changed")
    rows = []
    for pair in data["datalist"]:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("Soozhu date/price pair changed")
        rows.append({"日期":pair[0], "价格":pair[1], "source_product_id":actual[0],
                     "source_product":actual[1], "source_unit":actual[2]})
    return rows


def enrich(capture, name):
    receipt = capture.receipts[-1]
    if receipt["host"] != "www.soozhu.com" or receipt["status"] != 200:
        raise ValueError("Soozhu price evidence missing")
    raw = gzip.decompress((capture.root/(receipt["sha256"]+".gz")).read_bytes())
    if name == YEAR_TREND:
        if len(capture.receipts) != 2:
            raise ValueError("Soozhu national chart expects its page and public data response")
        first = capture.receipts[0]
        if first["url"] != "https://www.soozhu.com/price/data/center/" or first["status"] != 200:
            raise ValueError("Soozhu national chart source page missing")
        page = gzip.decompress((capture.root/(first["sha256"]+".gz")).read_bytes())
        rows = parse_year_trend(page, json.loads(raw))
        capture.dataset_metadata = {"complete_returned_window": True, "complete_history": False,
            "source_count": len(rows), "source_product": "全国出栏均价", "source_unit": "元/公斤",
            "history_limit": "source recent window may be shorter than the advertised year; never infer missing Jan-to-date observations"}
        return rows
    rows = parse(json.loads(raw), name)
    capture.dataset_metadata = {"complete_returned_window":True, "complete_history":False,
        "history_limit":"source returns a rolling recent window; older dates cannot be requested here",
        "source_count":len(rows), "source_product":PRODUCTS[name][1], "source_unit":PRODUCTS[name][2],
        "recovery":"retain every published date; flag gaps longer than the returned window"}
    return rows


def validate(rows, job, as_of, numeric, source_date):
    name = job["function"]
    if name == "spot_goods":
        symbol = GOODS[job["parameters"]["symbol"]]
        days = []
        for row in rows:
            day = source_date(row["opendate"])
            if (row.get("symbol") != symbol or row.get("source_product") != GOODS_NAMES[symbol]
                    or row.get("source_unit") != "native value; physical unit not specified by source page"
                    or day != row["日期"] or not day or day > as_of):
                raise ValueError("spot goods product/date/raw-unit mismatch")
            if numeric(row["price"]) is None or numeric(row["price"]) <= 0 or row["price"] != row["指数"]:
                raise ValueError("spot goods price invalid")
            numeric(row["zde"])
            numeric(row["zdf"])
            days.append(day)
        if len(set(days)) != len(days):
            raise ValueError("duplicate spot goods date")
        return {"contract_reviewed": True, "business_key": ["symbol", "日期"],
            "unique_keys": len(days), "source_rows": len(rows), "gaps": [],
            "units": {"price": "native value; physical unit unspecified", "zdf": "native scale, mixed historic fraction/percent; not normalized"},
            "frequency": "source_daily_release_overlap_revisions", "latest_period": max(days), "latest_announcement": None,
            "limitations": ["提供原生数值和来源名称；页面未明示物理单位，不猜元/吨或美元/吨",
                            "错误年份原行单独隔离并可核查，不修日期、不静默丢弃；历史涨跌幅不统一换算"]}
    if name not in PRODUCTS and name != YEAR_TREND:
        return None
    expected = PRODUCTS[name] if name in PRODUCTS else (None, "全国出栏均价", "元/公斤")
    dates = []
    for row in rows:
        if (row.get("source_product_id"), row.get("source_product"), row.get("source_unit")) != expected:
            raise ValueError("Soozhu product identity or unit absent/mismatched")
        day = source_date(row.get("日期"))
        price = numeric(row.get("价格"))
        if not day or day > as_of or price is None or price <= 0:
            raise ValueError("Soozhu price/date invalid")
        dates.append(day)
    if len(set(dates)) != len(rows):
        raise ValueError("duplicate SooZhu date in one product")
    return {"contract_reviewed":True, "business_key":["source_product" if name == YEAR_TREND else "source_product_id","日期"],
        "unique_keys":len(dates), "source_rows":len(rows), "gaps":[],
        "units":{"价格":"CNY/head" if expected[2] == "元/头" else "CNY/kg"},
        "frequency":"source_daily_price_release_in_recent_rolling_window", "latest_period":max(dates),
        "latest_announcement":None, "limitations":["报价规格及元/头、元/公斤分别保留，不按体重擅自换算",
            "仅源近期窗口；每日重叠采集保存新增及修订；超过窗口的漏采需报告不可回补，不能声称全历史"]}
def collect_goods(capture, params):
    """Retain invalid-year rows explicitly while serving unambiguous dates.

    Every native row reconciles to a returned observation or the quarantine;
    neither date prefixes nor historical percentage scales are repaired.
    """
    from urllib.parse import urlencode
    from ashare_data.providers.free_update_dates import reviewed_date
    if set(params) != {"symbol"} or params["symbol"] not in GOODS:
        raise ValueError("spot goods needs an explicit reviewed product")
    symbol = GOODS[params["symbol"]]
    response = json.loads(capture.get("https://stock.finance.sina.com.cn/futures/api/openapi.php/GoodsIndexService.get_goods_index?"+urlencode({"symbol": symbol, "table": "0"})))
    if response["result"]["status"]["code"] != 0:
        raise ValueError("spot goods response rejected")
    rows = response["result"]["data"]["data"]
    if not rows or any(r.get("symbol") != symbol for r in rows):
        raise ValueError("spot goods product identity/empty response invalid")
    valid, excluded = [], []
    for index, row in enumerate(rows):
        if reviewed_date(row["opendate"]) != row["opendate"]:
            excluded.append({"source_row_index": index, "native": row,
                             "reason": "invalid source date; original retained, no year repair"})
        else:
            valid.append(row | {"日期": row["opendate"], "指数": row["price"], "涨跌额": row["zde"], "涨跌幅": row["zdf"],
                                "source_product": GOODS_NAMES[symbol], "source_product_evidence": GOODS_PAGE,
                                "source_unit": "native value; physical unit not specified by source page"})
    capture.dataset_metadata = {"source_count": len(valid), "native_source_count": len(rows), "symbol": symbol,
        "complete_returned_window": True, "complete_history": False,
        "quarantined_count": len(excluded), "quarantined_rows": excluded,
        "scope": "all valid source-date observations; every invalid row retained separately without date repair",
        "units_verified": False, "limitation": "native price value only; physical unit and historical zdf scale unspecified, never converted"}
    if not valid:
        raise ValueError("spot goods has no valid dated observations; originals retained")
    return valid
