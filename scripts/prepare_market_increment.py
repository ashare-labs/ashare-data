"""Freeze an increment's metadata, source observations and evidence into an apply plan."""

import argparse
import gzip
import hashlib
from collections import Counter, defaultdict
import json
from pathlib import Path
import shutil
import sqlite3

from ashare_data.market.increment import MAPPING_VERSION, MINUTE_SERIES
from ashare_data.storage.market_release import (
    MarketRelease,
    ReleaseWriter,
    canonical,
    atomic_write,
    sha_file,
    connection,
)


def prepare(root, build_name="build"):
    stage = root / build_name
    report = json.loads((stage / "build-report.json").read_bytes())
    assert not report["preview"] and report["scopes_built"] == report["scopes_requested"]
    r = MarketRelease(
        report["parent"]["root"],
        report["parent"]["release_id"],
        expected_manifest_sha256=report["parent"]["manifest_sha256"],
    )
    scope = json.loads((root / "scope.json").read_bytes())
    assert report["scopes_built"] == len(scope["instruments"]), (
        "a bounded trial cannot publish as a full increment"
    )
    with sqlite3.connect((stage / "checkpoint.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        results = [
            json.loads(x[0]) for x in db.execute("select body from batches order by instrument_id")
        ]
    captures = []
    grouped = defaultdict(list)
    for path in sorted((root / "jobs").glob("*.json")):
        body = json.loads(path.read_bytes())
        digest = sha_file(path)
        captures.append({"path": str(path), "sha256": digest, "body": body})
        grouped[body["job_key"]].append((body["completed_at"], digest))
    capture_hash = hashlib.sha256(
        canonical([(key, sorted(items)) for key, items in sorted(grouped.items())])
    ).hexdigest()
    assert (
        capture_hash == json.loads((stage / "settings.json").read_bytes())["job_inventory_sha256"]
    ), "captures changed after build"
    capture_path = stage / "capture-receipts.json.gz"
    capture_body = gzip.compress(canonical(captures), mtime=0)
    if capture_path.exists():
        assert capture_path.read_bytes() == capture_body
    else:
        atomic_write(capture_path, capture_body)
    plan_path = stage / "replacement-plan.json"
    if plan_path.exists():
        print("Existing fixed plan", plan_path)
        return json.loads(plan_path.read_bytes())
    mapping = dict(
        schema="ad-recent-source-mapping.v1",
        version=MAPPING_VERSION,
        source_timezone="Asia/Shanghai",
        canonical_timezone="UTC",
        source_label_shift_seconds=0,
        sources={
            "sina_minute": {
                "raw": "decimal strings",
                "price_scale": 1,
                "volume_scale": 1,
                "amount_scale": 1,
                "bar_origin": "source_reported",
                "volume_unit": "share",
                "amount_unit": "CNY",
                "interval_semantics": "unknown",
            },
            "sohu": {
                "raw": "decimal strings",
                "price_scale": 1,
                "raw_volume_scale": 100,
                "raw_amount_scale": 10000,
                "volume_basis": "source_reported_rounded_lots",
            },
            "tencent": {
                "raw": "decimal strings",
                "price_scale": 1,
                "raw_volume_scale": 100,
                "amount": "not supplied; NULL",
            },
        },
        numeric_contract="DECIMAL(38,12) only after exact round trip; integral reported volume BIGINT; no zero/null substitution",
        daily_selection="Frozen priority baostock/eastmoney/sina/sohu/tencent. Existing stockdb supplemental days retained. Later higher-priority arrivals can replace a lower-priority whole daily observation. Same-priority changed values are quarantined pending adjudication.",
        minute_selection="Existing HF/stockdb days retained. Sina fills subsequent security days. Same-source equal labels deduplicate; previously absent labels append; conflicting revisions quarantine. Full-cap response left-edge day cannot establish a new whole security day.",
        observation_selection="Explicit source research uses the ordered release-pinned capture chain, independently of canonical winners.",
        scope_sha256=sha_file(root / "scope.json"),
        strict_historical_pit=False,
        coverage="Observed values only; unknown listings, source window limits and frozen calendar boundaries are not repaired by inference",
    )
    mapping_path = stage / "mapping.json"
    atomic_write(mapping_path, canonical(mapping))
    mapping_hash = sha_file(mapping_path)
    observation_path = stage / "recent-daily-observations.sqlite3"
    columns = [
        "instrument_id",
        "symbol",
        "date",
        "source",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "preclose",
        "tradestatus",
        "is_st",
        "row_hash",
        "response_id",
        "observed_at",
    ]
    with sqlite3.connect(observation_path) as db:
        db.execute(
            "create table if not exists observations(instrument_id INTEGER NOT NULL,symbol TEXT NOT NULL,date TEXT NOT NULL,source TEXT NOT NULL,open TEXT,high TEXT,low TEXT,close TEXT,volume TEXT,amount TEXT,preclose TEXT,tradestatus TEXT,is_st TEXT,row_hash TEXT,response_id TEXT,observed_at TEXT,PRIMARY KEY(instrument_id,date,source)) WITHOUT ROWID"
        )
        for value in results:
            db.executemany(
                "insert or ignore into observations values(" + ",".join("?" for _ in columns) + ")",
                [tuple(x[k] for k in columns) for x in value["observations"]],
            )
        assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert db.execute("select count(*) from observations").fetchone()[0] == sum(
            len(v["observations"]) for v in results
        )
        for value in results:
            for row in value["observations"]:
                actual = db.execute(
                    "select "
                    + ",".join(columns)
                    + " from observations where instrument_id=? and date=? and source=?",
                    (row["instrument_id"], row["date"], row["source"]),
                ).fetchone()
                assert actual == tuple(row[k] for k in columns), (
                    "retained observation candidate changed"
                )
    metadata_path = stage / "metadata.sqlite3"
    if not metadata_path.exists():
        shutil.copyfile(r.metadata_path, metadata_path)
    evidence = []
    with ReleaseWriter(r.root, retain_work_files=True) as writer:
        writer.budget()
        # This is a small new observation asset, never a copy of the historical source DB.
        copy = (
            writer.root
            / "work"
            / ("increment-observations-" + sha_file(observation_path) + ".sqlite3")
        )
        if not copy.exists():
            shutil.copyfile(observation_path, copy)
        observation = writer.object(copy, "sqlite3")
    with sqlite3.connect(metadata_path) as db:
        db.execute("pragma journal_mode=DELETE")
        assets = {x["asset_id"]: x for value in results for x in value["assets"]}
        instrument_by_security = {x["security"]: x["instrument_id"] for x in scope["instruments"]}
        for key, asset in assets.items():
            assert sha_file(asset["path"]) == asset["sha256"]
            db.execute(
                "insert or ignore into source_assets values(?,?)", (key, canonical(asset).decode())
            )
            assert (
                db.execute("select body from source_assets where asset_id=?", (key,)).fetchone()[0]
                == canonical(asset).decode()
            )
            instrument = instrument_by_security[asset["security"]]
            db.execute(
                "insert or ignore into source_aliases values(?,?,?,?)",
                (
                    key,
                    asset["security"],
                    instrument,
                    "Source request identifier bound to this immutable response; not a claim of historical exchange-code validity",
                ),
            )
        for value in results:
            for batch in value["batches"]:
                body = {
                    **batch,
                    "scope_sha256": mapping["scope_sha256"],
                    "mapping_version": MAPPING_VERSION,
                    "previous_watermarks": value["previous_watermarks"],
                    "source_revision_available_at": None,
                    "knowledge_status": "local_observation_only; strict_PIT_unknown",
                }
                batch_row = (
                    batch["batch_id"],
                    batch["batch_key"],
                    r.release_id + 1,
                    sha_file(Path(__file__).with_name("build_market_increment.py")),
                    mapping_hash,
                    canonical(body).decode(),
                )
                db.execute("insert or ignore into data_batches values(?,?,?,?,?,?)", batch_row)
                assert (
                    db.execute(
                        "select * from data_batches where batch_id=?", (batch["batch_id"],)
                    ).fetchone()
                    == batch_row
                ), "batch identity already has different contents"
                db.execute(
                    "insert or ignore into release_batches values(?,?)",
                    (batch["batch_key"], canonical(body).decode()),
                )
                assert (
                    db.execute(
                        "select body from release_batches where batch_key=?", (batch["batch_key"],)
                    ).fetchone()[0]
                    == canonical(body).decode()
                )
                db.executemany(
                    "insert or ignore into batch_inputs values(?,?)",
                    [(batch["batch_id"], asset) for asset in batch["assets"]],
                )
                assert {
                    x[0]
                    for x in db.execute(
                        "select asset_id from batch_inputs where batch_id=?",
                        (batch["batch_id"],),
                    )
                } == set(batch["assets"])
        series_body = dict(
            series_id=MINUTE_SERIES,
            frequency="1m",
            price_basis="unadjusted",
            basis_evidence_status="source_declared",
            volume_unit="share",
            volume_basis="source_reported",
            amount_unit="CNY",
            canonical_timezone="UTC",
            semantic_status="partially_verified",
            numeric_storage="DECIMAL(38,12) exact source decimal strings; integral BIGINT quantity",
            label_role="unknown",
            interval_closure="unknown",
            bar_origin="source_reported",
        )
        prior = db.execute(
            "select body from series_contracts where series_id=?", (MINUTE_SERIES,)
        ).fetchone()
        if prior:
            assert json.loads(prior[0]) == series_body
        else:
            db.execute(
                "insert into series values(?,?,?)",
                (MINUTE_SERIES, "sina_source_label_increment", "1m"),
            )
            db.execute(
                "insert into series_contracts values(?,?)",
                (MINUTE_SERIES, canonical(series_body).decode()),
            )
        minute_selection = {
            "sources": {"neigezhu": 2, "tdx": 3, "free_stockdb": 4, "sina": MINUTE_SERIES},
            "canonical_series": [2, 4, MINUTE_SERIES],
            "day_rule": "disjoint whole-security-day sources; no cross-source intraday splicing",
        }
        db.execute(
            "insert or replace into release_contracts values(?,?)",
            ("minute_selection", canonical(minute_selection).decode()),
        )
        old = db.execute(
            "select body from release_contracts where name='incremental_observations'"
        ).fetchone()
        observation_chain = (
            json.loads(old[0])
            if old
            else {"schema": "ad-incremental-source-observations.v1", "daily_assets": []}
        )
        if not any(x["sha256"] == observation["sha256"] for x in observation_chain["daily_assets"]):
            observation_chain["daily_assets"].append(
                {
                    **observation,
                    "sources": ["sohu", "tencent"],
                    "capture_start": scope["start"],
                    "capture_end": scope["end"],
                    "selection": "latest retained capture in this ordered immutable chain",
                }
            )
        db.execute(
            "insert or replace into release_contracts values(?,?)",
            ("incremental_observations", canonical(observation_chain).decode()),
        )
        db.execute(
            "insert or replace into release_contracts values(?,?)",
            ("recent_increment", canonical(mapping).decode()),
        )
        # Update only affected catalogue identities; do not scan all historical facts.
        c = connection(memory="512MB", threads=1)
        for value in results:
            incoming = {x["series_id"]: x for x in value["chunks"] if x["table"] == "bars_1d"}
            if not incoming:
                continue
            instrument = value["instrument_id"]
            counts = Counter()
            bounds = {}
            for series in (1, 5):
                if series in incoming:
                    path = incoming[series]["parquet"]
                else:
                    prior = next(
                        (
                            f
                            for f in r.files
                            if f["table"] == "bars_1d"
                            and f["series_id"] == series
                            and f["instrument_id"] == instrument
                        ),
                        None,
                    )
                    if not prior:
                        continue
                    path = str(r.verify(prior))
                for source, n, first, last in c.execute(
                    "select source,count(*),min(trade_date),max(trade_date) from read_parquet(?) group by source",
                    [path],
                ).fetchall():
                    counts[source] += n
                    previous = bounds.get(source, (first, last))
                    bounds[source] = (min(first, previous[0]), max(last, previous[1]))
            db.execute("delete from release_daily_summary where instrument_id=?", (instrument,))
            db.executemany(
                "insert into release_daily_summary values(?,?,?,?,?)",
                [
                    (instrument, k, n, str(bounds[k][0]), str(bounds[k][1]))
                    for k, n in counts.items()
                ],
            )
        c.close()
        assert not db.execute("pragma foreign_key_check").fetchall()
        db.commit()
    # Freeze scripts, response/capture inventory, per-row destination ledger and stage.
    inventory = {key: value for key, value in assets.items()}
    atomic_write(stage / "response-inventory.json", canonical(inventory))
    retained = [
        root / "scope.json",
        root / "collection-report.json",
        stage / "settings.json",
        stage / "build-report.json",
        mapping_path,
        stage / "response-inventory.json",
        observation_path,
        capture_path,
    ]
    retained += [
        Path(__file__),
        Path(__file__).with_name("build_market_increment.py"),
        Path(__file__).with_name("collect_market_increment.py"),
    ]
    import ashare_data.market.increment as normalization
    import ashare_data.providers.daily_sources as daily_parser

    retained += [Path(normalization.__file__), Path(daily_parser.__file__)]
    retained += sorted((root / "evidence").glob("*.json")) + sorted(
        (root / "evidence").glob("collector-*.py")
    )
    journal = stage / "sealed-checkpoint.sqlite3"
    with (
        sqlite3.connect((stage / "checkpoint.sqlite3").as_uri() + "?mode=ro", uri=True) as source,
        sqlite3.connect(journal) as dest,
    ):
        source.backup(dest)
    retained.append(journal)
    retained += [Path(value["ledger"]["path"]) for value in results]
    evidence = [{"path": str(p), "sha256": sha_file(p)} for p in retained]
    inputs = {
        **r.manifest["input_dispositions"],
        "increment_" + scope["end"]: report["input_dispositions"],
    }
    chunks = [
        {k: x[k] for k in ("table", "series_id", "instrument_id", "parquet", "sha256")}
        for v in results
        for x in v["chunks"]
    ]
    by_day = defaultdict(Counter)
    for value in results:
        for table, days in value["incoming_by_day"].items():
            for day, count in days.items():
                by_day[table][day] += count
    report.update(
        changed_chunks=len(chunks),
        source_assets=len(assets),
        observation_rows=sum(len(v["observations"]) for v in results),
        changed_rows={
            table: sum(
                x["changed_rows"] for v in results for x in v["chunks"] if x["table"] == table
            )
            for table in ("bars_1d", "bars_1m")
        },
        replaced_rows=sum(x["replaced_rows"] for v in results for x in v["chunks"]),
        admitted_by_day=by_day,
        canonical_minute_series=[2, 4, MINUTE_SERIES],
        expected_coverage="unknown; no newly inferred listings or calendar",
        future_historical_gaps="Sina cannot paginate beyond its latest 1023 source labels. Earlier per-security gaps remain visible, not silently certified complete.",
    )
    acceptance = stage / "increment-summary.json"
    atomic_write(acceptance, canonical(report))
    evidence.append({"path": str(acceptance), "sha256": sha_file(acceptance)})
    plan = dict(
        schema="ad-market-replacement-plan.v1",
        parent_manifest_sha256=r.sha256,
        change_reason="Publish sealed recent daily observations by frozen priority and a distinct Sina minute series; retain all earlier releases and unresolved limits",
        chunks=chunks,
        metadata={"path": str(metadata_path), "sha256": sha_file(metadata_path)},
        evidence=evidence,
        input_dispositions=inputs,
        retain_work_files=True,
        incremental_validation=True,
        increment_summary={"path": str(acceptance), "sha256": sha_file(acceptance)},
        mapping_sha256=mapping_hash,
    )
    atomic_write(plan_path, canonical(plan))
    print(
        json.dumps(
            {
                "plan": str(plan_path),
                "chunks": len(chunks),
                "new_rows": report["new_rows"],
                "source_assets": len(assets),
            },
            ensure_ascii=False,
        )
    )
    return plan


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--build-name", default="build")
    a = p.parse_args()
    prepare(a.root, a.build_name)
