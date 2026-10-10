"""Finite daily source flags; no current-state, price, or calendar inference."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from .d1_types import Projection, SourceRecord
from .model import canonical, digest, require
from .research import _day

FIELDS = "date,code,tradestatus,isST,adjustflag"
VERSION = "baostock-daily-status-1/receipt-4.1"
POLICY = "daily-source-status-1"


@dataclass(frozen=True)
class DailyStatusFlag(Projection):
    source_field: str
    raw_value: str | None
    value: bool | None
    state: str


@dataclass(frozen=True)
class DailyStatusRow(SourceRecord):
    security: str
    trade_date: date
    suspended: DailyStatusFlag
    is_st: DailyStatusFlag
    capture_id: str
    raw_record_sha256: str | None
    raw_response_sha256: str | None
    response_received_at: str | None
    evidence_kind: str
    raw_json: str
    historical_available_at: None = None
    historical_eligible: None = None
    intraday_halts_verified: bool = False
    execution_permission: bool = False


@dataclass(frozen=True)
class DailyStatusResult:
    capture_id: str
    rows: tuple[DailyStatusRow, ...]
    report_json: str

    @property
    def report(self):
        return json.loads(self.report_json)

    def to_dict(self):
        return {"rows": [r.to_dict() for r in self.rows], "report": self.report}


def validate(rows, params):
    """Keep blank/unrecognized strings for explicit unknown reporting, never coerce."""
    start, end = _day(params["start_date"]), _day(params["end_date"])
    previous = None
    for row in rows:
        require(
            set(row) == set(FIELDS.split(","))
            and all(isinstance(v, str) for v in row.values())
            and row["code"] == params["code"]
            and row["adjustflag"] == "3",
            "SOURCE_SCHEMA_ERROR",
            "日状态字段、字符串类型或请求身份异常",
        )
        day = _day(row["date"])
        require(
            start <= day <= end and (previous is None or previous < day),
            "SOURCE_SCHEMA_ERROR",
            "日状态日期越界、重复或无序",
        )
        previous = day


def _flag(row, requested, name, *, inverted=False):
    if name not in requested:
        return DailyStatusFlag(name, None, None, "field_not_requested")
    if row is None:
        return DailyStatusFlag(name, None, None, "row_missing_unknown")
    if name not in row:
        return DailyStatusFlag(name, None, None, "response_field_missing")
    raw = row[name]
    if raw == "":
        return DailyStatusFlag(name, raw, None, "source_empty")
    if raw not in {"0", "1"}:
        return DailyStatusFlag(name, raw, None, "unsupported_enum")
    return DailyStatusFlag(name, raw, raw == ("0" if inverted else "1"), "source_observed")


def project(view, *, require_known=False):
    require(type(require_known) is bool, "INVALID_ARGUMENT", "require_known必须为bool")
    req = view._attempt["request"]
    params = req["params"]
    require(
        req["method"] == "query_history_k_data_plus" and params.get("frequency") == "d",
        "QUERY_KIND_MISMATCH",
        "日状态仅支持固定日线或daily_status证据；资料/分钟不替代",
    )
    require(
        view._status in {"research_rows", "empty_unknown"},
        "SOURCE_REQUEST_FAILED",
        "日状态源响应未达到研究读取条件",
        {"capture_id": view._id, "status": view._status},
    )
    requested = params["fields"].split(",")
    start, end = _day(params["start_date"]), _day(params["end_date"])
    by_date = {r["date"]: r for r in view._rows}
    pages = {r["date"]: page for page in view._receipt["pages"] for r in page["rows"]}
    code = params["code"]
    security = code[3:] + (".XSHG" if code.startswith("sh.") else ".XSHE")
    rows = []
    day = start
    while day <= end:
        row = by_date.get(day.isoformat())
        page = pages.get(day.isoformat())
        rows.append(
            DailyStatusRow(
                security,
                day,
                _flag(row, requested, "tradestatus", inverted=True),
                _flag(row, requested, "isST"),
                view._id,
                digest(row) if row is not None else None,
                page["raw_response_sha256"] if page else None,
                page["last_byte_received_at"] if page else None,
                view._attempt["evidence_kind"],
                canonical(row or {}).decode(),
            )
        )
        day += timedelta(days=1)
    missing = [d.isoformat() for d in (r.trade_date for r in rows) if d.isoformat() not in by_date]
    unknown = [
        {"date": r.trade_date.isoformat(), "field": f.source_field, "state": f.state}
        for r in rows
        for f in (r.suspended, r.is_st)
        if f.value is None
    ]
    coverage = dict(
        view.coverage(),
        requested_calendar_days=len(rows),
        source_rows=len(by_date),
        missing_dates=missing,
        unknown_fields=unknown,
        requested_values_known=not unknown,
        calendar_inferred=False,
    )
    report = {
        "capture_id": view._id,
        "policy": POLICY,
        "request": req,
        "network_used": False,
        "quality": dict(view.quality(), capture_clock_state=view._receipt["clock_order_state"]),
        "coverage": coverage,
        "definitions": {
            "suspended": "tradestatus 0=true, 1=false",
            "is_st": "isST 1=true, 0=false",
        },
        "definition_url": "https://www.baostock.com/mainContent?file=stockKData.md",
        "limitations": [
            "source daily flags, not intraday halt intervals or trading eligibility",
            "natural-day request grid; missing rows do not prove closure or suspension",
            "response receipt time is not historical publication or PIT",
        ],
    }
    require(
        not require_known or not unknown,
        "DAILY_STATUS_UNKNOWN",
        "日状态必需字段含未知",
        {"capture_id": view._id, "unknown_fields": unknown},
    )
    return DailyStatusResult(view._id, tuple(rows), canonical(report).decode())
