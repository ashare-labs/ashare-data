"""Publish explicitly selected CMES files to an isolated standard MarketRelease."""

from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd

from ashare_data.core.model import DataError, require, timestamp
from ashare_data.providers import cmes
from ashare_data.storage.local_daily import SCHEMA as LOCAL_SCHEMA
from ashare_data.storage.market_release import (
    FIELDS, MarketRelease, ReleaseWriter, atomic_write, canonical, connection, sha_file,
)

SCHEMA = "ad-cmes-candidate.v1"
COLUMNS = ["table_name", "series_id", "security", "trade_date", "label_at", *FIELDS,
           "preclose", "source_symbol", "source_asset_id", "source_member", "source_row"]
METADATA_SCHEMA = """
CREATE TABLE release_instruments(instrument_id INTEGER PRIMARY KEY,security TEXT UNIQUE,
 name TEXT,identity_status TEXT,venue_id TEXT,asset_class TEXT);
CREATE TABLE release_identifiers(security TEXT PRIMARY KEY,instrument_id INTEGER,namespace TEXT,
 valid_from TEXT,valid_to TEXT,validity_precision TEXT,source_version TEXT);
CREATE TABLE source_assets(asset_id TEXT PRIMARY KEY,body TEXT);
CREATE TABLE source_aliases(instrument_id INTEGER,source_symbol TEXT,source_version TEXT);
CREATE TABLE series_contracts(series_id INTEGER PRIMARY KEY,body TEXT);
CREATE TABLE release_contracts(name TEXT PRIMARY KEY,body TEXT);
CREATE TABLE data_batches(batch_id INTEGER PRIMARY KEY,batch_key TEXT,release_id INTEGER,
 builder_sha256 TEXT,mapping_sha256 TEXT,body TEXT);
CREATE TABLE batch_inputs(batch_id INTEGER,asset_id TEXT,PRIMARY KEY(batch_id,asset_id));
CREATE TABLE release_daily_summary(instrument_id INTEGER,source TEXT,rows INTEGER,first TEXT,last TEXT);
CREATE TABLE cmes_calendar(venue TEXT,trade_date TEXT,is_open INTEGER,previous_date TEXT,
 source_asset_id TEXT,source_member TEXT,source_row INTEGER,PRIMARY KEY(venue,trade_date));
CREATE TABLE cmes_st(security TEXT,trade_date TEXT,name TEXT,status_type TEXT,status_name TEXT,
 source_asset_id TEXT,source_member TEXT,source_row INTEGER,PRIMARY KEY(security,trade_date,status_type));
CREATE TABLE cmes_rejections(source_asset_id TEXT,source_member TEXT,source_row INTEGER,
 security TEXT,reason TEXT,native TEXT);
"""


def input_paths(paths, *, securities=None):
    """Only inspect explicitly selected files/directories, never recurse into raw downloads."""
    result = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        require(path.is_dir() or path.is_file() and path.suffix.lower() in {".zip", ".csv"},
                "CMES_INPUT", "Select an existing file or directory", {"path": str(path)})
        children = sorted(path.iterdir()) if path.is_dir() else [path]
        for child in children:
            if child.name.startswith(".") or child.suffix.lower() not in {".zip", ".csv"}:
                continue
            if path.is_dir() and securities is not None:
                try:
                    security = cmes.normalize_security(child.stem)
                except DataError:
                    security = None
                if security is not None and security not in securities:
                    continue
            result.add(child)
    return sorted(result)


def _insert_bars(db, buffer):
    if buffer:
        db.register("incoming", pd.DataFrame(buffer, columns=COLUMNS))
        try:
            db.execute("INSERT INTO staging SELECT * FROM incoming")
        finally:
            db.unregister("incoming")
        buffer.clear()


def _request_identity(spec):
    # Later revalidation is not a new first observation. All other inputs,
    # source hashes, semantics and conversion identity must still match exactly.
    return {**spec, "inputs": [
        {key: value for key, value in asset.items() if key != "observed_at"}
        for asset in spec["inputs"]
    ]}


