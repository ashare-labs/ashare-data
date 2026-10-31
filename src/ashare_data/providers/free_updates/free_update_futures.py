"""Named Sina daily bars and reproducible calendar aggregation, without JS execution.

Product units are reviewed separately. A vendor continuous symbol is not an
executable delivery contract; navigation groups are not exchange identities.
"""
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re

from ashare_data.providers.free_updates import digest

FUNCTIONS = {"futures_main_sina", "futures_resample"}
FIELDS = {"d": "日期", "o": "开盘价", "h": "最高价", "l": "最低价", "c": "收盘价",
          "v": "成交量", "p": "持仓量", "s": "动态结算价"}
PRODUCTS = json.loads(Path(__file__).with_name("futures-product-contracts.json").read_text())["products"]


def exact_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{8}", value):
        raise ValueError("futures date requires full ISO date or YYYYMMDD")
    day = date.fromisoformat(value)
    if not 1900 <= day.year <= 2200:
        raise ValueError("futures date year outside reviewed range")
    return day.isoformat()


def bucket(day, period):
    value = date.fromisoformat(day)
    if period == "W-FRI":
        end = value + timedelta(days=(4-value.weekday()) % 7)
        return (end-timedelta(days=6)).isoformat(), end.isoformat()
    if period == "ME":
        return value.replace(day=1).isoformat(), value.replace(day=monthrange(value.year, value.month)[1]).isoformat()
    raise ValueError("unreviewed futures aggregation period")


def daily_rows(body, symbol):
    text = body.decode("utf-8")
    # Strip only the provider's inert anti-embedding comment, never evaluate it.
    prefix = "/*<script>location.href='//sina.com';</script>*/"
    if text.startswith(prefix):
        text = text[len(prefix):].strip()
    match = re.fullmatch(r"var _"+re.escape(symbol)+r"2021_08_17=\((\[.*\])\);?", text, re.S)
    if not match:
        raise ValueError("futures callback/schema changed; no JavaScript evaluation")
    source = json.loads(match[1])
    if not isinstance(source, list) or not 1 <= len(source) <= 20000:
        raise ValueError("futures source empty or exceeds bounded daily history")
    rows = []
    for native in source:
        if not isinstance(native, dict) or set(native) != set(FIELDS):
            raise ValueError("futures native daily fields changed")
        row = {FIELDS[k]: v for k, v in native.items()}
        row.update(日期=exact_day(native["d"]), source_symbol=symbol, exchange=PRODUCTS.get(symbol, {}).get("exchange"),
                   continuous_definition="Sina native main continuous; roll/adjustment equivalence to CMES unverified")
        row["settlement_zero_placeholder"] = Decimal(str(native["s"])) == 0
        rows.append(row)
    if len({r["日期"] for r in rows}) != len(rows):
        raise ValueError("duplicate futures native trading dates")
    return sorted(rows, key=lambda r: r["日期"])


def aggregate(rows, period, end_date, first_source_date):
    groups = {}
    for row in rows:
        groups.setdefault(bucket(row["日期"], period), []).append(row)
    output = []
    for (start, end), group in sorted(groups.items()):
        first, last = group[0], group[-1]
        output.append({"source_symbol": first["source_symbol"], "exchange": first["exchange"],
            "continuous_definition": first["continuous_definition"], "日期": last["日期"],
            "周期开始": start, "周期结束": end, "周期未结束": end >= end_date,
            "source_left_boundary_truncated": first_source_date > start,
            "source_daily_dates": [r["日期"] for r in group], "source_daily_sha256": digest(group),
            "开盘": first["开盘价"], "最高": max((r["最高价"] for r in group), key=lambda v: Decimal(str(v))),
            "最低": min((r["最低价"] for r in group), key=lambda v: Decimal(str(v))), "收盘": last["收盘价"],
            "成交量": str(sum(Decimal(str(r["成交量"])) for r in group)), "持仓量": last["持仓量"]})
    return output


def market_values(row, prices, numeric):
    op, hi, lo, close = [numeric(row.get(k)) for k in prices]
    if any(v is None or v <= 0 for v in (op, hi, lo, close)) or not lo <= min(op, close) <= max(op, close) <= hi:
        raise ValueError("futures OHLC order/value invalid")
    for field in ("成交量", "持仓量"):
        value = numeric(row.get(field))
        if value is None or value < 0 or value != value.to_integral_value():
            raise ValueError("futures volume/open interest must be native integer contract counts")


