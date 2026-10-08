"""D1-BR1 conditional projections. Strict D1 facts and admission remain unchanged."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.resources import files

from . import d1
from .d1_types import (
    D1Instrument,
    D1LimitCandidate,
    D1OwnerReceipt,
    D1PrevCloseCall,
    D1PrevCloseCandidate,
    Projection,
    SourceRecord,
)
from .model import DataError, canonical, digest, require
from .storage import identifier, immutable_write, now

VERSION = "0.6.0.dev2"
MODE = "conditional_research"
PROFILE_SHA256 = "ded0d9dc2994aa70a8fa031a42987381937981445338c80671188857e6183cbd"
STRICT_DATASET = "4b75c097def48a69dd2042a7df70d835f1e29c7d1507be5c7cd4c6894bac8078"
ASSUMPTIONS = ("A-EQ", "A-NORMAL", "A-AD08")
TABLE = """CREATE TABLE IF NOT EXISTS br1_snapshots
(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('prepared','published','aborted')), created_at TEXT NOT NULL)"""


@dataclass(frozen=True)
class BR1Assumption(Projection):
    assumption_id: str
    statement: str
    depends_on: tuple[str, ...]
    evidence_fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class BR1Profile(SourceRecord):
    profile_id: str
    profile_version: str
    profile_sha256: str
    assumptions: tuple[BR1Assumption, ...]
    allowed_calls: tuple[D1PrevCloseCall, ...]
    required_native_checks: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    raw_json: str


@dataclass(frozen=True)
class BR1Descriptor(Projection):
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
class BR1Admission(Projection):
    descriptor: BR1Descriptor
    assumption_ids: tuple[str, ...]
    required_native_checks: tuple[str, ...]
    strict_blockers: tuple[str, ...]
    model_projection_permission: bool = True
    implementation_review: str = "PENDING_INDEPENDENT_REVIEW"
    execution_permission: bool = False
    execution_blockers: tuple[str, ...] = (
        "BR1_IMPLEMENTATION_REVIEW_REQUIRED",
        "BR1_BACKEND_BINDING_REVIEW_REQUIRED",
    )


@dataclass(frozen=True)
class BR1Receipt(Projection):
    owner: str
    descriptor: BR1Descriptor
    profile: BR1Profile
    strict_receipt: D1OwnerReceipt
    admission: BR1Admission
    evidence_status: str = "bounded_corroborated_review"
    result_classification: str = MODE


@dataclass(frozen=True)
class BR1Instrument(Projection):
    fact: D1Instrument
    profile_sha256: str
    assumption_ids: tuple[str, ...] = ("A-NORMAL",)
    model_eligible: bool = True
    evidence_status: str = "assumed_normal_applicability"
    execution_permission: bool = False


@dataclass(frozen=True)
class BR1ModeledEvents(Projection):
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
class BR1PriceLimits(Projection):
    trade_date: date
    lower: Decimal
    upper: Decimal
    source_candidate: D1LimitCandidate
    profile_sha256: str
    assumption_ids: tuple[str, ...] = ("A-EQ", "A-NORMAL")
    evidence_status: str = "derived_under_declared_assumptions"
    unit: str = "CNY/share"
    execution_permission: bool = False


@dataclass(frozen=True)
class BR1PrevClose(Projection):
    call: D1PrevCloseCall
    value: Decimal
    source_candidate: D1PrevCloseCandidate
    profile_sha256: str
    assumption_ids: tuple[str, ...] = ("A-EQ", "A-AD08")
    evidence_status: str = "derived_under_declared_assumptions"
    absolute_adjustment_factor: None = None
    unit: str = "CNY/share"
    execution_permission: bool = False


def _opt_in(mode, assumption_ids=None):
    require(
        type(mode) is str and mode == MODE,
        "BR1_EXPLICIT_MODE_REQUIRED",
        "须显式选择conditional_research；严格D1不会自动转入此模式",
    )
    if assumption_ids is not None:
        require(
            isinstance(assumption_ids, (list, tuple))
            and all(type(x) is str for x in assumption_ids)
            and len(assumption_ids) == 3
            and set(assumption_ids) == set(ASSUMPTIONS),
            "BR1_ASSUMPTIONS_REQUIRED",
            "须显式接受且仅接受A-EQ、A-NORMAL、A-AD08，各一次",
        )


def _profile_bytes():
    data = files("ashare_data").joinpath("br1-profile.json").read_bytes()
    require(d1._sha(data) == PROFILE_SHA256, "BR1_PROFILE_INTEGRITY", "安装包profile hash不符")
    return data


def _profile(data):
    p = json.loads(data)
    calls = []
    for c in p["allowed_calls"]:
        c = dict(c)
        for k in ("trade_date", "history_dt", "adjust_orig"):
            c[k] = date.fromisoformat(c[k])
        calls.append(D1PrevCloseCall(**c))
    return BR1Profile(
        p["profile_id"],
        p["profile_version"],
        PROFILE_SHA256,
        tuple(
            BR1Assumption(
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
        info.security == d1.SECURITY
        and info.kind == "ordinary_A_share"
        and info.exchange == "XSHG",
        "BR1_IDENTITY_CONFLICT",
        "BR1只支持固定普通A股身份",
    )
    statuses = view.statuses()
    require(
        len(statuses) == 4
        and tuple(x.trade_date for x in statuses) == d1.DAYS
        and all(
            x.security == d1.SECURITY and x.suspended is False and x.is_st is False
            for x in statuses
        ),
        "BR1_STATUS_BLOCKED",
        "源停牌/ST必须已知且为False；不关闭状态检查",
    )
    rules = view.rules()
    require(
        rules.buy_round_lot == 100
        and rules.price_tick == Decimal(".01")
        and rules.same_day_resale is False
        and rules.trading_cycle == "T+1",
        "BR1_RULE_CONFLICT",
        "固定lot/tick/T+1规则不符",
    )
    bars = view.bars()
    require(
        len(bars) == 4
        and tuple(x.trade_date for x in bars) == d1.DAYS
        and all(
            x.security == d1.SECURITY
            and x.price_basis == "raw_unadjusted"
            and x.object_sha256 == d1.PRICE_OBJECT
            for x in bars
        ),
        "BR1_PRICE_CONFLICT",
        "固定原价范围或身份不符",
    )
    for event in view.known_events():
        if event.entitled_security != d1.SECURITY:
            # Two confirmed preferred-share events are deliberately distinct.
            require(
                event.entitled_security == "360003.XSHG",
                "BR1_IDENTITY_CONFLICT",
                "出现未审事件权利证券",
            )
            continue
        require(event.record_date is not None, "BR1_EVENT_UNKNOWN", "普通股事件缺少资格日期")
        require(
            not date(2020, 1, 3) <= event.record_date <= date(2020, 1, 6),
            "BR1_RELATED_EVENT",
            "持有/登记窗口内存在相关事件，禁止模型空事件",
        )
        for key, d in (
            ("ex_date", event.ex_date),
            ("effective_date", event.effective_date),
            ("conversion_start", event.conversion_start),
            (
                "new_share_listing_date",
                d1._optional_date(event.source_fields.get("new_share_listing_date")),
            ),
        ):
            if d is not None:
                require(
                    not date(2019, 12, 31) <= d <= d1.END,
                    "BR1_RELATED_EVENT",
                    "价基窗口存在需处理事项，退出BR1",
                    {"event": event.event_id, "field": key},
                )
    candidates = view.prev_close_candidates()
    require(
        len(candidates) == 4 and all(x.observed_equality for x in candidates),
        "BR1_PRICE_CONFLICT",
        "源前收与有限raw依赖出现矛盾；不能套用等价假设",
    )


def _body():
    p = json.loads(_profile_bytes())
    return {
        "schema_version": 1,
        "kind": "d1_br1",
        "owner_version": VERSION,
        "mode": MODE,
        "strict_dataset_id": STRICT_DATASET,
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
        "SELECT 1 FROM sqlite_master WHERE name='br1_snapshots' AND type='table'"
    ).fetchone()


def _validate(store, body):
    require(
        canonical(body) == canonical(_body()),
        "BR1_MANIFEST_INTEGRITY",
        "BR1清单改变范围/假设/版本/依赖",
    )
    data = d1._read(d1._safe_path(store.root, "br1-objects/" + PROFILE_SHA256), 1024 * 1024)
    require(data == _profile_bytes(), "BR1_PROFILE_INTEGRITY", "封存profile hash/内容不符")
    view = store.d1(STRICT_DATASET)
    _conditions(view)
    return view


def compose(store, strict_dataset_id, *, mode, assumption_ids):
    _opt_in(mode)
    require(assumption_ids is not None, "BR1_ASSUMPTIONS_REQUIRED", "必须显式列出三项假设")
    _opt_in(mode, assumption_ids)
    require(
        identifier(strict_dataset_id) == STRICT_DATASET,
        "BR1_UNREVIEWED_INPUT",
        "BR1只接受固定严格D1组合，不能更换证券/时期",
    )
    _conditions(store.d1(strict_dataset_id))
    body = _body()
    data, sid = canonical(body), digest(body)
    with store._writer():
        for name in ("br1-objects", "br1-manifests"):
            d1._safe_path(store.root, name).mkdir(exist_ok=True)
        with store._db(write=True) as db:
            db.execute(TABLE)
            prior = db.execute("SELECT status FROM br1_snapshots WHERE id=?", (sid,)).fetchone()
        if prior and prior[0] == "published":
            BR1View(store, sid, mode=mode)
            return sid
        require(prior is None, "BR1_RECOVERY_REQUIRED", "已有未完成/拒绝发布；先显式恢复")
        immutable_write(
            d1._safe_path(store.root, "br1-objects/" + PROFILE_SHA256), _profile_bytes()
        )
        _validate(store, body)
        with store._db(write=True) as db:
            db.execute(
                "INSERT INTO br1_snapshots VALUES (?,?,?,?)",
                (sid, data.decode(), "prepared", now()),
            )
        immutable_write(d1._safe_path(store.root, "br1-manifests/" + sid + ".json"), data)
        with store._db(write=True) as db:
            db.execute("UPDATE br1_snapshots SET status='published' WHERE id=?", (sid,))
    return sid


def snapshots(store):
    with store._db() as db:
        if not _exists(db):
            return []
        return [
            dict(r)
            for r in db.execute(
                "SELECT id AS dataset_id,status,created_at FROM br1_snapshots ORDER BY id"
            )
        ]


def recover(store):
    results = []
    with store._writer():
        with store._db() as db:
            if not _exists(db):
                return []
            rows = db.execute(
                "SELECT id,body FROM br1_snapshots WHERE status='prepared' ORDER BY id"
            ).fetchall()
        for row in rows:
            try:
                sid, data = identifier(row["id"]), row["body"].encode()
                require(d1._sha(data) == sid, "BR1_MANIFEST_INTEGRITY", "prepared hash不符")
                _validate(store, d1._json(data))
                immutable_write(d1._safe_path(store.root, "br1-manifests/" + sid + ".json"), data)
                status, error = "published", None
            except (DataError, OSError, KeyError, TypeError, ValueError) as exc:
                status, error = (
                    "aborted",
                    exc.code if isinstance(exc, DataError) else "BR1_INTEGRITY",
                )
            with store._db(write=True) as db:
                db.execute("UPDATE br1_snapshots SET status=? WHERE id=?", (status, row["id"]))
            results.append({"dataset_id": row["id"], "status": status, "error": error})
    return results


class BR1View:
    """Assumption-labeled model inputs only. Never a native execution permission."""

    def __init__(self, store, dataset_id, *, mode):
        _opt_in(mode)
        sid = identifier(dataset_id)
        with store._db() as db:
            require(_exists(db), "BR1_NOT_FOUND", "没有固定BR1版本")
            row = db.execute("SELECT body,status FROM br1_snapshots WHERE id=?", (sid,)).fetchone()
        require(row is not None, "BR1_NOT_FOUND", "固定BR1版本不存在")
        require(row["status"] == "published", "BR1_NOT_PUBLISHED", "BR1尚未发布")
        data = d1._read(d1._safe_path(store.root, "br1-manifests/" + sid + ".json"), 1024 * 1024)
        require(
            d1._sha(data) == sid and data == row["body"].encode(),
            "BR1_MANIFEST_INTEGRITY",
            "清单与目录hash不符",
        )
        self._strict = _validate(store, d1._json(data))
        self._id = sid

    def descriptor(self) -> BR1Descriptor:
        s = self._strict.descriptor()
        return BR1Descriptor(
            self._id,
            self._id,
            s.dataset_id,
            PROFILE_SHA256,
            s.price_dataset_id,
            s.facts_component_id,
            s.source_manifest_sha256,
        )

    def profile(self) -> BR1Profile:
        return _profile(_profile_bytes())

    def admission(self) -> BR1Admission:
        return BR1Admission(
            self.descriptor(),
            ASSUMPTIONS,
            self.profile().required_native_checks,
            self._strict.admission().blockers,
        )

    def owner_receipt(self) -> BR1Receipt:
        return BR1Receipt(
            "ashare-data",
            self.descriptor(),
            self.profile(),
            self._strict.owner_receipt(),
            self.admission(),
        )

    def instrument(self) -> BR1Instrument:
        return BR1Instrument(self._strict.instrument(), PROFILE_SHA256)

    def rules(self):
        return self._strict.rules()

    def bars(self):
        return self._strict.bars()

    def calendar(self):
        return self._strict.calendar()

    def statuses(self):
        return self._strict.statuses()

    def modeled_events(self) -> BR1ModeledEvents:
        return BR1ModeledEvents(
            PROFILE_SHA256, tuple(x.event_id for x in self._strict.known_events())
        )

    def price_limits(self, trade_date: str | date) -> BR1PriceLimits:
        d = d1._scoped_date(trade_date.isoformat() if type(trade_date) is date else trade_date)
        c = next(x for x in self._strict.limit_candidates() if x.trade_date == d)
        return BR1PriceLimits(d, c.lower, c.upper, c, PROFILE_SHA256)

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
    ) -> BR1PrevClose:
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
            "BR1_UNREVIEWED_CALL",
            "AD08类型须精确；bool不能作为1或0",
        )
        call = next((x for x in self.profile().allowed_calls if x.to_dict() == supplied), None)
        require(call is not None, "BR1_UNREVIEWED_CALL", "拒绝四个固定tuple之外的请求")
        c = next(x for x in self._strict.prev_close_candidates() if x.call == call)
        return BR1PrevClose(call, c.candidate_value, c, PROFILE_SHA256)

    def require_execution(self):
        raise DataError(
            "BR1_REVIEW_REQUIRED",
            "公共实现与后端绑定仍待独审，当前只开放条件投影",
            self.admission().to_dict(),
        )