def _reference(db, asset, *, encoding, securities):
    kind = asset["kind"]
    count = Counter(input_rows=0, adopted=0, equal_duplicate=0)
    table = "cmes_calendar" if kind == "calendar" else "cmes_st"
    for row in cmes.reference_rows(asset["path"], kind=kind, encoding=encoding, securities=securities):
        count["input_rows"] += 1
        if kind == "calendar":
            key = [row["venue"], row["trade_date"]]
            values = [*key, row["is_open"], row["previous_date"]]
            old = db.execute("SELECT venue,trade_date,is_open,previous_date FROM cmes_calendar "
                             "WHERE venue=? AND trade_date=?", key).fetchone()
        else:
            key = [row["security"], row["trade_date"], row["status_type"]]
            values = [row[k] for k in ("security", "trade_date", "name", "status_type", "status_name")]
            old = db.execute("SELECT security,trade_date,name,status_type,status_name FROM cmes_st "
                             "WHERE security=? AND trade_date=? AND status_type=?", key).fetchone()
        require(old is None or list(old) == values, "CMES_REFERENCE_CONFLICT",
                "Conflicting CMES reference key", {"key": key, "asset": asset["asset_id"]})
        if old is not None:
            count["equal_duplicate"] += 1
        else:
            values += [asset["asset_id"], row["source_member"], row["source_row"]]
            db.execute("INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in values) + ")", values)
            count["adopted"] += 1
    return dict(count)