def collect(capture, name, params):
    expected = {"symbol", "start_date", "end_date"} | ({"period"} if name == "futures_resample" else set())
    if name not in FUNCTIONS or set(params) != expected or not re.fullmatch(r"[A-Z]{1,3}0", params["symbol"]):
        raise ValueError("daily continuous request must explicitly bind symbol and date window")
    symbol = params["symbol"]
    start, end = exact_day(params["start_date"]), exact_day(params["end_date"])
    if start > end:
        raise ValueError("futures date window reversed")
    period = params.get("period")
    effective_start = bucket(start, period)[0] if period else start
    url = (f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{symbol}2021_08_17="
           f"/InnerFuturesNewService.getDailyKLine?symbol={symbol}&_=2021_08_17")
    all_rows = daily_rows(capture.get(url), symbol)
    rows = [r for r in all_rows if effective_start <= r["日期"] <= end]
    # Validate every input bar before aggregation can hide a bad price or quantity.
    from ashare_data.providers.free_updates.free_update_semantics import numeric
    for row in rows:
        market_values(row, ["开盘价", "最高价", "最低价", "收盘价"], numeric)
        settlement = numeric(row["动态结算价"])
        if settlement is None or settlement < 0:
            raise ValueError("futures source settlement missing/negative")
    if period:
        rows = aggregate(rows, period, end, all_rows[0]["日期"])
    capture.dataset_metadata = {"source_count": len(all_rows), "pages_received": 1, "returned_rows": len(rows),
        "source_first_date": all_rows[0]["日期"], "source_last_date": all_rows[-1]["日期"],
        "requested_window": {"start_date": start, "end_date": end}, "effective_start_date": effective_start,
        "complete_returned_daily_window": True, "complete_returned_window": True,
        "unique_observations": len(rows), "complete_universe": False,
        "complete_history": start <= all_rows[0]["日期"] and end >= all_rows[-1]["日期"],
        "product_contract": PRODUCTS.get(symbol), "aggregation": period,
        "time_basis": "source daily trading-date label, Asia/Shanghai; no minute/night-session date inference",
        "unit_evidence": PRODUCTS.get(symbol, {}).get("sources", []),
        "limitation": "vendor continuous series only; original source history and same-day bars may be revised; CMES roll equivalence not established"}
    return rows


def validate(rows, job, as_of, numeric):
    if job["function"] not in FUNCTIONS:
        return None
    params = job["parameters"]
    symbol, period = params["symbol"], params.get("period")
    definition = PRODUCTS.get(symbol)
    if definition is None:
        return {"contract_reviewed": False, "gaps": ["期货品种的交易所、单位及身份尚未逐项审定"]}
    keys, days = [], []
    gaps = []
    prices = ["开盘", "最高", "最低", "收盘"] if period else ["开盘价", "最高价", "最低价", "收盘价"]
    for row in rows:
        day = exact_day(row.get("日期"))
        if row.get("source_symbol") != symbol or row.get("exchange") != definition["exchange"] or day > as_of:
            raise ValueError("futures source identity/exchange/trading date mismatch")
        if row.get("continuous_definition") != "Sina native main continuous; roll/adjustment equivalence to CMES unverified":
            raise ValueError("futures continuous construction cannot be silently changed")
        market_values(row, prices, numeric)
        if period:
            start, end = bucket(day, period)
            source_days = row.get("source_daily_dates", [])
            if (row.get("周期开始"), row.get("周期结束")) != (start, end) or row.get("周期未结束") != (end >= exact_day(params["end_date"])):
                raise ValueError("futures period boundary/provisional flag mismatch")
            if not source_days or source_days != sorted(set(source_days)) or source_days[-1] != day or any(not start <= exact_day(d) <= day for d in source_days):
                raise ValueError("futures aggregate source dates missing/duplicated/outside bucket")
            if row.get("source_left_boundary_truncated"):
                gaps.append("来源首周期缺少完整左边界；不得称该周期完整覆盖")
            keys.append((symbol, start, end))
        else:
            settlement = numeric(row.get("动态结算价"))
            if settlement is None or settlement < 0 or row.get("settlement_zero_placeholder") != (settlement == 0):
                raise ValueError("futures dynamic settlement missing/placeholder mismatch")
            keys.append((symbol, day))
        days.append(day)
    if not rows or len(keys) != len(set(keys)):
        raise ValueError("futures empty or duplicate business keys")
    units = {**dict.fromkeys(prices, definition["price_unit"]), "成交量": "source contract counts (hands)", "持仓量": "source contract counts (hands)"}
    if not period:
        units["动态结算价"] = definition["price_unit"]+"; native zero is an unavailable placeholder, not a traded price"
    return {"contract_reviewed": True, "business_key": ["source_symbol", "周期开始", "周期结束"] if period else ["source_symbol", "日期"],
        "unique_keys": len(keys), "units": units, "frequency": "daily source bars and revisions; derived "+(period or "daily"),
        "latest_period": max(days), "latest_announcement": None, "source_rows": len(rows), "product_contract": definition,
        "gaps": sorted(set(gaps)), "limitations": ["仅显式品种和请求窗口；未完成全品种与全部交割合约覆盖",
            "交易日期沿用新浪日线标签，时区上海；未推断分钟夜盘所属交易日",
            "周/月由原生日线完整日历桶聚合；末周期可修订，源零结算占位原样保留",
            "主连不是可交易交割合约；换月与复权构造未证明等价CMES，不填补跳空",
            "源成交量及持仓为手，保留历史原统计口径，不按合约吨数换成成交额或统一单双边统计"]}
