"""Pinned, offline, finite D1 facts. No acquisition or execution side effects.

Admission is owner policy for one reviewed evidence package, never an imported
claim. New evidence or wider scope requires a new reviewed policy and snapshot.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path, PurePosixPath

from .d1_types import (
    D1Admission,
    D1Bar,
    D1CalendarDay,
    D1Coverage,
    D1Descriptor,
    D1Event,
    D1EventCoverage,
    D1FactClaim,
    D1Instrument,
    D1LimitCandidate,
    D1Lineage,
    D1OwnerReceipt,
    D1PrevCloseCall,
    D1PrevCloseCandidate,
    D1Quality,
    D1Requirement,
    D1Rules,
    D1SourcePrevClose,
    D1Status,
    Evidence,
    FactRecord,
)
from .model import DataError, canonical, day, digest, require
from .storage import identifier, immutable_write, now

OWNER_VERSION = "0.6.0.dev1"
POLICY = "d1-600000-202001-review-1"
SOURCE_MANIFEST = "499cc2b4e2985f6597f1d8923fbf1878fcb148f4a0764835607cb3919b040d82"
PRICE_DATASET = "31bc25c99e00c8b5c43f8d77daeae9ae9bc41116f40802b0aad10567e6259973"
PRICE_OBJECT = "e7a455dce264f9be2ead339c7268c8cb1d93321f5853dbd742cab331caa5395e"
CALENDAR_OBJECT = "b436cc262b51a36bea2d48a5d35a0c92b9dca39791a79aa4aec9c2003df1bacd"
PLAN_SHA256 = "8863e4598b8c70f1813a2c86ce2ef0abb4e568828bc495ad769bca1101208bcd"
REQUEST_ID = "AQ-D1-600000-20200103-20200106-v2"
SECURITY = "600000.XSHG"
START, END = date(2020, 1, 2), date(2020, 1, 7)
DAYS = tuple(date(2020, 1, d) for d in (2, 3, 6, 7))
PREVIOUS = (date(2019, 12, 31), *DAYS[:-1])
MAX_BYTES = 32 * 1024 * 1024
TABLE = """CREATE TABLE IF NOT EXISTS d1_snapshots
(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('prepared','published','aborted')), created_at TEXT NOT NULL)"""
BLOCKERS = {
    "instrument": "窗口内历史资格、上市转换及例外边界未闭合；当前元数据和板块推断不能替代。",
    "price_limits": "普通公式已知；每日普通规则适用性、例外及除权参考价未获确认。",
    "event_absence": "有限标题与精选正文审阅未识别相关事件，尚无按类型/资格日/价基日闭合的无相关事件证明。",
    "adjusted_prev_close": "已审调用tuple仍缺相应history_dt至adjust_orig无调整的证据；数值相等不是证明。",
}
REQUIREMENTS = (
    "identity",
    "raw_bars",
    "calendar",
    "instrument",
    "trading_rules",
    "suspended",
    "st_status",
    "price_limits",
    "event_absence",
    "adjusted_prev_close",
    "owner_scope",
    "receipt",
    "historical_pit",
    "market_authenticity",
    "closing_queue",
    "personal_fees",
    "amount",
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise DataError("D1_INTEGRITY", "D1 JSON损坏") from exc


def _read(path, limit=MAX_BYTES):
    try:
        path = Path(path)
        require(
            not path.is_symlink() and path.is_file() and path.stat().st_size <= limit,
            "D1_INPUT_INVALID",
            "文件缺失、符号链接或超过有界大小",
        )
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        require(len(data) <= limit, "D1_INPUT_INVALID", "文件过大")
        return data
    except OSError as exc:
        raise DataError("D1_INPUT_INVALID", "本地文件无法读取") from exc


def _relative(name):
    require(
        isinstance(name, str) and bool(name) and "\\" not in name,
        "D1_INPUT_INVALID",
        "证据路径无效",
    )
    p = PurePosixPath(name)
    require(
        not p.is_absolute() and str(p) == name and not any(x in {".", ".."} for x in p.parts),
        "D1_INPUT_INVALID",
        "证据路径须为规范相对路径",
    )
    return p


def _safe_path(root, name):
    p = _relative(name)
    result = root
    require(not root.is_symlink(), "D1_INPUT_INVALID", "拒绝链接目录")
    for part in p.parts:
        result = result / part
        require(not result.is_symlink(), "D1_INPUT_INVALID", "拒绝链接证据路径")
    return result


def _base(kind):
    return {
        "schema_version": 1,
        "kind": kind,
        "owner_version": OWNER_VERSION,
        "policy_version": POLICY,
        "plan_sha256": PLAN_SHA256,
        "security": SECURITY,
        "scope_start": START.isoformat(),
        "scope_end": END.isoformat(),
        "complete": False,
        "execution_permission": False,
    }


def _facts_body(source):
    require(
        _sha(source) == SOURCE_MANIFEST,
        "D1_UNREVIEWED_EVIDENCE",
        "只接纳此版本已审阅的精确证据清单；改写和重算hash不构成审阅",
    )
    parsed = _json(source)
    entries = parsed.get("files")
    require(
        isinstance(entries, list) and len(entries) == 150,
        "D1_INPUT_INVALID",
        "须为已审阅的150文件包",
    )
    names, total = set(), 0
    for entry in entries:
        name = str(_relative(entry["path"]))
        require(
            name not in names and type(entry["bytes"]) is int and 0 <= entry["bytes"] <= MAX_BYTES,
            "D1_INPUT_INVALID",
            "重复文件或无效字节数",
        )
        identifier(entry["sha256"])
        names.add(name)
        total += entry["bytes"]
    require(total <= MAX_BYTES, "D1_INPUT_INVALID", "包合计超过32MiB")
    return {**_base("d1_facts"), "source_manifest_sha256": SOURCE_MANIFEST, "files": entries}


def _composite_body(facts_id):
    return {
        **_base("d1_composite"),
        "facts_component_id": identifier(facts_id),
        "price_dataset_id": PRICE_DATASET,
        "source_manifest_sha256": SOURCE_MANIFEST,
        "request_id": REQUEST_ID,
        "price_basis": "raw_unadjusted",
        "initial_quantity": 0,
        "initial_entitlements": 0,
        "required_unknown": list(BLOCKERS),
    }


def _exists(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='d1_snapshots' AND type='table'"
    ).fetchone()


def snapshots(store):
    with store._db() as db:
        if not _exists(db):
            return []
        return [
            dict(r)
            for r in db.execute(
                "SELECT id AS dataset_id,status,created_at FROM d1_snapshots ORDER BY id"
            )
        ]


def _object(store, h):
    return _read(_safe_path(store.root, "d1-objects/" + identifier(h)))


def _validate_facts(store, body):
    source = _object(store, SOURCE_MANIFEST)
    expected = _facts_body(source)
    require(canonical(body) == canonical(expected), "D1_INTEGRITY", "事实组件清单不符已审阅版本")
    objects = {}
    for entry in expected["files"]:
        blob = _object(store, entry["sha256"])
        require(
            len(blob) == entry["bytes"] and _sha(blob) == entry["sha256"],
            "D1_INTEGRITY",
            "事实原件大小/hash不符",
            {"path": entry["path"]},
        )
        objects[entry["path"]] = blob
    return objects


def _price(store):
    # The public research reader rechecks its own journal, manifest and every object.
    view = store.research(PRICE_DATASET)
    lineage = view.lineage()
    rows = {r["source_label"]: r for r in lineage["raw_records"] if r["security"] == SECURITY}
    calendar = lineage["source_calendar"]
    require(
        all(d.isoformat() in rows for d in (*DAYS, PREVIOUS[0])),
        "D1_PRICE_GAP",
        "缺少四日原价或有限前收依赖日",
    )
    for d in DAYS:
        r = rows[d.isoformat()]
        require(
            r["object_sha256"] == PRICE_OBJECT
            and r["source_row"]["tradestatus"] == "1"
            and r["source_row"]["isST"] == "0",
            "D1_INTEGRITY",
            "源日状态不符冻结输入",
        )
    require(
        isinstance(calendar, dict)
        and all(
            calendar.get((START + timedelta(days=i)).isoformat())
            is ((START + timedelta(days=i)) in DAYS)
            for i in range(6)
        ),
        "D1_PRICE_GAP",
        "自然日历缺失或改变",
    )
    return view, rows, calendar


def _validate(store, body):
    require(isinstance(body, dict), "D1_INTEGRITY", "清单须为对象")
    if body.get("kind") == "d1_facts":
        return _validate_facts(store, body)
    require(body.get("kind") == "d1_composite", "D1_INTEGRITY", "不支持的D1组件")
    require(
        canonical(body) == canonical(_composite_body(body.get("facts_component_id"))),
        "D1_INTEGRITY",
        "组合清单不符已审阅策略或组件",
    )
    D1Facts(store, body["facts_component_id"])
    _price(store)
    return None


def _load(store, sid, kind):
    identifier(sid)
    with store._db() as db:
        require(_exists(db), "D1_NOT_FOUND", "没有D1固定版本")
        row = db.execute("SELECT body,status FROM d1_snapshots WHERE id=?", (sid,)).fetchone()
    require(row is not None, "D1_NOT_FOUND", "D1固定版本不存在")
    require(row["status"] == "published", "D1_NOT_PUBLISHED", "未发布；须显式恢复或检查aborted")
    data = _read(_safe_path(store.root, "d1-manifests/" + sid + ".json"), 1024 * 1024)
    require(
        _sha(data) == sid and data == row["body"].encode(), "D1_INTEGRITY", "清单与目录hash不符"
    )
    body = _json(data)
    require(
        isinstance(body, dict) and body.get("kind") == kind, "D1_WRONG_COMPONENT", "组件类型不符"
    )
    return body, _validate(store, body)


def _publish(store, body, objects):
    data, sid = canonical(body), digest(body)
    with store._writer():
        for name in ("d1-objects", "d1-manifests"):
            path = _safe_path(store.root, name)
            path.mkdir(exist_ok=True)
        with store._db(write=True) as db:
            db.execute(TABLE)
            prior = db.execute("SELECT status FROM d1_snapshots WHERE id=?", (sid,)).fetchone()
        if prior and prior[0] == "published":
            _load(store, sid, body["kind"])
            return sid
        require(prior is None, "D1_RECOVERY_REQUIRED", "已有未完成/拒绝发布；显式检查d1-recover")
        for h, blob in objects.items():
            require(_sha(blob) == h, "D1_INTEGRITY", "待写对象hash不符")
            immutable_write(_safe_path(store.root, "d1-objects/" + identifier(h)), blob)
        _validate(store, body)
        with store._db(write=True) as db:
            db.execute(
                "INSERT INTO d1_snapshots VALUES (?,?,?,?)", (sid, data.decode(), "prepared", now())
            )
        immutable_write(_safe_path(store.root, "d1-manifests/" + sid + ".json"), data)
        with store._db(write=True) as db:
            db.execute("UPDATE d1_snapshots SET status='published' WHERE id=?", (sid,))
    return sid


def import_facts(store, directory):
    root = Path(directory).expanduser().absolute()
    source = _read(_safe_path(root, "manifest.json"), 1024 * 1024)
    body = _facts_body(source)
    objects = {SOURCE_MANIFEST: source}
    for entry in body["files"]:
        blob = _read(_safe_path(root, entry["path"]))
        require(
            len(blob) == entry["bytes"] and _sha(blob) == entry["sha256"],
            "D1_INTEGRITY",
            "源包字节不符清单",
            {"path": entry["path"]},
        )
        objects[entry["sha256"]] = blob
    return _publish(store, body, objects)


def compose(store, price_dataset_id, facts_component_id):
    require(
        identifier(price_dataset_id) == PRICE_DATASET,
        "D1_UNREVIEWED_PRICE",
        "只接纳既有冻结价格ID；不重采或静默换源",
    )
    D1Facts(store, facts_component_id)
    _price(store)
    return _publish(store, _composite_body(facts_component_id), {})


def recover(store):
    results = []
    with store._writer():
        with store._db() as db:
            if not _exists(db):
                return []
            rows = db.execute(
                "SELECT id,body FROM d1_snapshots WHERE status='prepared' ORDER BY id"
            ).fetchall()

        # A composite cannot recover until its referenced facts are published.
        def recovery_order(row):
            try:
                body = _json(row["body"])
                return (
                    0 if isinstance(body, dict) and body.get("kind") == "d1_facts" else 1,
                    row["id"],
                )
            except DataError:
                return (1, row["id"])

        rows = sorted(rows, key=recovery_order)
        for row in rows:
            try:
                sid, data = identifier(row["id"]), row["body"].encode()
                require(_sha(data) == sid, "D1_INTEGRITY", "prepared hash不符")
                _validate(store, _json(data))
                immutable_write(_safe_path(store.root, "d1-manifests/" + sid + ".json"), data)
                status, error = "published", None
            except (DataError, OSError, KeyError, TypeError, ValueError) as exc:
                status = "aborted"
                error = exc.code if isinstance(exc, DataError) else "D1_INTEGRITY"
            with store._db(write=True) as db:
                db.execute("UPDATE d1_snapshots SET status=? WHERE id=?", (status, row["id"]))
            results.append({"dataset_id": row["id"], "status": status, "error": error})
    return results


def _date(value):
    return date.fromisoformat(day(value))


def _optional_date(value):
    return _date(value) if value is not None else None


def _evidence(raw):
    return tuple(
        Evidence(
            s["path"],
            s["sha256"],
            s.get("url"),
            s.get("locator"),
            datetime.fromisoformat(s["retrieved_at"]) if s.get("retrieved_at") else None,
            datetime.fromisoformat(s["historical_available_at"])
            if s.get("historical_available_at")
            else None,
            canonical(s).decode(),
        )
        for s in raw
    )


class D1Facts:
    """Read-only owner component. Original source paths are text, never file accesses."""

    def __init__(self, store, component_id):
        self._body, self._objects = _load(store, component_id, "d1_facts")
        self._id = component_id
        self._facts = _json(self._objects["facts.json"])

    def descriptor(self) -> D1Descriptor:
        return D1Descriptor(
            self._id,
            self._id,
            "d1_facts",
            self._id,
            SOURCE_MANIFEST,
            None,
            OWNER_VERSION,
            POLICY,
            PLAN_SHA256,
            SECURITY,
            START,
            END,
        )

    def records(self) -> tuple[FactRecord, ...]:
        return tuple(
            FactRecord(
                f["id"],
                canonical(f["value"]).decode(),
                f["evidence_status"],
                _evidence(f["sources"]),
                canonical(f).decode(),
            )
            for f in self._facts["facts"]
        )

    def evidence(self, path: str) -> bytes:
        _relative(path)
        require(path in self._objects, "D1_EVIDENCE_NOT_FOUND", "证据不在固定组件中")
        return self._objects[path]


def _scoped_date(value):
    d = _date(value)
    require(d in DAYS, "D1_OUT_OF_SCOPE", "只支持四个已审交易日")
    return d


class D1View:
    """Posthoc diagnostic view, not a strategy-clock or engine adapter."""

    def __init__(self, store, dataset_id):
        self._body, _ = _load(store, dataset_id, "d1_composite")
        self._id = dataset_id
        self._component = D1Facts(store, self._body["facts_component_id"])
        self._research, self._rows, self._calendar = _price(store)
        self._facts = {f.fact_id: f for f in self._component.records()}

    def descriptor(self) -> D1Descriptor:
        return D1Descriptor(
            self._id,
            self._id,
            "d1_composite",
            self._component._id,
            SOURCE_MANIFEST,
            PRICE_DATASET,
            OWNER_VERSION,
            POLICY,
            PLAN_SHA256,
            SECURITY,
            START,
            END,
        )

    def _sources(self, *ids):
        sources = {canonical(s.to_dict()): s for key in ids for s in self._facts[key].evidence}
        return tuple(sources.values())

    def instrument(self) -> D1Instrument:
        identity = self._facts["security_identity"].source_fields["value"]
        return D1Instrument(
            SECURITY,
            identity["name"],
            identity["exchange"],
            identity["kind"],
            _date(self._facts["initial_listing_date"].source_fields["value"]),
            "main_board",
            "bounded_historical_inference",
            None,
            self._sources(
                "security_identity", "initial_listing_date", "historical_board_candidate"
            ),
            (BLOCKERS["instrument"], "事后证据，不证明历史可得性或个人账户资格。"),
        )

    def rules(self) -> D1Rules:
        return D1Rules(
            SECURITY,
            100,
            Decimal("0.01"),
            False,
            "T+1",
            date(2018, 8, 20),
            START,
            END,
            Decimal("0.10"),
            self._sources(
                "buy_round_lot",
                "price_tick",
                "same_day_resale",
                "stock_trading_cycle",
                "normal_daily_limit_ratio",
            ),
            (
                "普通A股竞价买入100股倍数；不足100股余股一次卖出。",
                "T+1为可卖约束，不是完整交收时刻表。",
                BLOCKERS["price_limits"],
            ),
        )

    def bars(self) -> tuple[D1Bar, ...]:
        result = []
        for d in DAYS:
            r = self._rows[d.isoformat()]
            s = r["source_row"]
            result.append(
                D1Bar(
                    SECURITY,
                    d,
                    *(Decimal(s[k]) for k in ("open", "high", "low", "close")),
                    int(s["volume"]),
                    Decimal(s["amount"]),
                    r["object_sha256"],
                    r["raw_record_sha256"],
                    canonical(s).decode(),
                )
            )
        return tuple(result)

    def statuses(self) -> tuple[D1Status, ...]:
        return tuple(
            D1Status(
                SECURITY,
                d,
                r["source_row"]["tradestatus"] == "0",
                r["source_row"]["isST"] == "1",
                r["object_sha256"],
                r["raw_record_sha256"],
                canonical(r["source_row"]).decode(),
            )
            for d in DAYS
            for r in (self._rows[d.isoformat()],)
        )

    def calendar(self) -> tuple[D1CalendarDay, ...]:
        opened = sorted(_date(d) for d, is_open in self._calendar.items() if is_open)
        return tuple(
            D1CalendarDay(
                d,
                self._calendar[d.isoformat()],
                max((x for x in opened if x < d), default=None),
                min((x for x in opened if x > d), default=None),
                CALENDAR_OBJECT,
            )
            for d in (START + timedelta(days=i) for i in range(6))
        )

    def source_prev_close(self, trade_date: str) -> D1SourcePrevClose:
        d = _scoped_date(trade_date)
        r = self._rows[d.isoformat()]
        return D1SourcePrevClose(
            SECURITY,
            d,
            Decimal(r["source_row"]["preclose"]),
            r["object_sha256"],
            r["raw_record_sha256"],
            canonical(r["source_row"]).decode(),
        )

    def known_events(self) -> tuple[D1Event, ...]:
        dates = (
            "issuer_announcement_date",
            "exchange_disclosure_date",
            "record_date",
            "ex_date",
            "pay_date",
            "effective_date",
            "subscription_date",
            "bond_listing_date",
            "conversion_start",
        )
        values = ("cash_per_share_before_tax", "coupon_before", "coupon_after")
        return tuple(
            D1Event(
                e["id"],
                e["event_type"],
                e["entitled_security"],
                e.get("resulting_security"),
                *(_optional_date(e.get(k)) for k in dates),
                *(Decimal(e[k]) if k in e else None for k in values),
                e["d1_relation"],
                e["evidence_status"],
                _evidence(e["sources"]),
                canonical(e).decode(),
            )
            for e in _json(self._component.evidence("events.json"))
        )

    def event_coverage(self) -> D1EventCoverage:
        f = self._facts["window_corporate_action_review"]
        return D1EventCoverage(
            date(2020, 1, 3),
            date(2020, 1, 6),
            date(2019, 12, 31),
            END,
            date(2019, 1, 1),
            date(2020, 12, 31),
            248,
            5,
            "no_relevant_event_identified",
            "bounded_corroborated_review",
            (f.source_fields["limitation"], BLOCKERS["event_absence"]),
            f.evidence,
        )

    def limit_candidates(self) -> tuple[D1LimitCandidate, ...]:
        result = []
        for d in DAYS:
            p = self.source_prev_close(d.isoformat())
            result.append(
                D1LimitCandidate(
                    SECURITY,
                    d,
                    p.value,
                    (p.value * Decimal("0.90")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    (p.value * Decimal("1.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    Decimal("0.10"),
                    Decimal("0.01"),
                    ("normal_daily_limit_ratio", "ex_right_reference"),
                    p.object_sha256,
                    p.raw_record_sha256,
                    (BLOCKERS["price_limits"],),
                )
            )
        return tuple(result)

    def prev_close_candidates(self) -> tuple[D1PrevCloseCandidate, ...]:
        result = []
        for d, previous in zip(DAYS, PREVIOUS):
            source = self.source_prev_close(d.isoformat())
            history = self._rows[previous.isoformat()]
            close = Decimal(history["source_row"]["close"])
            call = D1PrevCloseCall(SECURITY, d, previous, d, "1d", "close", 1, False, False, "pre")
            result.append(
                D1PrevCloseCandidate(
                    call,
                    source.value,
                    source.value,
                    close,
                    source.value == close,
                    source.object_sha256,
                    source.raw_record_sha256,
                    history["raw_record_sha256"],
                    (
                        BLOCKERS["adjusted_prev_close"],
                        "只作该history_dt至adjust_orig的有限等价审阅，不含今日前复权。",
                    ),
                )
            )
        return tuple(result)

    def price_limits(self, trade_date: str):
        _scoped_date(trade_date)
        self.require_fact("price_limits")

    def corporate_actions(self):
        self.require_fact("event_absence")

    def adjusted_prev_close(
        self,
        *,
        security: str,
        trade_date: str,
        history_dt: str,
        adjust_orig: str,
        frequency: str,
        field: str,
        bar_count: int,
        include_now: bool,
        skip_suspended: bool,
        adjustment_requested: str,
    ):
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
        require(
            type(bar_count) is int
            and type(include_now) is bool
            and type(skip_suspended) is bool
            and any(supplied == x.call.to_dict() for x in self.prev_close_candidates()),
            "D1_UNREVIEWED_CALL",
            "仅支持明确审阅的完整AD08参数组合",
        )
        self.require_fact("adjusted_prev_close")

    def coverage(self) -> D1Coverage:
        return D1Coverage(SECURITY, START, END, DAYS, True, ())

    def quality(self) -> D1Quality:
        return D1Quality()

    def lineage(self) -> D1Lineage:
        return D1Lineage(
            self.descriptor(),
            tuple(x["sha256"] for x in self._research.descriptor()["objects"]),
            tuple((x["path"], x["sha256"]) for x in self._component._body["files"]),
            tuple(self._facts),
            tuple(e.event_id for e in self.known_events()),
            canonical(self._body).decode(),
        )

    def admission(self) -> D1Admission:
        refs = {
            "identity": ("manifest:" + self._id, "fact:security_identity"),
            "raw_bars": ("object:" + PRICE_OBJECT,),
            "calendar": ("object:" + CALENDAR_OBJECT,),
            "instrument": (
                "fact:security_identity",
                "fact:initial_listing_date",
                "fact:historical_board_candidate",
            ),
            "trading_rules": ("fact:buy_round_lot", "fact:price_tick", "fact:stock_trading_cycle"),
            "suspended": ("object:" + PRICE_OBJECT + "#tradestatus",),
            "st_status": ("object:" + PRICE_OBJECT + "#isST",),
            "price_limits": ("fact:normal_daily_limit_ratio", "fact:ex_right_reference"),
            "event_absence": ("fact:window_corporate_action_review",),
            "adjusted_prev_close": ("ad08-candidates.json", "object:" + PRICE_OBJECT),
            "owner_scope": ("manifest:" + self._id, "policy:" + POLICY, "plan:" + PLAN_SHA256),
        }
        results = []
        for i, key in enumerate(REQUIREMENTS):
            cls = (
                "required_fact"
                if i < 11
                else ("not_used" if key == "amount" else "declared_model_assumption")
            )
            supported = cls == "required_fact" and key not in BLOCKERS
            status = "owner_reviewed_fixed_scope" if supported else "unknown"
            if key in {"raw_bars", "calendar", "suspended", "st_status"}:
                status = "source_observed"
            if key == "trading_rules":
                status = "official_rule_bounded_review"
            limitation = BLOCKERS.get(
                key,
                "仅限固定版本事后研究，不升级价格质量或执行许可。"
                if supported
                else "按冻结后端计划为模型假设或不消费字段；owner未验证。",
            )
            results.append(
                D1Requirement(
                    cls,
                    D1FactClaim(
                        key,
                        "supported" if supported else "unknown",
                        status,
                        refs.get(key, ()),
                        (limitation,),
                    ),
                )
            )
        return D1Admission(self._id, PLAN_SHA256, tuple(results), tuple(BLOCKERS))

    def owner_receipt(self) -> D1OwnerReceipt:
        return D1OwnerReceipt(
            "ashare-data",
            OWNER_VERSION,
            REQUEST_ID,
            self._id,
            self._id,
            SECURITY,
            START,
            END,
            tuple(r.claim for r in self.admission().requirements),
        )

    def require_fact(self, key: str) -> D1FactClaim:
        require(isinstance(key, str) and key in REQUIREMENTS, "D1_UNKNOWN_FACT", "未知D1准入字段")
        item = next(x for x in self.admission().requirements if x.claim.key == key)
        require(
            item.classification == "required_fact" and item.claim.conclusion == "supported",
            "D1_FACT_UNAVAILABLE",
            "字段未达到限定研究准入",
            item.to_dict(),
        )
        return item.claim

    def require_execution(self, *, plan_sha256: str):
        require(plan_sha256 == PLAN_SHA256, "D1_PLAN_MISMATCH", "必须使用固定D1 v2计划")
        raise DataError(
            "D1_ADMISSION_BLOCKED", "四项必需事实未解决，禁止D1执行", self.admission().to_dict()
        )
