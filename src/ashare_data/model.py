"""Versioned input contracts. No implicit units, dates, calendars or source selection."""
from __future__ import annotations

import hashlib
import copy
import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
QUALITIES = {"observed", "inferred", "unverified", "synthetic"}
MAX_ROWS = 100_000
MAX_MINUTES = 2_000_000
# The only imported instrument-set fields projected onto a public result envelope.
# Every other source field stays under source_fields, including future/unknown names.
INSTRUMENT_SET_FIELDS = (
    "effective_date", "available_at", "observed_at", "evidence", "scope", "instruments",
)


class DataError(Exception):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, copy.deepcopy(details or {})

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": copy.deepcopy(self.details)}


def require(condition, code, message, details=None):
    if not condition:
        raise DataError(code, message, details)


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value, *, minute=False) -> datetime:
    try:
        dt = datetime.fromisoformat(value) if isinstance(value, str) else value
        require(isinstance(dt, datetime) and dt.tzinfo is not None and dt.utcoffset() is not None,
                "INVALID_TIME", "时间必须包含 UTC offset")
        dt = dt.astimezone(TZ)
        require(not minute or (dt.second == 0 and dt.microsecond == 0),
                "INVALID_TIME", "bar 范围和标签必须对齐整分钟")
        return dt
    except (ValueError, TypeError) as exc:
        raise DataError("INVALID_TIME", f"无效时间: {value}") from exc


def day(value) -> str:
    try:
        require(isinstance(value, str) and len(value) == 10, "INVALID_DATE", "日期须为 YYYY-MM-DD")
        return date.fromisoformat(value).isoformat()
    except (ValueError, TypeError) as exc:
        raise DataError("INVALID_DATE", f"无效日期: {value}") from exc


def symbol(value) -> str:
    require(isinstance(value, str) and re.fullmatch(r"\d{6}\.(XSHG|XSHE)", value),
            "INVALID_SYMBOL", "证券代码须为 6 位数字加 .XSHG/.XSHE")
    return value


def number(value) -> Decimal:
    try:
        require(not isinstance(value, bool), "INVALID_NUMBER", "布尔值不是价格或数量")
        n = Decimal(str(value))
        require(n.is_finite() and n >= 0 and n < Decimal("100000000000000000"),
                "INVALID_NUMBER", "价格和数量必须是有限非负数且小于 1e17")
        require(n == n.quantize(Decimal("0.000001")), "INVALID_NUMBER", "最多支持 6 位小数，不静默舍入")
        return n
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DataError("INVALID_NUMBER", f"无效数值: {value}") from exc


def nonempty(value, name):
    require(isinstance(value, str) and bool(value.strip()), "INVALID_BUNDLE", f"缺少 {name}")


def validate_ohlc(o, h, low, c, *, code="INVALID_OHLC"):
    """Shared live/import price gate; callers retain their numeric precision contract."""
    require(all(x.is_finite() and 0 < x < Decimal("1e17") for x in (o, h, low, c))
            and low <= min(o, c) <= max(o, c) <= h, code,
            "OHLC 必须为有限正数、小于 1e17 并满足 low <= open/close <= high")
    # Public DataFrame prices are binary64. Reject subnormal/underflow values
    # before a response can enter the cache; original decimal text is retained.
    require(all(x >= Decimal.from_float(sys.float_info.min) and math.isfinite(float(x))
                and float(x) > 0 for x in (o, h, low, c)), code,
            "OHLC 超出公开 float64 的正常正数表示范围；不下溢、补零或截断")


