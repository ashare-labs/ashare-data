"""Immutable file-manifest market releases, with bounded independent DuckDB readers.

Only committed manifests are visible. A reader fixes one manifest for its lifetime;
there is no glob, mutable current/history join, shared writable DuckDB, or service.
Changed chunks produce new objects. Every earlier manifest and object is retained.
"""

from collections import OrderedDict
from contextlib import closing, contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from threading import Lock
from uuid import uuid4

import duckdb

from ashare_data.core.model import require
from ashare_data.storage.read_replicas import consumer_metadata_replica, read_replica
from ashare_data.storage.verified_objects import fingerprint, verify_cached

SCHEMA = "ad-market-release.v1"
ENGINE = "1.4.3"
DISPOSITIONS = ("adopted", "equal_duplicate", "resolved_unused", "quarantined", "format_rejected")
FIELDS = ("open_price", "high_price", "low_price", "close_price", "volume_shares", "turnover_cny")

# Reuse only immutable bytes, never the caller-mutable parsed manifest/files.
# The existing opt-in verification-cache trust model also covers this process-local
# cache. One entry avoids retaining every release in a long-running consumer.
# Larger manifests still read normally; this is a retention cap, not a data limit.
_MANIFEST_CACHE_BYTES = 512 * 1024**2
_manifest_cache = None
_manifest_lock = Lock()
# Non-target ancestors need only their immutable link, not another giant raw
# manifest or parsed dict. Keep the selected manifest's one-entry byte cache.
_PARENT_CACHE_ENTRIES = 128
_parent_link_cache = OrderedDict()
_PARSED_CACHE_ENTRIES = 4
_SMALL_MANIFEST_BYTES = 1024 * 1024
_parsed_release_cache = OrderedDict()
_metadata_db_pool = OrderedDict()
_pool_lock = Lock()


def _verified_manifest_bytes(path, expected, before=None, *, replica=None):
    identity = replica.fingerprint if replica else lambda: fingerprint(path)
    before = tuple(identity()) if before is None else before
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    require(len(raw) == before[0] and actual == expected,
            "MARKET_RELEASE_INTEGRITY", "Published manifest chain changed")
    require(tuple(identity()) == before, "MARKET_RELEASE_INTEGRITY",
            "Published manifest changed during verification")
    return raw, actual


def _manifest_bytes(path, expected):
    """Keep CURRENT/chain selection fresh while reusing a verified file identity.

    A hit requires path, expected SHA and the same size/mtime/ctime/inode/device
    before and after access. Changed files always undergo a new full-content
    check. Disabling ASHARE_MARKET_VERIFY_CACHE retains uncached verification.
    """
    global _manifest_cache
    replica = read_replica(expected, ".json")
    if replica:
        path = replica.path
    identity = replica.fingerprint if replica else lambda: fingerprint(path)
    if not os.environ.get("ASHARE_MARKET_VERIFY_CACHE"):
        with _manifest_lock:
            _manifest_cache = None
            _parent_link_cache.clear()
        return _verified_manifest_bytes(path, expected, replica=replica)
    # Serialize cache misses so simultaneous discovery requests do not each read
    # and retain the same potentially large manifest. Parsing happens outside.
    with _manifest_lock:
        before = tuple(identity())
        key = (path, expected, before)
        if _manifest_cache is not None and _manifest_cache[0] == key:
            if tuple(identity()) != before:
                _manifest_cache = None
                require(False, "MARKET_RELEASE_INTEGRITY",
                        "Published manifest changed during cache access")
            return _manifest_cache[1], expected
        _manifest_cache = None
        raw, actual = _verified_manifest_bytes(path, expected, before, replica=replica)
        if len(raw) <= _MANIFEST_CACHE_BYTES:
            _manifest_cache = (key, raw)
        return raw, actual


def _checked_parent_link(link, current_id):
    parent_id, parent_sha = link
    # Only non-target ancestors are traversed. A selected root terminates the
    # constructor before this helper, so its null terminal link remains valid.
    require(type(current_id) is int and type(parent_id) is int
            and 0 < parent_id < current_id
            and isinstance(parent_sha, str) and re.fullmatch(r"[0-9a-f]{64}", parent_sha),
            "MARKET_RELEASE_INTEGRITY", "Invalid linear release chain")
    return link


