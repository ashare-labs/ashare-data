"""Bounded, resumable pages from reviewed exchange and public-market sources.

No source JS, authentication cookies or challenge tokens are executed/generated.
Every page is an independently timed observation, never an atomic whole-market
snapshot. Source-native text is retained alongside explicit numeric units.
"""
from datetime import datetime, timedelta
from decimal import Decimal
import json
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup


def amount(text):
    text = text.strip().replace(",", "")
    if text in {"", "--", "-"}:
        return None
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(万|亿)?", text)
    if not match:
        raise ValueError("unrecognized monetary quantity: " + text)
    scale = {None: 1, "万": 10000, "亿": 100000000}[match[2]]
    return float(Decimal(match[1]) * scale)


def parse_fund_flow(body, page):
    soup = BeautifulSoup(body.decode("gb18030"), "lxml")
    table = soup.select_one("table.J-ajax-table")
    info = soup.select_one("span.page_info")
    if table is None or info is None:
        raise ValueError("THS public fund-flow table/page metadata absent")
    current, total = map(int, info.get_text(strip=True).split("/"))
    if current != page or not 1 <= page <= total:
        raise ValueError("fund-flow source returned a different page")
    expected = ["序号", "股票代码", "股票简称", "最新价", "涨跌幅", "换手率",
                "流入资金(元)", "流出资金(元)", "净额(元)", "成交额(元)"]
    if [h.get_text(strip=True) for h in table.select("th")] != expected:
        raise ValueError("fund-flow source columns/units changed")
    rows = []
    for tr in table.select("tbody tr"):
        values = [td.get_text(strip=True) for td in tr.select("td")]
        if len(values) != len(expected):
            raise ValueError("fund-flow row width changed")
        row = dict(zip(expected, values))
        row["source_text"] = dict(row)
        for column in expected[3:]:
            value = row[column]
            if column in {"涨跌幅", "换手率"}:
                if value not in {"--", "-"} and not value.endswith("%"):
                    raise ValueError("fund-flow percent suffix absent")
                value = value.removesuffix("%")
            row[column] = amount(value)
        rows.append(row)
    return rows, {"page": page, "total_pages": total, "next_page": page + 1 if page < total else None,
        "complete_universe": total == 1, "page_order": "stock_code_desc",
        "publication_time": "not supplied; actual receipt time only",
        "atomic_snapshot": False, "scope": "current public fund-flow page"}


def parse_carbon(body, page):
    soup = BeautifulSoup(body.decode("utf-8"), "lxml")
    table = soup.select_one("table.jyhq_tab")
    if table is None:
        raise ValueError("Beijing carbon table absent")
    headers = [h.get_text(strip=True) for h in table.select("th")]
    if headers != ["日期", "成交量(吨)", "成交均价(元/吨)", "成交额(元)"]:
        raise ValueError("carbon table columns/units changed")
    current = re.search(r'currentpage\s*=\s*"(\d+)"', str(table))
    total = re.search(r'totalpage\s*=\s*"(\d+)"', str(table))
    if not current or not total or int(current[1]) != page:
        raise ValueError("carbon source page identity absent/mismatched")
    total = int(total[1])
    if not 1 <= page <= total:
        raise ValueError("carbon page outside source coverage")
    rows = []
    for tr in table.select("tr"):
        values = [td.get_text(strip=True) for td in tr.select("td")]
        if not values:
            continue
        if len(values) != 4:
            raise ValueError("carbon row width changed")
        matched = re.fullmatch(r"([\d,.]+)\(([^()]+)\)", values[3])
        if not matched:
            raise ValueError("carbon product code/amount absent")
        rows.append({"日期": values[0], "成交量": amount(values[1]),
                     "成交均价": amount(values[2]), "成交额": amount(matched[1]),
                     "产品": matched[2], "source_text": dict(zip(headers, values))})
    return rows, {"page": page, "total_pages": total, "next_page": page + 1 if page < total else None,
        "complete_history": total == 1, "units": {"成交量": "tonne", "成交均价": "CNY/tonne", "成交额": "CNY"},
        "complete_returned_window": True, "source_count": len(rows),
        "source_window": "all rows on the explicitly requested page; other historical pages are not collected",
        "publication_time": "trading date only; historical first-publication timestamp unavailable"}


def collect(capture, name, params):
    if name == "fund_etf_scale_sse":
        requested = datetime.strptime(params["date"], "%Y%m%d").date()
        # A weekend or an unpublished same-day snapshot is not a schema error.
        for lag in range(5):
            day = (requested - timedelta(days=lag)).isoformat()
            query = {"isPagination": "true", "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1",
                     "pageHelp.beginPage": "1", "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
                     "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L", "STAT_DATE": day}
            data = json.loads(capture.get("https://query.sse.com.cn/commonQuery.do?" + urlencode(query),
                                          {"Referer": "https://www.sse.com.cn/"}))
            rows = data.get("result")
            if rows is None or rows == []:
                continue
            if not isinstance(rows, list) or int(data["pageHelp"]["total"]) != len(rows):
                raise ValueError("SSE ETF response truncated/invalid; split source pagination")
            fields = {"SEC_CODE": "基金代码", "SEC_NAME": "基金简称", "ETF_TYPE": "ETF类型",
                      "STAT_DATE": "统计日期", "TOT_VOL": "基金份额"}
            output = []
            for row in rows:
                item = {label: row[key] for key, label in fields.items()}
                if str(item["统计日期"])[:10] != day:
                    raise ValueError("SSE ETF date mismatch")
                item["基金份额"] = float(Decimal(str(row["TOT_VOL"])) * 10000)
                item["source_shares_10k"] = row["TOT_VOL"]
                output.append(item)
            capture.dataset_metadata = {"requested_date": requested.isoformat(), "statistical_date": day,
                "fallback_calendar_days": lag, "complete_universe": True, "exchange": "SSE",
                "complete_returned_window": True, "source_count": len(output),
                "source_window": "complete official current ETF file for the published statistics date",
                "historical_api_required": False,
                "units": {"基金份额": "shares", "source_shares_10k": "10000 shares"}}
            return output
        raise ValueError("no SSE ETF snapshot in bounded five-day publication window")
    page = int(params["page"])
    if not 1 <= page <= 10000:
        raise ValueError("source page outside allowed range")
    if name == "stock_fund_flow_individual":
        if params["symbol"] != "即时":
            raise ValueError("only reviewed current fund-flow table is supported")
        url = f"http://data.10jqka.com.cn/funds/ggzjl/field/code/order/desc/page/{page}/ajax/1/free/1/"
        rows, coverage = parse_fund_flow(capture.get(url), page)
    else:
        url = "https://www.bjets.com.cn/article/jyxx/" + (f"?{page}" if page > 1 else "")
        rows, coverage = parse_carbon(capture.get(url), page)
    capture.dataset_metadata = coverage
    return rows
