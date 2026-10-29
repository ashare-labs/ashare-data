"""Native SGE daily reports and metadata-bound rebar spot/futures observations.

Quotation units are per contract. Unspecified turnover/volume scales and old
native aliases remain labelled raw, rather than guessed or discarded.
"""
import gzip
import json
import re
from urllib.parse import parse_qs, urlsplit

from ashare_data.providers.free_update_dates import reviewed_date

SGE_URL = "https://cdn.jin10.com/data_center/reports/sge.json"
SGE_FIELDS = ("商品", "开盘价", "最高价", "最低价", "收盘价", "涨跌", "涨跌幅",
              "加权平均价", "成交量", "成交金额", "持仓量", "交收方向", "交收量")
# Exact codes only; never infer an old alias or a trading-lot conversion.
PRICE_UNITS = {"Au99.99": "CNY/g", "Au99.95": "CNY/g", "Pt99.95": "CNY/g", "Ag(T+D)": "CNY/kg"}
UNIT_PAGES = {"Au99.99": "https://www.sge.com.cn/cpfw/Au9999",
              "Au99.95": "https://www.sge.com.cn/cpfw/Au9995",
              "Pt99.95": "https://www.sge.com.cn/cpfw/Pt9995",
              "Ag(T+D)": "https://www.sge.com.cn/cpfw/Ag(T%2BD)"}
QH_PAGE = "https://www.99qh.com/data/spotTrend"
QH_SESSION = "https://centerapi.fx168api.com/app/common/v.js"
QH_API = "https://centerapi.fx168api.com/app/qh/api/spot/trend"
QH_QUERY = {"productId": ["11"], "pageNo": ["1"], "pageSize": ["50000"],
            "startDate": [""], "endDate": ["2050-01-01"], "appCategory": ["web"]}


def sge(capture, params):
    if params:
        raise ValueError("SGE daily report requires the source's whole current product window")
    url = SGE_URL
    # Old SDK added a numeric cache buster. Replay that exact receipt only;
    # do not weaken the generic URL binding or invent a POST receipt.
    if hasattr(capture, "result"):
        receipts = capture.result["receipts"]
        if len(receipts) != 1:
            raise ValueError("SGE daily report requires one complete response")
        candidate = receipts[0]["url"]
        split = urlsplit(candidate)
        query = parse_qs(split.query)
        if (candidate.split("?")[0] != SGE_URL or split.fragment
                or (query and (set(query) != {"_"} or len(query["_"]) != 1
                               or not re.fullmatch(r"\d+(?:\.\d+)?", query["_"][0])))):
            raise ValueError("SGE report receipt identity changed")
        url = candidate
    data = json.loads(capture.get(url))
    if [key["name"] for key in data["keys"]] != list(SGE_FIELDS):
        raise ValueError("SGE daily report field order changed")
    source_units = {key["name"]: key.get("unit", "") for key in data["keys"]}
    rows, excluded = [], []
    for day, values in data["values"].items():
        for index, value in enumerate(values):
            if not isinstance(value, list) or len(value) != len(SGE_FIELDS):
                raise ValueError("SGE report row shape changed")
            native = dict(zip(SGE_FIELDS, value, strict=True))
            if reviewed_date(day) != day or not isinstance(native["商品"], str) or not native["商品"].strip():
                excluded.append({"source_date": day, "source_row_index": index, "native": native,
                                 "reason": "invalid date or empty native product code; no repair"})
                continue
            rows.append(native | {"日期": day, "source_price_unit": PRICE_UNITS.get(native["商品"]),
                                   "source_price_unit_evidence": UNIT_PAGES.get(native["商品"]),
                                   "source_field_units": source_units,
                                   "source_scale_policy": "unknown scales remain native; no volume/percentage/alias conversion"})
    if not rows:
        raise ValueError("SGE report contains no identifiable daily observations")
    capture.dataset_metadata = {"complete_returned_window": True, "complete_history": False,
        "source_count": len(rows), "native_source_count": len(rows)+len(excluded),
        "quarantined_rows": excluded, "quarantined_count": len(excluded),
        "source_products": sorted({r["商品"] for r in rows}),
        "scope": "all named native products/dates; aliases kept separate; unidentified rows retained in quarantine",
        "units": PRICE_UNITS, "unknown_units": "volume, turnover, open interest, delivery volume and historical percentage scale",
        "source": "Jin10 SGE daily report; not the SGE benchmark fixing"}
    return rows


