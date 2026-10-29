"""Explicit public equity sources reused from the daily-data provider contracts."""
import calendar
from datetime import datetime, timedelta
from decimal import Decimal
import json
import re

from ashare_data.providers import daily_sources


class CapturedDailyHTTP:
    def __init__(self, capture):
        self.capture = capture

    def get(self, url, referer=None):
        before = len(self.capture.receipts)
        body = self.capture.get(url, {"Referer":referer} if referer else None)
        return body, self.capture.receipts[before:]


def collect(capture, name, params):
    if name == "sina_trade_calendar":
        end = datetime.strptime(params["end_date"], "%Y%m%d").date().isoformat()
        days, _ = daily_sources.calendar(CapturedDailyHTTP(capture), end)
        capture.dataset_metadata = {"complete_returned_window": True, "source_count": len(days),
            "source_window": "source calendar from 2010 through requested collection day",
            "requested_end": end, "future_calendar_claim": False}
        return [{"trade_date": day} for day in days]
    symbol = params["symbol"]
    if not re.fullmatch(r"(?:sh|sz)\d{6}", symbol):
        raise ValueError("explicit Shanghai/Shenzhen security required")
    if name == "tencent_raw_daily":
        start, end = [datetime.strptime(params[k], "%Y%m%d").date().isoformat()
                      for k in ("start_date", "end_date")]
        rows, _ = daily_sources.tencent_daily(CapturedDailyHTTP(capture),symbol,start,end)
        return [dict(r, security=symbol, price_basis="unadjusted") for r in rows]
    if name in {"sina_raw_daily", "sina_period_bars"}:
        start, end = [datetime.strptime(params[k], "%Y%m%d").date().isoformat()
                      for k in ("start_date", "end_date")]
        expanded = start
        if name == "sina_period_bars":
            first = datetime.fromisoformat(start).date()
            if params["period"] == "weekly":
                expanded = (first-timedelta(days=first.weekday())).isoformat()
            elif params["period"] == "monthly":
                expanded = first.replace(day=1).isoformat()
            else:
                raise ValueError("period aggregation must be weekly/monthly")
        rows, _ = daily_sources.sina_daily(CapturedDailyHTTP(capture), symbol, expanded, end)
        if name == "sina_period_bars":
            rows = aggregate(rows, params["period"], start, end)
        capture.dataset_metadata = {"complete_returned_window": True, "source_count": len(rows),
            "source_window": "all source rows in explicitly requested date window",
            "requested_start": start, "requested_end": end, "whole_market": False,
            "price_basis": "unadjusted", "quantity_basis": "native volume/amount, no conversion",
            "aggregation": params.get("period", "native daily"), "expanded_start": expanded}
        return [dict(r, security=symbol, price_basis="unadjusted", asset_type=params.get("asset_type", "stock")) for r in rows]
    if name == "stock_zh_a_daily" and params["adjust"] in {"qfq-factor", "hfq-factor"}:
        kind = params["adjust"].split("-")[0]
        url = f"https://finance.sina.com.cn/realstock/company/{symbol}/{kind}.js"
        raw = capture.get(url, {"Referer":"https://finance.sina.com.cn/"}).decode("utf-8")
        match = re.search(r"\bvar\s+"+re.escape(symbol+kind)+r"\s*=\s*(\{[^\r\n]*\})",raw)
        if not match:
            raise ValueError("Sina adjustment identity/payload mismatch")
        payload = json.loads(match[1])
        rows = [{"date":r["d"], kind+"_factor":daily_sources.decimal(r["f"]), "security":symbol,
                 "baseline_sentinel":r["d"] == "1900-01-01",
                 "price_formula":"raw_price / factor" if kind == "qfq" else "raw_price * factor"}
                for r in payload["data"]]
        if len(rows)!=int(payload["total"]) or len({r["date"] for r in rows})!=len(rows):
            raise ValueError("Sina factor count or business-key mismatch")
        for row in rows:
            datetime.strptime(row["date"],"%Y-%m-%d")
            if Decimal(row[kind+"_factor"])<=0:
                raise ValueError("nonpositive Sina adjustment factor")
        capture.dataset_metadata = {"complete_source_history_for_requested_security":True,
            "complete_returned_window":True, "complete_history":True,
            "source_count":int(payload["total"]), "factor_kind":kind,
            "baseline":"1900-01-01 is a source baseline sentinel, not an actual corporate action",
            "publication_time":None, "source_basis":"Sina; CMES adjustment basis remains to be compared"}
        return rows
    raise ValueError("unknown explicit equity source")