def _read_parent_link(path, expected, current_id, before=None, *, replica=None):
    raw, _ = _verified_manifest_bytes(path, expected, before, replica=replica)
    parent = json.loads(raw)
    require(isinstance(parent, dict) and parent.get("schema") == SCHEMA
            and type(parent.get("release_id")) is int and parent["release_id"] == current_id,
            "MARKET_RELEASE_INTEGRITY", "Unexpected parent release identity")
    return _checked_parent_link((parent.get("parent_release_id"), parent.get("parent_manifest_sha256")),
                                current_id)


def _parent_link(path, expected, current_id):
    """Verify one ancestor and retain only a bounded, immutable parent link.

    This deliberately does not call _manifest_bytes: an ancestor would evict the
    selected manifest, and reacquiring the same lock would deadlock. Cold readers
    share one complete check/parse while CURRENT and all link fingerprints stay
    fresh. A cache entry never owns raw bytes or a mutable parsed manifest.
    """
    global _manifest_cache
    replica = read_replica(expected, ".json")
    if replica:
        path = replica.path
    identity = replica.fingerprint if replica else lambda: fingerprint(path)
    if not os.environ.get("ASHARE_MARKET_VERIFY_CACHE"):
        with _manifest_lock:
            _manifest_cache = None
            _parent_link_cache.clear()
        return _read_parent_link(path, expected, current_id, replica=replica)
    with _manifest_lock:
        before = tuple(identity())
        # A SHA-named replica may be selected under a different CURRENT id;
        # only the id whose self-identity was validated may reuse this link.
        key = (path, expected, before, current_id)
        if key in _parent_link_cache:
            if tuple(identity()) != before:
                _parent_link_cache.pop(key)
                require(False, "MARKET_RELEASE_INTEGRITY",
                        "Published parent manifest changed during cache access")
            _parent_link_cache.move_to_end(key)
            return _checked_parent_link(_parent_link_cache[key], current_id)
        link = _read_parent_link(path, expected, current_id, before, replica=replica)
        # Parsing can itself take time for a large manifest; check again before
        # publishing the small derived link to other readers.
        require(tuple(identity()) == before, "MARKET_RELEASE_INTEGRITY",
                "Published parent manifest changed during parsing")
        if _PARENT_CACHE_ENTRIES > 0:
            _parent_link_cache[key] = link
            while len(_parent_link_cache) > _PARENT_CACHE_ENTRIES:
                _parent_link_cache.popitem(last=False)
        return link


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode()