def qh_parse(page, body):
    from bs4 import BeautifulSoup
    script = BeautifulSoup(page, "lxml").select_one("script#__NEXT_DATA__")
    if script is None:
        raise ValueError("QH product metadata missing")
    groups = json.loads(script.text)["props"]["pageProps"]["data"]["varietyListData"]
    products = [p for group in groups for p in group["productList"] if p["productId"] == 11]
    if len(products) != 1:
        raise ValueError("QH rebar identity is ambiguous")
    product = products[0]
    expected = {"code": "rb", "name": "螺纹钢", "priceUnit": "元（人民币）/吨",
                "productId": 11, "qhExchangeName": "上海期货交易所", "quoteCode": "RBSH00"}
    if any(product.get(k) != v for k, v in expected.items()):
        raise ValueError("QH rebar product/quotation unit changed")
    data = json.loads(body)
    page = data["data"]
    values = page["list"]
    if (data.get("code") != 0 or page.get("hasNextPage") is not False
            or page.get("isLastPage") is not True or page.get("endRow") != len(values)
            or page.get("pageNum") != 1 or page.get("total") != len(values)):
        raise ValueError("QH report is incomplete or its pagination changed")
    rows = [r | {"日期": r["date"], "期货价格": r["fp"], "现货价格": r["sp"],
                 "source_product": product, "source_price_unit": "CNY/tonne"} for r in values]
    return rows, {"complete_returned_window": True, "complete_history": False,
        "source_count": len(rows), "source_product": product,
        "limitation": "RBSH00 is the provider's futures series; no invented delivery contract, grade or roll rule"}


def qh_urls(receipts):
    if len(receipts) != 3 or [r["url"] for r in receipts[:2]] != [QH_PAGE, QH_SESSION]:
        raise ValueError("QH product page/session/data evidence missing")
    url = receipts[2]["url"]
    if url.split("?")[0] != QH_API or parse_qs(urlsplit(url).query, keep_blank_values=True) != QH_QUERY:
        raise ValueError("QH rebar request identity/pagination changed")
    return [r["url"] for r in receipts]


def qh_replay(capture, params):
    if params != {"symbol": "螺纹钢"}:
        raise ValueError("only metadata-reviewed rebar is admitted")
    bodies = [capture.get(url) for url in qh_urls(capture.result["receipts"])]
    rows, capture.dataset_metadata = qh_parse(bodies[0], bodies[2])
    return rows


def qh_enrich(capture):
    qh_urls(capture.receipts)
    bodies = [gzip.decompress((capture.root/(r["sha256"]+".gz")).read_bytes()) for r in capture.receipts]
    rows, capture.dataset_metadata = qh_parse(bodies[0], bodies[2])
    return rows


def validate(rows, job, as_of, numeric, source_date):
    name = job["function"]
    if name not in {"macro_china_au_report", "spot_price_qh"}:
        return None
    keys, days = [], []
    for row in rows:
        day = source_date(row.get("日期"))
        if not day or day > as_of:
            raise ValueError("commodity date is invalid/future")
        days.append(day)
        if name == "macro_china_au_report":
            product = row["商品"]
            if (not isinstance(product, str) or not product.strip()
                    or row.get("source_price_unit") != PRICE_UNITS.get(product)
                    or row.get("source_price_unit_evidence") != UNIT_PAGES.get(product)):
                raise ValueError("SGE native contract or quotation unit mismatch")
            for field in SGE_FIELDS[1:]:
                if field != "交收方向":
                    numeric(row[field])
            keys.append((day, product))
        else:
            if row["source_product"]["productId"] != 11 or row.get("source_price_unit") != "CNY/tonne":
                raise ValueError("QH product identity/unit mismatch")
            for field, raw in [("现货价格", "sp"), ("期货价格", "fp")]:
                value = numeric(row[field])
                if value is None or value <= 0 or row[field] != row[raw]:
                    raise ValueError("QH price projection invalid")
            keys.append(day)
    if len(set(keys)) != len(rows):
        raise ValueError("duplicate native commodity observation")
    return {"contract_reviewed": True, "business_key": ["日期", "商品"] if name == "macro_china_au_report" else ["日期"],
        "source_rows": len(rows), "unique_keys": len(keys), "gaps": [], "latest_period": max(days),
        "latest_announcement": None, "frequency": "source_daily_release_overlap_revisions",
        "units": PRICE_UNITS if name == "macro_china_au_report" else {"现货价格": "CNY/tonne", "期货价格": "CNY/tonne"},
        "limitations": (["按原合约代码保留；未核报价单位、成交量额及涨跌幅尺度只提供原值，不换算",
                          "交易日是源日标签；不推定公告时间；别名不合并；空身份原行隔离保留"]
                         if name == "macro_china_au_report" else ["源期货代码RBSH00；不推定换月、具体交割月或现货规格"])}
