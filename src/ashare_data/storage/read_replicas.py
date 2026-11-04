"""Select existing, private replicas of immutable manifest/metadata objects.

This module never copies, creates or repairs a replica. A selected replica must
still pass the ordinary content check against its published SHA and size. Its
filesystem identity belongs to the replica, never to the original source path.
"""

from contextlib import closing
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from ashare_data.core.model import DataError, require
from ashare_data.storage.verified_objects import fingerprint as file_fingerprint

PROJECTION_SCHEMA = "ad-consumer-metadata-projection.v1"
_ST_ASSET = "cmes:c75f0048296e5223a82746ea30e0954db515b9ffa1aea0aa575229f47f422308"
_ST_IMPORT = "cmes-import:b29686ed8e88dde31dff1a975636b2de607d3ea2ec65a1914bbcf89391b1ef2f"
_ST_ROWS = 683228
_BIG_TABLES = {
    "cmes_business_selection",
    "cmes_business_conflicts",
    "cmes_rejections",
    "source_aliases",
    "source_assets",
    "cmes_ingested",
    "batch_inputs",
    "cmes_imports",
}
_SELECTION_TABLES = (
    "reference.cmes_company",
)


def _directory(path):
    try:
        info = path.lstat()
    except OSError as exc:
        raise DataError("MARKET_READ_REPLICA", "Replica directory is unavailable") from exc
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            "MARKET_READ_REPLICA", "Replica directory must be private, owned and not a symlink")
    return info


@dataclass(frozen=True)
class ReadReplica:
    directory: Path
    path: Path
    directory_identity: tuple[int, int]
    projection_sha256: str | None = None
    projection_bytes: int | None = None

    def check_directory(self):
        info = _directory(self.directory)
        resolved = self.directory.resolve(strict=True)
        actual = _directory(self.path.parent)
        require(resolved == self.path.parent
                and (info.st_dev, info.st_ino) == self.directory_identity
                and (actual.st_dev, actual.st_ino) == self.directory_identity,
                "MARKET_READ_REPLICA", "Replica directory changed during use")

    def fingerprint(self):
        """Recheck both configured directory and file, including cached reads."""
        self.check_directory()
        try:
            value = self.path.lstat()
        except OSError as exc:
            raise DataError("MARKET_READ_REPLICA", "Selected replica is unavailable") from exc
        require(stat.S_ISREG(value.st_mode) and value.st_uid == os.getuid()
                and stat.S_IMODE(value.st_mode) in {0o400, 0o600},
                "MARKET_READ_REPLICA", "Replica must be a private owned regular file, not a symlink")
        return [value.st_size, value.st_mtime_ns, value.st_ino, value.st_ctime_ns, value.st_dev]


def read_replica(expected, suffix):
    """Return an existing SHA-named replica, or None when that object is absent.

    A configured but invalid directory or present unsafe object is an error, not
    permission to silently fall back. System parent aliases may resolve normally;
    the configured directory itself and the final object may not be symlinks.
    """
    configured = os.environ.get("ASHARE_MARKET_READ_REPLICAS")
    if not configured:
        return None
    require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected)
            and suffix in {".json", ".sqlite3"},
            "MARKET_READ_REPLICA", "Replica selection needs a published SHA and supported type")
    directory = Path(configured).expanduser()
    require(directory.is_absolute(), "MARKET_READ_REPLICA", "Replica directory must be absolute")
    info = _directory(directory)
    root = directory.resolve(strict=True)
    path = root / (expected + suffix)
    replica = ReadReplica(directory, path, (info.st_dev, info.st_ino))
    replica.check_directory()
    # lstat distinguishes an absent object from a broken symlink. Do not create
    # directories or copy anything when a replica has not been prepared.
    try:
        path.lstat()
    except FileNotFoundError:
        replica.check_directory()
        return None
    replica.fingerprint()
    return replica


def _ReusableStSource(root, exclude_path):
    for candidate in sorted(root.glob("*.consumer.sqlite3"), key=lambda p: p.stat().st_mtime_ns, reverse=True):
        if candidate == exclude_path:
            continue
        try:
            with closing(sqlite3.connect(candidate.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                count = db.execute("SELECT count(*) FROM cmes_st WHERE source_asset_id=?", (_ST_ASSET,)).fetchone()[0]
                if count == _ST_ROWS:
                    return candidate
        except Exception:
            continue
    return None


def _build_consumer_projection(source_path, target_path, root):
    reusable_st = _ReusableStSource(root, target_path)
    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as src:
        fd, tmp_name = tempfile.mkstemp(prefix=".consumer-proj-", suffix=".sqlite3", dir=root)
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with closing(sqlite3.connect(tmp_path)) as dst:
                dst.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=4096;")
                for name, sql in src.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ):
                    dst.execute(sql)
                    if name == "cmes_st" and reusable_st is not None:
                        with closing(sqlite3.connect(reusable_st.as_uri() + "?mode=ro&immutable=1", uri=True)) as st_db:
                            rows = st_db.execute("SELECT * FROM cmes_st").fetchall()
                        if rows:
                            placeholders = ",".join("?" for _ in rows[0])
                            dst.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
                    elif name not in _BIG_TABLES:
                        rows = src.execute(f'SELECT * FROM "{name}"').fetchall()
                        if rows:
                            placeholders = ",".join("?" for _ in rows[0])
                            dst.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
                    elif name in ("cmes_business_selection", "cmes_business_conflicts"):
                        marks = ",".join("?" for _ in _SELECTION_TABLES)
                        rows = src.execute(
                            f'SELECT * FROM "{name}" WHERE table_name IN ({marks})',
                            _SELECTION_TABLES,
                        ).fetchall()
                        if rows:
                            placeholders = ",".join("?" for _ in rows[0])
                            dst.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
                    elif name == "cmes_imports":
                        rows = src.execute(
                            "SELECT * FROM cmes_imports WHERE import_id=?", (_ST_IMPORT,)
                        ).fetchall()
                        if rows:
                            dst.executemany("INSERT INTO cmes_imports VALUES (?,?,?)", rows)
                    elif name == "source_assets":
                        rows = src.execute(
                            "SELECT * FROM source_assets WHERE asset_id IN ('daily-sqlite', ?) OR asset_id LIKE 'response:%'",
                            (_ST_ASSET,),
                        ).fetchall()
                        if rows:
                            dst.executemany("INSERT INTO source_assets VALUES (?,?)", rows)
                    elif name == "cmes_ingested":
                        rows = src.execute(
                            "SELECT * FROM cmes_ingested WHERE asset_id=?", (_ST_ASSET,)
                        ).fetchall()
                        if rows:
                            dst.executemany("INSERT INTO cmes_ingested VALUES (?,?,?)", rows)
                    elif name == "batch_inputs":
                        rows = src.execute(
                            "SELECT * FROM batch_inputs WHERE batch_id=2 AND asset_id=?", (_ST_ASSET,)
                        ).fetchall()
                        if rows:
                            dst.executemany("INSERT INTO batch_inputs VALUES (?,?)", rows)
                for _, sql in src.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"
                ):
                    dst.execute(sql)
                dst.commit()
            with tmp_path.open("rb") as stream:
                os.fsync(stream.fileno())
            tmp_path.chmod(0o400)
            os.replace(tmp_path, target_path)
        finally:
            tmp_path.unlink(missing_ok=True)


