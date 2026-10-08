"""Source minute labels and explicit grid hypotheses, never fabricated bars."""

from datetime import datetime, time, timedelta
import re

from .model import DataError, TZ, require, validate_ohlc
from .research import _day, _decimal

FREQUENCIES = {"5m": "5", "15m": "15", "30m": "30", "60m": "60"}
FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"
VERSION = "baostock-research-2/receipt-4.1"


def cutoff(end, inclusive=True):
    """Explicit aware source-label ceiling; never a publication/closure clock."""
    require(type(inclusive) is bool, "INVALID_ARGUMENT", "end_inclusive须为布尔值")
    if end is None:
        require(inclusive, "INVALID_ARGUMENT", "排除端点须同时提供end")
        return None
    try:
        if isinstance(end, str):
            require(
                re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
                    end,
                )
                is not None,
                "INVALID_TIME",
                "end须为含秒及明确时区的ISO时间，不接受日期或无时区时间",
            )
            # fromisoformat normalizes invalid offset minutes; reject them first.
            if not end.endswith("Z"):
                require(int(end[-5:-3]) < 24 and int(end[-2:]) < 60, "INVALID_TIME", "时区偏移无效")
            end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        require(
            type(end) is datetime and end.tzinfo is not None and end.utcoffset() is not None,
            "INVALID_TIME",
            "end须为带时区datetime或ISO时间",
        )
        return end.astimezone(TZ), inclusive
    except (ValueError, OverflowError, TypeError) as exc:
        raise DataError("INVALID_TIME", "end不是有效日期时间") from exc


def intersect(first, second):
    if first is None:
        return second
    if second is None:
        return first
    if first[0] == second[0]:
        return first[0], first[1] and second[1]
    return min(first, second, key=lambda bound: bound[0])


def within(stamp, bound):
    if bound is None:
        return True
    value = stamp.replace(tzinfo=TZ)
    return value <= bound[0] if bound[1] else value < bound[0]


def selection(bound):
    return {
        "mode": "source_label" if bound else "unrestricted_research",
        "end": bound[0].isoformat() if bound else None,
        "end_inclusive": bound[1] if bound else None,
        "timezone": "Asia/Shanghai",
        "closed_bar_verified": False,
        "historical_pit_verified": False,
        "actual_visibility_verified": False,
    }


def label(row):
    value = row["time"]
    require(
        isinstance(value, str) and len(value) == 17 and value.isascii() and value.isdigit(),
        "SOURCE_SCHEMA_ERROR",
        "分钟源time须为17位YYYYMMDDHHMMSSsss",
    )
    try:
        # strptime permits variable-width directives and can reinterpret hour25.
        # The source wire contract has fixed-width fields, so parse exact slices.
        stamp = datetime(
            int(value[:4]),
            int(value[4:6]),
            int(value[6:8]),
            int(value[8:10]),
            int(value[10:12]),
            int(value[12:14]),
            int(value[14:17]) * 1000,
        )
    except ValueError as exc:
        from .model import DataError

        raise DataError("SOURCE_SCHEMA_ERROR", "分钟源time不是有效日期时间") from exc
    require(
        stamp.date() == _day(row["date"]), "SOURCE_IDENTITY_MISMATCH", "分钟date与time日期不一致"
    )
    return stamp


def validate(rows, params):
    previous = None
    for row in rows:
        require(set(row) == set(FIELDS.split(",")), "SOURCE_SCHEMA_ERROR", "分钟字段集合不匹配")
        stamp = label(row)
        require(
            row["code"] == params["code"]
            and row["adjustflag"] == "3"
            and params["start_date"] <= row["date"] <= params["end_date"]
            and (previous is None or stamp > previous),
            "SOURCE_IDENTITY_MISMATCH",
            "分钟证券/日期/原价身份、排序或唯一性不符",
        )
        values = {f: _decimal(row[f]) for f in ("open", "high", "low", "close", "volume")}
        validate_ohlc(
            *(values[f] for f in ("open", "high", "low", "close")), code="SOURCE_SCHEMA_ERROR"
        )
        require(
            values["volume"] == values["volume"].to_integral_value(),
            "SOURCE_SCHEMA_ERROR",
            "分钟成交量须为整数股",
        )
        if row["amount"]:
            _decimal(row["amount"])
        previous = stamp


def diagnostics(rows, params, bound=None):
    """Apply an end-label hypothesis on dates actually observed, not a calendar."""
    step = int(params["frequency"])
    observed = {}
    for row in rows:
        observed.setdefault(row["date"], []).append(label(row))
    days = []
    for day, labels in sorted(observed.items()):
        grid = []
        for hour, minute in ((9, 30), (13, 0)):
            start = datetime.combine(_day(day), datetime.min.time()).replace(
                hour=hour, minute=minute
            )
            grid.extend(
                t
                for n in range(step, 121, step)
                if within(t := start + timedelta(minutes=n), bound)
            )
        got, expected = set(labels), set(grid)

        def encode(values):
            return [t.isoformat(timespec="milliseconds") for t in sorted(values)]

        days.append(
            {
                "date": day,
                "observed_rows": len(labels),
                "first_label": min(labels).isoformat(timespec="milliseconds"),
                "last_label": max(labels).isoformat(timespec="milliseconds"),
                "end_label_hypothesis_matches": got == expected,
                "hypothesized_grid_rows": len(grid),
                "missing_hypothesized_labels": encode(expected - got),
                "outside_hypothesized_grid": encode(got - expected),
                "observed_at_1130": any(
                    t.hour == 11 and t.minute == 30 and t.second == 0 and t.microsecond == 0
                    for t in labels
                ),
                "observed_at_1500": any(
                    t.hour == 15 and t.minute == 0 and t.second == 0 and t.microsecond == 0
                    for t in labels
                ),
                "inside_lunch_break": encode(
                    t for t in labels if time(11, 30) < t.time() < time(13, 0)
                ),
                "missing_amount_rows": sum(not r["amount"] for r in rows if r["date"] == day),
            }
        )
    requested = []
    day = _day(params["start_date"])
    while day <= _day(params["end_date"]):
        if within(datetime.combine(day, datetime.min.time()), bound):
            requested.append(day.isoformat())
        day += timedelta(days=1)
    return {
        "frequency": str(step) + "m",
        "timezone": "Asia/Shanghai",
        "label_role": "source_timestamp; session_end_is_hypothesis",
        "grid_hypothesis": "09:30-11:30 and 13:00-15:00 session-end labels",
        "days": days,
        "dates_without_rows": sorted(set(requested) - set(observed)),
        "calendar_verified": False,
        "closed_bar_verified": False,
        "market_coverage": "unknown",
        "bar_start": None,
        "bar_end": None,
    }
