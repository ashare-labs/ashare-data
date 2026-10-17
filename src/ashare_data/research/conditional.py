"""FACTS-CONDITIONAL conditional projections. Strict FACTS facts and admission remain unchanged."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.resources import files

from ashare_data.research import finite_facts as facts
from ashare_data.research.fact_types import (
    FactInstrument,
    FactLimitCandidate,
    FactReceipt,
    PreviousCloseCall,
    FactPrevCloseCandidate,
    Projection,
    SourceRecord,
)
from ashare_data.core.model import DataError, canonical, digest, require
from ashare_data.storage.immutable import identifier, immutable_write, now

VERSION = "0.10.0.dev1"
MODE = "conditional_research"
PROFILE_SHA256 = "c6ffda7c801f0c6c939938b92c2f3d71851d0151695bb59c4be1f3796405c8ed"
ASSUMPTIONS = ("A-EQ", "A-NORMAL", "A-AD08")
TABLE = """CREATE TABLE IF NOT EXISTS conditional_snapshots
(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('prepared','published','aborted')), created_at TEXT NOT NULL)"""


@dataclass(frozen=True)
class ConditionalAssumption(Projection):
    assumption_id: str
    statement: str
    depends_on: tuple[str, ...]
    evidence_fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class ConditionalProfile(SourceRecord):
    profile_id: str
    profile_version: str
    profile_sha256: str
    assumptions: tuple[ConditionalAssumption, ...]
    allowed_calls: tuple[PreviousCloseCall, ...]
    required_native_checks: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    raw_json: str


@dataclass(frozen=True)
class ConditionalDescriptor(Projection):
    dataset_id: str
    manifest_sha256: str
    strict_dataset_id: str
    profile_sha256: str
    price_dataset_id: str
    facts_component_id: str
    facts_manifest_sha256: str
    owner_version: str = VERSION
    mode: str = MODE
    complete: bool = False
    verified_absent: bool = False
    execution_permission: bool = False
    historical_pit: bool = False
    status: str = "CONDITIONAL_PROJECTION_ONLY"


@dataclass(frozen=True)
class ConditionalAdmission(Projection):
    descriptor: ConditionalDescriptor
    assumption_ids: tuple[str, ...]
    required_native_checks: tuple[str, ...]
    strict_blockers: tuple[str, ...]
    model_projection_permission: bool = True
    implementation_review: str = "PENDING_INDEPENDENT_REVIEW"
    execution_permission: bool = False
    execution_blockers: tuple[str, ...] = (
        "CONDITIONAL_IMPLEMENTATION_REVIEW_REQUIRED",
        "CONDITIONAL_BACKEND_BINDING_REVIEW_REQUIRED",
    )


@dataclass(frozen=True)
class ConditionalReceipt(Projection):
    owner: str
    descriptor: ConditionalDescriptor
    profile: ConditionalProfile
    strict_receipt: FactReceipt
    admission: ConditionalAdmission
    evidence_status: str = "bounded_corroborated_review"
    result_classification: str = MODE


@dataclass(frozen=True)
class ConditionalInstrument(Projection):
    fact: FactInstrument
    profile_sha256: str
    assumption_ids: tuple[str, ...] = ("A-NORMAL",)
    model_eligible: bool = True
    evidence_status: str = "assumed_normal_applicability"
    execution_permission: bool = False


@dataclass(frozen=True)
class ConditionalModeledEvents(Projection):
    profile_sha256: str
    known_event_ids: tuple[str, ...]
    modeled_events: tuple = ()
    assumption_ids: tuple[str, ...] = ("A-EQ",)
    evidence_status: str = "assumed_no_relevant_events"
    source_evidence_status: str = "bounded_corroborated_review"
    verified_absent: bool = False
    complete_economic_event_set: bool = False
    execution_permission: bool = False


@dataclass(frozen=True)
class ConditionalPriceLimits(Projection):
    trade_date: date
    lower: Decimal
    upper: Decimal
    source_candidate: FactLimitCandidate
    profile_sha256: str
    assumption_ids: tuple[str, ...] = ("A-EQ", "A-NORMAL")
    evidence_status: str = "derived_under_declared_assumptions"
    unit: str = "CNY/share"
    execution_permission: bool = False


@dataclass(frozen=True)
class ConditionalPrevClose(Projection):
    call: PreviousCloseCall
    value: Decimal
    source_candidate: FactPrevCloseCandidate
    profile_sha256: str
    assumption_ids: tuple[str, ...] = ("A-EQ", "A-AD08")
    evidence_status: str = "derived_under_declared_assumptions"
    absolute_adjustment_factor: None = None
    unit: str = "CNY/share"
    execution_permission: bool = False


def _opt_in(mode, assumption_ids=None):
    require(
        type(mode) is str and mode == MODE,
        "CONDITIONAL_EXPLICIT_MODE_REQUIRED",
        "须显式选择conditional_research；严格FACTS不会自动转入此模式",
    )
    if assumption_ids is not None:
        require(
            isinstance(assumption_ids, (list, tuple))
            and all(type(x) is str for x in assumption_ids)
            and len(assumption_ids) == 3
            and set(assumption_ids) == set(ASSUMPTIONS),
            "CONDITIONAL_ASSUMPTIONS_REQUIRED",
            "须显式接受且仅接受A-EQ、A-NORMAL、A-AD08，各一次",
        )


def _profile_bytes():
    data = files("ashare_data.research").joinpath("conditional-profile.json").read_bytes()
    require(
        facts._sha(data) == PROFILE_SHA256,
        "CONDITIONAL_PROFILE_INTEGRITY",
        "安装包profile hash不符",
    )
    return data


def _profile(data):
    p = json.loads(data)
    calls = []
    for c in p["allowed_calls"]:
        c = dict(c)
        for k in ("trade_date", "history_dt", "adjust_orig"):
            c[k] = date.fromisoformat(c[k])
        calls.append(PreviousCloseCall(**c))
    return ConditionalProfile(
        p["profile_id"],
        p["profile_version"],
        PROFILE_SHA256,
        tuple(
            ConditionalAssumption(
                a["id"], a["statement"], tuple(a["depends_on"]), tuple(a["evidence_fact_ids"])
            )
            for a in p["assumptions"]
        ),
        tuple(calls),
        tuple(p["required_native_checks"]),
        tuple(p["stop_conditions"]),
        data.decode(),
    )


def _conditions(view):
    """Refuse contradictory scoped facts; no absence claim is inferred here."""
    info = view.instrument()
    require(
        info.security == facts.SECURITY
        and info.kind == "ordinary_A_share"
        and info.exchange == "XSHG",
        "CONDITIONAL_IDENTITY_CONFLICT",
        "CONDITIONAL只支持固定普通A股身份",
    )
    statuses = view.statuses()
    require(
        len(statuses) == 4
        and tuple(x.trade_date for x in statuses) == facts.DAYS
        and all(
            x.security == facts.SECURITY and x.suspended is False and x.is_st is False
            for x in statuses
        ),
        "CONDITIONAL_STATUS_BLOCKED",
        "源停牌/ST必须已知且为False；不关闭状态检查",
    )
    rules = view.rules()
    require(
        rules.buy_round_lot == 100
        and rules.price_tick == Decimal(".01")
        and rules.same_day_resale is False
        and rules.trading_cycle == "T+1",
        "CONDITIONAL_RULE_CONFLICT",
        "固定lot/tick/T+1规则不符",
    )
    bars = view.bars()
    require(
        len(bars) == 4
        and tuple(x.trade_date for x in bars) == facts.DAYS
        and all(
            x.security == facts.SECURITY
            and x.price_basis == "raw_unadjusted"
            and x.object_sha256 == facts.PRICE_OBJECT
            for x in bars
        ),
        "CONDITIONAL_PRICE_CONFLICT",
        "固定原价范围或身份不符",
    )
    for event in view.known_events():
        if event.entitled_security != facts.SECURITY:
            # Two confirmed preferred-share events are deliberately distinct.
            require(
                event.entitled_security == "360003.XSHG",
                "CONDITIONAL_IDENTITY_CONFLICT",
                "出现未审事件权利证券",
            )
            continue
        require(
            event.record_date is not None, "CONDITIONAL_EVENT_UNKNOWN", "普通股事件缺少资格日期"
        )
        require(
            not date(2020, 1, 3) <= event.record_date <= date(2020, 1, 6),
            "CONDITIONAL_RELATED_EVENT",
            "持有/登记窗口内存在相关事件，禁止模型空事件",
        )
        for key, d in (
            ("ex_date", event.ex_date),
            ("effective_date", event.effective_date),
            ("conversion_start", event.conversion_start),
            (
                "new_share_listing_date",
                facts._optional_date(event.source_fields.get("new_share_listing_date")),
            ),
        ):
            if d is not None:
                require(
                    not date(2019, 12, 31) <= d <= facts.END,
                    "CONDITIONAL_RELATED_EVENT",
                    "价基窗口存在需处理事项，退出CONDITIONAL",
                    {"event": event.event_id, "field": key},
                )
    candidates = view.prev_close_candidates()
    require(
        len(candidates) == 4 and all(x.observed_equality for x in candidates),
        "CONDITIONAL_PRICE_CONFLICT",
        "源前收与有限raw依赖出现矛盾；不能套用等价假设",
    )


def _body(strict_dataset_id):
    p = json.loads(_profile_bytes())
    return {
        "schema_version": 1,
        "kind": "facts_conditional",
        "owner_version": VERSION,
        "mode": MODE,
        "strict_dataset_id": strict_dataset_id,
        "profile_sha256": PROFILE_SHA256,
        "price_dataset_id": p["price_dataset_id"],
        "facts_manifest_sha256": p["facts_manifest_sha256"],
        "assumption_ids": list(ASSUMPTIONS),
        "allowed_calls": p["allowed_calls"],
        "rules_effective_from": p["rules_effective_from"],
        "limit_calculation_version": p["limit_calculation_version"],
        "rqalpha_wheel_sha256": p["rqalpha_wheel_sha256"],
        "independent_closure_addendum_sha256": p["independent_closure_addendum_sha256"],
        "backend_profile_reference_sha256": p["backend_profile_reference_sha256"],
        "complete": False,
        "verified_absent": False,
        "execution_permission": False,
    }


def _exists(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='conditional_snapshots' AND type='table'"
    ).fetchone()


def _validate(store, body):
    require(
        canonical(body) == canonical(_body(body.get("strict_dataset_id"))),
        "CONDITIONAL_MANIFEST_INTEGRITY",
        "CONDITIONAL清单改变范围/假设/版本/依赖",
    )
    data = facts._read(
        facts._safe_path(store.root, "conditional-objects/" + PROFILE_SHA256), 1024 * 1024
    )
    require(data == _profile_bytes(), "CONDITIONAL_PROFILE_INTEGRITY", "封存profile hash/内容不符")
    view = store.facts(identifier(body["strict_dataset_id"]))
    _conditions(view)
    return view


def compose(store, strict_dataset_id, *, mode, assumption_ids):
    _opt_in(mode)
    require(assumption_ids is not None, "CONDITIONAL_ASSUMPTIONS_REQUIRED", "必须显式列出三项假设")
    _opt_in(mode, assumption_ids)
    # FiniteFactsView validates the exact price, source manifest and policy.
    # Its composite ID changes when this implementation's owner version changes.
    try:
        view = store.facts(identifier(strict_dataset_id))
    except DataError as exc:
        raise DataError("CONDITIONAL_UNREVIEWED_INPUT", "需要此版本验证的有限事实组合") from exc
    _conditions(view)
    body = _body(strict_dataset_id)
    data, sid = canonical(body), digest(body)
    with store._writer():
        for name in ("conditional-objects", "conditional-manifests"):
            facts._safe_path(store.root, name).mkdir(exist_ok=True)
        with store._db(write=True) as db:
            db.execute(TABLE)
            prior = db.execute(
                "SELECT status FROM conditional_snapshots WHERE id=?", (sid,)
            ).fetchone()
        if prior and prior[0] == "published":
            ConditionalView(store, sid, mode=mode)
            return sid
        require(prior is None, "CONDITIONAL_RECOVERY_REQUIRED", "已有未完成/拒绝发布；先显式恢复")
        immutable_write(
            facts._safe_path(store.root, "conditional-objects/" + PROFILE_SHA256), _profile_bytes()
        )
        _validate(store, body)
        with store._db(write=True) as db:
            db.execute(
                "INSERT INTO conditional_snapshots VALUES (?,?,?,?)",
                (sid, data.decode(), "prepared", now()),
            )
        immutable_write(
            facts._safe_path(store.root, "conditional-manifests/" + sid + ".json"), data
        )
        with store._db(write=True) as db:
            db.execute("UPDATE conditional_snapshots SET status='published' WHERE id=?", (sid,))
    return sid


def snapshots(store):
    with store._db() as db:
        if not _exists(db):
            return []
        return [
            dict(r)
            for r in db.execute(
                "SELECT id AS dataset_id,status,created_at FROM conditional_snapshots ORDER BY id"
            )
        ]


def recover(store):
    results = []
    with store._writer():
        with store._db() as db:
            if not _exists(db):
                return []
            rows = db.execute(
                "SELECT id,body FROM conditional_snapshots WHERE status='prepared' ORDER BY id"
            ).fetchall()
        for row in rows:
            try:
                sid, data = identifier(row["id"]), row["body"].encode()
                require(
                    facts._sha(data) == sid, "CONDITIONAL_MANIFEST_INTEGRITY", "prepared hash不符"
                )
                _validate(store, facts._json(data))
                immutable_write(
                    facts._safe_path(store.root, "conditional-manifests/" + sid + ".json"), data
                )
                status, error = "published", None
            except (DataError, OSError, KeyError, TypeError, ValueError) as exc:
                status, error = (
                    "aborted",
                    exc.code if isinstance(exc, DataError) else "CONDITIONAL_INTEGRITY",
                )
            with store._db(write=True) as db:
                db.execute(
                    "UPDATE conditional_snapshots SET status=? WHERE id=?", (status, row["id"])
                )
            results.append({"dataset_id": row["id"], "status": status, "error": error})
    return results


class ConditionalView:
    """Assumption-labeled model inputs only. Never a native execution permission."""

    def __init__(self, store, dataset_id, *, mode):
        _opt_in(mode)
        sid = identifier(dataset_id)
        with store._db() as db:
            require(_exists(db), "CONDITIONAL_NOT_FOUND", "没有固定CONDITIONAL版本")
            row = db.execute(
                "SELECT body,status FROM conditional_snapshots WHERE id=?", (sid,)
            ).fetchone()
        require(row is not None, "CONDITIONAL_NOT_FOUND", "固定CONDITIONAL版本不存在")
        require(row["status"] == "published", "CONDITIONAL_NOT_PUBLISHED", "CONDITIONAL尚未发布")
        data = facts._read(
            facts._safe_path(store.root, "conditional-manifests/" + sid + ".json"), 1024 * 1024
        )
        require(
            facts._sha(data) == sid and data == row["body"].encode(),
            "CONDITIONAL_MANIFEST_INTEGRITY",
            "清单与目录hash不符",
        )
        self._strict = _validate(store, facts._json(data))
        self._id = sid
        self._store = store

    def listing_contract(self):
        from ashare_data.research.conditional_listing import contract

        return contract(self._store.conditional(self._id, mode=MODE))

    def listing(self, *, contract_sha256):
        from ashare_data.research.conditional_listing import ConditionalListingView

        return ConditionalListingView(self._store, self._id, contract_sha256=contract_sha256)

    def descriptor(self) -> ConditionalDescriptor:
        s = self._strict.descriptor()
        return ConditionalDescriptor(
            self._id,
            self._id,
            s.dataset_id,
            PROFILE_SHA256,
            s.price_dataset_id,
            s.facts_component_id,
            s.source_manifest_sha256,
        )

    def profile(self) -> ConditionalProfile:
        return _profile(_profile_bytes())

    def admission(self) -> ConditionalAdmission:
        return ConditionalAdmission(
            self.descriptor(),
            ASSUMPTIONS,
            self.profile().required_native_checks,
            self._strict.admission().blockers,
        )

    def owner_receipt(self) -> ConditionalReceipt:
        return ConditionalReceipt(
            "ashare-data",
            self.descriptor(),
            self.profile(),
            self._strict.owner_receipt(),
            self.admission(),
        )

    def instrument(self) -> ConditionalInstrument:
        return ConditionalInstrument(self._strict.instrument(), PROFILE_SHA256)

    def rules(self):
        return self._strict.rules()

    def bars(self):
        return self._strict.bars()

    def calendar(self):
        return self._strict.calendar()

    def statuses(self):
        return self._strict.statuses()

    def modeled_events(self) -> ConditionalModeledEvents:
        return ConditionalModeledEvents(
            PROFILE_SHA256, tuple(x.event_id for x in self._strict.known_events())
        )

    def price_limits(self, trade_date: str | date) -> ConditionalPriceLimits:
        d = facts._scoped_date(trade_date.isoformat() if type(trade_date) is date else trade_date)
        c = next(x for x in self._strict.limit_candidates() if x.trade_date == d)
        return ConditionalPriceLimits(d, c.lower, c.upper, c, PROFILE_SHA256)

    def adjusted_prev_close(
        self,
        *,
        security: str,
        trade_date: str | date,
        history_dt: str | date,
        adjust_orig: str | date,
        frequency: str,
        field: str,
        bar_count: int,
        include_now: bool,
        skip_suspended: bool,
        adjustment_requested: str,
    ) -> ConditionalPrevClose:
        supplied = dict(
            security=security,
            trade_date=trade_date,
            history_dt=history_dt,
            adjust_orig=adjust_orig,
            frequency=frequency,
            field=field,
            bar_count=bar_count,
            include_now=include_now,
            skip_suspended=skip_suspended,
            adjustment_requested=adjustment_requested,
        )
        # Backend DTOs use exact date objects. No datetime/tz coercion or date
        # inference occurs here; JSON calls use the equivalent ISO date strings.
        for key in ("trade_date", "history_dt", "adjust_orig"):
            if type(supplied[key]) is date:
                supplied[key] = supplied[key].isoformat()
        require(
            type(bar_count) is int and type(include_now) is bool and type(skip_suspended) is bool,
            "CONDITIONAL_UNREVIEWED_CALL",
            "AD08类型须精确；bool不能作为1或0",
        )
        call = next((x for x in self.profile().allowed_calls if x.to_dict() == supplied), None)
        require(call is not None, "CONDITIONAL_UNREVIEWED_CALL", "拒绝四个固定tuple之外的请求")
        c = next(x for x in self._strict.prev_close_candidates() if x.call == call)
        return ConditionalPrevClose(call, c.candidate_value, c, PROFILE_SHA256)

    def require_execution(self):
        raise DataError(
            "CONDITIONAL_REVIEW_REQUIRED",
            "公共实现与后端绑定仍待独审，当前只开放条件投影",
            self.admission().to_dict(),
        )
