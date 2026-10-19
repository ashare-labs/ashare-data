"""Finish a verified resumable market build and publish its immutable manifest.

The source code, journals, source contracts, complete input dispositions and coverage
are frozen together. Source files remain in their registered locations.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd

from ashare_data.storage.market_release import (
    ReleaseWriter,
    canonical,
    sha_file,
    fingerprint,
    sql_literal,
    DISPOSITIONS,
    FIELDS,
)
from merge_market import Merge, sid, RULE


def save_json(m, name, value):
    p = m.work / (name + ".json")
    p.write_bytes(canonical(value))
    return m.w.object(p, "json")


def tdx(m):
    if m.done("tdx:complete"):
        return
    spec = m.profile["minute_sources"]["tdx"]
    source = m.asset(spec["asset"])
    assert sha_file(source) == spec["content"]["sha256"]
    document = json.loads(source.read_bytes())
    copy = m.work / "tdx-source.json"
    copy.write_bytes(source.read_bytes())
    original = m.w.object(copy, "json")
    facts, evidence = [], [original]
    records = defaultdict(list)
    for ordinal, row in enumerate(document["minute"]):
        assert (
            row["price_basis"] == "raw"
            and row["volume_unit"] == "share"
            and row["amount_unit"] == "CNY"
        )
        symbol = ("sh" if row["security"].endswith(".XSHG") else "sz") + row["security"][:6]
        instrument = m.symbol_ids[symbol]
        values = [
            Decimal(row[k]) if row[k] is not None else None
            for k in ("open", "high", "low", "close", "volume", "amount")
        ]
        assert all(
            v is None or (v.is_finite() and v == v.quantize(Decimal("0.000000000001")))
            for v in values
        )
        assert values[4] is None or values[4] == values[4].to_integral_value()
        records[instrument].append(
            {
                "ordinal": ordinal,
                "label": row["source_label"],
                "trade_date": row["date"],
                **dict(zip(FIELDS, [None if x is None else str(x) for x in values])),
            }
        )
    total = Counter()
    for instrument, rows in records.items():
        m.c.register("tdx_input", pd.DataFrame(rows))
        cast = ",".join(
            "cast("
            + field
            + " as "
            + ("BIGINT" if field == "volume_shares" else "DECIMAL(38,12)")
            + ") as "
            + field
            for field in FIELDS
        )
        m.c.execute(
            "create or replace temp table tdx_values as select ordinal,cast(label as TIMESTAMPTZ) as label_at,cast(trade_date as DATE) as trade_date,"
            + cast
            + " from tdx_input"
        )
        assert (
            m.c.execute("select count(*)-count(distinct label_at) from tdx_values").fetchone()[0]
            == 0
        )
        invalid = """open_price is null or high_price is null or low_price is null or close_price is null
          or least(open_price,high_price,low_price,close_price)<=0 or high_price<greatest(open_price,close_price)
          or low_price>least(open_price,close_price) or high_price<low_price or volume_shares<0 or turnover_cny<0"""
        m.c.execute(
            "create or replace temp table tdx_classified as select *,case when "
            + invalid
            + " then 'quarantined' else 'adopted' end as disposition from tdx_values"
        )
        query = f"""select 3::USMALLINT as series_id,{instrument}::UINTEGER as instrument_id,trade_date,label_at,
          {",".join(FIELDS)},{200000 + instrument}::UINTEGER as batch_id from tdx_classified where disposition='adopted' order by label_at"""
        fact = m.fact(query, f"tdx-{instrument}", "bars_1m", instrument, 3)
        if fact:
            facts.append(fact)
        evidence.append(
            m.w.parquet(
                m.c,
                "select ordinal,trade_date,label_at,disposition from tdx_classified order by ordinal",
                [],
                f"tdx-disposition-{instrument}",
            )
        )
        counts = dict(
            m.c.execute(
                "select disposition,count(*) from tdx_classified group by disposition"
            ).fetchall()
        )
        total.update(counts)
        m.c.unregister("tdx_input")
    total["input_rows"] = len(document["minute"])
    for key in DISPOSITIONS:
        total.setdefault(key, 0)
    m.finish(
        "tdx:complete",
        {
            "source": "tdx",
            "files": facts,
            "evidence": evidence,
            "source_assets": [
                {
                    "asset_id": 200000,
                    "path": str(source),
                    "sha256": sha_file(source),
                    "fingerprint": fingerprint(source),
                }
            ],
            "input_dispositions": dict(total),
            "contract": {
                "source_timezone": "explicit +08:00 offset",
                "scale_factors": {"price": 1, "volume": 1, "amount": 1},
                "price": "exact DECIMAL(38,12)",
                "volume": "exact BIGINT",
                "amount": "exact DECIMAL(38,12)",
                "label_role": "unknown",
                "series_id": 3,
                "price_basis": "unadjusted",
                "volume_unit": "share",
                "amount_unit": "CNY",
                "semantic_status": "partially_verified",
                "strict_execution_admitted": False,
            },
            "input_locator": "minute array ordinal in hash-pinned source JSON; closing_auction_inputs are not extra bars",
        },
    )


def stockdb(m, directory):
    if m.done("stockdb:complete"):
        return
    report = json.loads((directory / "report.json").read_bytes())
    assert report["completed"]
    db = sqlite3.connect((directory / "checkpoint.sqlite3").as_uri() + "?mode=ro", uri=True)

    def setting(key):
        return json.loads(
            db.execute("select value from metadata where key=?", (key,)).fetchone()[0]
        )

    report["tables"]["日k"]["same_label_equal_native_numeric_rows"] = None
    report["overlap_scope"] = (
        "Same source code/label diagnostic; stable aliases are separately frozen. These quarantined inputs do not enter target duplicate selection. Daily binary values versus decimal source strings were not labeled equal or unequal."
    )
    report["source_aliases"] = []
    for code in setting("equity_codes"):
        symbol = (
            "sh" if code.startswith("6") else "sz" if code.startswith(("0", "3")) else "bj"
        ) + code
        report["source_aliases"].append(
            {"source_identifier": code, "instrument_id": m.symbol_ids[symbol]}
        )
    source = Path(report["source_asset"]["root"])
    for name, expected in setting("source_inventory").items():
        assert fingerprint(source / name) == expected, "Stockdb source changed after audit"
    copy = m.work / "stockdb-audit.sqlite3"
    copy.unlink(missing_ok=True)
    target = sqlite3.connect(copy)
    db.backup(target)
    target.close()
    db.close()
    auditor = Path(__file__).with_name("audit_stockdb_merge.py")
    audit_copy = m.work / "stockdb-audit-algorithm.py"
    audit_copy.write_bytes(auditor.read_bytes())
    audit_object = m.w.object(audit_copy, "py")
    report["audit_algorithm"] = {
        "source_object": audit_object,
        "version": RULE,
        "pin_scope": "Finalized reproducible algorithm; initial query process source-byte hash was not recorded. No stockdb facts are admitted by this release.",
    }
    evidence = [m.w.object(copy, "sqlite3"), audit_object, save_json(m, "stockdb-report", report)]
    total = Counter()
    for counts in report["tables"].values():
        total.update({key: counts.get(key, 0) for key in DISPOSITIONS})
        total["input_rows"] += counts["rows"]
    m.finish(
        "stockdb:complete",
        {
            "source": "free_stockdb",
            "files": [],
            "evidence": evidence,
            "input_dispositions": dict(total),
            "report": report,
            "accounting_rule": "Every scoped original KV bar retains its unique key in the sealed original. Quarantine is a location and restriction, not a discarded row.",
        },
    )


def extend_identities(m):
    additions = m.setting("final_identity_extensions")
    if additions is None:
        maximum = max(m.symbol_ids.values())
        additions = {}
        for row in csv.DictReader((m.hf / "metadata/absent_instruments.csv").open()):
            symbol = row["exchange"].lower() + row["symbol"]
            if symbol not in m.symbol_ids:
                maximum += 1
                additions[symbol] = maximum
        m.setting("final_identity_extensions", additions)
    m.symbol_ids.update(additions)
    return additions


def coverage(m, batches):
    """Generate the frozen provider universe, including days with zero accepted rows."""
    marker = m.db.execute("select value from config where key='final_coverage'").fetchone()
    if marker:
        return json.loads(marker[0])
    calendar = [
        r["date"]
        for r in csv.DictReader((m.hf / "metadata/market_calendar.csv").open())
        if r["is_trading_day"] == "True" and r["within_published_span"] == "True"
    ]
    missing = defaultdict(dict)
    for row in csv.DictReader((m.hf / "metadata/no_bar_day_classification.csv").open()):
        missing[row["exchange"].lower() + row["symbol"]][row["date"]] = row
    summaries = {
        int(key.split(":")[1]): v["evidence"][1] for key, v in batches if key.startswith("hf:")
    }
    files = []
    totals = Counter()
    securities = []
    universe = list(m.coverage)
    for row in csv.DictReader((m.hf / "metadata/absent_instruments.csv").open()):
        universe.append(
            {
                **row,
                "absent": True,
                "expected_start_date": max(row["listing_date"] or "2010-01-04", "2010-01-04"),
                "expected_end_date": min(row["delisting_date"] or "2026-08-07", "2026-08-07"),
            }
        )
    for pos, row in enumerate(universe):
        symbol = row["exchange"].lower() + row["symbol"]
        instrument = m.symbol_ids.get(symbol)
        # Absent securities have no fact keys. Keep their source identity explicitly,
        # rather than inventing a join to a different code or dropping the denominator.
        securities.append(
            {"symbol": symbol, "instrument_id": instrument, "absent": bool(row.get("absent"))}
        )
        dates = [d for d in calendar if row["expected_start_date"] <= d <= row["expected_end_date"]]
        if not dates:
            continue
        frame = pd.DataFrame(
            {
                "trade_date": dates,
                "classification": [
                    missing[symbol].get(d, {}).get("classification", "") for d in dates
                ],
            }
        )
        m.c.register("coverage_dates", frame)
        observed = summaries.get(instrument)
        if observed:
            relation = "read_parquet(" + sql_literal(m.root / observed["path"]) + ")"
        else:
            relation = "(select NULL::DATE as trade_date,0::BIGINT as observed_count,0::BIGINT as source_observed_count where false)"
        state = (
            "case when classification='confirmed_suspension' then 'not_expected' else 'expected' end"
            if not row.get("absent")
            else "'unknown'"
        )
        query = f"""select 2::USMALLINT as series_id,{instrument if instrument is not None else "NULL"}::UINTEGER as instrument_id,
          {sql_literal(symbol)} as source_identifier,cast(e.trade_date as date) as trade_date,{state} as expected_state,
          NULL::INTEGER as expected_count,coalesce(o.observed_count,0) as observed_count,
          coalesce(o.source_observed_count,0) as source_observed_count,
          case when ({state})='expected' and coalesce(o.observed_count,0)=0 then 1 else 0 end as missing_day,
          classification as source_classification,'provider universe and calendar; minute count/interval unverified' as coverage_evidence
          from coverage_dates e left join {relation} o on cast(e.trade_date as date)=o.trade_date order by e.trade_date"""
        item = m.w.parquet(m.c, query, [], f"hf-expected-{pos}")
        item.update(
            kind="bar_day_checks",
            check_version="provider-presence-v1",
            series_id=2,
            source_identifier=symbol,
            instrument_id=instrument,
        )
        files.append(item)
        stats = m.c.execute(
            "select expected_state,count(*),sum(missing_day),sum(case when observed_count>0 then 1 else 0 end) from ("
            + query
            + ") group by expected_state"
        ).fetchall()
        for state, n, gap, present in stats:
            totals[state + "_days"] += n
            totals[state + "_missing_days"] += gap
            totals[state + "_observed_days"] += present
        m.c.unregister("coverage_dates")
        if pos % 500 == 0:
            print("COVERAGE", pos + 1, len(universe), flush=True)
    value = {
        "files": files,
        "counts": dict(totals),
        "universe": securities,
        "known_expected_is": "frozen provider-declared expected presence, not independent exchange certification",
        "expected_minute_count": None,
        "absent_security_expected_state": "unknown; listing/delisting evidence insufficient",
    }
    m.setting("final_coverage", value)
    return value


def daily_key_reconciliation(m, batches):
    """Reconcile multi-source input rows with old symbol-days and stable identity-days."""
    totals = Counter()
    reasons = Counter()
    for key, batch in batches:
        if batch["source"] != "daily_sqlite":
            continue
        item = batch["evidence"][0]
        relation = "read_parquet(" + sql_literal(m.root / item["path"]) + ")"
        n, source_days, stable_days = m.c.execute(
            "select count(*),count(distinct (source_symbol,date)),count(distinct date) from "
            + relation
        ).fetchone()
        totals.update(
            input_rows=n, source_symbol_days=source_days, stable_identity_days=stable_days
        )
        categories = m.c.execute(
            "select case when accepted=1 then 'adopted_keys' when quarantined then 'unresolved_keys' else 'adjudicated_unused_keys' end,count(*) "
            "from (select date,count(*) filter(where disposition='adopted') accepted,"
            "bool_or(disposition in ('quarantined','format_rejected')) quarantined from "
            + relation
            + " group by date) group by 1"
        ).fetchall()
        totals.update(dict(categories))
        for disposition, reason, count in m.c.execute(
            "select disposition,reason,count(*) from " + relation + " group by 1,2"
        ).fetchall():
            reasons[disposition + ":" + reason] += count
    assert totals["input_rows"] == 38736814
    assert totals["source_symbol_days"] == 14394671
    assert totals["stable_identity_days"] == sum(
        totals[k] for k in ("adopted_keys", "unresolved_keys", "adjudicated_unused_keys")
    )
    totals["alias_key_collapses"] = totals["source_symbol_days"] - totals["stable_identity_days"]
    return {
        "counts": dict(totals),
        "input_disposition_reasons": dict(reasons),
        "interpretation": "38,736,814 are multi-source input rows; 14,394,671 are raw symbol-days. Difference is not automatically same-value duplicates. Stable identities are reconciled before final key classification.",
    }


def metadata(m, files, batches, coverage_report):
    target = m.work / "published-reference.sqlite3"
    target.unlink(missing_ok=True)
    with sqlite3.connect(m.reference) as src, sqlite3.connect(target) as d:
        src.backup(d)
        d.executescript("""
        ALTER TABLE release_instruments ADD COLUMN identity_key TEXT;
        CREATE TABLE source_aliases(source_version TEXT,source_identifier TEXT,instrument_id INTEGER,
          interpretation TEXT,PRIMARY KEY(source_version,source_identifier));
        CREATE TABLE data_batches(batch_id INTEGER PRIMARY KEY,batch_key TEXT NOT NULL,release_id INTEGER NOT NULL,
          parser_sha256 TEXT NOT NULL,mapping_sha256 TEXT NOT NULL,body TEXT NOT NULL);
        CREATE TABLE batch_inputs(batch_id INTEGER NOT NULL REFERENCES data_batches(batch_id),
          asset_id TEXT NOT NULL REFERENCES source_assets(asset_id),PRIMARY KEY(batch_id,asset_id));
        CREATE TABLE release_daily_summary(instrument_id INTEGER,source TEXT,rows INTEGER,first TEXT,last TEXT,
          PRIMARY KEY(instrument_id,source));
        CREATE TABLE release_contracts(name TEXT PRIMARY KEY,body TEXT NOT NULL);
        CREATE TABLE series(series_id INTEGER PRIMARY KEY,series_code TEXT NOT NULL UNIQUE,frequency_code TEXT NOT NULL);
        INSERT INTO series VALUES(1,'daily_raw_research','1d'),(2,'hf_source_label_raw','1m'),(3,'tdx_source_label_raw','1m');
        """)
        d.execute(
            "update release_instruments set identity_status='provisional',identity_key=security"
        )
        for symbol, instrument in m.setting("final_identity_extensions").items():
            public = "BJ." + symbol[2:] if symbol.startswith("bj") else sid(symbol)
            d.execute(
                "insert into release_instruments values (?,?,?,?,?,?,?)",
                (instrument, public, "", "provisional", symbol[:2].upper(), "equity", public),
            )
            d.execute(
                "insert into release_identifiers values (?,?,?,?,?,?,?)",
                (
                    public,
                    instrument,
                    "venue_equity_code",
                    None,
                    None,
                    "unknown",
                    m.setting("hf_manifest_sha256"),
                ),
            )
        # Stable IDs keep the original identity key; today's display code is separate.
        names = dict(d.execute("select symbol,name from instruments"))
        for event in m.events:
            public = sid(event["new_symbol"])
            instrument = m.symbol_ids[event["new_symbol"]]
            d.execute(
                "update release_instruments set security=?,name=? where instrument_id=?",
                (public, names.get(event["new_symbol"], ""), instrument),
            )
        for name, value in m.contracts().items():
            d.execute(
                "insert into release_contracts values (?,?)", (name, canonical(value).decode())
            )
        daily_asset = {
            "asset_id": "daily-sqlite",
            "files": m.setting("source_daily_hashes"),
            "fingerprints": m.setting("source_daily_stats"),
            "consistency": "cooperative writer lock plus read transaction, unchanged DB/WAL fingerprints, per-batch source values digest",
            "original_table": "bars",
            "original_locator": ["symbol", "date", "source"],
        }
        d.execute(
            "insert into source_assets values (?,?)",
            ("daily-sqlite", canonical(daily_asset).decode()),
        )
        daily_source_version = hashlib.sha256(canonical(daily_asset)).hexdigest()
        for symbol in m.daily_symbols:
            i = m.symbol_ids[symbol]
            d.execute(
                "insert into source_aliases values (?,?,?,?)",
                (
                    daily_source_version,
                    symbol,
                    i,
                    "source snapshot alias; NOT proof of exchange code validity for every bar date",
                ),
            )
        for key, batch in batches:
            d.execute("insert into release_batches values (?,?)", (key, canonical(batch).decode()))
            source = batch["source"]
            if source == "daily_sqlite":
                ids = [int(key.split(":")[1])]
                assets = ["daily-sqlite"]
            elif source == "hf":
                ids = [a["asset_id"] for a in batch["source_assets"]]
                assets = [str(x) for x in ids]
            elif source == "tdx":
                ids = [200000 + f["instrument_id"] for f in batch["files"]]
                assets = ["200000"]
            else:
                ids = []
                assets = []
            for asset in batch.get("source_assets", []):
                asset_id = str(asset["asset_id"])
                d.execute(
                    "insert or ignore into source_assets values (?,?)",
                    (asset_id, canonical(asset).decode()),
                )
                if source == "hf":
                    d.execute(
                        "insert or ignore into source_aliases values (?,?,?,?)",
                        (
                            asset["source_version"],
                            str(asset["path"]),
                            asset["instrument_id"],
                            "immutable file may backfill earlier observations under a later code; exchange validity is separate",
                        ),
                    )
            if source == "free_stockdb":
                d.execute(
                    "insert into source_assets values (?,?)",
                    ("free-stockdb", canonical(batch["report"]["source_asset"]).decode()),
                )
                d.executemany(
                    "insert into source_aliases values (?,?,?,?)",
                    [
                        (
                            batch["report"]["scope"]["source_version"],
                            alias["source_identifier"],
                            alias["instrument_id"],
                            "quarantined source snapshot alias; exchange validity is separate",
                        )
                        for alias in batch["report"]["source_aliases"]
                    ],
                )
            if source == "tdx":
                original = json.loads((m.root / batch["evidence"][0]["path"]).read_bytes())
                for security in sorted({row["security"] for row in original["minute"]}):
                    symbol = ("sh" if security.endswith(".XSHG") else "sz") + security[:6]
                    d.execute(
                        "insert into source_aliases values (?,?,?,?)",
                        (
                            batch["source_assets"][0]["sha256"],
                            security,
                            m.symbol_ids[symbol],
                            "immutable TDX source snapshot alias; exchange validity is separate",
                        ),
                    )
                d.execute(
                    "insert into series_contracts values (?,?)",
                    (
                        3,
                        canonical(
                            {
                                "series_id": 3,
                                "frequency": "1m",
                                "price_basis": "unadjusted",
                                "volume_unit": "share",
                                "amount_unit": "CNY",
                                "canonical_timezone": "UTC",
                                "label_role": "unknown",
                                "interval_closure": "unknown",
                                "semantic_status": "partially_verified",
                            }
                        ).decode(),
                    ),
                )
            for batch_id in ids:
                d.execute(
                    "insert into data_batches values (?,?,?,?,?,?)",
                    (
                        batch_id,
                        key,
                        1,
                        batch["parser_sha256"],
                        batch["rule_hash"],
                        canonical({"immutable_build_batch": key}).decode(),
                    ),
                )
                for asset_id in assets:
                    if source != "hf" or asset_id == str(batch_id):
                        d.execute("insert into batch_inputs values (?,?)", (batch_id, asset_id))
        for f in files:
            if f["table"] != "bars_1d":
                continue
            rows = m.c.execute(
                "select source,count(*),min(trade_date),max(trade_date) from read_parquet(?) group by source",
                [str(m.root / f["path"])],
            ).fetchall()
            d.executemany(
                "insert into release_daily_summary values (?,?,?,?,?)",
                [
                    (f["instrument_id"], source, n, str(first), str(last))
                    for source, n, first, last in rows
                ],
            )
        d.execute(
            "insert into release_contracts values (?,?)",
            (
                "coverage",
                canonical({k: v for k, v in coverage_report.items() if k != "files"}).decode(),
            ),
        )
        assert not d.execute("pragma foreign_key_check").fetchall()
        d.commit()
    return m.w.object(target, "sqlite3")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--entry", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--stockdb-audit", type=Path, required=True)
    p.add_argument("--publish", action="store_true")
    args = p.parse_args()
    with ReleaseWriter(args.root) as writer:
        m = Merge(args.entry, args.root)
        m.w = writer
        if (m.root / "CURRENT.json").exists() and (m.work / "candidate-manifest.json").exists():
            from ashare_data.storage.market_release import MarketRelease
            from ashare_data.product.release_cli import verify

            existing = MarketRelease(m.root, 1)
            candidate = json.loads((m.work / "candidate-manifest.json").read_bytes())
            assert all(existing.manifest.get(k) == v for k, v in candidate.items()), (
                "Published first release differs from build candidate"
            )
            print(json.dumps({"unchanged": True, **verify(existing)}), flush=True)
            return
        m.prepare()
        m.source_check()
        extend_identities(m)
        daily = [
            json.loads(v)
            for k, v in m.db.execute("select key,value from batches where key like 'daily:%'")
        ]
        hf = [
            json.loads(v)
            for k, v in m.db.execute("select key,value from batches where key like 'hf:%'")
        ]
        assert sum(x["input_dispositions"]["input_rows"] for x in daily) == 38736814, (
            "Daily incomplete"
        )
        assert sum(x["input_dispositions"]["input_rows"] for x in hf) == sum(
            int(r["decoded_rows"]) for r in m.source_manifest
        ), "HF incomplete"
        final_source = Path(__file__)
        final_copy = m.work / "finalize-parser.py"
        final_copy.write_bytes(final_source.read_bytes())
        m.parser_sha256 = sha_file(final_source)
        m.parser_object = m.w.object(final_copy, "py")
        tdx(m)
        stockdb(m, args.stockdb_audit)
        report, files, evidence, batches = m.report()
        annotated = {}
        for key, batch in batches:
            source = batch["source"]
            if source not in ("daily_sqlite", "hf"):
                continue
            instrument = int(key.split(":")[1])
            for index, item in enumerate(batch["evidence"]):
                kind = (
                    ("input_dispositions" if index == 0 else "bar_day_checks")
                    if source == "daily_sqlite"
                    else ("issues" if index == 0 else "source_day_observations")
                )
                annotated[item["path"]] = {
                    **item,
                    "kind": kind,
                    "instrument_id": instrument,
                    "series_id": 1 if source == "daily_sqlite" else 2,
                    "check_version": "daily-provider-presence-v1"
                    if source == "daily_sqlite"
                    else "hf-observation-v1",
                }
        evidence = [annotated.get(item["path"], item) for item in evidence]
        daily_reconciliation = daily_key_reconciliation(m, batches)
        evidence.append(save_json(m, "daily-key-reconciliation", daily_reconciliation))
        coverage_report = coverage(m, batches)
        evidence.extend(coverage_report["files"])
        evidence.extend(v["parser_object"] for _, v in batches)
        for key, value in m.db.execute("select key,value from config"):
            obj = json.loads(value)
            if isinstance(obj, dict) and {"path", "sha256", "bytes"} <= obj.keys():
                evidence.append(obj)
        evidence.append(save_json(m, "source-contracts", m.contracts()))
        final_source = Path(__file__)
        copy = m.work / "finalize-parser.py"
        copy.write_bytes(final_source.read_bytes())
        evidence.append(m.w.object(copy, "py"))
        trial = m.root.parent / "migration-20261019/trial/report.json"
        if trial.exists():
            evidence.append(save_json(m, "storage-trial", json.loads(trial.read_bytes())))
        comparison = m.root.parent / "migration-20261019/minute-daily-comparison.json"
        if comparison.exists():
            evidence.append(
                save_json(m, "minute-daily-comparison", json.loads(comparison.read_bytes()))
            )
        import importlib.metadata
        import platform

        reproduction = {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "dependencies": {
                name: importlib.metadata.version(name) for name in ("duckdb", "pandas", "numpy")
            },
            "scope": "Exact numerical engine/dependency versions for reproduction; per-batch parser objects and mapping hashes are separate",
        }
        evidence.append(save_json(m, "reproduction-runtime", reproduction))
        meta = metadata(m, files, batches, coverage_report)
        evidence = list({x["path"]: x for x in evidence}.values())
        # Verify references, keys, source/target values and every partition's row count.
        with sqlite3.connect(m.root / meta["path"]) as db:
            valid_ids = {r[0] for r in db.execute("select instrument_id from release_instruments")}
            valid_batches = {r[0] for r in db.execute("select batch_id from data_batches")}
        for pos, f in enumerate(files):
            assert f["instrument_id"] in valid_ids
            observed = m.c.execute(
                "select count(*),count(distinct batch_id),min(instrument_id),max(instrument_id),min(series_id),max(series_id) from read_parquet(?)",
                [str(m.root / f["path"])],
            ).fetchone()
            assert observed[0] == f["rows"] and observed[2:] == (
                f["instrument_id"],
                f["instrument_id"],
                f["series_id"],
                f["series_id"],
            )
            ids = {
                r[0]
                for r in m.c.execute(
                    "select distinct batch_id from read_parquet(?)", [str(m.root / f["path"])]
                ).fetchall()
            }
            assert ids <= valid_batches
            if pos % 1000 == 0:
                print("FINAL GATE", pos + 1, len(files), flush=True)
        for item in evidence:
            assert sha_file(m.root / item["path"]) == item["sha256"]
        m.source_check()
        for _, batch in batches:
            for asset in batch.get("source_assets", []):
                assert fingerprint(asset["path"]) == asset["fingerprint"], (
                    "Source changed before publication"
                )
        manifest = {
            "metadata": meta,
            "files": files,
            "evidence": evidence,
            "input_dispositions": report["input_dispositions"],
            "target_unique_keys": report["target_unique_keys"],
            "daily_key_reconciliation": daily_reconciliation,
            "rules": m.contracts(),
            "coverage": {
                k: v for k, v in coverage_report.items() if k not in ("files", "universe")
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rule_version": RULE,
            "limitations": [
                "Pre-2010 daily unit/basis uncertainty and invalid observations are quarantined in preserved originals.",
                "HF source-declared raw price/share/CNY semantics; intervals, 09:30 and strict historical PIT are unverified.",
                "HF latest observed day 2026-08-07 ends at 10:21, with incomplete intraday coverage.",
                "free-stockdb raw daily/minute inventory is accounted for but quarantined until unit/label contracts are evidenced.",
                "TDX is a separate bounded 960-row series, with exact decimal values and unverified interval/PIT semantics.",
                "Provider universe and calendar are frozen research evidence, not an independently complete historical exchange universe.",
            ],
            "capacity": {
                "asset_budget_bytes": writer.budget_bytes,
                "free_reserve_bytes": writer.free_reserve_bytes,
                "current_owned_bytes": writer.budget(),
                "fact_bytes": sum(item["bytes"] for item in files),
                "evidence_bytes": sum(item["bytes"] for item in evidence),
                "metadata_bytes": meta["bytes"],
                "observed_batch_peak_rss_bytes": max(
                    batch["peak_rss_bytes"] for _, batch in batches
                ),
                "per_query_memory_limit": "2GB for imports; bounded queries have independent connections",
                "spill_limit_bytes": 8 * 1024**3,
            },
            "integrity": {
                "input_accounting": "exclusive dispositions for every input row",
                "key_value_check": "bounded EXCEPT ALL in both directions for every output object",
                "fact_unique_keys": "per complete instrument/series chunk; publication rejects overlapping chunks",
                "reference_check": "all target instrument and batch IDs verified",
                "physical_fact_pk_fk": False,
                "enforcement": "cooperative single writer plus full release gate",
            },
        }
        # Keep the draft outside the default pointer; only --publish makes it visible.
        draft = m.work / "candidate-manifest.json"
        if draft.exists():
            prior = json.loads(draft.read_bytes())
            for key in ("files", "metadata", "evidence", "input_dispositions", "rules"):
                assert prior[key] == manifest[key], "Interrupted candidate changed: " + key
            manifest = prior
        else:
            draft.write_bytes(canonical(manifest))
        if args.publish:
            print(json.dumps(writer.publish(manifest, expected_parent=None)), flush=True)
        else:
            print(
                json.dumps(
                    {
                        "candidate": str(draft),
                        "input_dispositions": report["input_dispositions"],
                        "target_unique_keys": report["target_unique_keys"],
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
