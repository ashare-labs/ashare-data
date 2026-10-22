"""Append normalized public observations to an isolated CMES-based release chain."""

from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pandas as pd

from ashare_data.core.model import require
from ashare_data.providers.cmes import native_security
from ashare_data.storage.daily_increment import IncrementStore
from ashare_data.storage.market_release import (
    FIELDS, MarketRelease, ReleaseWriter, atomic_write, canonical, connection, sha_file,
)

VERSION = "ad-free-daily-candidate.v1"


class CandidateWriter(ReleaseWriter):
    def __init__(self, root, store):
        self.store = store
        super().__init__(root, budget_bytes=store.config["budget_bytes"],
                         free_reserve_bytes=store.config["free_reserve_bytes"])

    def budget(self):
        return self.store.budget()


def input_identity(store, day):
    digest = hashlib.sha256(canonical(dict(day=day, schema=VERSION, config=store.config)))
    for query in (
        "SELECT source,frequency,security,trade_date,label_at,row_hash,asset_id,source_row FROM observations WHERE trade_date=? ORDER BY source,frequency,security,label_at,row_hash",
        "SELECT security,kind,state,body FROM coverage WHERE day=? ORDER BY security,kind",
        "SELECT security,source,row_hash,body FROM factors WHERE day=? ORDER BY security,source,row_hash",
        "SELECT security,expected_state,source FROM scopes WHERE day=? ORDER BY security",
    ):
        for row in store.db.execute(query, (day,)):
            digest.update(canonical(list(row)))
            digest.update(b"\n")
    return digest.hexdigest()