def consumer_metadata_replica(source_path, item, *, hash_file):
    """Return or build a compact consumer metadata SQLite projection for a CMES release."""
    configured = os.environ.get("ASHARE_MARKET_READ_REPLICAS")
    if not configured:
        return None
    expected = item["sha256"]
    size = item["bytes"]
    require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected)
            and type(size) is int and size > 0,
            "MARKET_READ_REPLICA", "Consumer metadata projection requires published SHA and size")
    directory = Path(configured).expanduser()
    require(directory.is_absolute(), "MARKET_READ_REPLICA", "Replica directory must be absolute")
    info = _directory(directory)
    root = directory.resolve(strict=True)
    db_path = root / (expected + ".consumer.sqlite3")
    meta_path = root / (expected + ".consumer.json")
    lock_path = root / (expected + ".consumer.lock")
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    with os.fdopen(os.open(lock_path, flags, 0o600), "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if db_path.exists() and meta_path.exists():
            try:
                meta_info = meta_path.lstat()
                if (stat.S_ISREG(meta_info.st_mode) and meta_info.st_uid == os.getuid()
                        and stat.S_IMODE(meta_info.st_mode) in {0o400, 0o600}):
                    receipt = json.loads(meta_path.read_bytes())
                    proj_sha = receipt.get("projection_sha256")
                    proj_bytes = receipt.get("projection_bytes")
                    replica = ReadReplica(
                        directory, db_path, (info.st_dev, info.st_ino),
                        projection_sha256=proj_sha, projection_bytes=proj_bytes,
                    )
                    fp = replica.fingerprint()
                    if (receipt.get("schema") == PROJECTION_SCHEMA
                            and receipt.get("source_sha256") == expected
                            and receipt.get("source_bytes") == size
                            and isinstance(proj_sha, str) and re.fullmatch(r"[0-9a-f]{64}", proj_sha)
                            and type(proj_bytes) is int and proj_bytes == fp[0]
                            and receipt.get("fingerprint") == fp):
                        return replica
            except (OSError, ValueError, TypeError, DataError):
                pass
        source = Path(source_path).resolve()
        before = file_fingerprint(source)
        require(before[0] == size, "MARKET_OBJECT_INTEGRITY", "Immutable metadata size changed")
        if db_path.exists():
            db_path.chmod(0o600)
            db_path.unlink(missing_ok=True)
        if meta_path.exists():
            meta_path.chmod(0o600)
            meta_path.unlink(missing_ok=True)
        _build_consumer_projection(source, db_path, root)
        require(file_fingerprint(source) == before, "MARKET_OBJECT_INTEGRITY",
                "Published metadata changed during consumer projection")
        proj_sha = hash_file(db_path)
        replica = ReadReplica(
            directory, db_path, (info.st_dev, info.st_ino),
            projection_sha256=proj_sha, projection_bytes=db_path.stat().st_size,
        )
        fp = replica.fingerprint()
        receipt = {
            "schema": PROJECTION_SCHEMA,
            "source_sha256": expected,
            "source_bytes": size,
            "projection_sha256": proj_sha,
            "projection_bytes": fp[0],
            "fingerprint": fp,
        }
        fd, tmp_meta = tempfile.mkstemp(prefix=".consumer-meta-", suffix=".json", dir=root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp_meta, 0o400)
            os.replace(tmp_meta, meta_path)
        finally:
            if os.path.exists(tmp_meta):
                os.unlink(tmp_meta)
        # Keep at most 2 consumer metadata projections on internal SSD (<300 MB total).
        projections = sorted(
            root.glob("*.consumer.sqlite3"),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        for old_db in projections[2:]:
            if old_db != db_path:
                prefix = old_db.name.removesuffix(".consumer.sqlite3")
                for suffix in (".consumer.sqlite3", ".consumer.json", ".consumer.lock"):
                    candidate = root / (prefix + suffix)
                    try:
                        candidate.chmod(0o600)
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
        return replica