def sha_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def atomic_write(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".partial")
    with temp.open("wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def connection(temp=None, memory="2GB", threads=2):
    c = duckdb.connect()
    try:
        c.execute("SET threads=?", [threads])
        c.execute("SET memory_limit=?", [os.environ.get("ASHARE_DUCKDB_MEMORY_LIMIT", memory)])
        c.execute("SET TimeZone='UTC'; SET preserve_insertion_order=false")
        # Library consumers and JSON CLIs own stdout; engine progress is not payload.
        c.execute("SET enable_progress_bar=false")
        directory = temp if temp is not None else os.environ.get("ASHARE_DUCKDB_TEMP_DIRECTORY")
        if directory is not None:
            c.execute("SET temp_directory=?", [str(directory)])
            c.execute("SET max_temp_directory_size=?",
                      [os.environ.get("ASHARE_DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "8GB")])
        return c
    except BaseException:
        c.close()
        raise


class ReleaseWriter:
    """Single cooperative writer. Unpublished objects and checkpoints are resumable."""

    def __init__(
        self,
        root,
        *,
        budget_bytes=200 * 1024**3,
        free_reserve_bytes=150 * 1024**3,
        retain_work_files=False,
        categorized=False,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget_bytes = budget_bytes
        self.free_reserve_bytes = free_reserve_bytes
        self.retain_work_files = retain_work_files
        # Match the categorized layout already used by the CMES controller.
        self.categorized = categorized or (self.root / "CATALOG.json").exists()
        self.work = self.root / (".work" if self.categorized else "work")
        for name in (("data", "metadata", "releases", ".work") if self.categorized
                     else ("objects", "releases", "work")):
            (self.root / name).mkdir(exist_ok=True)
        self.lock = None

    def __enter__(self):
        self.lock = (self.root / "writer.lock").open("a+b")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            self.lock = None
            raise
        return self

    def __exit__(self, *unused):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def budget(self):
        require(
            shutil.disk_usage(self.root).free >= self.free_reserve_bytes,
            "MARKET_CAPACITY",
            "Free-space reserve reached; completed chunks remain resumable",
        )
        used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        require(used < self.budget_bytes, "MARKET_CAPACITY", "Market asset budget reached")
        return used

    def object(self, path, kind="parquet", *, table=None):
        path = Path(path)
        digest = sha_file(path)
        directory = self.root / "objects"
        if self.categorized:
            directory = self.root / ("metadata" if kind == "sqlite3" else "data")
            if table:
                parts = table.split(".")
                require(len(parts) == 2 and all(p.replace("_", "").isalnum() for p in parts),
                        "MARKET_TABLE", "Use a schema-qualified table for categorized objects")
                directory = directory.joinpath(*parts)
            directory.mkdir(parents=True, exist_ok=True)
        target = directory / (digest + "." + kind)
        if target.exists():
            require(sha_file(target) == digest, "MARKET_INTEGRITY", "Existing object changed")
            if target != path and not self.retain_work_files:
                path.unlink()
        else:
            os.replace(path, target)
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
            from ashare_data.storage.immutable import fsync_dir
            fsync_dir(target.parent)
        return {
            "path": str(target.relative_to(self.root)),
            "sha256": digest,
            "bytes": target.stat().st_size,
        }

    def parquet(self, c, sql, params, name, *, table=None):
        suffix = "-" + uuid4().hex if self.retain_work_files else ""
        path = self.work / (name + suffix + ".partial.parquet")
        if path.exists():
            path.unlink()  # This writer's incomplete output only; never a source or published object.
        try:
            c.execute(
                "COPY ("
                + sql
                + ") TO "
                + sql_literal(path)
                + " (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)",
                params,
            )
            # Verify every projected value in both directions. A batch must be bounded upstream.
            projection = c.execute("DESCRIBE " + sql, params).fetchall()
            columns = ",".join('"' + r[0].replace('"', '""') + '"' for r in projection)
            restored = "SELECT " + columns + " FROM read_parquet(" + sql_literal(path) + ")"
            query = "SELECT * FROM (" + sql + ")"
            difference = c.execute(
                "SELECT count(*) FROM (("
                + query
                + " EXCEPT ALL "
                + restored
                + ") UNION ALL ("
                + restored
                + " EXCEPT ALL "
                + query
                + "))",
                list(params) + list(params),
            ).fetchone()[0]
            require(difference == 0, "MARKET_RECONCILIATION", "Key/value roundtrip differs")
            result = self.object(path, table=table)
            result["key_value_mismatches"] = difference
            return result
        except BaseException:
            if not self.retain_work_files:
                path.unlink(missing_ok=True)
            raise

    def publish(self, manifest, *, expected_parent, verify_unchanged=True,
                expected_parent_sha256=None, native_macro_boundary=None):
        require(self.lock is not None, "MARKET_WRITER", "Acquire the writer lock")
        pointer = self.root / "CURRENT.json"
        old = json.loads(pointer.read_bytes()) if pointer.exists() else None
        parent = old["release_id"] if old else None
        prior_financial = None
        require(
            parent == expected_parent, "MARKET_PARENT_CHANGED", "Publish against the current parent"
        )
        if old:
            require(expected_parent_sha256 is None or expected_parent_sha256 == old["sha256"],
                    "MARKET_PARENT_CHANGED", "Current parent SHA differs; prepare again")
            prior_body = (self.root / "releases" / f"{parent:08d}.json").read_bytes()
            require(hashlib.sha256(prior_body).hexdigest() == old["sha256"],
                    "MARKET_RELEASE_INTEGRITY", "Current parent manifest changed")
            prior = json.loads(prior_body)
            prior_financial = prior.get("financial_reference_tables")
            if prior_financial:
                incoming = manifest.get("financial_reference_tables")
                require(bool(incoming), "FIN_CONTRACT", "Preserve the published financial contract")
                require(set(prior_financial["candidate_ids"]) <= set(incoming.get("candidate_ids", [])),
                        "FIN_CONTRACT", "Preserve financial candidate history")
                def financial_objects(value):
                    return {(x["table"], x["partition_key"], x["sha256"])
                            for x in value.get("evidence", []) if x.get("kind") == "financial_table"}
                require(financial_objects(prior) <= financial_objects(manifest),
                        "FIN_CONTRACT", "Previously published financial table set must be retained")
        release_id = (parent or 0) + 1
        inherited = {}
        if old and not verify_unchanged:
            prior_release = MarketRelease(self.root, parent, expected_manifest_sha256=old["sha256"])
            inherited = {(f["table"], f["series_id"], f["instrument_id"]): f for f in prior_release.files}
        seen = set()
        for item in manifest["files"]:
            key = (item["table"], item["series_id"], item["instrument_id"])
            require(
                key not in seen,
                "MARKET_PARTITION_OVERLAP",
                "Exactly one complete instrument chunk per series/table in a release",
            )
            seen.add(key)
            require(
                item["key_value_mismatches"] == 0 and item["duplicate_keys"] == 0,
                "MARKET_RECONCILIATION",
                "Unverified chunk",
            )
            if inherited.get(key) != item:
                require(
                    sha_file(self.root / item["path"]) == item["sha256"],
                    "MARKET_INTEGRITY",
                    "Cannot publish a changed object",
                )
        for source, count in manifest["input_dispositions"].items():
            require(
                count["input_rows"] == sum(count.get(k, 0) for k in DISPOSITIONS),
                "MARKET_INPUT_ACCOUNTING",
                "Input dispositions do not balance: " + source,
            )
        metadata = manifest["metadata"]
        require(
            sha_file(self.root / metadata["path"]) == metadata["sha256"],
            "MARKET_INTEGRITY",
            "Metadata object changed",
        )
        if prior_financial:
            prior_metadata = self.root / prior["metadata"]["path"]
            require(sha_file(prior_metadata) == prior["metadata"]["sha256"],
                    "MARKET_INTEGRITY", "Previous financial metadata changed")
            uri = (self.root / metadata["path"]).as_uri() + "?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri, uri=True)) as db:
                db.execute("ATTACH DATABASE ? AS previous",
                           (prior_metadata.as_uri() + "?mode=ro&immutable=1",))
                present = {r[0] for r in db.execute("SELECT name FROM main.sqlite_master")}
                for table in ("release_issuers", "release_issuer_instruments", "source_assets",
                              "data_batches", "batch_inputs", "source_aliases", "release_identifiers"):
                    require(table in present, "FIN_METADATA", "Preserve financial metadata table " + table)
                    columns = ("security,instrument_id" if table == "release_identifiers" else
                               ",".join(r[1] for r in db.execute("PRAGMA previous.table_info(" + table + ")")))
                    missing = db.execute("SELECT count(*) FROM (SELECT " + columns + " FROM previous." + table
                                         + " EXCEPT SELECT " + columns + " FROM main." + table + ")").fetchone()[0]
                    require(missing == 0, "FIN_METADATA",
                            "Existing financial metadata or identifier mapping changed: " + table)
        if manifest.get("financial_reference_tables") or any(
            item.get("kind") == "financial_table" for item in manifest.get("evidence", [])
        ):
            from ashare_data.storage.financial_release import verify_manifest_contract
            verify_manifest_contract(self.root, manifest)
        from ashare_data.storage.unit_publication import (
            CONTRACT as unit_contract, verify_contract as verify_units, preserves_prior, layers,
        )
        if old and prior.get(unit_contract):
            require(preserves_prior(prior[unit_contract], manifest.get(unit_contract)), "FIN_UNIT_HISTORY",
                    "Preserve the immutable unit contract when publishing unrelated increments")
        if old and prior.get(unit_contract) and manifest.get(unit_contract) != prior[unit_contract]:
            added = layers(manifest[unit_contract])[-1]
            require(added["base_release_id"] == parent and added["base_manifest_sha256"] == old["sha256"],
                    "FIN_UNIT_HISTORY", "Unit increment must bind the immediately previous release")
        if manifest.get(unit_contract):
            verify_units(self.root, manifest)
        from ashare_data.storage.reference_coverage import CONTRACT as coverage_contract, validate as validate_coverage
        if old and prior.get(coverage_contract):
            require(manifest.get(coverage_contract) == prior[coverage_contract], 'FIN_COVERAGE_HISTORY',
                    'Preserve prior coverage evidence on unrelated releases')
        if manifest.get(coverage_contract):
            validate_coverage(self.root, manifest)
        from ashare_data.storage.native_macro_release import publication_guard
        publication_guard(self, manifest, prior if old else None, old, native_macro_boundary)
        value = {
            **manifest,
            "schema": SCHEMA,
            "release_id": release_id,
            "parent_release_id": parent,
            "parent_manifest_sha256": old["sha256"] if old else None,
            "query_engine": "duckdb==" + ENGINE,
            "snapshot_model": "immutable file manifests; retained replaced chunks, no sparse row history",
            "publication_validation": "all fact objects"
            if verify_unchanged
            else "changed fact objects; exact unchanged descriptors inherited from verified parent manifest; selected objects reverified on read",
        }
        body = canonical(value)
        digest = hashlib.sha256(body).hexdigest()
        release = self.root / "releases" / f"{release_id:08d}.json"
        if release.exists():
            require(
                release.read_bytes() == body,
                "MARKET_RELEASE_EXISTS",
                "An interrupted different release occupies this ID; inspect before recovery",
            )
        else:
            atomic_write(release, body)
        pointer_value = {
            "schema": SCHEMA,
            "release_id": release_id,
            "manifest": str(release.relative_to(self.root)),
            "sha256": digest,
        }
        atomic_write(pointer, canonical(pointer_value))
        return pointer_value


class MarketRelease:
    """Fix one local release directly by target manifest without recursive chain or object SHA-256."""

    def __init__(self, root, release_id=None, *, expected_manifest_sha256=None):
        self.root = Path(root).expanduser().resolve()
        pointer = json.loads((self.root / "CURRENT.json").read_bytes())
        release_id = pointer["release_id"] if release_id is None else release_id
        require(
            type(release_id) is int and 0 < release_id <= pointer["release_id"],
            "MARKET_RELEASE_ID",
            "Use a committed release in the published chain",
        )
        path = self.root / "releases" / f"{release_id:08d}.json"
        require(path.is_file(), "MARKET_RELEASE_INTEGRITY", "Release manifest not found")
        st = path.stat()
        cache_key = (path, st.st_size, st.st_mtime_ns, release_id)
        with _manifest_lock:
            cached = _parsed_release_cache.get(cache_key)
            if cached is not None:
                _parsed_release_cache.move_to_end(cache_key)
            else:
                raw = path.read_bytes()
                parsed = json.loads(raw)
                require(
                    isinstance(parsed, dict)
                    and parsed.get("schema") == SCHEMA
                    and parsed.get("release_id") == release_id,
                    "MARKET_RELEASE_INTEGRITY",
                    "Unexpected release identity",
                )
                sha256 = (
                    pointer["sha256"]
                    if release_id == pointer["release_id"]
                    else hashlib.sha256(raw).hexdigest()
                )
                self.metadata_path = self.verify(parsed["metadata"])
                with self.metadata(parsed["metadata"]) as db:
                    instruments = {
                        r["security"]: dict(r)
                        for r in db.execute("SELECT * FROM release_instruments")
                    }
                    identifiers = {
                        r["security"]: r["instrument_id"]
                        for r in db.execute("SELECT * FROM release_identifiers")
                    }
                file_index = {}
                for item in parsed["files"]:
                    file_index.setdefault((item["table"], item["instrument_id"]), []).append(item)
                cached = {
                    "manifest": parsed,
                    "sha256": sha256,
                    "instruments": instruments,
                    "identifiers": identifiers,
                    "file_index": file_index,
                }
                _parsed_release_cache[cache_key] = cached
                while len(_parsed_release_cache) > _PARSED_CACHE_ENTRIES:
                    _parsed_release_cache.popitem(last=False)
        self.metadata_path = self.verify(cached["manifest"]["metadata"])
        self.sha256 = (
            pointer["sha256"] if release_id == pointer["release_id"] else cached["sha256"]
        )
        if expected_manifest_sha256:
            require(
                self.sha256 == expected_manifest_sha256,
                "MARKET_RELEASE_INTEGRITY",
                "Frozen release manifest changed",
            )
        self.release_id = release_id
        self._verified = {}
        if st.st_size <= _SMALL_MANIFEST_BYTES:
            self.manifest = deepcopy(cached["manifest"])
            self.files = self.manifest["files"]
            self.instruments = deepcopy(cached["instruments"])
            self.identifiers = dict(cached["identifiers"])
            file_index = {}
            for item in self.files:
                file_index.setdefault((item["table"], item["instrument_id"]), []).append(item)
            self._file_index = file_index
        else:
            self.manifest = {
                k: (
                    list(v)
                    if k == "files" and isinstance(v, list)
                    else dict(v)
                    if isinstance(v, dict) and len(v) <= 64
                    else v
                )
                for k, v in cached["manifest"].items()
            }
            self.files = self.manifest["files"]
            self.instruments = dict(cached["instruments"])
            self.identifiers = dict(cached["identifiers"])
            self._file_index = cached["file_index"]
        self._files_list_ref = self.files
        self._initial_files_len = len(self.files)
        self._edge_file_refs = (self.files[0], self.files[-1]) if self.files else ()

    def _can_use_file_index(self):
        if (
            self._file_index is None
            or self.files is not self._files_list_ref
            or self.manifest.get("files") is not self.files
            or len(self.files) != self._initial_files_len
        ):
            return False
        if not self.files:
            return not self._edge_file_refs
        return (
            len(self._edge_file_refs) == 2
            and self.files[0] is self._edge_file_refs[0]
            and self.files[-1] is self._edge_file_refs[1]
        )

    def verify(self, item):
        relative = Path(item["path"])
        require(
            bool(relative.parts) and not relative.is_absolute() and ".." not in relative.parts,
            "MARKET_OBJECT_PATH",
            "Object must be within the selected asset",
        )
        path = (self.root / relative).resolve()
        require(path.is_relative_to(self.root), "MARKET_OBJECT_PATH", "Object escaped asset root")
        require(path.is_file(), "MARKET_OBJECT_INTEGRITY", "Object not found: " + str(relative))
        return path

    def open_metadata_db(self, metadata=None):
        item = self.manifest["metadata"] if metadata is None else metadata
        path = self.verify(item)
        st = path.stat()
        pool_key = (path, st.st_size, st.st_mtime_ns, st.st_ino)
        with _pool_lock:
            pool = _metadata_db_pool.get(pool_key)
            while pool:
                db = pool.pop()
                try:
                    _ = db.in_transaction
                except sqlite3.ProgrammingError:
                    continue
                db.row_factory = sqlite3.Row
                _metadata_db_pool.move_to_end(pool_key)
                return pool_key, db
        db = sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1", uri=True, check_same_thread=False
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA cache_size=-65536")
        return pool_key, db

    def close_metadata_db(self, pool_key, db):
        try:
            if db.in_transaction:
                db.rollback()
        except sqlite3.ProgrammingError:
            return
        with _pool_lock:
            pool = _metadata_db_pool.setdefault(pool_key, [])
            _metadata_db_pool.move_to_end(pool_key)
            if len(pool) < 4:
                db.row_factory = sqlite3.Row
                pool.append(db)
                db = None
            while len(_metadata_db_pool) > _PARSED_CACHE_ENTRIES:
                _, evicted = _metadata_db_pool.popitem(last=False)
                for conn in evicted:
                    conn.close()
        if db is not None:
            db.close()

    @contextmanager
    def metadata(self, metadata=None):
        pool_key, db = self.open_metadata_db(metadata)
        try:
            yield db
        finally:
            self.close_metadata_db(pool_key, db)

    def selected_files(self, table, instrument_id, start, end, series_id=None):
        start_s = str(start)[:10]
        end_s = str(end)[:10]
        if self._can_use_file_index():
            source = self._file_index.get((table, instrument_id), ())
        else:
            source = self.files
        return [
            r
            for r in source
            if r["table"] == table
            and r["instrument_id"] == instrument_id
            and (series_id is None or r["series_id"] == series_id)
            and r["first_date"] <= end_s
            and r["last_date"] >= start_s
        ]

    def read(self, table, security, start, end, *, series_id=None):
        require(
            table in {"bars_1d", "bars_1m"}, "MARKET_TABLE", "Choose a logical market fact table"
        )
        require(
            security in self.identifiers, "MARKET_SECURITY", "Unknown frozen security identifier"
        )
        instrument_id = self.identifiers[security]
        items = self.selected_files(table, instrument_id, start, end, series_id)
        if not items:
            return []
        column = "trade_date" if table == "bars_1d" else "label_at"
        c = connection()
        result = []
        try:
            # Different series can retain different physical numeric representations.
            # A SQL union of DOUBLE and DECIMAL silently rounds decimal source values.
            unique = {(item["path"], item["series_id"]): item for item in items}
            for item in unique.values():
                path = str(self.verify(item))
                cursor = c.execute(
                    "SELECT * FROM read_parquet(?) WHERE "
                    + column
                    + " BETWEEN ? AND ? AND instrument_id=? AND series_id=? ORDER BY "
                    + column,
                    [path, start, end, instrument_id, item["series_id"]],
                )
                names = [r[0] for r in c.description]
                while batch := cursor.fetchmany(4096):
                    result.extend(dict(zip(names, row)) for row in batch)
            return sorted(result, key=lambda r: (r[column], r["series_id"]))
        finally:
            c.close()

    def sql(self, *, tables=("bars_1d", "bars_1m"), securities=None, include_market_evidence=True):
        """Open the two logical fact views over this exact manifest, with explicit paths.

        Caller owns the returned in-memory connection. Restrict securities for bounded
        research; whole-market access verifies all selected immutable objects once.
        """
        require(set(tables) <= {"bars_1d", "bars_1m"}, "MARKET_TABLE", "Unknown fact table")
        ids = {self.identifiers[s] for s in securities} if securities is not None else None
        c = connection()
        c.execute(
            "CREATE SCHEMA market; CREATE SCHEMA reference; CREATE SCHEMA meta; CREATE SCHEMA quality"
        )
        for table in tables:
            if ids is not None and self._can_use_file_index():
                items = [
                    f
                    for iid in sorted(ids)
                    for f in self._file_index.get((table, iid), ())
                    if f["table"] == table and f["instrument_id"] in ids
                ]
            else:
                items = [
                    f
                    for f in self.files
                    if f["table"] == table and (ids is None or f["instrument_id"] in ids)
                ]
            require(bool(items), "MARKET_EMPTY_SCOPE", "No files in the requested table/scope")
            if table == "bars_1d" and len({f["series_id"] for f in items}) == 1:
                paths = ",".join(sql_literal(self.verify(f))
                                 for f in {f["path"]: f for f in items}.values())
                selected_ids = sorted({f["instrument_id"] for f in items})
                scope = " WHERE instrument_id IN (" + ",".join(map(str, selected_ids)) + ")"
                scope += " AND series_id=" + str(items[0]["series_id"])
                c.execute(
                    "CREATE VIEW market."
                    + table
                    + " AS SELECT * FROM read_parquet(["
                    + paths
                    + "], union_by_name=true)" + scope
                )
                continue
            # Mixed physical series use lossless tagged numeric values. Consumers
            # choose a series before numeric arithmetic and extract its declared tag.
            parts = []
            for series_id in sorted({f["series_id"] for f in items}):
                selected = [f for f in items if f["series_id"] == series_id]
                paths = ",".join(sql_literal(self.verify(f))
                                 for f in {f["path"]: f for f in selected}.values())
                selected_ids = sorted({f["instrument_id"] for f in selected})
                scope = " WHERE instrument_id IN (" + ",".join(map(str, selected_ids)) + ")"
                scope += " AND series_id=" + str(series_id)
                relation = "read_parquet([" + paths + "], union_by_name=true)"
                types = dict(
                    (r[0], r[1]) for r in c.execute("DESCRIBE SELECT * FROM " + relation).fetchall()
                )
                columns = [
                    "series_id",
                    "instrument_id",
                    "trade_date",
                    *(["label_at"] if table == "bars_1m" else []),
                    "batch_id",
                    "volume_shares",
                ]
                for field in (*FIELDS[:4], "turnover_cny"):
                    tag = "binary_value" if types[field] in ("FLOAT", "DOUBLE") else "fixed_value"
                    columns.append(
                        "CAST(union_value("
                        + tag
                        + ":="
                        + field
                        + ") AS UNION(binary_value DOUBLE, fixed_value DECIMAL(38,12))) AS "
                        + field
                    )
                parts.append("SELECT " + ",".join(columns) + " FROM " + relation + scope)
            c.execute("CREATE VIEW market." + table + " AS " + " UNION ALL ".join(parts))
        if include_market_evidence:
            checks = [
                item
                for item in self.manifest.get("evidence", [])
                if item.get("kind") == "bar_day_checks"
                and (ids is None or item.get("instrument_id") in ids)
            ]
            if checks:
                parts = []
                for version in sorted({x["check_version"] for x in checks}):
                    paths = ",".join(
                        sql_literal(self.verify(x)) for x in checks if x["check_version"] == version
                    )
                    parts.append(
                        "SELECT *,"
                        + sql_literal(version)
                        + " AS check_version,"
                        + str(self.release_id)
                        + " AS asof_release_id FROM read_parquet(["
                        + paths
                        + "],union_by_name=true)"
                    )
                c.execute("CREATE VIEW quality.bar_day_checks AS " + " UNION ALL BY NAME ".join(parts))
        import pandas as pd

        with self.metadata() as db:
            for original, target in (
                ("release_instruments", "reference.instruments"),
                ("release_identifiers", "reference.instrument_identifiers"),
            ):
                rows = db.execute("SELECT * FROM " + original)
                frame = pd.DataFrame(rows.fetchall(), columns=[r[0] for r in rows.description])
                c.register("metadata_rows", frame)
                c.execute(
                    "CREATE TABLE "
                    + target
                    + " AS SELECT *,"
                    + sql_literal(self.manifest["metadata"]["sha256"])
                    + " AS reference_version FROM metadata_rows"
                )
                c.unregister("metadata_rows")
            present = {
                r[0] for r in db.execute("select name from sqlite_master where type='table'")
            }
            for original in (
                "series_contracts",
                "data_batches",
                "batch_inputs",
                "source_assets",
                "source_aliases",
            ):
                if original not in present or not include_market_evidence:
                    continue
                cursor = db.execute("SELECT * FROM " + original)
                frame = pd.DataFrame(cursor.fetchall(), columns=[r[0] for r in cursor.description])
                c.register("metadata_rows", frame)
                c.execute("CREATE TABLE meta." + original + " AS SELECT * FROM metadata_rows")
                c.unregister("metadata_rows")
        c.execute(
            "CREATE TABLE meta.data_releases AS SELECT "
            + str(self.release_id)
            + " AS release_id,"
            + sql_literal(self.sha256)
            + " AS manifest_sha256"
        )
        return c

    def receipt(self):
        return {
            "release_id": self.release_id,
            "manifest_sha256": self.sha256,
            "root": str(self.root),
            "query_engine": "duckdb==" + ENGINE,
            "snapshot": "one immutable file manifest for this reader lifetime",
            "strict_historical_pit": False,
        }

    def financial(self):
        """Financial/reference reader bound to this exact common release."""
        from ashare_data.product.financial_release import FinancialTables
        return FinancialTables(self)

    def native_macro(self):
        """Explicit native macro source reader pinned to this exact release."""
        from ashare_data.product.native_macro import NativeMacro
        return NativeMacro(self)
