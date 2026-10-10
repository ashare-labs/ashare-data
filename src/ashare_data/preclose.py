"""Source preclose references; no substitution, adjustment, or trading inference."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .d1_types import Projection, SourceRecord
from .daily_status import page_received_times
from .model import DataError, canonical, digest, require
from .research import _day

FIELDS = "date,code,preclose,adjustflag"
VERSION = "baostock-source-preclose-1/receipt-4.1"
POLICY = "source-preclose-1"
BASIS = "source_reference_adjustflag_3"
DEFINITION_VERSION = "baostock-preclose-definition-1"
DEFINITION_URL = "https://www.baostock.com/mainContent?file=stockKData.md"
DEFINITION_SHA256 = "2a6db3b24ab21a199aa17bd949dfbe545d3279007bea1664854f9b7c20542aac"


@dataclass(frozen=True)
class SourcePrecloseValue(Projection):
    source_field: str
    raw_value: str | None
    value: Decimal | None
    state: str


@dataclass(frozen=True)
class SourcePrecloseRow(SourceRecord):
    security: str
    trade_date: date
    preclose: SourcePrecloseValue
    source_adjustflag: str | None
    request_adjustflag: str
    capture_id: str
    raw_record_sha256: str | None
    raw_response_sha256: str | None
    response_received_at: str | None
    evidence_kind: str
    raw_json: str
    unit: str = "CNY/share"
    price_basis: str = BASIS
    definition_version: str = DEFINITION_VERSION
    historical_available_at: None = None
    reference_price_eligible: None = None
    events_complete: bool = False
    execution_permission: bool = False


@dataclass(frozen=True)
class SourcePrecloseResult:
    capture_id: str
    rows: tuple[SourcePrecloseRow, ...]
    report_json: str

    @property
    def report(self):
        return json.loads(self.report_json)

    def to_dict(self):
        return {"rows": [r.to_dict() for r in self.rows], "report": self.report}


def validate(rows, params):
    start, end = _day(params["start_date"]), _day(params["end_date"])
    previous = None
    for row in rows:
        require(
            set(row) == set(FIELDS.split(","))
            and all(isinstance(v, str) for v in row.values())
            and row["code"] == params["code"]
            and row["adjustflag"] == "3",
            "SOURCE_SCHEMA_ERROR",
            "前收字段、字符串类型或请求身份异常",
        )
        day = _day(row["date"])
        require(
            start <= day <= end and (previous is None or previous < day),
            "SOURCE_SCHEMA_ERROR",
            "前收日期越界、重复或无序",
        )
        previous = day


def received_times(receipt):
    try:
        return page_received_times(receipt)
    except DataError as exc:
        if exc.code != "DAILY_STATUS_CLOCK_INVALID":
            raise
        raise DataError("SOURCE_PRECLOSE_CLOCK_INVALID", exc.message, exc.details) from exc


def _value(row, requested):
    if "preclose" not in requested:
        return SourcePrecloseValue("preclose", None, None, "field_not_requested")
    if row is None:
        return SourcePrecloseValue("preclose", None, None, "row_missing_unknown")
    raw = row["preclose"]  # Column presence/types were bound to the source schema.
    if raw == "":
        return SourcePrecloseValue("preclose", raw, None, "source_empty")
    if len(raw) > 64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
        return SourcePrecloseValue("preclose", raw, None, "invalid_numeric")
    value = Decimal(raw)
    return SourcePrecloseValue(
        "preclose", raw, value, "source_zero" if value == 0 else "source_observed"
    )


def project(view, *, require_known=False):
    require(type(require_known) is bool, "INVALID_ARGUMENT", "require_known必须为bool")
    req = view._attempt["request"]
    params = req["params"]
    require(
        req["method"] == "query_history_k_data_plus" and params.get("frequency") == "d",
        "QUERY_KIND_MISMATCH",
        "源前收仅支持固定日线证据，基本资料/分钟不替代",
    )
    require(
        view._status in {"research_rows", "empty_unknown"},
        "SOURCE_REQUEST_FAILED",
        "源前收响应未达到研究读取条件",
        {"capture_id": view._id, "status": view._status},
    )
    requested = params["fields"].split(",")
    start, end = _day(params["start_date"]), _day(params["end_date"])
    by_date = {r["date"]: r for r in view._rows}
    clocks = received_times(view._receipt)
    pages = {r["date"]: p for p in view._receipt["pages"] for r in p["rows"]}
    code = params["code"]
    security = code[3:] + (".XSHG" if code.startswith("sh.") else ".XSHE")
    rows = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        row, page = by_date.get(day.isoformat()), pages.get(day.isoformat())
        rows.append(
            SourcePrecloseRow(
                security,
                day,
                _value(row, requested),
                row.get("adjustflag") if row else None,
                params["adjustflag"],
                view._id,
                digest(row) if row is not None else None,
                page["raw_response_sha256"] if page else None,
                clocks[page["page_index"]] if page else None,
                view._attempt["evidence_kind"],
                canonical(row or {}).decode(),
            )
        )
    unknown = [
        {"date": r.trade_date.isoformat(), "field": "preclose", "state": r.preclose.state}
        for r in rows
        if r.preclose.value is None
    ]
    report = {
        "capture_id": view._id,
        "policy": POLICY,
        "request": req,
        "network_used": False,
        "quality": dict(
            view.quality(),
            price_basis=BASIS,
            capture_clock_state=view._receipt["clock_order_state"],
            reference_price_eligible=None,
            events_complete=False,
        ),
        "coverage": dict(
            view.coverage(),
            requested_calendar_days=len(rows),
            source_rows=len(by_date),
            missing_dates=[
                r.trade_date.isoformat() for r in rows if r.trade_date.isoformat() not in by_date
            ],
            unknown_fields=unknown,
            requested_values_known=not unknown,
            zero_dates=[
                r.trade_date.isoformat() for r in rows if r.preclose.state == "source_zero"
            ],
            calendar_inferred=False,
        ),
        "definition": {
            "version": DEFINITION_VERSION,
            "url": DEFINITION_URL,
            "document_sha256": DEFINITION_SHA256,
            "source_field": "preclose",
            "unit": "CNY/share",
            "price_basis": BASIS,
            "request_adjustflag": "3",
            "meaning": "Source daily reference; ex-right/ex-dividend dates can differ from previous-day actual close.",
        },
        "limitations": [
            "No previous-close substitution, rounding, adjustment calculation or event-absence inference.",
            "Known source values, including zero, do not establish reference-price or trading eligibility.",
            "Natural-day grid; missing rows do not establish closure or suspension.",
            "Recorded last-byte receipt time is not verified completion or historical publication/PIT.",
        ],
    }
    require(
        not require_known or not unknown,
        "SOURCE_PRECLOSE_UNKNOWN",
        "源前收含未知值",
        {"capture_id": view._id, "unknown_fields": unknown},
    )
    return SourcePrecloseResult(view._id, tuple(rows), canonical(report).decode())
