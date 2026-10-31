"""Native THS index bars from its public static quote files.

Resolved index codes do not depend on the separately served board directory.
No challenge cookies or scripts are generated. Annual and current-day files
are retained independently; current-day data may revise the same daily bar.
"""

from datetime import datetime
import json
import re

FUNCTIONS = {"stock_board_concept_index_ths", "stock_board_industry_index_ths"}
FIELDS = ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]
TODAY_FIELDS = ["1", "7", "8", "9", "11", "13", "19"]


def parameters(name, params):
    if name not in FUNCTIONS or set(params) != {"symbol", "start_date", "end_date"}:
        raise ValueError("THS bars require a native index code and explicit dates")
    if not re.fullmatch(r"\d{6}", str(params["symbol"])):
        raise ValueError("THS native six-digit index code required; no directory lookup")
    start, end = [
        datetime.strptime(params[k], "%Y%m%d").date().isoformat()
        for k in ("start_date", "end_date")
    ]
    if start > end:
        raise ValueError("THS date window is reversed")
    return start, end


def request_url(code, period):
    if not re.fullmatch(r"\d{6}", str(code)) or not re.fullmatch(r"\d{4}|today", str(period)):
        raise ValueError("invalid static index identity")
    return f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/{period}.js"


def decode(body, code, period):
    prefix = f"quotebridge_v4_line_bk_{code}_01_{period}("
    text = body.decode("utf-8").strip().removesuffix(";").rstrip()
    if not text.startswith(prefix) or not text.endswith(")"):
        raise ValueError("THS callback identity differs from requested code/period")
    value = json.loads(text[len(prefix):-1])
    if not isinstance(value, dict):
        raise ValueError("THS static payload is not an object")
    return value


def day(value):
    if not re.fullmatch(r"\d{8}", str(value)):
        raise ValueError("THS source date format changed")
    return datetime.strptime(str(value), "%Y%m%d").date().isoformat()


def collect(capture, name, params):
    start, end = parameters(name, params)
    code = params["symbol"]
    history, urls, received = {}, [], 0
    url = request_url(code, "today")
    current = decode(capture.get(url), code, "today")
    urls.append(url)
    if set(current) != {"bk_" + code}:
        raise ValueError("THS current index identity mismatch")
    quote = current["bk_" + code]
    if not quote.get("name") or any(key not in quote for key in TODAY_FIELDS):
        raise ValueError("THS current quote name or field missing")
    current_date = day(quote["1"])
    if current_date > end:
        raise ValueError("THS current quote is after requested as-of")
    # The current quote identifies the latest published year. On New Year
    # holidays, do not request an annual file for a year with no new bar.
    for year in range(int(start[:4]), int(current_date[:4]) + 1):
        url = request_url(code, year)
        payload = decode(capture.get(url), code, year)
        urls.append(url)
        if not isinstance(payload.get("data"), str):
            raise ValueError("THS annual data field missing")
        for line in filter(None, payload["data"].split(";")):
            cells = line.split(",")
            if len(cells) != 11:
                raise ValueError("THS annual native field count changed")
            date = day(cells[0])
            if date[:4] != str(year) or date > end or date in history:
                raise ValueError("THS annual date/duplicate differs from current requested window")
            history[date] = dict(zip(FIELDS, [date, *cells[1:7]])) | {
                "native_extra_fields": cells[7:],
                "source_file": "annual",
            }
            received += 1
    date = current_date
    # Prefer the explicitly current file on a repeated source date. Preserve
    # both original responses, and leave its native dt/finality uninterpreted.
    if start <= date <= end:
        history[date] = dict(zip(FIELDS, [date, *(quote[k] for k in TODAY_FIELDS[1:])])) | {
            "native_extra_fields": {k: v for k, v in quote.items() if k not in TODAY_FIELDS},
            "source_file": "today",
        }
    rows = [history[d] | {
        "板块代码": code,
        "板块名称": quote["name"],
        "板块类型": "concept" if "concept" in name else "industry",
        "价格口径": "source_native_index",
    } for d in sorted(history) if start <= d <= end]
    if not rows:
        raise ValueError("THS source has no bars in requested window")
    capture.dataset_metadata = {
        "source": urls,
        "source_count": len(rows),
        "annual_rows_received": received,
        "current_source_date": date,
        "source_code": code,
        "source_name": quote["name"],
        "complete_returned_window": True,
        "complete_history": False,
        "pages_received": len(urls),
        "start_date": start,
        "end_date": end,
        "quote_finality": "not_attested",
        "units": {"OHLC": "index_points", "成交量": "native field 13, unconverted", "成交额": "native field 19, unconverted"},
        "scope": "named native index, source-visible current date window; current name only",
    }
    return rows


def validate(rows, job, as_of, numeric):
    name, params = job["function"], job["parameters"]
    if name not in FUNCTIONS:
        return None
    start, end = parameters(name, params)
    if end != as_of or not rows:
        raise ValueError("THS static entry requires a nonempty current as-of window")
    dates = []
    for row in rows:
        date = datetime.strptime(row["日期"], "%Y-%m-%d").date().isoformat()
        if (row["板块代码"] != params["symbol"] or not start <= date <= end
                or row["板块类型"] != ("concept" if "concept" in name else "industry")
                or row["价格口径"] != "source_native_index"):
            raise ValueError("THS index identity, price basis or date differs")
        values = {k: numeric(row[k]) for k in FIELDS[1:]}
        if any(v is None for v in values.values()):
            raise ValueError("THS quote numeric field missing")
        if not 0 < values["最低"] <= min(values["开盘"], values["收盘"]) <= max(values["开盘"], values["收盘"]) <= values["最高"]:
            raise ValueError("THS index OHLC bounds invalid")
        if values["成交量"] < 0 or values["成交额"] < 0:
            raise ValueError("THS negative quantity")
        dates.append(date)
    if len(set(dates)) != len(rows):
        raise ValueError("THS duplicate daily index key")
    return {
        "contract_reviewed": True,
        "business_key": ["板块类型", "板块代码", "日期"],
        "gaps": [], "unique_keys": len(rows), "source_rows": len(rows),
        "latest_period": max(dates), "latest_announcement": None,
        "frequency": "trading_day",
        "units": {"OHLC": "index_points", "成交量": "native field 13, unconverted", "成交额": "native field 19, unconverted"},
        "limitations": ["explicit native index codes; directory coverage is independent",
                        "native volume/amount retained without guessed scale conversion",
                        "current-day bar finality and native dt are not inferred"],
    }
