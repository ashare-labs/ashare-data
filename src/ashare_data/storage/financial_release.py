"""Prepare and compose financial tables with the existing market release protocol.

Preparation writes only its explicit candidate directory. Composition is a
separate coordinator operation, guarded by the common release writer and parent
hash. Original source files are referenced and verified, never copied.
"""

from collections import Counter, defaultdict
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from decimal import Decimal

from ashare_data.core.model import DataError, require
from ashare_data.financials import tables as t
from ashare_data.financials import expansion as e
from ashare_data.storage.market_release import (
    MarketRelease, ReleaseWriter, atomic_write, canonical, connection, sha_file, sql_literal,
    fingerprint,
)

CANDIDATE = "ad.financial-table-candidate.v1"
CONTRACT_NAME = "financial_reference_tables"
TERMINAL = {"complete", "partial_source", "empty_unknown", "parse_failed", "failed_retryable", "deferred"}
# Frozen v1 candidates produced by reviewed commits b4abfe2 and 8e01efb.
# Additive importer entrypoints change the implementation hash, not these table
# schemas. Composition still verifies every object, registry and relationship.
COMPATIBLE_PREPARED_MAPPINGS = frozenset({
    "c4592ca38dccd80a180992debd6eb6fa74a9b0e0e9cad7679a4daf2763ae989f",
    "1e3b970124711dd3e343969f71f78f908ddfb349163964cb249c3a825da7a098",
})
REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS release_issuers(
 issuer_id TEXT PRIMARY KEY,source_namespace TEXT NOT NULL,source_issuer_key TEXT NOT NULL,
 identity_status TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS release_issuer_instruments(
 relation_id TEXT PRIMARY KEY,issuer_id TEXT NOT NULL,instrument_id INTEGER NOT NULL,
 relation_role TEXT NOT NULL,valid_from TEXT,valid_to TEXT,identity_status TEXT NOT NULL,
 evidence_asset_id TEXT,body TEXT NOT NULL);
"""
DELTA_SQL = """
CREATE TABLE IF NOT EXISTS source_assets(asset_id TEXT PRIMARY KEY,body TEXT);
CREATE TABLE IF NOT EXISTS data_batches(batch_id INTEGER PRIMARY KEY,batch_key TEXT NOT NULL,
 release_id INTEGER NOT NULL,parser_sha256 TEXT NOT NULL,mapping_sha256 TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch_inputs(batch_id INTEGER NOT NULL,asset_id TEXT NOT NULL,
 PRIMARY KEY(batch_id,asset_id));
CREATE TABLE IF NOT EXISTS source_aliases(source_version TEXT,source_identifier TEXT,instrument_id INTEGER,
 interpretation TEXT,PRIMARY KEY(source_version,source_identifier));
CREATE TABLE IF NOT EXISTS release_contracts(name TEXT PRIMARY KEY,body TEXT NOT NULL);
""" + REGISTRY_SQL


def mapper_sha256():
    return hashlib.sha256(canonical({
        "schema": t.VERSION, "tables": t.TABLES, "mapping": sha_file(Path(t.__file__)),
        "importer": sha_file(Path(__file__)),
        "official_mapping": sha_file(Path(t.__file__).with_name("table_official.py")),
        "share_mapping": sha_file(Path(t.__file__).with_name("table_shares.py")),
    })).hexdigest()


def immutable_json(path, value):
    body = canonical(value)
    if path.exists():
        require(path.read_bytes() == body, "FIN_FROZEN_INPUT", "Frozen candidate input changed")
    else:
        atomic_write(path, body)


def checked_path(root, relative):
    p = (Path(root) / relative).resolve()
    require(p.is_relative_to(Path(root).resolve()), "FIN_SOURCE_PATH", "Source escaped batch root")
    return p


class NumberLexeme(str):
    """Original JSON numeric token; its exponent and trailing zeros are retained."""


def bind_original_fields(row, raw):
    """Compare normalized scalar values to their exact verified response fields."""
    literals = {}
    if row["schema"] == "ad.financial-source-report.v1" and row["source"] == "sina":
        period = row["period_end"].replace("-", "")
        original = raw["result"]["data"]["report_list"][period]
        for item in row["fields"]:
            native = original["data"][item["ordinal"]]
            require(native.get("item_field") == item["source_field"]
                    and native.get("item_title") == item.get("source_title"),
                    "FIN_FIELD_RECONCILIATION", "Source field identity or ordinal differs")
            value, state = e.numeric(native.get("item_value"))
            require((value, state) == (item["value"], item["state"]),
                    "FIN_FIELD_RECONCILIATION", "Source/normalized numeric value differs")
            raw_value = native.get("item_value")
            literals[str(item["ordinal"])] = dict(
                value=raw_value, kind="absent" if "item_value" not in native else
                "json_number" if isinstance(raw_value, NumberLexeme) else t.raw_type(raw_value))
    elif row["schema"] == "ad.tushare-financial-version.v1":
        data = raw["data"]
        original = dict(zip(data["fields"], data["items"][row["source_ordinal"]], strict=True))
        for field, item in row["fields"].items():
            raw_value = original.get(field)
            expected = None if raw_value in (None, "", "--") else format(Decimal(str(raw_value)), "f")
            require(expected == item["value"], "FIN_FIELD_RECONCILIATION", "Tushare source/normalized field differs")
            literals[field] = dict(value=raw_value, kind="absent" if field not in original else
                                   "json_number" if isinstance(raw_value, NumberLexeme) else t.raw_type(raw_value))
    row["_field_literals"] = literals


@contextmanager
def closed_batch(root):
    """This is an import consistency lock, not a collector health probe."""
    lock_path = root / "writer.lock"
    require(lock_path.exists(), "FIN_BATCH_OPEN", "A cooperative source writer lock is required")
    with lock_path.open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataError("FIN_BATCH_OPEN", "Source partition is still being written") from exc
        yield


def verify_object(root, item):
    p = checked_path(root, item["path"])
    require(p.stat().st_size == item["bytes"] and sha_file(p) == item["sha256"],
            "FIN_OBJECT", "Candidate object checksum mismatch")
    return p


def table_views(c, files, verify):
    """All declared tables exist even when empty; paths come only from a manifest."""
    for schema in {"financial", "meta", "reference", "corporate_action", "quality"}:
        c.execute("CREATE SCHEMA IF NOT EXISTS " + schema)
    for table, fields in t.TABLES.items():
        selected = [f for f in files if f.get("table") == table]
        if selected:
            paths = ",".join(sql_literal(verify(f)) for f in selected)
            # Identical recaptures retain capture links but do not duplicate facts.
            query = "SELECT DISTINCT * FROM read_parquet([" + paths + "],union_by_name=false)"
        else:
            query = "SELECT " + ",".join("CAST(NULL AS " + typ + ") AS " + col
                                        for col, typ in fields.items()) + " WHERE false"
        c.execute("CREATE OR REPLACE VIEW " + table + " AS " + query)
    c.execute("""CREATE OR REPLACE VIEW financial.contexts AS
      SELECT DISTINCT report_version_id,extraction_version_id,context_id,context_version,issuer_id,
       statement,period_start,period_end,basis,scope,currency,column_role,dimensions_json,
       canonical_unit,raw_context_json FROM financial.observations""")
    c.execute("""CREATE OR REPLACE VIEW financial.current_source_values AS
      SELECT * FROM financial.observations WHERE quality_state='usable_current_source'
       AND canonical_value_decimal_text IS NOT NULL""")


def metadata_views(c, db):
    """Use the common registries; additions contain issuer nodes and dated relations."""
    import pandas as pd
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for source, target in (
        ("release_issuers", "reference.issuers"),
        ("release_issuer_instruments", "reference.issuer_instruments"),
    ):
        require(source in names, "FIN_REGISTRY", "Financial registry is absent")
        cursor = db.execute("SELECT * FROM " + source)
        frame = pd.DataFrame(cursor.fetchall(), columns=[col[0] for col in cursor.description])
        c.register("financial_registry_rows", frame)
        c.execute("CREATE OR REPLACE TABLE " + target + " AS SELECT * FROM financial_registry_rows")
        c.unregister("financial_registry_rows")


def validate_tables(c, registry):
    counts = {}
    for table, key in t.KEYS.items():
        counts[table] = c.execute("SELECT count(*) FROM " + table).fetchone()[0]
        bad = c.execute("SELECT count(*) FROM (SELECT " + key + " FROM " + table +
                        " GROUP BY " + key + " HAVING count(*)>1 OR " + key + " IS NULL)").fetchone()[0]
        require(bad == 0, "FIN_ID_CONFLICT", "Conflicting duplicate identity in " + table)
    require(c.execute("""SELECT count(*) FROM financial.observations o
      LEFT JOIN financial.extractions e USING(extraction_version_id)
      WHERE e.extraction_version_id IS NULL OR o.report_version_id<>e.report_version_id""").fetchone()[0] == 0,
            "FIN_FOREIGN_KEY", "Observation/extraction relationship is invalid")
    require(c.execute("""SELECT count(*) FROM financial.extractions e
      LEFT JOIN financial.report_versions r USING(report_version_id)
      WHERE r.report_version_id IS NULL""").fetchone()[0] == 0,
            "FIN_FOREIGN_KEY", "Extraction has no report")
    require(c.execute("""SELECT count(*) FROM financial.observations o
      JOIN financial.report_versions r USING(report_version_id) WHERE o.issuer_id<>r.issuer_id""").fetchone()[0] == 0,
            "FIN_FOREIGN_KEY", "Observation issuer differs from report issuer")
    require(c.execute("""SELECT count(*) FROM (
      SELECT report_version_id,extraction_version_id,context_id FROM financial.contexts
      GROUP BY ALL HAVING count(*)>1)""").fetchone()[0] == 0,
            "FIN_CONTEXT", "One context identity has different attributes")
    parts = [f"SELECT '{kind}' AS kind,{key} AS id FROM {table}"
             for kind, (table, key) in t.ENDPOINTS.items()]
    c.execute("CREATE OR REPLACE TEMP VIEW financial_endpoints AS " + " UNION ALL ".join(parts))
    bad = c.execute("""SELECT count(*) FROM financial.links l
      LEFT JOIN financial_endpoints a ON a.kind=l.from_type AND a.id=l.from_id
      LEFT JOIN financial_endpoints b ON b.kind=l.to_type AND b.id=l.to_id
      WHERE a.id IS NULL OR b.id IS NULL""").fetchone()[0]
    require(bad == 0, "FIN_LINK", "Relationship endpoint is absent")
    allowed = {"captured_in", "extracts", "date_claim", "disclosed_in", "replaces", "unresolved_revision", "proof_input"}
    actual = {r[0] for r in c.execute("SELECT DISTINCT relation FROM financial.links").fetchall()}
    require(actual <= allowed, "FIN_LINK", "Unknown relationship semantics")
    links = c.execute("""SELECT from_id,to_id,issuer_id,period_end,field,scope_state
       FROM financial.links WHERE relation='replaces'""").fetchall()
    graph = defaultdict(set)
    for new, old, issuer, period, field, scope in links:
        require(scope == "reviewed_exact" and issuer and period and field,
                "FIN_REVISION", "Replacement needs exact reviewed issuer/period/field scope")
        rows = c.execute("""SELECT observation_id,issuer_id,period_end,canonical_field,scope,basis,
         canonical_unit,currency,column_role FROM financial.observations
         WHERE observation_id IN (?,?)""", [new, old]).fetchall()
        require(len(rows) == 2 and rows[0][1:] == rows[1][1:]
                and rows[0][1:4] == (issuer, period, field),
                "FIN_REVISION", "Replacement scope differs")
        graph[new].add(old)
    for origin in graph:
        seen, pending = set(), list(graph[origin])
        while pending:
            node = pending.pop()
            require(node != origin, "FIN_REVISION", "Cyclic replacement")
            if node not in seen:
                seen.add(node)
                pending.extend(graph.get(node, ()))
    issuers = {r[0] for r in registry.execute("SELECT issuer_id FROM release_issuers")}
    referenced = {r[0] for r in c.execute("SELECT DISTINCT issuer_id FROM financial.report_versions").fetchall()}
    require(referenced <= issuers, "FIN_REGISTRY", "Report issuer is absent")
    relations = {r[0] for r in registry.execute("SELECT relation_id FROM release_issuer_instruments")}
    visible_relations = {r[0] for r in c.execute("SELECT issuer_relation_id FROM financial.visibility").fetchall()}
    require(visible_relations <= relations, "FIN_VISIBILITY", "Visibility lacks its issuer relation")
    require(c.execute("""SELECT count(*) FROM financial.visibility v
      LEFT JOIN financial.observations o USING(observation_id)
      LEFT JOIN financial.disclosure_events d USING(event_id)
      WHERE o.observation_id IS NULL OR d.event_id IS NULL OR o.issuer_id<>d.issuer_id
      OR v.available_from IS NULL OR v.policy NOT IN ('verified_official_daily','verified_exact_publication')
      OR NOT EXISTS (SELECT 1 FROM financial.links l WHERE l.from_type='report'
        AND l.from_id=o.report_version_id AND l.to_type='disclosure' AND l.to_id=d.event_id
        AND l.relation='disclosed_in')""").fetchone()[0] == 0,
            "FIN_VISIBILITY", "Visibility is not bound to the exact report disclosure")
    assets = {r[0] for r in registry.execute("SELECT asset_id FROM source_assets")}
    required_assets = {r[0] for r in c.execute("""SELECT evidence_asset_id FROM financial.visibility
      UNION SELECT evidence_asset_id FROM financial.links WHERE evidence_asset_id IS NOT NULL
      UNION SELECT raw_asset_id FROM meta.financial_captures WHERE raw_asset_id IS NOT NULL
      UNION SELECT normalized_asset_id FROM meta.financial_captures WHERE normalized_asset_id IS NOT NULL""").fetchall()}
    require(required_assets <= assets, "FIN_ASSET", "Evidence asset is not registered")
    accounting = c.execute("""SELECT sum(input_rows),sum(mapped_rows),sum(quarantined_rows)
       FROM meta.financial_coverage""").fetchone()
    input_rows, mapped, quarantine = [v or 0 for v in accounting]
    require(input_rows == mapped + quarantine, "FIN_INPUT_ACCOUNTING", "Input rows do not balance")
    return dict(table_rows=counts, input_rows=input_rows, mapped_rows=mapped, quarantined_rows=quarantine,
                duplicate_conflicts=0, broken_relationships=0, complete_history_asserted=False)


class TableBuilder:
    """Bounded task shards with resumable immutable checkpoints and exact roundtrip."""

    def __init__(self, parent, root, *, rows_per_shard=50000):
        self.parent = parent
        self.root = Path(root).resolve()
        self.mapping = mapper_sha256()
        self.rows_per_shard = rows_per_shard
        self.instruments = parent.identifiers
        self.writer = ReleaseWriter(self.root, free_reserve_bytes=8 * 1024**3)
        self.db = None

    def __enter__(self):
        self.writer.__enter__()
        try:
            config = dict(schema=CANDIDATE, mapping_sha256=self.mapping,
                          parent_manifest_sha256=self.parent.sha256,
                          parent_metadata_sha256=self.parent.manifest["metadata"]["sha256"],
                          parent_release_id=self.parent.release_id, parent_root=str(self.parent.root))
            immutable_json(self.root / "build.json", config)
            self.db = sqlite3.connect(self.root / "work/metadata-delta.sqlite3")
            self.db.executescript(DELTA_SQL)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *args):
        if self.db:
            self.db.close()
        self.writer.__exit__(*args)

    def asset(self, path, *, kind, expected=None):
        path = Path(path).resolve()
        actual = sha_file(path)
        require(expected is None or actual == expected, "FIN_SOURCE", "Source artifact changed")
        aid = t.identity("source-asset", str(path), actual, kind)
        value = dict(path=str(path), sha256=actual, bytes=path.stat().st_size, kind=kind,
                     verified_fingerprint=fingerprint(path),
                     retention="external_original_required; no copy in candidate")
        self.db.execute("INSERT OR IGNORE INTO source_assets VALUES (?,?)", (aid, t.text(value)))
        return aid

    def issuer(self, source, security, period, task):
        iid = self.instruments.get(security)
        require(iid is not None, "FIN_INSTRUMENT", "Source security absent from frozen registry")
        # A source entity node is deliberately distinct from a legal-issuer assertion.
        issuer_key = task.get("source_issuer_key", security)
        issuer = t.identity("issuer", source, issuer_key)
        body = dict(source_namespace=source, source_issuer_key=issuer_key,
                    identity_status="source_entity_unverified_legal_identity")
        self.db.execute("INSERT OR IGNORE INTO release_issuers VALUES (?,?,?,?,?)",
                        (issuer, source, issuer_key, body["identity_status"], t.text(body)))
        role = task.get("relation_role", "source_reported_under")
        relation = t.identity("issuer-instrument", issuer, iid, task.get("ipo"), task.get("out"), role,
                              task.get("evidence_asset_id"))
        self.db.execute("INSERT OR IGNORE INTO release_issuer_instruments VALUES (?,?,?,?,?,?,?,?,?)",
                        (relation, issuer, iid, role, task.get("ipo"), task.get("out"),
                         task.get("relation_status", "request_bound_not_legal_issuer_history"), task.get("evidence_asset_id"),
                         t.text({"security": security, "carry_across_codes": False})))
        return issuer

    def flush(self, buffer, batch_key, ordinal):
        require(shutil.disk_usage(self.root).free >= self.writer.free_reserve_bytes,
                "FIN_CAPACITY", "Preserve 8 GiB free space; completed import shards remain resumable")
        files = []
        c = connection(memory="768MB", threads=1)
        try:
            for table, rows in sorted(buffer.items()):
                if not rows:
                    continue
                fields = t.TABLES[table]
                c.execute("CREATE TABLE target (" + ",".join(k + " " + v for k, v in fields.items()) + ")")
                # JSON transport preserves decimal strings; no pandas float inference.
                with tempfile.NamedTemporaryFile(mode="w", dir=self.root / "work", suffix=".jsonl",
                                                 encoding="utf-8") as stream:
                    for row in rows:
                        stream.write(t.text(row) + "\n")
                    stream.flush()
                    types = "{" + ",".join(sql_literal(k) + ":" + sql_literal(v) for k, v in fields.items()) + "}"
                    c.execute("INSERT INTO target SELECT * FROM read_json(?,format='newline_delimited',columns="
                              + types + ")", [stream.name])
                key = t.KEYS[table]
                query = "SELECT DISTINCT * FROM target ORDER BY " + key
                item = self.writer.parquet(c, query, [], "financial-" + str(ordinal) + "-" + table.replace(".", "-"))
                count = c.execute("SELECT count(*) FROM (" + query + ")").fetchone()[0]
                files.append(dict(item, kind="financial_table", table=table, rows=count,
                                  partition_key=t.identity("partition", batch_key, ordinal, table),
                                  batch_key=batch_key, schema_version=t.VERSION))
                c.execute("DROP TABLE target")
        finally:
            c.close()
        return files

    def import_batch(self, root, *, stopped=False):
        root = Path(root).resolve()
        with closed_batch(root):
            _, manifest, tasks = e.load_batch(root)
            stopped_scope = None
            if stopped:
                stop, exited = root / "STOP", root / "worker-exit.json"
                require(stop.is_file() and exited.is_file(), "FIN_BATCH_OPEN",
                        "Stopped-source import requires retained stop and worker-exit evidence")
                require(Path(json.loads(stop.read_text())["legacy_batch"]).resolve() == root,
                        "FIN_SOURCE", "Stop evidence targets another source batch")
                stopped_scope = dict(stop_sha256=sha_file(stop), exit_sha256=sha_file(exited),
                                     receipts=[(task["id"], sha_file(path) if path.exists() else None)
                                               for task in tasks
                                               for path in [root / "receipts" / (task["id"] + ".json")]])
                scope_key = t.identity("stopped-source", str(root)).split(":")[1]
                immutable_json(self.root / ("source-cut-" + scope_key + ".json"), stopped_scope)
            parts = (str(root), sha_file(root / "manifest.json"), self.mapping)
            batch_key = t.identity("financial-stopped-batch", *parts, stopped_scope) if stopped else t.identity(
                "financial-batch", *parts)
            checkpoint = self.root / ("batch-" + batch_key.split(":")[1] + ".json")
            if checkpoint.exists():
                old = json.loads(checkpoint.read_text())
                for file in old["files"]:
                    verify_object(self.root, file)
                return old
            # Validate closure before mapping any rows; explicit failures remain coverage.
            for task in tasks:
                path = root / "receipts" / (task["id"] + ".json")
                require(path.exists() or stopped, "FIN_BATCH_OPEN", "Unvisited task; close collection before import")
                if not path.exists():
                    continue  # No response exists; retain a coverage gap, not a capture or fact.
                state = json.loads(path.read_bytes()).get("state")
                require(state in TERMINAL, "FIN_BATCH_OPEN", "Pending or running source task")
            batch_id = self.db.execute("SELECT batch_id FROM data_batches WHERE batch_key=?", (batch_key,)).fetchone()
            if batch_id:
                batch_id = batch_id[0]
            else:
                batch_id = self.db.execute("SELECT coalesce(max(batch_id),0)+1 FROM data_batches").fetchone()[0]
                self.db.execute("INSERT INTO data_batches VALUES (?,?,?,?,?,?)",
                                (batch_id, batch_key, 0, "per_capture_normalizer", self.mapping,
                                 t.text({"source_root": str(root), "manifest": manifest,
                                         "collection_closed_with_explicit_gaps": True,
                                         **({"stopped_source_scope": stopped_scope} if stopped else {})})))
            names = ("manifest.json", "tasks.json", "universe.json") + (("STOP", "worker-exit.json") if stopped else ())
            for name in names:
                aid = self.asset(root / name, kind="batch_" + name)
                self.db.execute("INSERT OR IGNORE INTO batch_inputs VALUES (?,?)", (batch_id, aid))
            resume_path = self.root / ("resume-" + batch_key.split(":")[1] + ".json")
            resume = json.loads(resume_path.read_text()) if resume_path.exists() else {}
            buffer = defaultdict(list)
            files, states = resume.get("files", []), Counter(resume.get("task_states", {}))
            seen, ordinal = resume.get("receipts", []), resume.get("next_shard", 0)
            completed = resume.get("completed_tasks", 0)
            require(len(seen) == completed and completed <= len(tasks),
                    "FIN_CHECKPOINT", "Invalid import checkpoint")
            for item in files:
                verify_object(self.root, item)
            for task, (task_id, checksum) in zip(tasks[:completed], seen, strict=True):
                receipt_path = root / "receipts" / (task_id + ".json")
                require(task["id"] == task_id
                        and (sha_file(receipt_path) if receipt_path.exists() else None) == checksum,
                        "FIN_CHECKPOINT", "Already imported source receipt changed")
            for task_index, task in enumerate(tasks[completed:], start=completed):
                receipt_path = root / "receipts" / (task["id"] + ".json")
                receipt_bytes = receipt_path.read_bytes() if receipt_path.exists() else None
                receipt = json.loads(receipt_bytes) if receipt_bytes is not None else {
                    "state": "unvisited_stopped", "coverage": {"source_response_observed": False,
                    "collection_state": "stopped", "absence_proved": False},
                    "parse_error": "not_requested_before_source_stop"}
                if receipt_bytes is not None:
                    e.verify_receipt(root, receipt)
                checksum = hashlib.sha256(receipt_bytes).hexdigest() if receipt_bytes is not None else None
                seen.append([task["id"], checksum])
                states[receipt["state"]] += 1
                assets = {}
                for name in ("raw", "normalized"):
                    if receipt.get(name):
                        item = receipt[name]
                        assets[name] = self.asset(checked_path(root, item["path"]), kind=name, expected=item["sha256"])
                if receipt_bytes is not None:
                    assets["receipt"] = self.asset(receipt_path, kind="receipt", expected=checksum)
                for aid in assets.values():
                    self.db.execute("INSERT OR IGNORE INTO batch_inputs VALUES (?,?)", (batch_id, aid))
                response_sha, raw_json = None, None
                if receipt.get("raw"):
                    body = gzip.decompress(checked_path(root, receipt["raw"]["path"]).read_bytes())
                    response_sha = hashlib.sha256(body).hexdigest()
                    attempts = [x for x in receipt.get("attempts", []) if x.get("response_sha256")]
                    if attempts:
                        require(response_sha == attempts[-1]["response_sha256"],
                                "FIN_SOURCE", "Raw response/receipt hash mismatch")
                    if task["kind"] in {"sina_income", "sina_balance", "sina_cashflow", "tushare_income",
                                        "tushare_balancesheet", "tushare_cashflow", "tushare_fina_indicator"}:
                        raw_json = json.loads(body, parse_float=NumberLexeme, parse_int=NumberLexeme)
                capture = t.identity("capture", batch_key, task["id"], checksum)
                parser = hashlib.sha256(canonical({k: v for k, v in receipt.items()
                         if (k.endswith("_sha256") and "normalizer" in k) or k == "corporate_parser_sha256"})).hexdigest()
                projection = t.Projection(instruments=self.instruments, register_issuer=self.issuer,
                                          mapping_sha256=self.mapping)
                count = mapped = quarantined = 0
                if receipt.get("normalized") and receipt["state"] in {"complete", "partial_source", "empty_unknown"}:
                    with gzip.open(checked_path(root, receipt["normalized"]["path"]), "rt") as stream:
                        for line in stream:
                            if not line.strip():
                                continue
                            row = json.loads(line)
                            require(row.get("response_sha256") == response_sha,
                                    "FIN_SOURCE", "Normalized row is bound to another response")
                            require(row.get("observed_at") == receipt.get("observed_at"),
                                    "FIN_SOURCE", "Normalized capture clock differs from receipt")
                            security = row.get("security")
                            if security in self.instruments:
                                self.db.execute("INSERT OR IGNORE INTO source_aliases VALUES (?,?,?,?)",
                                                (manifest["universe_sha256"], security, self.instruments[security],
                                                 "source request identity; not proof of legal issuer continuity"))
                            count += 1
                            before = {name: len(value) for name, value in projection.rows.items()}
                            try:
                                if raw_json is not None:
                                    bind_original_fields(row, raw_json)
                                result = projection.project(row, task, capture, parser)
                            except (DataError, ValueError, KeyError, TypeError) as exc:
                                for name, values in projection.rows.items():
                                    del values[before.get(name, 0):]
                                projection.quarantine(row, capture, type(exc).__name__ + ": " + str(exc))
                                result = False
                            if result is False:
                                quarantined += 1
                            else:
                                mapped += 1
                    require(count == receipt["row_count"], "FIN_SOURCE", "Normalized row count differs")
                if receipt_bytes is not None:
                    projection.add("meta.financial_captures", capture_id=capture, batch_key=batch_key,
                                   task_id=task["id"], source=task["kind"], raw_asset_id=assets.get("raw"),
                                   normalized_asset_id=assets.get("normalized"), response_sha256=response_sha,
                                   receipt_sha256=checksum, observed_at=receipt.get("observed_at"),
                                   source_state=receipt["state"], source_rows=count)
                projection.add("meta.financial_coverage", coverage_id=t.identity("coverage", batch_key, task["id"], checksum),
                               batch_key=batch_key, task_id=task["id"], source=task["kind"],
                               security=e.security(task["symbol"]) if task.get("symbol") else None,
                               source_state=receipt["state"], input_rows=count, mapped_rows=mapped,
                               quarantined_rows=quarantined, coverage_json=t.text(receipt.get("coverage", {})),
                               error=receipt.get("parse_error"))
                for name, rows in projection.rows.items():
                    buffer[name].extend(rows)
                if sum(len(rows) for rows in buffer.values()) >= self.rows_per_shard:
                    files.extend(self.flush(buffer, batch_key, ordinal))
                    buffer.clear()
                    ordinal += 1
                    self.db.commit()
                    atomic_write(resume_path, canonical(dict(
                        files=files, task_states=dict(states), receipts=seen,
                        completed_tasks=task_index + 1, next_shard=ordinal)))
                    atomic_write(self.root / "BUILD-PROGRESS.json", canonical(dict(
                        phase="mapping_closed_source", batch_key=batch_key, source_root=str(root),
                        completed_tasks=task_index + 1, planned_tasks=len(tasks),
                        written_shards=len(files), table_rows=dict(Counter({
                            name: sum(f["rows"] for f in files if f["table"] == name)
                            for name in t.TABLES})), published=False)))
            if buffer:
                files.extend(self.flush(buffer, batch_key, ordinal))
            result = dict(batch_key=batch_key, source_root=str(root), files=files,
                          task_states=dict(states), tasks=len(tasks), receipt_inventory_sha256=t.identity("receipts", seen))
            self.db.commit()
            immutable_json(checkpoint, result)
            return result

    def finish(self, batches):
        files = [item for batch in batches for item in batch["files"]]
        self.db.commit()
        c = connection(self.root / "work/spill", memory="1GB", threads=2)
        try:
            table_views(c, files, lambda item: verify_object(self.root, item))
            validation = validate_tables(c, self.db)
        finally:
            c.close()
        with tempfile.TemporaryDirectory(dir=self.root / "work") as temp:
            target = Path(temp) / "frozen-metadata.sqlite3"
            db = sqlite3.connect(target)
            self.db.backup(db)
            db.close()
            metadata = self.writer.object(target, "sqlite3")
        config = json.loads((self.root / "build.json").read_text())
        value = dict(config, files=files, metadata_delta=metadata,
                     input_specification=getattr(self, "input_specification", {}),
                     batches=[{k: v for k, v in x.items() if k != "files"} for x in batches],
                     validation=validation, original_files_copied=False,
                     published=False, full_history_complete=False,
                     limitations=["provider units/issuer identity may remain unverified",
                                  "capture clocks do not prove historical publication"])
        value["candidate_id"] = t.identity("candidate", value)
        immutable_json(self.root / "candidate.json", value)
        atomic_write(self.root / "BUILD-PROGRESS.json", canonical(dict(
            phase="candidate_validated", candidate_id=value["candidate_id"],
            validation=validation, published=False)))
        return value


def prepare(parent, root, batches, *, daily_reviews=(), official_batches=(), share_resolutions=(),
            official_repairs=(), stopped_batches=()):
    specification = {
        "batches": [str(Path(p).resolve()) for p in batches],
        "daily_reviews": [str(Path(p).resolve()) for p in daily_reviews],
        "official_batches": [str(Path(p).resolve()) for p in official_batches],
        "share_resolutions": [str(Path(p).resolve()) for p in share_resolutions]}
    if official_repairs:
        specification["official_repairs"] = [str(Path(p).resolve()) for p in official_repairs]
    if stopped_batches:
        specification["stopped_batches"] = [str(Path(p).resolve()) for p in stopped_batches]
    candidate = Path(root) / "candidate.json"
    if candidate.exists():
        existing = json.loads(candidate.read_text())
        require(existing["mapping_sha256"] == mapper_sha256()
                and existing["parent_manifest_sha256"] == parent.sha256
                and existing.get("input_specification") == specification,
                "FIN_FROZEN_INPUT", "Use a new candidate directory for different inputs or mapping")
        verify_object(root, existing["metadata_delta"])
        for item in existing["files"]:
            verify_object(root, item)
        return existing
    with TableBuilder(parent, root) as builder:
        done = [builder.import_batch(p) for p in batches]
        done.extend(builder.import_batch(p, stopped=True) for p in stopped_batches)
        if daily_reviews or official_batches:
            from ashare_data.financials.table_official import import_daily_review, import_archive
            done.extend(import_archive(builder, p) for p in official_batches)
            done.extend(import_daily_review(builder, p) for p in daily_reviews)
        if share_resolutions:
            from ashare_data.financials.table_shares import import_resolutions
            done.extend(import_resolutions(builder, p) for p in share_resolutions)
        if official_repairs:
            from ashare_data.financials.table_official import import_recovered_archive
            done.extend(import_recovered_archive(builder, p) for p in official_repairs)
        builder.input_specification = specification
        return builder.finish(done)


def insert_same(db, table, values, key_columns):
    columns = [row[1] for row in db.execute("PRAGMA table_info(" + table + ")")]
    key = [values[columns.index(k)] for k in key_columns]
    old = db.execute("SELECT * FROM " + table + " WHERE " +
                     " AND ".join(k + "=?" for k in key_columns), key).fetchone()
    require(old is None or tuple(old) == tuple(values), "FIN_REGISTRY_CONFLICT", "Registry row changed: " + table)
    if old is None:
        db.execute("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in values) + ")", values)


def compose(writer, parent, candidate_root, *, expected_parent_sha256):
    """Return a common-release manifest; caller owns the lock and publish decision.

    A changed parent is accepted only when explicitly selected by the coordinator
    and every referenced instrument identity still matches the prepared registry.
    """
    require(writer.lock is not None and writer.root == parent.root, "FIN_WRITER", "Use the common locked writer")
    require(parent.sha256 == expected_parent_sha256, "FIN_PARENT", "Explicit parent hash differs")
    root = Path(candidate_root).resolve()
    candidate = json.loads((root / "candidate.json").read_text())
    require(candidate["schema"] == CANDIDATE
            and candidate["mapping_sha256"] in COMPATIBLE_PREPARED_MAPPINGS | {mapper_sha256()},
            "FIN_CANDIDATE", "Unexpected candidate implementation")
    current = MarketRelease(writer.root)
    require(current.sha256 == expected_parent_sha256, "FIN_PARENT", "Market publisher advanced; recompose")
    prior = parent.manifest.get(CONTRACT_NAME, {})
    if candidate["candidate_id"] in prior.get("candidate_ids", []):
        return None
    source = sqlite3.connect(verify_object(root, candidate["metadata_delta"]))
    source.row_factory = sqlite3.Row
    try:
        for row in source.execute("SELECT body FROM source_assets"):
            asset = json.loads(row["body"])
            path = Path(asset["path"])
            require(path.exists(), "FIN_SOURCE", "Required external source artifact is missing")
            if fingerprint(path) != asset["verified_fingerprint"]:
                require(path.stat().st_size == asset["bytes"] and sha_file(path) == asset["sha256"],
                        "FIN_SOURCE", "Required external source artifact changed after preparation")
        for row in source.execute("SELECT instrument_id,body FROM release_issuer_instruments"):
            security = json.loads(row["body"])["security"]
            require(parent.identifiers.get(security) == row["instrument_id"],
                    "FIN_REGISTRY_CHANGED", "Prepared instrument identity changed")
        for row in source.execute("SELECT source_identifier,instrument_id FROM source_aliases"):
            require(parent.identifiers.get(row["source_identifier"]) == row["instrument_id"],
                    "FIN_REGISTRY_CHANGED", "Prepared reference instrument identity changed")
        path = writer.root / "work/financial-composed-metadata.sqlite3"
        require(not path.exists(), "FIN_OUTPUT", "Inspect retained unfinished composition before retry")
        with parent.metadata() as original:
            db = sqlite3.connect(path)
            original.backup(db)
        try:
            db.executescript(DELTA_SQL)
            for table, keys in (
                ("source_assets", ["asset_id"]), ("release_issuers", ["issuer_id"]),
                ("release_issuer_instruments", ["relation_id"]),
                ("source_aliases", ["source_version", "source_identifier"]),
            ):
                for row in source.execute("SELECT * FROM " + table):
                    insert_same(db, table, tuple(row), keys)
            id_map = {}
            for row in source.execute("SELECT * FROM data_batches"):
                old = db.execute("SELECT * FROM data_batches WHERE batch_key=?", (row["batch_key"],)).fetchall()
                require(len(old) <= 1, "FIN_REGISTRY_CONFLICT", "Ambiguous existing batch key")
                if old:
                    require(tuple(old[0])[3:] == tuple(row)[3:],
                            "FIN_REGISTRY_CONFLICT", "Existing batch semantics changed")
                    id_map[row["batch_id"]] = old[0][0]
                else:
                    new = db.execute("SELECT coalesce(max(batch_id),0)+1 FROM data_batches").fetchone()[0]
                    db.execute("INSERT INTO data_batches VALUES (?,?,?,?,?,?)",
                               (new, row["batch_key"], parent.release_id + 1,
                                row["parser_sha256"], row["mapping_sha256"], row["body"]))
                    id_map[row["batch_id"]] = new
            for row in source.execute("SELECT * FROM batch_inputs"):
                db.execute("INSERT OR IGNORE INTO batch_inputs VALUES (?,?)",
                           (id_map[row["batch_id"]], row["asset_id"]))
            files = list(parent.manifest.get("evidence", []))
            existing = {(x.get("kind"), x.get("sha256"), x.get("table")) for x in files}
            for item in candidate["files"]:
                src = verify_object(root, item)
                key = (item["kind"], item["sha256"], item["table"])
                if key in existing:
                    continue
                dst = writer.root / item["path"]
                if not dst.exists():
                    # New financial objects only. Market objects are never copied.
                    try:
                        os.link(src, dst)
                    except OSError:
                        with tempfile.NamedTemporaryFile(dir=writer.root / "work") as temporary:
                            shutil.copyfile(src, temporary.name)
                            require(sha_file(temporary.name) == item["sha256"],
                                    "FIN_OBJECT", "Financial copy differs")
                            with open(temporary.name, "rb") as stream:
                                os.fsync(stream.fileno())
                            # Preserve the temporary handle's cleanup, publish atomically.
                            os.link(temporary.name, dst)
                require(sha_file(dst) == item["sha256"], "FIN_OBJECT", "Composed object changed")
                files.append(item)
                existing.add(key)
            contract = dict(schema=t.VERSION, candidate_ids=prior.get("candidate_ids", []) + [candidate["candidate_id"]],
                            table_names=list(t.TABLES), mapping_sha256=candidate["mapping_sha256"],
                            full_history_complete=False, historical_pit_certified=False)
            contract["table_set_sha256"] = table_set_hash(files)
            db.execute("INSERT OR REPLACE INTO release_contracts VALUES (?,?)",
                       (CONTRACT_NAME, t.text(contract)))
            db.commit()
            c = connection(memory="1GB", threads=2)
            try:
                table_views(c, [f for f in files if f.get("kind") == "financial_table"],
                            lambda x: verify_object(writer.root, x))
                validation = validate_tables(c, db)
                validation["table_set_sha256"] = contract["table_set_sha256"]
            finally:
                c.close()
        finally:
            db.close()
        metadata = writer.object(path, "sqlite3")
        value = dict(parent.manifest, metadata=metadata, evidence=files, **{CONTRACT_NAME: contract})
        value["financial_table_validation"] = validation
        value["change_set"] = {"kind": "financial_reference_tables", "candidate_id": candidate["candidate_id"]}
        return value
    finally:
        source.close()


def table_set_hash(evidence):
    items = [{key: f[key] for key in ("table", "sha256", "bytes", "rows", "partition_key")}
             for f in evidence if f.get("kind") == "financial_table"]
    return t.identity("table-set", sorted(items, key=lambda x: (x["table"], x["partition_key"])))


def verify_manifest_contract(root, manifest):
    """Publication guard also protects financial tables during later market updates."""
    contract = manifest.get(CONTRACT_NAME)
    files = [f for f in manifest.get("evidence", []) if f.get("kind") == "financial_table"]
    require(bool(contract) or not files, "FIN_CONTRACT", "Financial objects need their release contract")
    if not contract:
        return
    require(contract["schema"] == t.VERSION and set(contract["table_names"]) == set(t.TABLES),
            "FIN_CONTRACT", "Unknown financial release schema")
    actual = table_set_hash(files)
    require(actual == contract["table_set_sha256"]
            and manifest["financial_table_validation"]["table_set_sha256"] == actual,
            "FIN_CONTRACT", "Financial table set was changed without reconciliation")
    seen = set()
    for item in files:
        key = (item["table"], item["partition_key"])
        require(item["table"] in t.TABLES and key not in seen and item["key_value_mismatches"] == 0,
                "FIN_CONTRACT", "Unknown, duplicate or unreconciled financial partition")
        seen.add(key)
        verify_object(root, item)
    metadata = verify_object(root, manifest["metadata"])
    with sqlite3.connect(metadata.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        stored = db.execute("SELECT body FROM release_contracts WHERE name=?", (CONTRACT_NAME,)).fetchone()
        require(stored and json.loads(stored[0]) == contract, "FIN_CONTRACT", "Frozen metadata contract differs")
