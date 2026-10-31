"""Public THS industry ranking: bind native headers, industry and leader codes."""
import re

BASE = "http://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/"
HEADERS = ["序号", "行业", "行业指数", "涨跌幅", "流入资金(亿)", "流出资金(亿)", "净额(亿)",
           "公司家数", "领涨股", "涨跌幅", "当前价(元)"]
FIELDS = ["序号", "行业", "行业指数", "行业涨跌幅", "流入资金", "流出资金", "净额", "公司家数",
          "领涨股", "领涨股涨跌幅", "领涨股当前价"]
UNITS = {"行业指数": "provider index points", "行业涨跌幅": "percent", "流入资金": "100000000 CNY",
         "流出资金": "100000000 CNY", "净额": "100000000 CNY", "公司家数": "companies",
         "领涨股涨跌幅": "percent", "领涨股当前价": "CNY"}


def parse(body):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body.decode("gb18030"), "lxml")
    table, pager = soup.select_one("table"), soup.select_one("span.page_info")
    if table is None or pager is None:
        raise ValueError("THS industry ranking/table pagination missing; no challenge execution")
    headers = [x.get_text(strip=True) for x in table.select("thead th")]
    if headers != HEADERS:
        raise ValueError("THS industry headers/units changed")
    match = re.fullmatch(r"(\d+)/(\d+)", pager.get_text(strip=True))
    if not match or not 1 <= int(match[1]) <= int(match[2]) <= 10:
        raise ValueError("THS industry pagination invalid/outside request budget")
    rows = []
    for tr in table.select("tbody tr"):
        cells = tr.select("td")
        if len(cells) != len(FIELDS):
            raise ValueError("THS industry row schema changed")
        industry, leader = cells[1].select_one("a"), cells[8].select_one("a")
        industry_code = re.fullmatch(r"https?://q\.10jqka\.com\.cn/thshy/detail/code/(\d{6})/?", industry["href"] if industry else "")
        leader_code = re.fullmatch(r"https?://stockpage\.10jqka\.com\.cn/(\d{6})/?", leader["href"] if leader else "")
        if not industry_code or not leader_code:
            raise ValueError("THS industry/leader code not present in source links")
        native = dict(zip(FIELDS, [x.get_text(strip=True) for x in cells], strict=True))
        rows.append(native | {"行业代码": industry_code[1], "领涨股代码": leader_code[1], "source_units": UNITS})
    return rows, int(match[1]), int(match[2])


def collect(capture, params):
    if params != {"symbol": "即时"}:
        raise ValueError("THS industry entry supports current snapshot only")
    first = capture.get(BASE+"ajax/1/free/1/")
    rows, page, total = parse(first)
    if page != 1:
        raise ValueError("THS industry first page mismatch")
    # A legacy SDK fetched page 1 twice. Consume and verify that recorded
    # duplicate without making the unnecessary request in a live collection.
    legacy_duplicate = False
    if hasattr(capture, "result") and len(capture.result["receipts"]) == total+1:
        duplicate = capture.get(BASE+"page/1/ajax/1/free/1/")
        if duplicate != first:
            raise ValueError("THS duplicate legacy discovery page differs")
        legacy_duplicate = True
    for index in range(2, total+1):
        part, page, declared = parse(capture.get(BASE+f"page/{index}/ajax/1/free/1/"))
        if page != index or declared != total:
            raise ValueError("THS industry page sequence changed")
        rows.extend(part)
    ranks = [int(row["序号"]) for row in rows]
    if ranks != list(range(1, len(rows)+1)) or len({r["行业代码"] for r in rows}) != len(rows):
        raise ValueError("THS industry pages have missing/duplicate identities or ranks")
    capture.dataset_metadata = {"complete_returned_window": True, "complete_history": False,
        "source_count": len(rows), "pages": total, "legacy_duplicate_page_verified": legacy_duplicate,
        "scope": "current THS industry ranking and leading stock; received time is observation time, not a source trading date",
        "units": UNITS, "headers": HEADERS}
    return rows


def validate(rows, job, as_of, numeric, source_date):
    if job["function"] != "stock_fund_flow_industry":
        return None
    for row in rows:
        if row.get("source_units") != UNITS or not re.fullmatch(r"\d{6}", row["行业代码"]) or not re.fullmatch(r"\d{6}", row["领涨股代码"]):
            raise ValueError("THS ranking unit/identity mismatch")
        for field in UNITS:
            value = numeric(row[field].removesuffix("%"))
            if value is None:
                raise ValueError("THS ranking required numeric field missing")
        # Native amounts are independently rounded to 0.01 hundred-million.
        if abs(numeric(row["流入资金"])-numeric(row["流出资金"])-numeric(row["净额"])) > numeric("0.02"):
            raise ValueError("THS industry fund-flow amount columns do not reconcile")
    if len({r["行业代码"] for r in rows}) != len(rows):
        raise ValueError("duplicate THS industry identity")
    return {"contract_reviewed": True, "business_key": ["行业代码", "_snapshot_date"],
        "source_rows": len(rows), "unique_keys": len(rows), "gaps": [], "units": UNITS,
        "frequency": "current_snapshot_daily_retention", "latest_period": None, "latest_announcement": None,
        "limitations": ["按实际接收日期留存当前行业榜；页面无独立交易日，不伪造日期",
                        "主力流入流出为同花顺自身口径；领涨股保留原链接代码，不用名称推身份"]}
