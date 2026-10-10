"""Source calendar observations and bounded neighbours; no eligibility or weekday inference."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from .daily_status import page_received_times
from .d1_types import Projection, SourceRecord
from .model import DataError, canonical, digest, require

POLICY = "source-calendar-1"


def _date(value):
    if type(value) is date:
        return value
    if type(value) is str:
        try:
            day = date.fromisoformat(value)
            if day.isoformat() == value:
                return day
        except ValueError:
            pass
    raise DataError("INVALID_ARGUMENT", "日历日期须为date或YYYY-MM-DD，不截断datetime")


@dataclass(frozen=True)
class SourceCalendarFlag(Projection):
    raw_value: str | None
    value: bool | None
    state: str
    source_field: str = "is_trading_day"


@dataclass(frozen=True)
class SourceCalendarRow(SourceRecord):
    calendar_date: date
    is_open: SourceCalendarFlag
    capture_id: str
    raw_record_sha256: str | None
    raw_response_sha256: str | None
    response_received_at: str | None
    evidence_kind: str
    raw_json: str
    source: str = "baostock"
    exchange_scope: str = "provider_calendar_unspecified_exchange"
    historical_available_at: None = None
    historical_pit: bool = False
    security_eligibility: None = None
    exchange_certified: bool = False
    execution_permission: bool = False


@dataclass(frozen=True)
class SourceCalendarResult:
    capture_id: str
    rows: tuple[SourceCalendarRow, ...]
    report_json: str

    @property
    def report(self):
        return json.loads(self.report_json)

    def to_dict(self):
        return {"rows": [row.to_dict() for row in self.rows], "report": self.report}


@dataclass(frozen=True)
class SourceCalendarLink(Projection):
    trading_date: date
    previous_open: date
    next_open: date
    evidence_rows: tuple[SourceCalendarRow, ...]
    capture_id: str
    classification: str = "derived_from_source_calendar"
    historical_pit: bool = False
    security_eligibility: None = None
    execution_permission: bool = False


@dataclass(frozen=True)
class SourceCalendarLinksResult:
    capture_id: str
    rows: tuple[SourceCalendarLink, ...]
    report_json: str

    @property
    def report(self):
        return json.loads(self.report_json)

    def to_dict(self):
        return {"rows": [row.to_dict() for row in self.rows], "report": self.report}


def project(view, *, require_known=False):
    require(type(require_known) is bool, "INVALID_ARGUMENT", "require_known必须为bool")
    request = view._attempt["request"]
    require(
        request["method"] == "query_trade_dates",
        "QUERY_KIND_MISMATCH",
        "须为固定源日历capture；行情/状态不能替代日历",
    )
    require(
        view._status != "source_schema_invalid",
        "SOURCE_SCHEMA_ERROR",
        "源日历缺行、重复、越界或枚举无效；不推断关闭日",
        {"capture_id": view._id, "status": view._status},
    )
    require(
        view._status in {"research_rows", "empty_unknown"},
        "SOURCE_REQUEST_FAILED",
        "源日历响应未达到研究读取条件",
        {"capture_id": view._id, "status": view._status},
    )
    start = _date(request["params"]["start_date"])
    end = _date(request["params"]["end_date"])
    require(0 <= (end - start).days < 31, "SOURCE_SCHEMA_ERROR", "源日历须为1至31自然日")
    try:
        received = page_received_times(view._receipt)
    except DataError as exc:
        raise DataError(
            "SOURCE_CALENDAR_CLOCK_INVALID", "日历逐页时间与原始时钟事件不匹配或非法",
            {"capture_id": view._id, "cause": exc.code},
        ) from exc
    by_date = {r["calendar_date"]: r for r in view._rows}
    pages = {r["calendar_date"]: p for p in view._receipt["pages"] for r in p["rows"]}
    rows = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        raw = by_date.get(day.isoformat())
        page = pages.get(day.isoformat())
        value = raw["is_trading_day"] if raw else None
        require(value in (None, "0", "1"), "SOURCE_SCHEMA_ERROR", "日历源枚举未知，不转换为关闭")
        rows.append(SourceCalendarRow(
            calendar_date=day,
            is_open=SourceCalendarFlag(value, None if value is None else value == "1",
                                       "row_missing_unknown" if value is None else "source_observed"),
            capture_id=view._id,
            raw_record_sha256=digest(raw) if raw is not None else None,
            raw_response_sha256=page["raw_response_sha256"] if page else None,
            response_received_at=received[page["page_index"]] if page else None,
            evidence_kind=view._attempt["evidence_kind"],
            raw_json=canonical(raw if raw is not None else {}).decode(),
        ))
    missing = [r.calendar_date.isoformat() for r in rows if r.is_open.value is None]
    report = {
        "capture_id": view._id,
        "policy": POLICY,
        "request": request,
        "network_used": False,
        "quality": dict(view.quality(), exchange_certified=False, security_eligibility=None),
        "coverage": dict(view.coverage(), requested_calendar_days=len(rows), source_rows=len(by_date),
                         missing_dates=missing, requested_values_known=not missing,
                         weekday_inference=False),
        "definitions": {"is_open": "source is_trading_day: 1=open, 0=closed"},
        "limitations": ["provider calendar statement; exchange scope not independently certified",
                        "no security eligibility, intraday sessions, finality or historical PIT",
                        "receipt time is not historical publication time"],
    }
    require(
        not require_known or not missing, "SOURCE_CALENDAR_UNKNOWN", "请求自然日含未知日历状态",
        {"capture_id": view._id, "missing_dates": missing},
    )
    return SourceCalendarResult(view._id, tuple(rows), canonical(report).decode())


def links(view, *, trading_dates):
    require(
        type(trading_dates) in (list, tuple) and 1 <= len(trading_dates) <= 31,
        "INVALID_ARGUMENT", "trading_dates须为1至31个日期的list或tuple",
    )
    dates = tuple(_date(d) for d in trading_dates)
    require(len(set(dates)) == len(dates), "INVALID_ARGUMENT", "trading_dates不能重复")
    result = project(view)
    indexes = {r.calendar_date: i for i, r in enumerate(result.rows)}
    linked = []
    for day in dates:
        require(day in indexes, "SOURCE_CALENDAR_OUT_OF_SCOPE", "锚点越出固定capture请求范围",
                {"capture_id": view._id, "date": day.isoformat()})
        center = indexes[day]
        anchor = result.rows[center]
        require(anchor.is_open.value is not None, "SOURCE_CALENDAR_UNKNOWN", "锚点状态未知",
                {"capture_id": view._id, "date": day.isoformat()})
        require(anchor.is_open.value, "SOURCE_CALENDAR_NOT_OPEN", "来源声明锚点关闭，不能当执行日",
                {"capture_id": view._id, "date": day.isoformat()})

        def adjacent(step):
            index = center + step
            while 0 <= index < len(result.rows):
                row = result.rows[index]
                require(row.is_open.value is not None, "SOURCE_CALENDAR_UNKNOWN", "邻接路径含未知日",
                        {"capture_id": view._id, "date": row.calendar_date.isoformat()})
                if row.is_open.value:
                    return index
                index += step
            raise DataError(
                "SOURCE_CALENDAR_BOUNDARY_UNKNOWN", "捕获边界内没有所需开市邻日，禁止向外推算",
                {"capture_id": view._id, "date": day.isoformat(),
                 "direction": "previous" if step < 0 else "next"},
            )

        previous, following = adjacent(-1), adjacent(1)
        linked.append(SourceCalendarLink(
            day, result.rows[previous].calendar_date, result.rows[following].calendar_date,
            result.rows[previous:following + 1], view._id,
        ))
    report = dict(result.report, query={"trading_dates": [d.isoformat() for d in dates]},
                  classification="derived_from_source_calendar", neighbour_scope="same_capture_only",
                  execution_permission=False)
    return SourceCalendarLinksResult(view._id, tuple(linked), canonical(report).decode())
