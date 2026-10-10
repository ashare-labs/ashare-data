"""One reviewed issuer fact, year precision, immutable and offline.

This is not a generic document parser or a historical eligibility service.
The owner pins original documents, receipts, extraction and review together.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .d1_types import Projection
from .model import DataError, canonical, digest, require
from .storage import identifier, immutable_write, now

PACKAGE_SHA256 = "08eb0578e608ed92d2ae8df9c2b7f72092016bdd63d13eaab26162da53d9f9e5"
OWNER_VERSION = "0.8.2.dev1"
MAX_BYTES = 8 * 1024 * 1024
TABLE = """CREATE TABLE IF NOT EXISTS listing_facts
(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('prepared','published','aborted')), created_at TEXT NOT NULL)"""


@dataclass(frozen=True)
class ListingFactDescriptor(Projection):
    snapshot_id: str
    package_sha256: str
    owner_version: str
    security: str
    supported_fields: tuple[str, ...]
    query_dates: tuple[date, ...]
    evidence_received_at: datetime
    price_context_dataset_id: str
    unsupported_fields: tuple[str, ...]
    evidence_files: tuple[str, ...]
    historical_available_at: None = None
    complete_instrument_history: bool = False
    execution_permission: bool = False
    network_required: bool = False


@dataclass(frozen=True)
class ListingFactEvidence(Projection):
    role: str
    path: str
    source_sha256: str
    source_url: str
    receipt_path: str
    text_path: str
    pdf_pages: tuple[int, ...]
    source_index_disclosure_date: date
    received_at: datetime
    historical_available_at: None = None


@dataclass(frozen=True)
class ListingYearFact(Projection):
    snapshot_id: str
    package_sha256: str
    security: str
    on_date: date
    field: str
    value: int
    precision: str
    evidence_status: str
    evidence_received_at: datetime
    visibility: str
    knowledge_at: datetime | None
    evidence: tuple[ListingFactEvidence, ...]
    actual_initial_listing_date: None = None
    historical_eligible: None = None
    historical_available_at: None = None
    execution_permission: bool = False


def _sha(blob):
    return hashlib.sha256(blob).hexdigest()


def _safe(root, name):
    # All internal names come from this module or the exactly pinned package.
    require(not root.is_symlink(), "LISTING_INPUT_INVALID", "拒绝链接目录")
    path = root / name
    require(not path.parent.is_symlink() and not path.is_symlink(),
            "LISTING_INPUT_INVALID", "拒绝链接证据或存储路径")
    return path


def _read(path, limit=MAX_BYTES):
    try:
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= limit,
                "LISTING_INPUT_INVALID", "文件缺失、链接或超出大小限制")
        with path.open('rb') as stream:
            blob = stream.read(limit + 1)
        require(len(blob) <= limit, "LISTING_INPUT_INVALID", "文件超过读取上限")
        return blob
    except (OSError, ValueError) as exc:
        raise DataError("LISTING_INPUT_INVALID", "证据文件无法读取") from exc


def _package(blob):
    require(_sha(blob) == PACKAGE_SHA256, "LISTING_UNREVIEWED_PACKAGE",
            "仅接纳固定审阅包；自行改值并重算hash不构成事实采纳")
    return json.loads(blob)


def _decode_manifest(data):
    try:
        return json.loads(data)
    except (ValueError, TypeError) as exc:
        raise DataError("LISTING_INTEGRITY", "清单不是有效JSON") from exc


def _body(package):
    return {"kind": "reviewed_listing_year", "owner_version": OWNER_VERSION,
            "package_sha256": PACKAGE_SHA256, "files": package['files']}


def _exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='listing_facts' AND type='table'").fetchone()


def snapshots(store):
    with store._db() as db:
        if not _exists(db):
            return []
        return [dict(r) for r in db.execute(
            "SELECT id AS snapshot_id,status,created_at FROM listing_facts ORDER BY id")]


def _validate(store, body):
    require(type(body) is dict, "LISTING_INTEGRITY", "清单必须是对象")
    blob = _read(_safe(store.root, "listing-objects/" + PACKAGE_SHA256), 1024 * 1024)
    package = _package(blob)
    require(canonical(body) == canonical(_body(package)), "LISTING_INTEGRITY", "清单不符固定契约")
    for entry in package['files']:
        value = _read(_safe(store.root, 'listing-objects/' + entry['sha256']))
        require(len(value) == entry['bytes'] and _sha(value) == entry['sha256'],
                "LISTING_INTEGRITY", "原文/回执/转换证据字节不符", {"path": entry['path']})
    return package


def _load(store, snapshot_id):
    identifier(snapshot_id)
    with store._db() as db:
        require(_exists(db), "LISTING_NOT_FOUND", "没有上市事实组件")
        row = db.execute("SELECT body,status FROM listing_facts WHERE id=?", (snapshot_id,)).fetchone()
    require(row is not None, "LISTING_NOT_FOUND", "固定组件不存在")
    require(row['status'] == 'published', "LISTING_NOT_PUBLISHED", "组件未发布；请显式恢复")
    data = _read(_safe(store.root, 'listing-manifests/' + snapshot_id + '.json'), 1024 * 1024)
    require(_sha(data) == snapshot_id and data == row['body'].encode(),
            "LISTING_INTEGRITY", "文件清单/SQLite目录身份不一致")
    return _validate(store, _decode_manifest(data))


def import_evidence(store, directory):
    try:
        root = Path(directory).expanduser().absolute()
    except (TypeError, ValueError) as exc:
        raise DataError("LISTING_INPUT_INVALID", "须提供本地证据目录") from exc
    blob = _read(_safe(root, 'package.json'), 1024 * 1024)
    package = _package(blob)
    objects = {PACKAGE_SHA256: blob}
    for entry in package['files']:
        value = _read(_safe(root, entry['path']))
        require(len(value) == entry['bytes'] and _sha(value) == entry['sha256'],
                "LISTING_INTEGRITY", "证据与固定包不符", {"path": entry['path']})
        objects[entry['sha256']] = value
    body = _body(package)
    data, sid = canonical(body), digest(body)
    with store._writer():
        for name in ('listing-objects', 'listing-manifests'):
            _safe(store.root, name).mkdir(exist_ok=True)
        with store._db(write=True) as db:
            db.execute(TABLE)
            prior = db.execute("SELECT status FROM listing_facts WHERE id=?", (sid,)).fetchone()
        if prior and prior[0] == 'published':
            _load(store, sid)
            return sid
        require(prior is None, "LISTING_RECOVERY_REQUIRED", "已有中断/拒绝记录，须显式检查恢复结果")
        for sha256, value in objects.items():
            immutable_write(_safe(store.root, 'listing-objects/' + sha256), value)
        _validate(store, body)
        with store._db(write=True) as db:
            db.execute("INSERT INTO listing_facts VALUES (?,?,?,?)", (sid, data.decode(), 'prepared', now()))
        immutable_write(_safe(store.root, 'listing-manifests/' + sid + '.json'), data)
        with store._db(write=True) as db:
            db.execute("UPDATE listing_facts SET status='published' WHERE id=?", (sid,))
    return sid


def recover(store):
    results = []
    with store._writer():
        with store._db() as db:
            if not _exists(db):
                return []
            rows = db.execute("SELECT id,body FROM listing_facts WHERE status='prepared' ORDER BY id").fetchall()
        for row in rows:
            try:
                sid, data = identifier(row['id']), row['body'].encode()
                require(_sha(data) == sid, "LISTING_INTEGRITY", "prepared身份不符")
                _validate(store, _decode_manifest(data))
                immutable_write(_safe(store.root, 'listing-manifests/' + sid + '.json'), data)
                status, error = 'published', None
            except (DataError, OSError, ValueError, KeyError, TypeError) as exc:
                status, error = 'aborted', exc.code if isinstance(exc, DataError) else 'LISTING_INTEGRITY'
            with store._db(write=True) as db:
                db.execute("UPDATE listing_facts SET status=? WHERE id=?", (status, row['id']))
            results.append({'snapshot_id': row['id'], 'status': status, 'error': error})
    return results


def _day(value):
    if type(value) is date:
        return value
    if type(value) is str:
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
    raise DataError("LISTING_DATE_INVALID", "须为date或精确YYYY-MM-DD，不截断datetime")


def _clock(value):
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise DataError("LISTING_CLOCK_INVALID", "须为带时区时间") from exc
    require(type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None,
            "LISTING_CLOCK_INVALID", "须为带时区时间")
    return value.astimezone(timezone.utc)


def _evidence(package):
    return tuple(ListingFactEvidence(
        e['role'], e['raw_path'], e['source_sha256'], e['url'], e['receipt_path'], e['text_path'],
        tuple(e['pdf_pages']),
        _day(e['source_index_disclosure_date']), _clock(e['received_at'])) for e in package['evidence'])


class ListingFactView:
    def __init__(self, store, snapshot_id):
        self._store, self._id = store, snapshot_id
        _load(store, snapshot_id)

    def descriptor(self):
        p = _load(self._store, self._id)
        return ListingFactDescriptor(self._id, PACKAGE_SHA256, OWNER_VERSION, p['security'],
                                     (p['field'],), tuple(_day(x) for x in p['query_dates']),
                                     _clock(p['evidence_received_at']), p['price_context_dataset_id'],
                                     tuple(p['unsupported']), tuple(e['path'] for e in p['files']))

    def lineage(self):
        return _evidence(_load(self._store, self._id))

    def evidence(self, name):
        p = _load(self._store, self._id)
        entries = [e for e in p['files'] if e['path'] == name]
        require(len(entries) == 1, "LISTING_EVIDENCE_NOT_FOUND", "证据名不在固定清单内")
        return _read(_safe(self._store.root, 'listing-objects/' + entries[0]['sha256']))

    def validate(self):
        d = self.descriptor()
        return {"status": "VALID_YEAR_FACT_ONLY", "descriptor": d.to_dict(),
                "complete_instrument_history": False, "execution_permission": False}

    def get(self, security, on_date, *, field='initial_listing_year',
            visibility='posthoc', knowledge_at=None):
        p = _load(self._store, self._id)
        require(type(security) is str and security == p['security'],
                "LISTING_SECURITY_SCOPE", "本组件仅300750.XSHE")
        d = _day(on_date)
        require(d.isoformat() in p['query_dates'], "LISTING_DATE_SCOPE", "仅共同窗口2026-09-28至30")
        require(field == p['field'], "LISTING_FACT_UNAVAILABLE",
                "仅支持首次上市年份；具体实际日期及其他资格事实未证明", {"field": field})
        require(visibility in ('posthoc', 'received', 'verified'),
                "LISTING_VISIBILITY", "未知可见性模式")
        require(visibility != 'verified', "PIT_UNAVAILABLE", "没有历史首次可见/修订证明")
        received = _clock(p['evidence_received_at'])
        clock = None
        if visibility == 'posthoc':
            require(knowledge_at is None, "LISTING_CLOCK_MODE", "带知识时钟须明确选择received或verified")
        else:
            clock = _clock(knowledge_at)
            require(clock >= received, "VISIBILITY_UNKNOWN", "知识时钟早于本批证据接收完成")
        return ListingYearFact(self._id, PACKAGE_SHA256, security, d, p['field'], p['value'],
                               p['precision'], p['evidence_status'], received, visibility, clock,
                               _evidence(p))

    def require_eligible(self, security, on_date):
        self.get(security, on_date)
        raise DataError("LISTING_ELIGIBILITY_UNKNOWN", "首次上市年份不能证明窗口内持续上市或可交易")
