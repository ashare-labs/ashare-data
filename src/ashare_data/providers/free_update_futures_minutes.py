"""Native minute labels are not inferred trading dates or UTC timestamps."""

from datetime import datetime
import json
import re
from urllib.parse import urlencode

from ashare_data.providers.free_update_futures import PRODUCTS

FUNCTION = "futures_zh_minute_sina"
FIELDS = {
    "d": "datetime",
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "p": "hold",
}
PERIODS = {"1", "5", "15", "30", "60"}
TIME_GAPS = [
    "源分钟标签是区间起点或终点尚未证实，不能生成可交易时间戳",
    "夜盘自然日与交易日映射及节假日边界未核，交易日保持空值",
]


def request_identity(params):
    if set(params) != {"symbol", "period"} or params["period"] not in PERIODS:
        raise ValueError("futures minute requires explicit symbol and supported period")
    match = re.fullmatch(r"([A-Z]{1,3})(0|\d{3,4})", params["symbol"])
    if not match:
        raise ValueError("futures minute source symbol invalid")
    return match[1] + "0", "vendor_continuous" if match[2] == "0" else "delivery_symbol_unverified"


def label(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:00", value):
        raise ValueError("futures minute timestamp label must be exact source text")
    stamp = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    if not 1900 <= stamp.year <= 2200:
        raise ValueError("futures minute label year outside reviewed range")
    return stamp


def project(native, params):
    product, kind = request_identity(params)
    if not isinstance(native, dict) or set(native) != set(FIELDS):
        raise ValueError("futures minute native field schema changed")
    stamp = label(native["d"])
    row = {FIELDS[k]: value for k, value in native.items()}
    row.update(
        source_native=native,
        source_symbol=params["symbol"],
        source_period_minutes=int(params["period"]),
        identity_kind=kind,
        product_candidate=product,
        source_label_calendar_date=stamp.date().isoformat(),
        trading_date=None,
        timestamp=None,
        timestamp_timezone=None,
        bar_label_position=None,
        source_label_in_evening_or_early_morning=stamp.hour >= 18 or stamp.hour < 6,
    )
    return row


def parse(body, params):
    request_identity(params)
    text = body.decode("utf-8").strip()
    prefix = "/*<script>location.href='//sina.com';</script>*/"
    if text.startswith(prefix):
        text = text[len(prefix) :].strip()
    match = re.fullmatch(r"=\((\[.*\])\);?", text, re.S)
    if not match:
        raise ValueError("futures minute wrapper changed; no JavaScript execution")
    source = json.loads(match[1])
    if not isinstance(source, list) or not 1 <= len(source) <= 20000:
        raise ValueError("futures minute response empty or beyond bounded source window")
    rows = [project(native, params) for native in source]
    labels = [r["datetime"] for r in rows]
    if labels != sorted(set(labels)):
        raise ValueError("futures minute repeated/out-of-order source labels")
    return rows


def collect(capture, params):
    product, kind = request_identity(params)
    query = {"symbol": params["symbol"], "type": params["period"]}
    url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=/InnerFuturesNewService.getFewMinLine?"
        + urlencode(query)
    )
    rows = parse(capture.get(url), params)
    capture.dataset_metadata = {
        "source_count": len(rows),
        "pages_received": 1,
        "source_symbol": params["symbol"],
        "period_minutes": int(params["period"]),
        "first_source_time_label": rows[0]["datetime"],
        "last_source_time_label": rows[-1]["datetime"],
        "complete_history": False,
        "complete_returned_window": True,
        "complete_universe": False,
        "scope": "all bars returned by this bounded recent-window endpoint; no pagination or longer-history guarantee",
        "night_label_count": sum(r["source_label_in_evening_or_early_morning"] for r in rows),
        "timestamp_semantics_verified": False,
        "trading_date_verified": False,
        "identity_kind": kind,
        "product_candidate": product,
        "product_contract_candidate": PRODUCTS.get(product),
        "limitations": TIME_GAPS
        + ["供应商主连换月/复权与CMES等价未证实；交割合约不能只靠代码正则确认身份"],
    }
    return rows


def validate(rows, job, as_of, numeric):
    if job["function"] != FUNCTION:
        return None
    product, kind = request_identity(job["parameters"])
    if not rows:
        raise ValueError("empty minute rows cannot pass acceptance")
    keys = []
    for row in rows:
        if row != project(row["source_native"], job["parameters"]):
            raise ValueError("futures minute native projection/time inference changed")
        if label(row["datetime"]).date().isoformat() > as_of:
            raise ValueError("future minute calendar label")
        op, hi, lo, close = [numeric(row[f]) for f in ["open", "high", "low", "close"]]
        if (
            any(v is None or v <= 0 for v in [op, hi, lo, close])
            or not lo <= min(op, close) <= max(op, close) <= hi
        ):
            raise ValueError("futures minute invalid OHLC")
        for field in ["volume", "hold"]:
            value = numeric(row[field])
            if value is None or value < 0 or value != value.to_integral_value():
                raise ValueError("futures minute quantity is not a nonnegative integer")
        keys.append((row["source_symbol"], row["source_period_minutes"], row["datetime"]))
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate futures minute business key")
    return {
        "contract_reviewed": True,
        "acceptance_scope": "native OHLC and explicitly requested vendor symbol, period and unchanged time labels",
        "ready_for_trade_timestamp_backtest": False,
        "business_key": ["source_symbol", "source_period_minutes", "datetime"],
        "unique_keys": len(keys),
        "frequency": "source_recent_minute_window_and_revisions",
        "latest_period": None,
        "last_source_time_label": max(r["datetime"] for r in rows),
        "units": {**dict.fromkeys(["open", "high", "low", "close"], PRODUCTS.get(product, {}).get("price_unit", "source-native price; unit unverified")),
                  "volume": "source-native quantity; minute-specific single/double-sided convention unverified",
                  "hold": "source-native quantity; minute-specific counting convention unverified",
                  "datetime": "unaltered vendor time label; no UTC or trading-date conversion"},
        "gaps": [],
        "unresolved_field_semantics": TIME_GAPS
        + (["交割合约保留供应商请求代码；交易所生命周期未由本响应证明"] if kind != "vendor_continuous" else []),
        "night_label_count": sum(r["source_label_in_evening_or_early_morning"] for r in rows),
        "limitations": [
            "源自然日期只用于排查，永不作为交易日；没有把周五夜盘划到周五日线",
            "已明确的OHLC和原生时间标签可按源范围读取、续存；末根可能尚未结束，不能用未证实标签生成交易时点",
            "主连换月/复权与CMES等价未证明；保持独立来源标识，不直接拼接成同一价格序列",
            "保存实际接收时点和原文，未声称日线契约覆盖分钟；短回看窗口应及时留存",
        ],
    }
