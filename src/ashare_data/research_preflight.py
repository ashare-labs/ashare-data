"""Bounded post-hoc input inventory through public views; never an execution plan."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
import re

from .d1_types import Projection
from .model import DataError, digest, require

POLICY = "research-input-preflight-1"
ROLES = ("prior_day_research", "anchor_day_research", "successor_metadata")
REQUIREMENTS = {
    ROLES[0]: ("calendar_relation", "calendar_open", "close", "suspended", "is_st"),
    ROLES[1]: ("calendar_relation", "calendar_open", "open", "high", "low", "close",
               "volume", "amount", "source_preclose", "suspended", "is_st"),
    ROLES[2]: ("calendar_relation", "calendar_open", "suspended", "is_st"),
}
UNSUPPORTED = ("historical_eligibility", "listing_status", "official_price_limits", "rule_version",
               "corporate_action_coverage", "adjusted_prev_close", "historical_pit", "finality",
               "owner_execution_plan")


def _json(value):
    def default(v):
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        if isinstance(v, Decimal):
            return str(v)
        raise TypeError(type(v).__name__)
    return json.dumps(value, default=default, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _date(value):
    if type(value) is date:
        return value
    if type(value) is str:
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
    raise DataError("PREFLIGHT_ARGUMENT", "日期须为date或精确YYYY-MM-DD，不截断datetime")


@dataclass(frozen=True)
class ResearchInputRef(Projection):
    kind: str
    version_id: str
    store_path: str | None = None

    def identity(self):
        # Paths locate an already fixed version. Relocation does not change facts.
        return {"kind": self.kind, "version_id": self.version_id}


@dataclass(frozen=True)
class ResearchSecurityInputs(Projection):
    security: str
    price: ResearchInputRef | None = None
    status: ResearchInputRef | None = None
    preclose: ResearchInputRef | None = None


@dataclass(frozen=True)
class ResearchInputFact:
    field: str
    state: str
    value_json: str = "null"
    unit: str | None = None
    price_basis: str | None = None
    source_kind: str | None = None
    version_id: str | None = None
    evidence_json: str = "{}"
    reason: str | None = None

    @property
    def value(self):
        return json.loads(self.value_json)

    @property
    def evidence(self):
        return json.loads(self.evidence_json)

    def to_dict(self):
        return {"field": self.field, "state": self.state, "value": self.value,
                "unit": self.unit, "price_basis": self.price_basis,
                "source_kind": self.source_kind, "version_id": self.version_id,
                "evidence": self.evidence, "reason": self.reason}


@dataclass(frozen=True)
class ResearchInputRow:
    security: str
    anchor_date: date
    role: str
    trade_date: date | None
    facts: tuple[ResearchInputFact, ...]

    @property
    def research_values_complete(self):
        values = {f.field: f for f in self.facts}
        return all(values[name].state == "available" for name in REQUIREMENTS[self.role])

    @property
    def gaps(self):
        return tuple(f for f in self.facts if f.state != "available")

    def to_dict(self):
        body = {"security": self.security, "anchor_date": self.anchor_date.isoformat(),
                "role": self.role, "trade_date": self.trade_date.isoformat() if self.trade_date else None,
                "required_research_fields": list(REQUIREMENTS[self.role]),
                "research_values_complete": self.research_values_complete,
                "facts": [f.to_dict() for f in self.facts],
                "gaps": [{"field": f.field, "state": f.state, "reason": f.reason} for f in self.gaps],
                "execution_permission": False, "historical_pit": False}
        return dict(body, row_sha256=digest(body))


@dataclass(frozen=True)
class ResearchPreflightResult:
    report_id: str
    rows: tuple[ResearchInputRow, ...]
    request_json: str
    sources_json: str

    @property
    def request(self):
        return json.loads(self.request_json)

    @property
    def sources(self):
        return json.loads(self.sources_json)

    @property
    def research_values_complete(self):
        return all(row.research_values_complete for row in self.rows)

    def to_dict(self):
        return {"report_id": self.report_id, "policy": POLICY, "request": self.request,
                "sources": self.sources, "rows": [r.to_dict() for r in self.rows],
                "research_values_complete": self.research_values_complete,
                "execution_permission": False, "historical_pit": False, "network_used": False,
                "execution_admission": "BLOCKED_NO_OWNER_PLAN",
                "role_semantics": "research inventory only; no INITIALIZE/DECIDE/settlement permission"}


def _check_ref(ref, kinds):
    if ref is None:
        return
    require(type(ref) is ResearchInputRef and type(ref.kind) is str and ref.kind in kinds,
            "PREFLIGHT_ARGUMENT", "来源引用类型或kind不支持")
    require(type(ref.version_id) is str and re.fullmatch(r"[0-9a-f]{64}", ref.version_id),
            "PREFLIGHT_ARGUMENT", "必须显式固定完整小写版本ID")
    require(ref.store_path is None or type(ref.store_path) is str and bool(ref.store_path.strip()),
            "PREFLIGHT_ARGUMENT", "store_path须为本地路径字符串或None")


def _fact(name, value=None, *, state="available", ref=None, evidence=None, reason=None,
          unit=None, basis=None):
    return ResearchInputFact(name, state, _json(value), unit, basis,
                             ref.kind if ref else None, ref.version_id if ref else None,
                             _json(evidence or {}), reason)


def _missing(name, ref, reason, *, state="unknown", evidence=None):
    return _fact(name, state=state, ref=ref, reason=reason, evidence=evidence)


def _source_evidence(row, quality):
    return {"raw_record_sha256": row.raw_record_sha256,
            "raw_response_sha256": row.raw_response_sha256,
            "response_received_at": row.response_received_at,
            "historical_available_at": None, "source_fields": row.source_fields,
            "evidence_kind": row.evidence_kind, "quality": quality}


def preflight(store, *, inputs, calendar, trading_dates, require_complete=False):
    """Read explicit source bindings; malformed/corrupt bindings fail the whole call."""
    from .storage import Store
    require(type(require_complete) is bool, "PREFLIGHT_ARGUMENT", "require_complete必须为bool")
    require(type(inputs) in (list, tuple) and 1 <= len(inputs) <= 10,
            "PREFLIGHT_ARGUMENT", "inputs须包含1至10个证券绑定")
    require(type(trading_dates) in (list, tuple) and 1 <= len(trading_dates) <= 31,
            "PREFLIGHT_ARGUMENT", "trading_dates须包含1至31个锚点")
    dates = tuple(_date(d) for d in trading_dates)
    require(len(set(dates)) == len(dates) and (max(dates) - min(dates)).days < 31,
            "PREFLIGHT_ARGUMENT", "锚点不可重复，包络至多31自然日")
    codes = []
    for binding in inputs:
        require(type(binding) is ResearchSecurityInputs and type(binding.security) is str
                and re.fullmatch(r"[0-9]{6}\.(XSHG|XSHE)", binding.security),
                "PREFLIGHT_ARGUMENT", "证券须为规范六位ASCII代码与.XSHG/.XSHE")
        codes.append(binding.security)
        _check_ref(binding.price, {"research", "baostock"})
        _check_ref(binding.status, {"baostock"})
        _check_ref(binding.preclose, {"baostock"})
    require(len(set(codes)) == len(codes), "PREFLIGHT_ARGUMENT", "证券不可重复")
    _check_ref(calendar, {"baostock"})
    views, sources, projected = {}, {}, {}

    def open_ref(ref):
        if ref is None:
            return None
        key = (ref.kind, ref.version_id, ref.store_path)
        if key not in views:
            owner = store if ref.store_path is None else Store(ref.store_path)
            view = getattr(owner, ref.kind)(ref.version_id)
            views[key] = view
            sources[f"{ref.kind}:{ref.version_id}"] = {
                "identity": ref.identity(), "descriptor": view.descriptor(), "quality": view.quality()}
        return views[key]

    def daily_ref(ref, security, role):
        if ref is None:
            return None
        view = open_ref(ref)
        desc = view.descriptor()
        if ref.kind == "research":
            require(role == "price" and desc["price_basis"] == "raw_unadjusted"
                    and any(s["security"] == security and s["frequency"] == "daily"
                            for s in desc["scope"]),
                    "PREFLIGHT_BINDING_MISMATCH", "价格版本不含指定证券的原价日线")
        else:
            request = desc["request"]
            params = request["params"]
            expected = ("sh." if security.endswith(".XSHG") else "sz.") + security[:6]
            require(request["method"] == "query_history_k_data_plus"
                    and params.get("code") == expected and params.get("frequency") == "d"
                    and params.get("adjustflag") == "3",
                    "PREFLIGHT_BINDING_MISMATCH", "绑定的证券、日线频率或原价请求不匹配")
            key = (ref, role)
            if key not in projected:
                if role == "price":
                    value = view.get_price()
                    # Public status projection binds row/response hashes and clocks even
                    # when an old daily capture did not request the isST column.
                    proof = view.get_status()
                    projected[key] = (value, proof)
                else:
                    projected[key] = getattr(view, "get_status" if role == "status" else "get_preclose")()
        return view

    # Resolve all supplied IDs before producing a report, even when a date is missing.
    cal_view = open_ref(calendar)
    cal_result = cal_view.get_calendar() if cal_view else None
    cal_rows = {r.calendar_date: r for r in cal_result.rows} if cal_result else {}
    for b in inputs:
        for role in ("price", "status", "preclose"):
            daily_ref(getattr(b, role), b.security, role)

    def price_facts(binding, day, fields):
        ref = binding.price
        if ref is None or day is None:
            return [_missing(f, ref, "INPUT_NOT_BOUND" if ref is None else "DATE_UNRESOLVED") for f in fields]
        view = open_ref(ref)
        units = {"volume": "share", "amount": "CNY"}
        facts = []
        for field in fields:
            try:
                if ref.kind == "research":
                    result = view.get_price(binding.security, start_date=day.isoformat(),
                                            end_date=day.isoformat(), fields=[field])
                    rows = result.to_dict()["rows"]
                    require(len(rows) == 1 and rows[0]["time"].date() == day,
                            "PREFLIGHT_SOURCE_MISMATCH", "公共价格返回日期不等于请求日期")
                    records = [r for p in result.report["provenance"] for r in p["records"]]
                    require(len(records) == 1 and records[0]["security"] == binding.security
                            and records[0]["source_label"] == day.isoformat(),
                            "PREFLIGHT_SOURCE_MISMATCH", "公共价格证据身份不匹配")
                    r = records[0]
                    evidence = {"raw_record_sha256": r["raw_record_sha256"],
                                "raw_response_sha256": r["object_sha256"],
                                "response_received_at": r.get("response_completed_at"),
                                "historical_available_at": None, "source_fields": r["source_row"],
                                "quality": result.report["quality"],
                                "evidence_kind": "source_claim_unverified",
                                "observation_id": r.get("observation_id")}
                    value = rows[0][field]
                else:
                    result, proof = projected[(ref, "price")]
                    rows = [r for r in result.to_dict()["rows"] if r["source_label"] == day.isoformat()]
                    if not rows:
                        facts.append(_missing(field, ref, "DATE_NOT_CAPTURED"))
                        continue
                    require(len(rows) == 1 and rows[0]["security"] == binding.security,
                            "PREFLIGHT_SOURCE_MISMATCH", "公共价格行的身份不匹配")
                    source = next(r for r in proof.rows if r.trade_date == day)
                    evidence = _source_evidence(source, result.report["quality"])
                    value = rows[0].get(field)
                    if value is None:
                        facts.append(_missing(field, ref, "SOURCE_FIELD_MISSING", evidence=evidence))
                        continue
                facts.append(_fact(field, value, ref=ref, unit=units.get(field, "CNY/share"),
                                   basis="raw_unadjusted" if field in {"open", "high", "low", "close"} else None,
                                   evidence=evidence))
            except DataError as exc:
                if exc.code not in {"COVERAGE_INCOMPLETE", "SOURCE_FIELD_MISSING", "UNSUPPORTED_FIELD", "CACHE_MISS"}:
                    raise
                facts.append(_missing(field, ref, exc.code, evidence={"error": exc.as_dict()}))
        return facts

    def status_facts(binding, day):
        ref = binding.status
        if ref is None or day is None:
            return [_missing(f, ref, "INPUT_NOT_BOUND" if ref is None else "DATE_UNRESOLVED")
                    for f in ("suspended", "is_st")]
        result = projected[(ref, "status")]
        row = next((r for r in result.rows if r.trade_date == day), None)
        if row is None:
            return [_missing(f, ref, "DATE_OUT_OF_SCOPE") for f in ("suspended", "is_st")]
        return [_fact(name, flag.value, state="available" if flag.value is not None else "unknown",
                      ref=ref, reason=None if flag.value is not None else flag.state,
                      evidence=dict(_source_evidence(row, result.report["quality"]),
                                    source_field=flag.source_field, raw_value=flag.raw_value, source_state=flag.state))
                for name, flag in (("suspended", row.suspended), ("is_st", row.is_st))]

    def preclose_fact(binding, day):
        ref = binding.preclose
        if ref is None or day is None:
            return _missing("source_preclose", ref, "INPUT_NOT_BOUND" if ref is None else "DATE_UNRESOLVED")
        result = projected[(ref, "preclose")]
        row = next((r for r in result.rows if r.trade_date == day), None)
        if row is None:
            return _missing("source_preclose", ref, "DATE_OUT_OF_SCOPE")
        field = row.preclose
        return _fact("source_preclose", field.value,
                     state="available" if field.value is not None else "unknown",
                     ref=ref, unit="CNY/share", basis=row.price_basis,
                     reason=None if field.value is not None else field.state,
                     evidence=dict(_source_evidence(row, result.report["quality"]),
                                   raw_value=field.raw_value, source_state=field.state,
                                   definition=result.report["definition"]))

    rows = []
    for anchor in dates:
        link, error = None, None
        if cal_view:
            try:
                link = cal_view.get_calendar_links(trading_dates=[anchor]).rows[0]
            except DataError as exc:
                if exc.code not in {"SOURCE_CALENDAR_UNKNOWN", "SOURCE_CALENDAR_NOT_OPEN",
                                    "SOURCE_CALENDAR_OUT_OF_SCOPE", "SOURCE_CALENDAR_BOUNDARY_UNKNOWN"}:
                    raise
                error = exc.as_dict()
        else:
            error = {"code": "INPUT_NOT_BOUND", "message": "未绑定源日历", "details": {}}
        for b in inputs:
            for role, day in zip(ROLES, (link.previous_open if link else None, anchor,
                                       link.next_open if link else None)):
                relation = (_fact("calendar_relation", {"previous_open": link.previous_open,
                                  "trading_date": link.trading_date, "next_open": link.next_open},
                                  ref=calendar, evidence={"classification": link.classification,
                                      "days": [r.to_dict() for r in link.evidence_rows]}) if link else
                            _missing("calendar_relation", calendar, error["code"], evidence={"error": error}))
                cal_row = cal_rows.get(day)
                facts = [relation, (_fact("calendar_open", cal_row.is_open.value, ref=calendar,
                            state="available" if cal_row.is_open.value is not None else "unknown",
                            reason=None if cal_row.is_open.value is not None else cal_row.is_open.state,
                            evidence=_source_evidence(cal_row, cal_result.report["quality"])) if cal_row else
                            _missing("calendar_open", calendar, "DATE_UNRESOLVED" if day is None else
                                     "INPUT_NOT_BOUND" if calendar is None else "DATE_OUT_OF_SCOPE"))]
                if role == ROLES[0]:
                    facts.extend(price_facts(b, day, ("close",)))
                elif role == ROLES[1]:
                    facts.extend(price_facts(b, day, ("open", "high", "low", "close", "volume", "amount")))
                    facts.append(preclose_fact(b, day))
                facts.extend(status_facts(b, day))
                facts.extend(_missing(f, None, "NOT_SUPPORTED_BY_PREFLIGHT", state="unsupported") for f in UNSUPPORTED)
                rows.append(ResearchInputRow(b.security, anchor, role, day, tuple(facts)))
    request = {"inputs": [{"security": b.security, **{r: getattr(b, r).identity() if getattr(b, r) else None
                           for r in ("price", "status", "preclose")}} for b in inputs],
               "calendar": calendar.identity() if calendar else None,
               "trading_dates": [d.isoformat() for d in dates]}
    result = ResearchPreflightResult("", tuple(rows), _json(request), _json(sources))
    payload = result.to_dict()
    payload.pop("report_id")
    result = ResearchPreflightResult(digest(payload), result.rows, result.request_json, result.sources_json)
    require(not require_complete or result.research_values_complete, "PREFLIGHT_INCOMPLETE",
            "研究字段存在缺口；即使齐全也不授予执行许可", result.to_dict())
    return result


def from_request(store, request, *, require_complete=False):
    """Strict JSON adapter shared by public CLI and callers saving request documents."""
    require(type(request) is dict and set(request) == {"inputs", "calendar", "trading_dates"},
            "PREFLIGHT_ARGUMENT", "请求必须且只能包含inputs/calendar/trading_dates")
    require(type(request["inputs"]) is list and 1 <= len(request["inputs"]) <= 10,
            "PREFLIGHT_ARGUMENT", "inputs须为1至10个绑定的列表")

    def reference(value):
        if value is None:
            return None
        require(type(value) is dict and {"kind", "version_id"} <= set(value)
                and set(value) <= {"kind", "version_id", "store_path"},
                "PREFLIGHT_ARGUMENT", "来源引用只能包含kind/version_id/可选store_path")
        return ResearchInputRef(**value)

    bindings = []
    for b in request["inputs"]:
        require(type(b) is dict and "security" in b
                and set(b) <= {"security", "price", "status", "preclose"},
                "PREFLIGHT_ARGUMENT", "证券绑定字段无效")
        bindings.append(ResearchSecurityInputs(b["security"], *(reference(b.get(r))
                        for r in ("price", "status", "preclose"))))
    return preflight(store, inputs=bindings, calendar=reference(request["calendar"]),
                     trading_dates=request["trading_dates"], require_complete=require_complete)