def import_cmes(root, *, daily=(), daily_profile="daily-lots-kcny", minute=(), minute_profile=None, calendars=(), st=(),
                securities=None, encoding="utf-8-sig", free_reserve_bytes=1024**3,
                budget_bytes=200 * 1024**3):
    """Build a candidate; identical replays are no-ops, changed inputs need a new root.

    No deployment is discovered or changed. Original files remain external. Only
    the supplied source is present; rejected bars retain their input and reason.
    """
    selected = sorted({cmes.normalize_security(s) for s in securities}) if securities is not None else None
    require(selected is None or selected, "CMES_SCOPE", "An explicit security scope cannot be empty")
    require(daily_profile in cmes.BAR_PROFILES and cmes.BAR_PROFILES[daily_profile]["table"] == "bars_1d",
            "CMES_PROFILE", "Daily imports require an explicit daily unit profile")
    index = cmes.BAR_PROFILES[daily_profile].get("asset_class") == "index"
    require(not index or (daily and not minute and not st), "CMES_PROFILE",
            "Index price-only imports cannot mix stock execution inputs")
    require(not minute or minute_profile in cmes.BAR_PROFILES
            and cmes.BAR_PROFILES[minute_profile]["table"] == "bars_1m",
            "CMES_PROFILE", "Minute imports require an explicit minute unit profile")
    inputs = []
    for kind, paths, profile in (("bars", daily, daily_profile),
                                 ("bars", minute, minute_profile),
                                 ("calendar", calendars, None), ("st", st, None)):
        for path in input_paths(paths, securities=selected if kind == "bars" else None):
            inputs.append(cmes.source_asset(path, kind=kind, profile=profile))
    require(any(x["kind"] == "bars" for x in inputs), "CMES_INPUT", "Select at least one bar file")
    # One content identity cannot acquire two incompatible interpretations.
    assets = {}
    for item in inputs:
        old = assets.get(item["asset_id"])
        require(old is None or (old["kind"], old["profile"]) == (item["kind"], item["profile"]),
                "CMES_PROFILE", "The same file content has conflicting import profiles")
        assets.setdefault(item["asset_id"], item)
    inputs = sorted(assets.values(), key=lambda item: item["asset_id"])
    mapping = hashlib.sha256(canonical([sha_file(cmes.__file__), sha_file(__file__)])).hexdigest()
    spec = dict(schema=SCHEMA, source="cmes", mapping_version=cmes.MAPPING_VERSION,
                mapping_sha256=mapping, inputs=inputs, securities=selected, encoding=encoding,
                price_basis="raw", volume_unit="share", amount_unit="CNY")
    if index:
        spec.update(asset_class="index", price_unit="index_point", volume_unit=None, amount_unit=None,
                    omitted_fields=["volume", "amount"], omitted_reason="source units not asserted; price-only reference")
    root = Path(root).expanduser().resolve()
    require(all(not Path(x["path"]).is_relative_to(root) for x in inputs),
            "CMES_OUTPUT", "Candidate output must not contain an original input")
    marker = root / "CMES-CANDIDATE.json"
    if root.exists() and any(root.iterdir()):
        require(marker.is_file(),
                "CMES_OUTPUT", "Use a new isolated candidate root for changed inputs; never a live release")
    root.mkdir(parents=True, exist_ok=True)
    with ReleaseWriter(root, free_reserve_bytes=free_reserve_bytes, budget_bytes=budget_bytes) as writer:
        if marker.exists():
            frozen = json.loads(marker.read_bytes())
            require(_request_identity(frozen) == _request_identity(spec), "CMES_OUTPUT",
                    "Use a new isolated candidate root for changed inputs; never a live release")
            for asset in frozen["inputs"]:
                timestamp(asset["observed_at"])  # Require a timezone-aware frozen observation.
            spec, inputs = frozen, frozen["inputs"]
        else:
            atomic_write(marker, canonical(spec))
        import_id = hashlib.sha256(canonical(spec)).hexdigest()
        if (root / "CURRENT.json").exists():
            release = MarketRelease(root)
            require(release.manifest.get("cmes_import_id") == import_id,
                    "CMES_OUTPUT", "Candidate is owned by a different import")
            for item in release.files:
                release.verify(item)
            return dict(release.receipt(), unchanged=True, input_dispositions=release.manifest["input_dispositions"])
        writer.budget()
        meta_path = root / "work" / "cmes-metadata.sqlite3"
        # This import's unpublished scratch metadata is reproducible after interruption.
        meta_path.unlink(missing_ok=True)
        with closing(sqlite3.connect(meta_path)) as meta, closing(connection(
            root / "work" / "spill", memory="512MB", threads=1,
        )) as db:
            meta.executescript(LOCAL_SCHEMA + METADATA_SCHEMA)
            db.execute("""CREATE TABLE staging(table_name VARCHAR,series_id INTEGER,security VARCHAR,
                trade_date DATE,label_at TIMESTAMPTZ,open_price DECIMAL(38,12),high_price DECIMAL(38,12),
                low_price DECIMAL(38,12),close_price DECIMAL(38,12),volume_shares BIGINT,
                turnover_cny DECIMAL(38,12),preclose DECIMAL(38,12),source_symbol VARCHAR,
                source_asset_id VARCHAR,source_member VARCHAR,source_row BIGINT)""")
            db.execute("CREATE TABLE source_observations(source_asset_id VARCHAR PRIMARY KEY,observed_at VARCHAR)")
            db.executemany("INSERT INTO source_observations VALUES(?,?)",
                           [(asset["asset_id"], asset["observed_at"]) for asset in inputs])
            dispositions, seen_securities = {}, set()
            for asset in inputs:
                writer.budget()
                meta.execute("INSERT INTO source_assets VALUES(?,?)",
                             (asset["asset_id"], canonical(asset).decode()))
                if asset["kind"] != "bars":
                    dispositions[asset["asset_id"]] = _reference(meta, asset, encoding=encoding, securities=selected)
                    continue
                count = Counter(input_rows=0, quarantined=0)
                buffer = []
                for row in cmes.bar_rows(asset["path"], profile=asset["profile"],
                                         securities=selected, encoding=encoding):
                    count["input_rows"] += 1
                    seen_securities.add(row["security"])
                    if row["disposition"] != "adopted":
                        count["quarantined"] += 1
                        meta.execute("INSERT INTO cmes_rejections VALUES(?,?,?,?,?,?)", (
                            asset["asset_id"], row["source_member"], row["source_row"],
                            row["security"], row["reason"], canonical(row["native"]).decode(),
                        ))
                        continue
                    row.update(table_name=row["table"], source_asset_id=asset["asset_id"])
                    buffer.append([str(row[k]) if k in (*FIELDS, "preclose") and row[k] is not None
                                   else row[k] for k in COLUMNS])
                    if len(buffer) >= 5000:
                        _insert_bars(db, buffer)
                _insert_bars(db, buffer)
                dispositions[asset["asset_id"]] = dict(count)
            if selected is not None:
                require(set(selected) <= seen_securities, "CMES_SECURITY_MISSING",
                        "Requested securities have no source records",
                        {"missing": sorted(set(selected) - seen_securities)})
            keys = "table_name,series_id,security,trade_date,label_at"
            conflict = db.execute("SELECT " + keys + ",count(*) FROM staging GROUP BY " + keys
                                  + " HAVING count(DISTINCT (" + ",".join((*FIELDS, "preclose"))
                                  + "))>1 LIMIT 1").fetchone()
            require(conflict is None, "CMES_BAR_CONFLICT", "Conflicting bars; use an explicit corrected input",
                    {"key": [str(x) for x in conflict] if conflict else None})
            db.execute("CREATE TABLE ranked AS SELECT *,row_number() OVER(PARTITION BY " + keys
                       + " ORDER BY source_asset_id,source_member,source_row) AS ordinal FROM staging")
            db.execute("DROP TABLE staging")
            for asset, adopted, duplicate in db.execute("""SELECT source_asset_id,
                count(*) FILTER(WHERE ordinal=1),count(*) FILTER(WHERE ordinal>1)
                FROM ranked GROUP BY source_asset_id""").fetchall():
                dispositions[asset].update(adopted=adopted, equal_duplicate=duplicate)
            db.execute("CREATE VIEW accepted AS SELECT * EXCLUDE(ordinal) FROM ranked WHERE ordinal=1")
            require(db.execute("SELECT count(*) FROM accepted").fetchone()[0] > 0,
                    "CMES_EMPTY", "No valid bars; candidate was not published")
            ids = {security: i for i, security in enumerate(sorted(seen_securities), 1)}
            for security, instrument_id in ids.items():
                venue = "BJ" if security.startswith("BJ.") else security[-4:]
                meta.execute("INSERT INTO release_instruments VALUES(?,?,?,?,?,?)",
                             (instrument_id, security, None, "source code; issuer history unverified", venue,
                              "index" if index else "equity"))
                meta.execute("INSERT INTO release_identifiers VALUES(?,?,?,?,?,?,?)",
                             (security, instrument_id, "venue_index_code" if index else "venue_equity_code",
                              None, None, "unknown", import_id))
            for security, source_symbol, asset_id in db.execute(
                "SELECT DISTINCT security,source_symbol,source_asset_id FROM accepted"
            ).fetchall():
                meta.execute("INSERT INTO source_aliases VALUES(?,?,?)", (ids[security], source_symbol, asset_id))
            # Keep venue flags complete; the legacy common calendar contains only agreed SH/SZ sessions.
            meta.execute("""INSERT INTO calendar SELECT a.trade_date,'cmes',NULL FROM cmes_calendar a
                JOIN cmes_calendar b USING(trade_date) WHERE a.venue='XSHG' AND b.venue='XSHE'
                AND a.is_open=1 AND b.is_open=1""")
            meta.execute("INSERT INTO metadata VALUES('calendar_scope',?)", (
                json.dumps("CMES agreed SH/SZ sessions only; venue flags in cmes_calendar; no inferred BJ sessions or historical PIT"),
            ))
            meta.execute("INSERT INTO data_batches VALUES(1,?,1,?,?,?)",
                         (import_id, sha_file(__file__), mapping, canonical(spec).decode()))
            meta.executemany("INSERT INTO batch_inputs VALUES(1,?)", [(x["asset_id"],) for x in inputs])
            profiles = sorted({x["profile"] for x in inputs if x["kind"] == "bars"})
            for series_id in sorted({cmes.BAR_PROFILES[p]["series_id"] for p in profiles}):
                contract = dict(source="cmes", price_basis="raw", volume_unit="share", amount_unit="CNY",
                                mapping_version=cmes.MAPPING_VERSION, mapping_sha256=mapping,
                                profiles={p: cmes.BAR_PROFILES[p] for p in profiles
                                          if cmes.BAR_PROFILES[p]["series_id"] == series_id})
                if index:
                    contract.update(asset_class="index", price_unit="index_point", volume_unit=None, amount_unit=None)
                meta.execute("INSERT INTO series_contracts VALUES(?,?)", (series_id, canonical(contract).decode()))
            if minute:
                meta.execute("INSERT INTO release_contracts VALUES('minute_selection',?)", (
                    canonical({"sources": {"cmes": 7}, "canonical_series": [7]}).decode(),
                ))
            st_rows = meta.execute("SELECT security,trade_date FROM cmes_st").fetchall()
            db.register("st_rows", pd.DataFrame(st_rows, columns=["security", "trade_date"]))
            db.execute("CREATE TABLE st_membership AS SELECT DISTINCT CAST(security AS VARCHAR) AS security,"
                       "CAST(trade_date AS DATE) AS trade_date FROM st_rows")
            db.unregister("st_rows")
            files = []
            groups = db.execute("SELECT table_name,series_id,security,count(*),min(trade_date),max(trade_date) "
                                "FROM accepted GROUP BY ALL ORDER BY table_name,series_id,security").fetchall()
            for table, series_id, security, count, first, last in groups:
                writer.budget()
                instrument_id = ids[security]
                query = """SELECT a.* EXCLUDE(table_name,security,preclose),preclose::VARCHAR preclose,
                    ?::INTEGER instrument_id,
                    1::INTEGER batch_id,'cmes' AS source,NULL::VARCHAR response_id,observation.observed_at,
                    NULL::VARCHAR historical_available_at,
                    NULL::VARCHAR tradestatus,CASE WHEN EXISTS(SELECT 1 FROM st_membership s
                    WHERE s.security=a.security AND s.trade_date=a.trade_date) THEN '1' END is_st,
                    sha256(concat_ws('|',a.source_asset_id,source_member,source_row::VARCHAR)) row_hash
                    FROM accepted a JOIN source_observations observation
                    ON observation.source_asset_id=a.source_asset_id
                    WHERE table_name=? AND series_id=? AND security=?
                    ORDER BY trade_date,label_at"""
                obj = writer.parquet(db, query, [instrument_id, table, series_id, security],
                                     f"cmes-{series_id}-{instrument_id}")
                files.append(dict(obj, table=table, series_id=series_id, instrument_id=instrument_id,
                                  rows=count, first_date=str(first), last_date=str(last), duplicate_keys=0))
                if table == "bars_1d":
                    meta.execute("INSERT INTO release_daily_summary VALUES(?,'cmes',?,?,?)",
                                 (instrument_id, count, str(first), str(last)))
            meta.commit()
        for asset in inputs:
            require(Path(asset["path"]).stat().st_size == asset["bytes"] and sha_file(asset["path"]) == asset["sha256"],
                    "CMES_INPUT_CHANGED", "Original changed during import; candidate not published")
        metadata = writer.object(meta_path, "sqlite3")
        manifest = dict(metadata=metadata, files=files, input_dispositions=dispositions,
                        cmes_import_id=import_id, cmes=spec,
                        rules={"source": "cmes", "price_basis": "raw", "fallback": "none",
                               "duplicate_policy": "equal values deduplicated; conflicting values reject publication",
                               "invalid_bars": "quarantined with original record in metadata.cmes_rejections",
                               "calendar": "provider venue flags; common SH/SZ intersection only",
                               "st": "positive membership only; absence remains unknown",
                               "issuer_identity": "code aliases are not inferred; instrument_id is release scoped"})
        writer.publish(manifest, expected_parent=None)
    return dict(MarketRelease(root).receipt(), unchanged=False, input_dispositions=dispositions)
