"""Offline, hash-bound Sina response diagnostics. Never supplies corrected market bars."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal

from .model import DataError, TZ, day, digest, number, require, symbol, validate_ohlc
from .reconciliation import reconcile_turnover

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1023
PRICE_FIELDS = ("open", "high", "low", "close")


def _raw(body):
    require(
        type(body) is bytes and 0 < len(body) <= MAX_BYTES,
        "BOUNDED_IMPORT",
        "每份原文须为非空bytes且不超过2MiB",
    )
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def _constant(value):
    raise DataError("SOURCE_SCHEMA_ERROR", "JSON包含非有限常量: " + value)


def _pairs(items):
    out = {}
    for key, value in items:
        require(key not in out, "SOURCE_SCHEMA_ERROR", "JSON含重复键")
        out[key] = value
    return out


def _label(value):
    require(
        type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:00", value),
        "SOURCE_SCHEMA_ERROR",
        "分钟原标签须为YYYY-MM-DD HH:MM:00",
    )
    try:
        return datetime.fromisoformat(value).replace(tzinfo=TZ)
    except ValueError as exc:
        raise DataError("SOURCE_SCHEMA_ERROR", "分钟原标签无效") from exc


def _value(value):
    require(
        type(value) is str and len(value) <= 48 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value),
        "SOURCE_SCHEMA_ERROR",
        "源数值须为非负十进制文本；拒绝JSON浮点/指数/空白",
    )
    return number(value)


def _rows(body, trade_date, frequency):
    try:
        rows = json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant)
        require(rows is not None and rows != [], "SOURCE_NO_DATA", "原文没有K线数组记录")
        require(type(rows) is list, "SOURCE_SCHEMA_ERROR", "原文不是K线数组，错误对象不等于空行情")
        require(len(rows) <= MAX_ROWS, "BOUNDED_IMPORT", "每份K线原文最多1023行")
        prior = None
        for row in rows:
            require(type(row) is dict, "SOURCE_SCHEMA_ERROR", "源行须为对象")
            stamp = _label(row["day"])
            require(prior is None or stamp > prior, "SOURCE_SCHEMA_ERROR", "源行重复或倒序")
            prior = stamp
            if frequency == 5:
                require(stamp.minute % 5 == 0, "SOURCE_SCHEMA_ERROR", "5m原文标签未对齐5分钟")
            values = {key: _value(row[key]) for key in (*PRICE_FIELDS, "volume", "amount")}
            validate_ohlc(*(values[key] for key in PRICE_FIELDS), code="SOURCE_SCHEMA_ERROR")
            require(
                values["volume"] == values["volume"].to_integral_value(),
                "INVALID_VOLUME",
                "源股数须为整数",
            )
        selected = [row for row in rows if row["day"][:10] == trade_date]
        require(selected, "SOURCE_NO_DATA", "指定日期没有源记录，不能以其他日期替代")
        return selected, len(rows)
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise DataError("SOURCE_SCHEMA_ERROR", "原文格式或必要字段无效") from exc


def _reference(body, security, trade_date):
    source_symbol = ("sh" if security.endswith("XSHG") else "sz") + security[:6]
    try:
        match = re.fullmatch(
            r"var hq_str_" + source_symbol + r'="([^"\r\n]*)";\s*', body.decode("gb18030")
        )
        require(match is not None, "SOURCE_SCHEMA_ERROR", "quote证券/字符串赋值格式不符")
        fields = match.group(1).split(",")
        require(len(fields) >= 32, "SOURCE_SCHEMA_ERROR", "quote字段不足")
        require(fields[30] == trade_date, "REFERENCE_DATE_MISMATCH", "quote不属于指定日期")
        stamp = datetime.fromisoformat(fields[30] + " " + fields[31])
        require(
            stamp.strftime("%Y-%m-%d %H:%M:%S") == fields[30] + " " + fields[31],
            "SOURCE_SCHEMA_ERROR",
            "quote日期时间格式无效",
        )
        validate_ohlc(*(_value(fields[i]) for i in (1, 4, 5, 3)), code="SOURCE_SCHEMA_ERROR")
        _value(fields[8])
        _value(fields[9])
        return {
            "security": security,
            "trade_date": trade_date,
            "quote_time": stamp.replace(tzinfo=TZ).isoformat(),
            "volume": fields[8],
            "amount": fields[9],
            "volume_unit": "share",
            "amount_unit": "CNY",
            "trading_scope": "unknown",
            "scope_evidence": None,
            "uninterpreted_tail_field_33": fields[33] if len(fields) > 33 else None,
        }
    except (IndexError, ValueError, UnicodeError) as exc:
        raise DataError("SOURCE_SCHEMA_ERROR", "quote原文无法解析") from exc


def _totals(rows):
    return {
        "rows": len(rows),
        "volume_shares": str(sum((number(r["volume"]) for r in rows), Decimal(0))),
        "amount_cny": str(sum((number(r["amount"]) for r in rows), Decimal(0))),
    }


def reconcile_sina_day(security, trade_date, *, minute_body, five_minute_body, quote_body):
    """Read caller-supplied original bytes only; neither fetches nor authenticates provenance.

    Date selection is explicit. (five-minute label - 5m, label] is a diagnostic
    grouping hypothesis, never verified bar boundaries or proof of absent trades.
    """
    symbol(security)
    day(trade_date)
    inputs = {
        name: _raw(body)
        for name, body in (("1m", minute_body), ("5m", five_minute_body), ("quote", quote_body))
    }
    one, one_count = _rows(minute_body, trade_date, 1)
    five, five_count = _rows(five_minute_body, trade_date, 5)
    reference = _reference(quote_body, security, trade_date)
    for key, rows, count in (("1m", one, one_count), ("5m", five, five_count)):
        inputs[key].update(
            source_rows=count,
            selected_rows=len(rows),
            selected_row_sha256=[digest(row) for row in rows],
        )
    comparisons = {
        key: reconcile_turnover(rows, reference) for key, rows in (("1m", one), ("5m", five))
    }
    groups, assigned = [], set()
    for row in five:
        end = _label(row["day"])
        rows = [r for r in one if end - timedelta(minutes=5) < _label(r["day"]) <= end]
        assigned.update(r["day"] for r in rows)
        totals = _totals(rows)
        groups.append(
            {
                "label": row["day"],
                "minute_labels": [r["day"] for r in rows],
                **totals,
                "five_minus_minute_volume": str(
                    number(row["volume"]) - Decimal(totals["volume_shares"])
                ),
                "five_minus_minute_amount": str(
                    number(row["amount"]) - Decimal(totals["amount_cny"])
                ),
                "ohlc_equal": bool(rows)
                and all(
                    a == number(row[k])
                    for k, a in zip(
                        PRICE_FIELDS,
                        (
                            number(rows[0]["open"]),
                            max(number(r["high"]) for r in rows),
                            min(number(r["low"]) for r in rows),
                            number(rows[-1]["close"]),
                        ),
                    )
                ),
            }
        )
    missing = []
    for left, right in zip(one, one[1:]):
        a, b = _label(left["day"]), _label(right["day"])
        # Explicit diagnostic only: do not label the known lunch boundary as missing bars.
        same_half = a.hour < 12 and b.hour < 12 or a.hour >= 13 and b.hour >= 13
        if same_half:
            missing.extend(
                (a + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
                for i in range(1, int((b - a).total_seconds() // 60))
            )
    result = {
        "schema": "sina.turnover-diagnostic.v1",
        "security": security,
        "trade_date": trade_date,
        "status": "failed"
        if any(r["status"] == "failed" for r in comparisons.values())
        else "unknown",
        "accepted": False,
        "inputs": inputs,
        "reference": reference,
        "comparisons": comparisons,
        "label_grouping": {
            "rule": "(5m source label - 5 minutes, source label]",
            "semantics_verified": False,
            "groups": groups,
            "unassigned_minute_labels": [r["day"] for r in one if r["day"] not in assigned],
            "hypothesized_missing_intra_half_labels": missing,
            "missing_trade_count": None,
        },
        "precision": {
            key: {
                "sub_cent_amount_rows": sum(
                    number(r["amount"]) % Decimal("0.01") != 0 for r in rows
                )
            }
            for key, rows in (("1m", one), ("5m", five))
        },
        "cause": "SOURCE_UNRESOLVED",
        "library_sum": "Decimal over original source text",
        "identity_basis": "caller-declared Sina files; quote header/date checked; K-line payload has no security field",
        "source_authenticity_verified": False,
        "historical_pit_verified": False,
        "available_at": None,
        "finality": "UNVERIFIED",
        "trading_scope_verified": False,
        "network_requests": 0,
        "fabricated_rows": 0,
        "execution_permission": False,
    }
    return {**result, "diagnostic_id": digest(result)}
