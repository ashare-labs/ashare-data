"""Eastmoney board daily bars addressed by a previously resolved native code.

Directory discovery is separate from daily updates. A directory failure must
not prevent an already identified board's original quote endpoint being used.
"""

from datetime import datetime
import json
import re
from urllib.parse import urlencode

FUNCTIONS = {"stock_board_concept_hist_em", "stock_board_industry_hist_em"}
FIELDS = [
    "日期",
    "开盘",
    "收盘",
    "最高",
    "最低",
    "成交量",
    "成交额",
    "振幅",
    "涨跌幅",
    "涨跌额",
    "换手率",
]


def parameters(name, params):
    if name not in FUNCTIONS or set(params) != {"symbol", "start_date", "end_date"}:
        raise ValueError("board bars require explicit code and dates")
    if not re.fullmatch(r"BK\d{4}", str(params["symbol"])):
        raise ValueError("resolved native BK board code required; no directory lookup")
    start, end = [
        datetime.strptime(params[k], "%Y%m%d").date().isoformat()
        for k in ("start_date", "end_date")
    ]
    if start > end:
        raise ValueError("board date window is reversed")
    return start, end


def request_url(name, params):
    parameters(name, params)
    host = (
        "91.push2his.eastmoney.com"
        if name == "stock_board_concept_hist_em"
        else "7.push2his.eastmoney.com"
    )
    return (
        "https://"
        + host
        + "/api/qt/stock/kline/get?"
        + urlencode(
            {
                "secid": "90." + params["symbol"],
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": "0",
                "beg": params["start_date"],
                "end": params["end_date"],
                "smplmt": "10000",
                "lmt": "1000000",
            }
        )
    )


def collect(capture, name, params):
    start, end = parameters(name, params)
    payload = json.loads(capture.get(request_url(name, params)))
    data = payload.get("data") or {}
    if (
        payload.get("rc") != 0
        or data.get("code") != params["symbol"]
        or data.get("market") != 90
        or not data.get("name")
    ):
        raise ValueError("board source identity or response status differs")
    rows = []
    for line in data.get("klines", []):
        cells = line.split(",")
        if len(cells) != len(FIELDS):
            raise ValueError("board native field count changed")
        row = dict(zip(FIELDS, cells))
        if not start <= row["日期"] <= end:
            raise ValueError("board returned date outside requested window")
        rows.append(
            row
            | {
                "板块代码": data["code"],
                "板块名称": data["name"],
                "板块类型": "concept" if "concept" in name else "industry",
                "复权": "unadjusted",
            }
        )
    if not rows or len({r["日期"] for r in rows}) != len(rows):
        raise ValueError("board window empty or duplicate source dates")
    rows.sort(key=lambda r: r["日期"])
    capture.dataset_metadata = {
        "source": request_url(name, params),
        "source_count": len(rows),
        "source_code": data["code"],
        "source_name": data["name"],
        "complete_returned_window": True,
        "complete_history": False,
        "start_date": start,
        "end_date": end,
        "pages_received": 1,
        "source_total_history_count": data.get("dktotal"),
        "adjustment": "unadjusted",
        "units": {
            "OHLC": "index_points",
            "成交量": "native f56; scale not converted",
            "成交额": "native f57; scale not converted",
            "percent_fields": "native percentage points",
        },
        "scope": "all bars returned for the named board and date window; not the entire board universe",
    }
    return rows


def validate(rows, job, as_of, numeric):
    name, params = job["function"], job["parameters"]
    if name not in FUNCTIONS:
        return None
    start, end = parameters(name, params)
    if end > as_of or not rows:
        raise ValueError("future or empty board window")
    dates = []
    for row in rows:
        day = datetime.strptime(row["日期"], "%Y-%m-%d").date().isoformat()
        if (
            row["板块代码"] != params["symbol"]
            or not start <= day <= end
            or row["复权"] != "unadjusted"
        ):
            raise ValueError("board code, adjustment or date differs")
        values = {k: numeric(row[k]) for k in FIELDS[1:]}
        if any(values[k] is None for k in ["开盘", "收盘", "最高", "最低"]):
            raise ValueError("board OHLC missing")
        if (
            not 0
            < values["最低"]
            <= min(values["开盘"], values["收盘"])
            <= max(values["开盘"], values["收盘"])
            <= values["最高"]
        ):
            raise ValueError("invalid board OHLC bounds")
        if any(values[k] is not None and values[k] < 0 for k in ["成交量", "成交额"]):
            raise ValueError("negative board quantity")
        dates.append(day)
    if len(set(dates)) != len(rows):
        raise ValueError("duplicate board date")
    return {
        "contract_reviewed": True,
        "business_key": ["板块类型", "板块代码", "日期"],
        "gaps": [],
        "unique_keys": len(rows),
        "source_rows": len(rows),
        "latest_period": max(dates),
        "latest_announcement": None,
        "frequency": "trading_day",
        "units": {
            "OHLC": "index_points",
            "成交量": "unconverted native f56",
            "成交额": "unconverted native f57",
        },
        "limitations": [
            "volume and turnover kept in raw source scale; no CMES unit conversion",
            "explicit boards and current date window; directory coverage tracked separately",
        ],
    }
