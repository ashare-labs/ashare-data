"""Bounded SAFE central parity windows without repeated historical XLSX reads."""
from datetime import datetime
from decimal import Decimal

ENDPOINT = "https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do"
COLUMNS = "日期 美元 欧元 日元 港元 英镑 澳元 新西兰元 新加坡元 瑞士法郎 加元 澳门元 林吉特 卢布 兰特 韩元 迪拉姆 里亚尔 福林 兹罗提 丹麦克朗 瑞典克朗 挪威克朗 里拉 比索 泰铢".split()


def bounds(params):
    if set(params) != {"start_date", "end_date"}:
        raise ValueError("SAFE requires explicit fixing date bounds")
    start, end = [datetime.strptime(params[k], "%Y%m%d").date() for k in ("start_date", "end_date")]
    if not 0 <= (end-start).days <= 366:
        raise ValueError("SAFE source query exceeds one year")
    return start.isoformat(), end.isoformat()


def parse(body, params):
    from bs4 import BeautifulSoup
    start, end = bounds(params)
    soup = BeautifulSoup(body, "lxml")
    for field, expected in {"startDate": start, "endDate": end, "queryYN": "true"}.items():
        element = soup.find("input", attrs={"name": field})
        if element is None or element.get("value") != expected:
            raise ValueError("SAFE response does not echo requested date bounds")
    matches = []
    for table in soup.find_all("table"):
        lines = table.find_all("tr")
        if not lines:
            continue
        header = [c.get_text(" ", strip=True) for c in lines[0].find_all(["td", "th"], recursive=False)]
        if header == COLUMNS:
            matches.append(lines[1:])
    if len(matches) != 1:
        raise ValueError("SAFE fixing table/quotation columns ambiguous")
    rows = []
    for line in matches[0]:
        values = [c.get_text(" ", strip=True) for c in line.find_all(["td", "th"], recursive=False)]
        if len(values) != len(COLUMNS):
            raise ValueError("SAFE fixing row has unexpected columns")
        day = datetime.strptime(values[0], "%Y-%m-%d").date().isoformat()
        if not start <= day <= end:
            raise ValueError("SAFE returned a fixing outside requested dates")
        numbers = []
        for value in values[1:]:
            if value in {"", "-", "--"}:
                numbers.append(None)
            else:
                number = Decimal(value)
                if not number.is_finite() or number <= 0:
                    raise ValueError("invalid SAFE currency fixing")
                numbers.append(str(number))
        rows.append(dict(zip(COLUMNS, [day, *numbers], strict=True)))
    if not rows or len({r["日期"] for r in rows}) != len(rows):
        raise ValueError("SAFE source window empty or has duplicate fixing dates")
    return rows


def collect(capture, params):
    start, end = bounds(params)
    body = capture.post(ENDPOINT, {"startDate": start, "endDate": end, "queryYN": "true"})
    rows = parse(body, params)
    capture.dataset_metadata = {"source_count": len(rows), "complete_returned_window": True,
        "complete_history": False, "requested_start": start, "requested_end": end,
        "earliest_fixing": min(r["日期"] for r in rows), "latest_fixing": max(r["日期"] for r in rows),
        "scope": "all SAFE published central parity fixings in explicit requested window",
        "publication_at": None, "historical_excel_downloaded": False}
    return rows