def normalize(bundle):
    """Validate a bounded local bundle; retain original rows verbatim as JSON source_fields."""
    try:
        require(bundle["schema_version"] == 1, "SCHEMA_VERSION", "仅支持 schema_version=1")
        require(set(bundle) <= {"schema_version", "source", "bars", "calendar", "instrument_sets"},
                "INVALID_BUNDLE", "未知 bundle 字段")
        src = bundle["source"]
        for key in ("id", "kind", "location", "unit_evidence", "time_evidence"):
            nonempty(src[key], f"source.{key}")
        require(src["kind"] in {"synthetic", "local_reconstruction", "local_observation"},
                "UNSUPPORTED", "不支持的 source.kind")
        observed_at = timestamp(src["observed_at"])
        observed = observed_at.isoformat()
        require(src["label"] in {"start", "end"}, "INVALID_BUNDLE", "label 必须声明 start/end")
        require(src["time_semantics"] in {"verified", "unverified"}, "INVALID_BUNDLE", "缺少时间语义状态")
        require(src["price_unit"] == "CNY" and src["price_basis"] == "unadjusted",
                "UNSUPPORTED", "仅支持未复权人民币价格")
        require(src["volume_unit"] in {"shares", "lots"} and src["amount_unit"] in {"CNY", "CNY_10K"},
                "UNSUPPORTED", "单位必须显式为 shares/lots 和 CNY/CNY_10K")
        lot = number(src["lot_size"]) if src["volume_unit"] == "lots" else Decimal(1)
        require(lot > 0 and lot == lot.to_integral_value(), "INVALID_BUNDLE", "lot_size 必须为正整数")
        raw_rows = bundle["bars"]
        require(isinstance(raw_rows, list) and 0 < len(raw_rows) <= MAX_ROWS,
                "BOUNDED_IMPORT", f"每次须导入 1..{MAX_ROWS} 根，拒绝无界迁移")
        out, keys = [], set()
        for raw in raw_rows:
            sym = symbol(raw["symbol"])
            label = timestamp(raw["timestamp"], minute=True)
            begin = label if src["label"] == "start" else label - timedelta(minutes=1)
            end = begin + timedelta(minutes=1)
            q = raw["quality"]
            require(q in QUALITIES, "INVALID_QUALITY", "未知质量等级")
            if src["kind"] == "synthetic":
                require(q == "synthetic", "INVALID_QUALITY", "合成来源只能标为 synthetic")
            else:
                require(q != "synthetic", "INVALID_QUALITY", "真实来源不能标为 synthetic")
            if src["time_semantics"] == "unverified":
                require(q == "unverified", "INVALID_QUALITY", "时间语义未验证时禁止提升质量")
            if src["kind"] == "local_reconstruction":
                require(q in {"inferred", "unverified"}, "INVALID_QUALITY", "重建数据不可伪装 observed")
            nonempty(raw["quality_reason"], "bar.quality_reason")
            require(end <= observed_at, "INVALID_VISIBILITY", "完整 bar 的观测时间不可早于闭合时间",
                    {"symbol": sym, "bar_end": end.isoformat(), "observed_at": observed})
            available_value = raw.get("available_at")
            available = timestamp(available_value) if available_value is not None else None
            if available:
                nonempty(raw["visibility_evidence"], "bar.visibility_evidence")
                require(available >= end, "INVALID_VISIBILITY", "完整 bar 不可在闭合前可见")
                require(available <= observed_at, "INVALID_VISIBILITY", "可见时间不能晚于观测时间")
            o, h, low, c = (number(raw[k]) for k in ("open", "high", "low", "close"))
            validate_ohlc(o, h, low, c)
            v = number(raw["volume"]) * lot
            a = number(raw["amount"]) * (10000 if src["amount_unit"] == "CNY_10K" else 1)
            number(v)
            number(a)
            require(v == v.to_integral_value(), "INVALID_VOLUME", "标准化 volume 必须为整股")
            key = (sym, begin.isoformat())
            require(key not in keys, "DUPLICATE_BAR", "同一导入内证券分钟重复", {"key": key})
            keys.add(key)
            out.append({"symbol": sym, "bar_start": begin.isoformat(), "bar_end": end.isoformat(),
                        "open": o, "high": h, "low": low, "close": c, "volume": v, "amount": a,
                        "quality": q, "quality_reason": raw["quality_reason"],
                        "source_id": src["id"], "source_label": raw["timestamp"],
                        "source_label_kind": src["label"], "time_semantics": src["time_semantics"],
                        "available_at": available.isoformat() if available else None,
                        "observed_at": observed, "visibility_evidence": raw.get("visibility_evidence"),
                        "source_fields": canonical(raw).decode()})
        seen_dates = set()
        for cal in bundle.get("calendar", []):
            d = day(cal["date"])
            require(d not in seen_dates, "INVALID_CALENDAR", "重复日历日期")
            seen_dates.add(d)
            require(type(cal["is_open"]) is bool, "INVALID_CALENDAR", "is_open 须是 bool")
            nonempty(cal["evidence"], "calendar.evidence")
            timestamp(cal["available_at"])
            intervals = []
            for a, b in cal["sessions"]:
                a, b = timestamp(a, minute=True), timestamp(b, minute=True)
                require(a < b and a.date().isoformat() == d and b.date().isoformat() == d,
                        "INVALID_CALENDAR", "时段需在当日且开始早于结束")
                intervals.append((a, b))
            intervals.sort()
            require(bool(intervals) == cal["is_open"], "INVALID_CALENDAR", "开市必须有时段，休市无时段")
            require(all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:])),
                    "INVALID_CALENDAR", "时段不得重叠")
        seen_sets = set()
        for item in bundle.get("instrument_sets", []):
            d = day(item["effective_date"])
            require(d not in seen_sets, "INVALID_UNIVERSE", "重复证券集合日期")
            seen_sets.add(d)
            nonempty(item["evidence"], "instrument_sets.evidence")
            require(item["scope"] in {"sample", "all_a_shares"}, "INVALID_UNIVERSE", "未知证券集合范围")
            require(timestamp(item["available_at"]) <= timestamp(item["observed_at"]),
                    "INVALID_VISIBILITY", "证券集合可见时间不得晚于观测时间")
            syms = [symbol(r["symbol"]) for r in item["instruments"]]
            require(len(syms) == len(set(syms)), "INVALID_UNIVERSE", "重复证券")
            for row in item["instruments"]:
                require(row["type"] == "stock", "UNSUPPORTED", "首版只支持 stock")
                nonempty(row["name"], "instrument.name")
        return sorted(out, key=lambda r: (r["symbol"], r["bar_start"]))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("INVALID_BUNDLE", f"导入契约不完整或类型错误: {exc}") from exc


def request_range(symbols, start, end):
    require(isinstance(symbols, (list, tuple)) and 0 < len(symbols) <= 100,
            "INVALID_REQUEST", "symbols 须为 1..100 个明确证券的列表")
    syms = sorted(set(symbol(s) for s in symbols))
    require(len(syms) == len(symbols), "INVALID_REQUEST", "symbols 不得重复")
    a, b = timestamp(start, minute=True), timestamp(end, minute=True)
    require(a < b, "INVALID_RANGE", "start 必须早于 end")
    require((b-a).total_seconds()/60 * len(syms) <= MAX_MINUTES,
            "BOUNDED_QUERY", "查询范围过大，请拆分显式请求")
    return syms, a, b
