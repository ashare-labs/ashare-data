"""Immutable file-manifest market releases, with bounded independent DuckDB readers.

Only committed manifests are visible. A reader fixes one manifest for its lifetime;
there is no glob, mutable current/history join, shared writable DuckDB, or service.
Changed chunks produce new objects. Every earlier manifest and object is retained.
"""

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

import duckdb

from ashare_data.core.model import require

SCHEMA = "ad-market-release.v1"
ENGINE = "1.4.3"
DISPOSITIONS = ("adopted", "equal_duplicate", "resolved_unused", "quarantined", "format_rejected")
FIELDS = ("open_price", "high_price", "low_price", "close_price", "volume_shares", "turnover_cny")


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


def fingerprint(path):
    s = Path(path).stat()
    return [s.st_size, s.st_mtime_ns, s.st_ino]


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
    require(duckdb.__version__ == ENGINE, "MARKET_RUNTIME", "Use the frozen DuckDB version")
    c = duckdb.connect()
    try:
        c.execute("SET threads=?", [threads])
        c.execute("SET memory_limit=?", [memory])
        c.execute("SET TimeZone='UTC'; SET preserve_insertion_order=false")
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

    def __init__(self, root, *, budget_bytes=200 * 1024**3, free_reserve_bytes=150 * 1024**3):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget_bytes = budget_bytes
        self.free_reserve_bytes = free_reserve_bytes
        for name in ("objects", "releases", "work"):
            (self.root / name).mkdir(exist_ok=True)
        self.lock = None

    def __enter__(self):
        self.lock = (self.root / "writer.lock").open("a+b")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *unused):
        if self.lock is not None:
            self.lock.close()

    def budget(self):
        require(
            shutil.disk_usage(self.root).free >= self.free_reserve_bytes,
            "MARKET_CAPACITY",
            "Free-space reserve reached; completed chunks remain resumable",
        )
        used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        require(used < self.budget_bytes, "MARKET_CAPACITY", "Market asset budget reached")
        return used

    def object(self, path, kind="parquet"):
        path = Path(path)
        digest = sha_file(path)
        target = self.root / "objects" / (digest + "." + kind)
        if target.exists():
            require(sha_file(target) == digest, "MARKET_INTEGRITY", "Existing object changed")
            if target != path:
                path.unlink()
        else:
            os.replace(path, target)
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
        return {
            "path": str(target.relative_to(self.root)),
            "sha256": digest,
            "bytes": target.stat().st_size,
        }

    def parquet(self, c, sql, params, name):
        path = self.root / "work" / (name + ".partial.parquet")
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
            result = self.object(path)
            result["key_value_mismatches"] = difference
            return result
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def publish(self, manifest, *, expected_parent):
        require(self.lock is not None, "MARKET_WRITER", "Acquire the writer lock")
        pointer = self.root / "CURRENT.json"
        old = json.loads(pointer.read_bytes()) if pointer.exists() else None
        parent = old["release_id"] if old else None
        prior_financial = None
        require(
            parent == expected_parent, "MARKET_PARENT_CHANGED", "Publish against the current parent"
        )
        if old:
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
        value = {
            **manifest,
            "schema": SCHEMA,
            "release_id": release_id,
            "parent_release_id": parent,
            "parent_manifest_sha256": old["sha256"] if old else None,
            "query_engine": "duckdb==" + ENGINE,
            "snapshot_model": "immutable file manifests; retained replaced chunks, no sparse row history",
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
    """Fix one local release. The cached key includes manifest AND immutable object identity."""

    def __init__(self, root, release_id=None, *, expected_manifest_sha256=None):
        self.root = Path(root).expanduser().resolve()
        pointer = json.loads((self.root / "CURRENT.json").read_bytes())
        release_id = pointer["release_id"] if release_id is None else release_id
        require(
            type(release_id) is int and 0 < release_id <= pointer["release_id"],
            "MARKET_RELEASE_ID",
            "Use a committed release in the published chain",
        )
        current_id, expected = pointer["release_id"], pointer["sha256"]
        while True:
            path = self.root / "releases" / f"{current_id:08d}.json"
            raw = path.read_bytes()
            actual = hashlib.sha256(raw).hexdigest()
            require(
                actual == expected, "MARKET_RELEASE_INTEGRITY", "Published manifest chain changed"
            )
            if current_id == release_id:
                break
            parent = json.loads(raw)
            require(
                type(parent["parent_release_id"]) is int
                and parent["parent_release_id"] < current_id,
                "MARKET_RELEASE_INTEGRITY",
                "Invalid linear release chain",
            )
            current_id, expected = parent["parent_release_id"], parent["parent_manifest_sha256"]
        self.sha256 = actual
        if expected_manifest_sha256:
            require(
                self.sha256 == expected_manifest_sha256,
                "MARKET_RELEASE_INTEGRITY",
                "Frozen release manifest changed",
            )
        self.manifest = json.loads(raw)
        require(
            self.manifest["schema"] == SCHEMA and self.manifest["release_id"] == release_id,
            "MARKET_RELEASE_INTEGRITY",
            "Unexpected release identity",
        )
        self.release_id = release_id
        self._verified = {}
        self.files = self.manifest["files"]
        self.metadata_path = self.verify(self.manifest["metadata"])
        with self.metadata() as db:
            self.instruments = {
                r["security"]: dict(r) for r in db.execute("SELECT * FROM release_instruments")
            }
            self.identifiers = {
                r["security"]: r["instrument_id"]
                for r in db.execute("SELECT * FROM release_identifiers")
            }

    def verify(self, item):
        relative = Path(item["path"])
        require(
            not relative.is_absolute() and ".." not in relative.parts,
            "MARKET_OBJECT_PATH",
            "Object must be within the selected asset",
        )
        path = (self.root / relative).resolve()
        require(path.is_relative_to(self.root), "MARKET_OBJECT_PATH", "Object escaped asset root")
        current = fingerprint(path)
        key = (self.sha256, item["sha256"], str(path))
        if self._verified.get(key) != current:
            require(
                current[0] == item["bytes"] and sha_file(path) == item["sha256"],
                "MARKET_OBJECT_INTEGRITY",
                "Immutable object changed: " + str(relative),
            )
            require(
                fingerprint(path) == current,
                "MARKET_OBJECT_INTEGRITY",
                "Object changed during verification",
            )
            self._verified[key] = current
        return path

    @contextmanager
    def metadata(self):
        self.verify(self.manifest["metadata"])
        before = fingerprint(self.metadata_path)
        db = sqlite3.connect(self.metadata_path.as_uri() + "?mode=ro&immutable=1", uri=True)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()
            require(
                fingerprint(self.metadata_path) == before,
                "MARKET_OBJECT_INTEGRITY",
                "Metadata changed during read",
            )

    def selected_files(self, table, instrument_id, start, end, series_id=None):
        return [
            r
            for r in self.files
            if r["table"] == table
            and r["instrument_id"] == instrument_id
            and (series_id is None or r["series_id"] == series_id)
            and r["first_date"] <= str(end)[:10]
            and r["last_date"] >= str(start)[:10]
        ]

    def read(self, table, security, start, end, *, series_id=None, max_rows=100000):
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
            for item in items:
                path = str(self.verify(item))
                rows = c.execute(
                    "SELECT * FROM read_parquet(?) WHERE "
                    + column
                    + " BETWEEN ? AND ? ORDER BY "
                    + column
                    + " LIMIT ?",
                    [path, start, end, max_rows + 1],
                ).fetchall()
                names = [r[0] for r in c.description]
                result.extend(dict(zip(names, row)) for row in rows)
                require(len(result) <= max_rows, "MARKET_QUERY_BOUND", "Narrow the requested range")
                require(
                    self._verified[(self.sha256, item["sha256"], path)] == fingerprint(path),
                    "MARKET_OBJECT_INTEGRITY",
                    "Object changed during read",
                )
            return sorted(result, key=lambda r: (r[column], r["series_id"]))
        finally:
            c.close()

    def sql(self, *, tables=("bars_1d", "bars_1m"), securities=None):
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
            items = [
                f
                for f in self.files
                if f["table"] == table and (ids is None or f["instrument_id"] in ids)
            ]
            require(bool(items), "MARKET_EMPTY_SCOPE", "No files in the requested table/scope")
            if table == "bars_1d" and len({f["series_id"] for f in items}) == 1:
                paths = ",".join(sql_literal(self.verify(f)) for f in items)
                c.execute(
                    "CREATE VIEW market."
                    + table
                    + " AS SELECT * FROM read_parquet(["
                    + paths
                    + "], union_by_name=true)"
                )
                continue
            # Mixed physical series use lossless tagged numeric values. Consumers
            # choose a series before numeric arithmetic and extract its declared tag.
            parts = []
            for series_id in sorted({f["series_id"] for f in items}):
                selected = [f for f in items if f["series_id"] == series_id]
                paths = ",".join(sql_literal(self.verify(f)) for f in selected)
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
                parts.append("SELECT " + ",".join(columns) + " FROM " + relation)
            c.execute("CREATE VIEW market." + table + " AS " + " UNION ALL ".join(parts))
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
                if original not in present:
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