def aggregate(rows, period, start, end):
    groups = {}
    for row in rows:
        day = datetime.fromisoformat(row["date"]).date()
        left = day-timedelta(days=day.weekday()) if period == "weekly" else day.replace(day=1)
        groups.setdefault(left, []).append(row)
    output = []
    for left, values in sorted(groups.items()):
        right = left+timedelta(days=6) if period == "weekly" else left.replace(day=calendar.monthrange(left.year, left.month)[1])
        if values[-1]["date"] < start:
            continue
        item = {"date": values[-1]["date"], "open": values[0]["open"], "close": values[-1]["close"],
            "high": str(max(Decimal(r["high"]) for r in values)), "low": str(min(Decimal(r["low"]) for r in values)),
            "preclose": values[0].get("preclose"), "period_start": left.isoformat(), "period_end": right.isoformat(),
            "observed_from": values[0]["date"], "daily_rows": len(values), "aggregation": period,
            "period_still_open": right.isoformat() >= end, "source_daily_values_preserved": values}
        for field in ("volume", "amount"):
            item[field] = str(sum(Decimal(r[field]) for r in values)) if all(r.get(field) is not None for r in values) else None
        output.append(item)
    return output


def validate(rows, job, as_of, numeric):
    name, params = job["function"], job["parameters"]
    if name == "sina_trade_calendar":
        days = [r["trade_date"] for r in rows]
        end = datetime.strptime(params["end_date"], "%Y%m%d").date().isoformat()
        if not days or days != sorted(set(days)) or not all("2010-01-01" <= d <= end for d in days):
            raise ValueError("calendar date order, uniqueness or requested range invalid")
        for day in days:
            datetime.strptime(day, "%Y-%m-%d")
        return {"contract_reviewed":True, "business_key":["trade_date"], "gaps":[],
            "unique_keys":len(days), "source_rows":len(days), "latest_period":days[-1],
            "latest_announcement":None, "frequency":"source_trade_calendar",
            "units":{"trade_date":"Asia/Shanghai source trading date"},
            "limitations":["source calendar from 2010 through requested day; no future exchange schedule claim"]}
    if name not in {"sina_raw_daily", "sina_period_bars"}:
        return None
    start, end = [datetime.strptime(params[k], "%Y%m%d").date().isoformat()
                  for k in ("start_date", "end_date")]
    for row in rows:
        if row.get("security") != params["symbol"] or row.get("price_basis") != "unadjusted":
            raise ValueError("daily security or price basis mismatch")
    if name == "sina_period_bars":
        native = [v for r in rows for v in r["source_daily_values_preserved"]]
        expected = aggregate(native, params["period"], start, end)
        if [{k: v for k, v in r.items() if k not in {"security", "price_basis", "asset_type"}} for r in rows] != expected:
            raise ValueError("period OHLC/quantity aggregation differs from source days")
    checked = daily_sources.validate_bars(rows, start, end)
    if not rows or rows != checked or end > as_of:
        raise ValueError("daily window is empty, unordered or future-dated")
    return {"contract_reviewed":True, "business_key":["security","period_start" if name == "sina_period_bars" else "date"], "gaps":[],
        "unique_keys":len(rows), "source_rows":len(rows), "latest_period":rows[-1]["date"],
        "latest_announcement":None, "frequency":"native_daily_window",
        "units":{"OHLC":"native index points" if params.get("asset_type") == "index" else "native CNY quote", "volume":"native source quantity; unconverted",
                 "amount":"native source turnover; unconverted"},
        "limitations":["original source values and explicit securities/date window; no whole-market claim",
                       "missing amount/status fields remain null; no other source joined"]}