def clone_baseline(base, output):
    """Hard-link only immutable objects; do not copy the historical price library."""
    root = Path(output)
    for name in ("objects", "releases", "work"):
        (root / name).mkdir(parents=True, exist_ok=True)
    # Preserve the exact chosen history chain, including objects replaced in older releases.
    for number in range(1, base.release_id + 1):
        release = MarketRelease(base.root, number)
        manifest = release.manifest
        objects = [manifest["metadata"], *manifest["files"], *manifest.get("evidence", [])]
        for obj in objects:
            source = release.verify(obj)
            target = root / obj["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                require(sha_file(target) == obj["sha256"], "INCREMENT_OBJECT", "Existing baseline link differs")
            else:
                try:
                    os.link(source, target)
                except OSError as error:
                    raise ValueError("Candidate must share the baseline filesystem for no-copy links") from error
        atomic_write(root / "releases" / f"{number:08d}.json",
                     (base.root / "releases" / f"{number:08d}.json").read_bytes())
    atomic_write(root / "CURRENT.json", canonical(dict(
        schema=base.manifest["schema"], release_id=base.release_id,
        manifest=f"releases/{base.release_id:08d}.json", sha256=base.sha256)))


def metadata(store, parent, writer, day, digest, batch_id):
    path = writer.root / "work" / "increment-metadata.sqlite3"
    path.unlink(missing_ok=True)  # This writer's unpublished, reproducible small metadata only.
    db = sqlite3.connect(path)
    with parent.metadata() as source:
        source.backup(db)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS increment_coverage(day TEXT,security TEXT,kind TEXT,state TEXT,
      observed_count INTEGER,body TEXT,PRIMARY KEY(day,security,kind));
    CREATE TABLE IF NOT EXISTS increment_factors(day TEXT,security TEXT,source TEXT,row_hash TEXT,body TEXT,
      PRIMARY KEY(day,security,source,row_hash));
    CREATE TABLE IF NOT EXISTS increment_instruments(security TEXT PRIMARY KEY,body TEXT);
    CREATE TABLE IF NOT EXISTS increment_rejections(day TEXT,security TEXT,kind TEXT,asset_id TEXT,
      source_row INTEGER,reason TEXT,native TEXT,PRIMARY KEY(day,security,kind,asset_id,source_row));
    """)
    for row in store.db.execute("SELECT * FROM instruments ORDER BY instrument_id"):
        old = db.execute("SELECT instrument_id FROM release_identifiers WHERE security=?", (row["security"],)).fetchone()
        require(old is None or old[0] == row["instrument_id"], "INCREMENT_IDENTITY", "Stable identity differs from baseline")
        if old is None:
            db.execute("INSERT INTO release_instruments VALUES(?,?,?,?,?,?)", (
                row["instrument_id"], row["security"], row["name"], "provider identifier; issuer lineage unverified",
                {"SH": "XSHG", "SZ": "XSHE", "BJ": "BJ"}[row["venue"]], "equity"))
            db.execute("INSERT INTO release_identifiers VALUES(?,?,?,?,?,?,?)", (
                row["security"], row["instrument_id"], "venue_equity_code", None, None,
                "source membership; exchange code validity unknown", digest))
        db.execute("INSERT OR REPLACE INTO increment_instruments VALUES(?,?)",
                   (row["security"], canonical(dict(row)).decode()))
    for table in ("coverage", "factors", "rejections"):
        for row in store.db.execute("SELECT * FROM " + table + " WHERE day=?", (day,)):
            db.execute("INSERT OR REPLACE INTO increment_" + table + " VALUES(" + ",".join("?" for _ in row) + ")", tuple(row))
    used_assets = {r[0] for r in store.db.execute("SELECT DISTINCT asset_id FROM observations WHERE trade_date=? "
                                               "UNION SELECT asset_id FROM rejections WHERE day=?", (day, day))}
    for table in ("coverage", "factors"):
        for (body,) in store.db.execute("SELECT body FROM " + table + " WHERE day=?", (day,)):
            asset = json.loads(body).get("source_asset_id")
            if asset:
                used_assets.add(asset)
    scope_path = store.root / "runs" / day / "universe.json"
    if scope_path.exists():
        used_assets.update(r["sha256"] for r in json.loads(scope_path.read_bytes())["receipts"])
    batch_path = store.root / "runs" / day / "baostock-index.json"
    if batch_path.exists():
        used_assets.update(r["capture_sha256"] for r in json.loads(batch_path.read_bytes()).values())
    for asset in sorted(used_assets):
        row = store.db.execute("SELECT body FROM captures WHERE sha256=?", (asset,)).fetchone()
        require(row is not None, "INCREMENT_LINEAGE", "Observation has no retained source capture")
        db.execute("INSERT OR IGNORE INTO source_assets VALUES(?,?)", (asset, row[0]))
        db.execute("INSERT OR IGNORE INTO batch_inputs VALUES(?,?)", (batch_id, asset))
    contract = dict(schema=VERSION, source_priority=store.config["daily_sources"],
                    raw_prices=True, volume_unit="share", amount_unit="CNY",
                    sohu_volume_precision="rounded source lots multiplied by 100",
                    minute_label_timezone="Asia/Shanghai", interval_semantics="source labels, unverified boundaries",
                    factor_policy="native source observations; incompatible or unverified cross-source bases never concatenated",
                    historical_pit=False, input_identity=digest, trade_date=day)
    db.execute("INSERT INTO data_batches VALUES(?,?,?,?,?,?)", (
        batch_id, digest, parent.release_id + 1, sha_file(__file__), hashlib.sha256(canonical(contract)).hexdigest(), canonical(contract).decode()))
    db.execute("INSERT OR REPLACE INTO release_contracts VALUES('free_daily_increment',?)", (canonical(contract).decode(),))
    daily_contract = json.loads(db.execute("SELECT body FROM series_contracts WHERE series_id=1").fetchone()[0])
    daily_contract.update(source="release_pinned_selection", sources=["cmes", *store.config["daily_sources"]],
                          source_transition="after each baseline security's observed daily end only",
                          source_mapping_contract="free_daily_increment")
    db.execute("UPDATE series_contracts SET body=? WHERE series_id=1", (canonical(daily_contract).decode(),))
    db.execute("INSERT OR REPLACE INTO series_contracts VALUES(6,?)", (canonical(dict(
        source="sina", frequency="1m", price_basis="raw", volume_unit="share", amount_unit="CNY",
        label_role="unknown", timezone="Asia/Shanghai", source_window=store.config["minute_window"])).decode(),))
    selection = db.execute("SELECT body FROM release_contracts WHERE name='minute_selection'").fetchone()
    selection = json.loads(selection[0]) if selection else {"sources": {}, "canonical_series": []}
    selection["sources"]["sina"] = 6
    if 6 not in selection["canonical_series"]:
        selection["canonical_series"].append(6)
    db.execute("INSERT OR REPLACE INTO release_contracts VALUES('minute_selection',?)", (canonical(selection).decode(),))
    db.commit()
    return db, path, contract


def build_candidate(root, day, output):
    store = IncrementStore(root)
    output = Path(output).expanduser().resolve()
    try:
        binding = store.config.get("base_release")
        require(binding is not None, "INCREMENT_BASE", "Select a frozen CMES baseline")
        base = MarketRelease(binding["root"], binding["release_id"], expected_manifest_sha256=binding["manifest_sha256"])
        require(bool(base.manifest.get("cmes")), "INCREMENT_BASE", "The baseline must explicitly identify CMES inputs")
        require(output != store.root and not base.root.is_relative_to(output) and not output.is_relative_to(base.root),
                "INCREMENT_OUTPUT", "Use a separate candidate directory")
        require(output.is_relative_to(store.root / "candidates"), "INCREMENT_OUTPUT",
                "Keep candidate output inside this increment root's candidates directory")
        marker = output / "INCREMENT-CANDIDATE.json"
        identity = dict(schema=VERSION, updater_root=str(store.root), baseline=binding)
        if output.exists() and any(output.iterdir()):
            require(marker.is_file() and json.loads(marker.read_bytes()) == identity,
                    "INCREMENT_OUTPUT", "Output is not owned by this candidate workflow")
        output.mkdir(parents=True, exist_ok=True)
        with store.writer(), CandidateWriter(output, store) as writer:
            if not marker.exists():
                atomic_write(marker, canonical(identity))
            if not (output / "CURRENT.json").exists():
                clone_baseline(base, output)
            parent = MarketRelease(output)
            digest = input_identity(store, day)
            if parent.manifest.get("free_daily_updates", {}).get(day) == digest:
                return dict(parent.receipt(), unchanged=True)
            require(store.db.execute("SELECT 1 FROM scopes WHERE day=?", (day,)).fetchone() is not None,
                    "INCREMENT_SCOPE", "No collected daily scope")
            with parent.metadata() as db:
                batch_id = db.execute("SELECT coalesce(max(batch_id),0)+1 FROM data_batches").fetchone()[0]
            meta, meta_path, contract = metadata(store, parent, writer, day, digest, batch_id)
            try:
                files = {(x["table"], x["series_id"], x["instrument_id"]): x for x in parent.files}
                cutoff = {}
                for item in base.files:
                    key = (item["table"], item["instrument_id"])
                    cutoff[key] = max(cutoff.get(key, ""), item["last_date"])
                counts = Counter(input_rows=0, adopted=0, equal_duplicate=0, resolved_unused=0, quarantined=0, format_rejected=0)
                groups = store.db.execute("SELECT frequency,security FROM observations WHERE trade_date=? GROUP BY frequency,security ORDER BY frequency,security", (day,)).fetchall()
                with closing(connection(output / "work" / "spill", memory="512MB", threads=1)) as c:
                    for group_number, (frequency, security) in enumerate(groups, 1):
                        table, series = ("bars_1d", 1) if frequency == "1d" else ("bars_1m", 6)
                        instrument_id = store.db.execute("SELECT instrument_id FROM instruments WHERE security=?", (security,)).fetchone()[0]
                        raw = [dict(r) for r in store.db.execute("SELECT * FROM observations WHERE frequency=? AND security=? AND trade_date=? ORDER BY source,label_at,row_hash", (frequency, security, day))]
                        counts["input_rows"] += len(raw)
                        if day <= cutoff.get((table, instrument_id), ""):
                            counts["resolved_unused"] += len(raw)
                            continue
                        by_label = {}
                        for row in raw:
                            by_label.setdefault(row["label_at"], []).append(row)
                        accepted = []
                        priority = {source: n for n, source in enumerate(store.config["daily_sources"])}
                        for versions in by_label.values():
                            versions.sort(key=lambda r: priority.get(r["source"], 0))
                            selected = [r for r in versions if r["source"] == versions[0]["source"]]
                            if len(selected) != 1:
                                counts["quarantined"] += len(versions)
                                continue
                            accepted.append(selected[0])
                            counts["resolved_unused"] += len(versions) - 1
                        if not accepted:
                            continue
                        c.register("incoming_rows", pd.DataFrame(accepted))
                        casts = ",".join(k + "::" + ("BIGINT" if k == "volume_shares" else "DECIMAL(38,12)") + " AS " + k for k in FIELDS)
                        c.execute("CREATE OR REPLACE TEMP TABLE incoming AS SELECT ?::INTEGER series_id,?::INTEGER instrument_id,"
                                  "trade_date::DATE trade_date,NULLIF(label_at,'')::TIMESTAMPTZ label_at," + casts + ","
                                  "?::INTEGER batch_id,source,asset_id AS source_asset_id,source_row,observed_at,"
                                  "NULL::VARCHAR historical_available_at,preclose,tradestatus,is_st,row_hash,"
                                  "asset_id AS response_id,?::VARCHAR AS source_symbol FROM incoming_rows", [series, instrument_id, batch_id, native_security(security)])
                        c.unregister("incoming_rows")
                        key = (table, series, instrument_id)
                        old = files.get(key)
                        if old:
                            c.execute("CREATE OR REPLACE TEMP TABLE previous AS SELECT * FROM read_parquet(?)", [str(parent.verify(old))])
                        else:
                            c.execute("CREATE OR REPLACE TEMP TABLE previous AS SELECT * FROM incoming WHERE false")
                        join = "i.trade_date=p.trade_date" + (" AND i.label_at=p.label_at" if frequency == "1m" else "")
                        equal = " AND ".join("i." + col + " IS NOT DISTINCT FROM p." + col for col in FIELDS)
                        overlap, same = c.execute("SELECT count(*),count(*) FILTER(WHERE " + equal + ") FROM incoming i JOIN previous p ON " + join).fetchone()
                        counts["equal_duplicate"] += same
                        counts["quarantined"] += overlap - same
                        c.execute("CREATE OR REPLACE TEMP TABLE additions AS SELECT i.* FROM incoming i WHERE NOT EXISTS(SELECT 1 FROM previous p WHERE " + join + ")")
                        added = c.execute("SELECT count(*) FROM additions").fetchone()[0]
                        counts["adopted"] += added
                        if not added:
                            continue
                        query = "SELECT * FROM (SELECT * FROM previous UNION ALL BY NAME SELECT * FROM additions) ORDER BY trade_date,label_at"
                        obj = writer.parquet(c, query, [], f"increment-{series}-{instrument_id}")
                        size, first, last = c.execute("SELECT count(*),min(trade_date),max(trade_date) FROM (" + query + ")").fetchone()
                        files[key] = dict(obj, table=table, series_id=series, instrument_id=instrument_id,
                                          rows=size, first_date=str(first), last_date=str(last), duplicate_keys=0)
                        if table == "bars_1d":
                            meta.execute("DELETE FROM release_daily_summary WHERE instrument_id=?", (instrument_id,))
                            meta.execute("INSERT INTO release_daily_summary VALUES(?,?,?,?,?)", (instrument_id, "release-selected", size, str(first), str(last)))
                        if group_number % 100 == 0:
                            writer.budget()
                            print(json.dumps(dict(event="candidate_progress", groups=group_number, total=len(groups))), flush=True)
                    meta.commit()
                manifest = dict(parent.manifest)
                # Close the small SQLite metadata before moving it into immutable objects.
            finally:
                meta.close()
            manifest.update(metadata=writer.object(meta_path, "sqlite3"), files=sorted(files.values(), key=lambda x: (x["table"], x["series_id"], x["instrument_id"])),
                            input_dispositions={**parent.manifest["input_dispositions"], "free_update_" + digest: dict(counts)},
                            free_daily_updates={**parent.manifest.get("free_daily_updates", {}), day: digest},
                            free_daily_contract=contract,
                            free_input_accounting_scope="normalized candidate observations; raw input dispositions retained per security/day in increment_coverage",
                            free_daily_coverage=store.report(day))
            writer.publish(manifest, expected_parent=parent.release_id, verify_unchanged=False)
            return dict(MarketRelease(output).receipt(), unchanged=False, input_dispositions=dict(counts),
                        factors_rebased_to_cmes=False, default_changed=False)
    finally:
        store.close()
