"""Content addressed objects and a local, single-writer publication catalog."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .model import DataError, canonical, digest, normalize, require

COLUMNS = {
    "symbol": "VARCHAR", "bar_start": "VARCHAR", "bar_end": "VARCHAR",
    "open": "DECIMAL(24,6)", "high": "DECIMAL(24,6)", "low": "DECIMAL(24,6)",
    "close": "DECIMAL(24,6)", "volume": "DECIMAL(24,6)", "amount": "DECIMAL(24,6)",
    "quality": "VARCHAR", "quality_reason": "VARCHAR", "source_id": "VARCHAR",
    "source_label": "VARCHAR", "source_label_kind": "VARCHAR", "time_semantics": "VARCHAR",
    "available_at": "VARCHAR", "observed_at": "VARCHAR", "visibility_evidence": "VARCHAR",
    "source_fields": "VARCHAR",
}
SCHEMA = """
CREATE TABLE meta(version INTEGER NOT NULL);
INSERT INTO meta VALUES (1);
CREATE TABLE batches(id TEXT PRIMARY KEY, parquet_hash TEXT NOT NULL,
 row_count INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE reports(id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE snapshots(id TEXT PRIMARY KEY, body TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','published','aborted')), created_at TEXT NOT NULL);
CREATE TABLE tasks(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
 artifact_id TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
            "INVALID_ID", "必须提供完整 SHA256 id；不支持 latest 别名")
    return value


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def immutable_write(path, content):
    if path.exists():
        require(path.read_bytes() == content, "INTEGRITY", "不可变对象已存在且内容不同")
        return
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o444)
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def duck():
    # Built-in Parquet only; never install or auto-load a DuckDB extension.
    return duckdb.connect(":memory:", config={"autoinstall_known_extensions": "false",
                                             "autoload_known_extensions": "false"})


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.catalog = self.root / "catalog.sqlite"
        require(self.catalog.is_file(), "STORE_NOT_FOUND", "先显式运行 init 创建数据目录")
        with self._db() as db:
            require(db.execute("SELECT version FROM meta").fetchone()[0] == 1,
                    "SCHEMA_VERSION", "目录版本不支持")

    @classmethod
    def init(cls, root):
        root = Path(root).expanduser().resolve()
        if (root / "catalog.sqlite").is_file():
            return cls(root)
        require(not root.exists() or (root.is_dir() and not any(root.iterdir())),
                "DIRECTORY_NOT_EMPTY", "拒绝覆盖已有非空目录")
        root.mkdir(parents=True, exist_ok=True)
        for name in ("objects", "manifests"):
            (root / name).mkdir()
        (root / "writer.lock").touch(exist_ok=False)
        db = sqlite3.connect(root / "catalog.sqlite")
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(SCHEMA)
            db.commit()
        finally:
            db.close()
        fsync_dir(root)
        return cls(root)

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(str(self.catalog) if write else self.catalog.as_uri() + "?mode=ro",
                             uri=not write, timeout=0)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("PRAGMA synchronous=FULL")
            else:
                db.execute("PRAGMA query_only=ON")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _writer(self):
        with open(self.root / "writer.lock", "rb") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DataError("WRITER_BUSY", "已有本地写入者；稍后显式重试") from exc
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def baostock(self, capture_id):
        from .baostock import BaoStockView
        return BaoStockView(self, capture_id)

    def import_d1_facts(self, directory):
        """Seal the explicitly reviewed, local D1 evidence package."""
        from .d1 import import_facts
        return import_facts(self, directory)

    def d1_facts(self, component_id):
        from .d1 import D1Facts
        return D1Facts(self, component_id)

    def compose_d1(self, price_dataset_id, facts_component_id):
        from .d1 import compose
        return compose(self, price_dataset_id, facts_component_id)

    def d1(self, dataset_id):
        from .d1 import D1View
        return D1View(self, dataset_id)

    def d1_snapshots(self):
        from .d1 import snapshots
        return snapshots(self)

    def recover_d1(self):
        from .d1 import recover
        return recover(self)

    def baostock_snapshots(self):
        from .baostock import snapshots
        return snapshots(self)

    def recover_baostock(self):
        from .baostock import recover
        return recover(self)

    def research(self, dataset_id):
        """Reopen a fixed, verified local research dataset without network access."""
        from .research import ResearchView
        return ResearchView(self, dataset_id)

    def research_snapshots(self):
        from .research import snapshots
        return snapshots(self)

    def recover_research(self):
        from .research import recover
        return recover(self)

    def import_research(self, paths, *, format="baostock_daily", calendar_path=None):
        from .research import import_research
        return import_research(self, paths, format=format, calendar_path=calendar_path)

    def fetch_price(self, security, start_date=None, end_date=None, frequency="daily", count=None, timeout=15):
        from .research import fetch_price
        return fetch_price(self, security, start_date, end_date, frequency, count, timeout)

    @staticmethod
    def _task(db, kind, status, artifact):
        db.execute("INSERT INTO tasks(kind,status,artifact_id,created_at) VALUES (?,?,?,?)",
                   (kind, status, artifact, now()))

    def import_bundle(self, bundle):
        # Round-trip ensures caller mutation cannot race normalization/hashing.
        try:
            bundle = json.loads(canonical(bundle))
        except (TypeError, ValueError) as exc:
            raise DataError("INVALID_BUNDLE", "bundle 必须是有限数值的 JSON 兼容对象") from exc
        rows = normalize(bundle)
        batch_id = digest(bundle)
        with self._writer():
            with self._db() as db:
                exists = db.execute("SELECT id FROM batches WHERE id=?", (batch_id,)).fetchone()
            if exists:
                self._load_batches([batch_id])
                return batch_id
            obj = self.root / "objects"
            immutable_write(obj / f"{batch_id}.json", canonical(bundle))
            fd, temp = tempfile.mkstemp(suffix=".parquet", prefix=".pending-", dir=obj)
            os.close(fd)
            os.unlink(temp)  # DuckDB COPY requires a fresh destination.
            conn = duck()
            try:
                conn.execute("CREATE TABLE bars (" + ",".join(f'"{k}" {v}' for k, v in COLUMNS.items()) + ")")
                conn.executemany("INSERT INTO bars VALUES (" + ",".join("?" for _ in COLUMNS) + ")",
                                 [tuple(row[k] for k in COLUMNS) for row in rows])
                conn.execute("COPY bars TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [temp])
                content = Path(temp).read_bytes()
                immutable_write(obj / f"{batch_id}.parquet", content)
                pq_hash = hashlib.sha256(content).hexdigest()
            finally:
                conn.close()
                if os.path.exists(temp):
                    os.unlink(temp)
            with self._db(write=True) as db:
                db.execute("INSERT INTO batches VALUES (?,?,?,?)", (batch_id, pq_hash, len(rows), now()))
                self._task(db, "import", "complete", batch_id)
        return batch_id

    def _load_batches(self, batches):
        require(isinstance(batches, list) and 0 < len(batches) <= 100 and len(set(batches)) == len(batches),
                "INVALID_REQUEST", "必须显式选择 1..100 个无重复 batch")
        loaded = []
        with self._db() as db:
            for batch_id in sorted(batches):
                identifier(batch_id)
                meta = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
                require(meta is not None, "BATCH_NOT_FOUND", "找不到导入批次", {"id": batch_id})
                raw = self.root / "objects" / f"{batch_id}.json"
                parquet = self.root / "objects" / f"{batch_id}.parquet"
                require(raw.is_file() and parquet.is_file(), "INTEGRITY", "数据对象缺失")
                require(file_hash(raw) == batch_id and file_hash(parquet) == meta["parquet_hash"],
                        "INTEGRITY", "数据对象 hash 不匹配")
                bundle = json.loads(raw.read_bytes())
                # A valid hash proves unchanged bytes, not current semantic validity.
                # Recheck legacy batches before query/validate/publish without rewriting them.
                normalize(bundle)
                loaded.append({"id": batch_id, "bundle": bundle,
                               "parquet": parquet, "parquet_hash": meta["parquet_hash"],
                               "row_count": meta["row_count"]})
        return loaded

    def validate(self, batches, *, requirements, policy="observed"):
        from .query import DataView
        require(policy in {"observed", "synthetic", "research"}, "INVALID_QUALITY", "未知发布策略")
        require(isinstance(requirements, list) and 0 < len(requirements) <= 100,
                "INVALID_REQUEST", "发布前必须声明 1..100 个非空覆盖要求")
        with self._writer():
            loaded = self._load_batches(batches)
            view = DataView(loaded, snapshot_id=None)
            try:
                conflicts = view.conflicts()
                reports = []
                for r in requirements:
                    require(isinstance(r, dict) and {"symbols", "start", "end"} <= set(r),
                            "INVALID_REQUEST", "覆盖要求必须包含 symbols/start/end")
                    require(set(r) <= {"symbols", "start", "end", "as_of"},
                            "INVALID_REQUEST", "未知 coverage requirement 参数")
                    reports.append(view.coverage(**r, quality=policy))
                normalized_requirements = [
                    {**r["scope"], **({"as_of": r["as_of"]} if r["as_of"] is not None else {})}
                    for r in reports
                ]
                report = {"schema_version": 1, "batches": sorted(batches), "policy": policy,
                          "requirements": normalized_requirements, "coverage": reports,
                          "conflicts": conflicts,
                          "passed": not conflicts and all(r["complete"] and r["expected"] > 0 for r in reports),
                          "quality_counts": view.quality_counts(),
                          "objects": [{"id": x["id"], "parquet_hash": x["parquet_hash"]} for x in loaded]}
                report_id = digest(report)
                with self._db(write=True) as db:
                    db.execute("INSERT OR IGNORE INTO reports VALUES (?,?,?)",
                               (report_id, canonical(report).decode(), now()))
                    self._task(db, "validate", "passed" if report["passed"] else "blocked", report_id)
                return {"id": report_id, **report}
            finally:
                view.close()

    def _report(self, report_id):
        identifier(report_id)
        with self._db() as db:
            row = db.execute("SELECT body FROM reports WHERE id=?", (report_id,)).fetchone()
        require(row is not None, "REPORT_NOT_FOUND", "未找到验证报告")
        body = json.loads(row["body"])
        require(digest(body) == report_id, "INTEGRITY", "验证报告 hash 不匹配")
        return body

    def publish(self, report_id, *, _fault=None):
        """Publish only a passing immutable report. _fault is a deterministic test hook."""
        with self._writer():
            report = self._report(report_id)
            require(report["passed"], "VALIDATION_FAILED", "覆盖或质量检查未通过，禁止发布",
                    {"report_id": report_id})
            loaded = self._load_batches(report["batches"])
            require(report["objects"] == [{"id": x["id"], "parquet_hash": x["parquet_hash"]} for x in loaded],
                    "INTEGRITY", "对象与验证报告不一致")
            manifest = {"schema_version": 1, "report_id": report_id, "report": report,
                        "batches": report["batches"], "objects": report["objects"]}
            sid = digest(manifest)
            with self._db(write=True) as db:
                row = db.execute("SELECT status FROM snapshots WHERE id=?", (sid,)).fetchone()
                if row and row["status"] == "published":
                    self._check_manifest(sid, manifest)
                    return sid
                db.execute("INSERT INTO snapshots VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET status='prepared'",
                           (sid, canonical(manifest).decode(), "prepared", now()))
                self._task(db, "publish", "prepared", sid)
            if _fault == "after_prepare":
                raise RuntimeError("injected interruption after catalog prepare")
            immutable_write(self.root / "manifests" / f"{sid}.json", canonical(manifest))
            if _fault == "after_manifest":
                raise RuntimeError("injected interruption after manifest fsync")
            with self._db(write=True) as db:
                db.execute("UPDATE snapshots SET status='published' WHERE id=?", (sid,))
                self._task(db, "publish", "complete", sid)
            return sid

    def _check_manifest(self, sid, manifest):
        require(digest(manifest) == sid, "INTEGRITY", "目录 manifest hash 不匹配")
        path = self.root / "manifests" / f"{sid}.json"
        require(path.is_file() and file_hash(path) == sid, "INTEGRITY", "快照 manifest 缺失或被修改")
        require(digest(manifest["report"]) == manifest["report_id"], "INTEGRITY", "manifest 验证报告被修改")

    def recover(self):
        outcomes = []
        with self._writer():
            with self._db() as db:
                pending = db.execute("SELECT * FROM snapshots WHERE status='prepared'").fetchall()
            for row in pending:
                sid, manifest = row["id"], json.loads(row["body"])
                try:
                    require(digest(manifest) == sid and manifest["report"]["passed"],
                            "INTEGRITY", "准备中的 manifest 无效")
                    loaded = self._load_batches(manifest["batches"])
                    require(manifest["objects"] == [{"id": x["id"], "parquet_hash": x["parquet_hash"]} for x in loaded],
                            "INTEGRITY", "恢复对象不匹配")
                    require(digest(manifest["report"]) == manifest["report_id"], "INTEGRITY", "恢复报告不匹配")
                    immutable_write(self.root / "manifests" / f"{sid}.json", canonical(manifest))
                    self._check_manifest(sid, manifest)
                    status, error = "published", None
                except DataError as exc:
                    status, error = "aborted", exc.as_dict()
                with self._db(write=True) as db:
                    db.execute("UPDATE snapshots SET status=? WHERE id=?", (status, sid))
                    self._task(db, "recover", status, sid)
                outcomes.append({"snapshot_id": sid, "status": status, "error": error})
        return outcomes

    def snapshots(self):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT id,status,created_at FROM snapshots ORDER BY created_at,id")]

    def snapshot(self, snapshot_id):
        from .query import DataView
        identifier(snapshot_id)
        with self._db() as db:
            row = db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        require(row is not None and row["status"] == "published", "SNAPSHOT_NOT_PUBLISHED", "快照不存在或未完成发布")
        manifest = json.loads(row["body"])
        self._check_manifest(snapshot_id, manifest)
        loaded = self._load_batches(manifest["batches"])
        require(manifest["objects"] == [{"id": x["id"], "parquet_hash": x["parquet_hash"]} for x in loaded],
                "INTEGRITY", "快照引用与目录对象不一致")
        return DataView(loaded, snapshot_id=snapshot_id, manifest=manifest)

    def capabilities(self):
        return {"version": "0.1.0", "network": "unsupported", "snapshot_required": True,
                "frequency": ["1m"], "prices": "unadjusted", "volume_unit": "shares",
                "amount_unit": "CNY", "timezone": "Asia/Shanghai",
                "instruments_as_of": "requires dated visible universe evidence",
                "calendar": "imported evidence only", "corporate_actions": "unsupported",
                "adjustment_factors": "unsupported", "joinquant": "names only, explicit subset",
                "quality_policies": {"observed": ["observed"], "synthetic": ["synthetic"],
                                     "research": ["observed", "inferred", "unverified"]}}
