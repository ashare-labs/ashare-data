"""Reuse the maintained Sina bar parser for explicit stock, ETF and index scopes.

Native labels and quantities are retained. This is a rolling source window,
not arbitrary historical paging, and makes no exchange interval/PIT claim.
"""
from datetime import datetime
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ashare_data.market.client import KLINE_URL, _bars

FUNCTION = "sina_native_minutes"
PERIODS = {"1", "5", "15", "30", "60"}
PATTERNS = {
    "stock": r"sh(?:60|68)\d{4}|sz(?:00|30)\d{4}",
    "etf": r"sh5\d{5}|sz1[56]\d{4}",
    "index": r"sh000\d{3}|sz399\d{3}",
}


def parameters(params):
    if set(params) != {"symbol", "period", "asset_type"}:
        raise ValueError("explicit symbol, native period and asset type required")
    kind, symbol, period = (params[k] for k in ("asset_type", "symbol", "period"))
    if kind not in PATTERNS or not re.fullmatch(PATTERNS[kind], symbol) or period not in PERIODS:
        raise ValueError("minute security/market/type or period mismatch")
    return kind, symbol, period


def collect(capture, params):
    kind, symbol, period = parameters(params)
    url = KLINE_URL + "?" + urlencode(dict(symbol=symbol, scale=int(period), ma="no", datalen=800))
    raw = capture.get(url, {"Referer": "https://finance.sina.com.cn/"})
    records = _bars(raw, int(period))
    received = datetime.fromisoformat(capture.receipts[-1]["received_at"])
    rows, pending = [], []
    for record in records:
        stamp = datetime.fromisoformat(record["day"]).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        # Keep future labels in the immutable HTTP body and report them explicitly.
        # They are not available completed observations and are never relabelled.
        if stamp > received:
            pending.append(record["day"])
            continue
        rows.append(dict(record, security=symbol, asset_type=kind, frequency=period+"m",
                         price_basis="unadjusted", source_label_at=stamp.isoformat()))
    capture.dataset_metadata = {
        "complete_returned_window": True, "source_count": len(rows),
        "source_returned_count": len(records), "deferred_source_labels": pending,
        "source_window": "latest at most 800 native bars for this explicit identity and period",
        "arbitrary_history_supported": False, "whole_market": False,
        "identity_basis": "explicit exchange code and asset type in request; source body does not echo code",
        "price_basis": "unadjusted", "timestamp_basis": "native source label in Asia/Shanghai, unchanged",
        "quantity_basis": "native volume and amount retained without multiplication or inferred exchange precision",
        "increment": "overlapping rolling window; preserve received history and revisions",
    }
    return rows


def validate(rows, job, as_of, numeric):
    if job["function"] != FUNCTION:
        return None
    kind, symbol, period = parameters(job["parameters"])
    labels = []
    for row in rows:
        stamp = datetime.fromisoformat(row["source_label_at"])
        if (row.get("security") != symbol or row.get("asset_type") != kind
                or row.get("frequency") != period+"m" or row.get("price_basis") != "unadjusted"
                or stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 28800
                or stamp.strftime("%Y-%m-%d %H:%M:%S") != row["day"]
                or stamp.date().isoformat() > as_of):
            raise ValueError("native minute identity/label/basis mismatch")
        prices = [numeric(row[k]) for k in ("open", "high", "low", "close")]
        opening, high, low, close = prices
        if any(v is None for v in prices) or not 0 < low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError("invalid native minute OHLC")
        for field in ("volume", "amount"):
            if field in row and (numeric(row[field]) is None or numeric(row[field]) < 0):
                raise ValueError("invalid native minute quantity")
        labels.append(row["day"])
    if labels != sorted(set(labels)):
        raise ValueError("duplicate or unordered native minute keys")
    return {
        "contract_reviewed": True, "business_key": ["security", "frequency", "day"],
        "unique_keys": len(labels), "source_rows": len(rows), "frequency": period+"m_native_rolling_window",
        "latest_period": max(labels) if labels else None, "latest_announcement": None, "gaps": [],
        "units": {"OHLC": "index points" if kind == "index" else "native CNY quote",
                  "volume": "native source quantity; unconverted", "amount": "native source turnover; unconverted"},
        "limitations": ["source labels are preserved; precise interval start/end convention is not asserted",
                        "native quantity is not silently converted to lots, shares or fund units",
                        "explicit requested identities and rolling window; not whole market or CMES full history",
                        "missed history outside the source window cannot be reconstructed by this endpoint"],
    }
