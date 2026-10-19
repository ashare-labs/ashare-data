"""Verify and publish the completed coverage-repair journals as one retained release.

The current pointer changes only after full bounded key/value gates and metadata,
source, disposition, and coverage checks pass. Restart uses the saved candidate.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import shutil
import sqlite3


from ashare_data.storage.market_release import (
    MarketRelease,
    ReleaseWriter,
    canonical,
    atomic_write,
    connection,
    sha_file,
    fingerprint,
    sql_literal,
    DISPOSITIONS,
)
from ashare_data.storage.market_coverage import refresh
from repair_market_coverage import fact
from ashare_data.storage.source_observations import daily_source


def write_object(writer, name, body, suffix="json"):
    p = writer.root / "work" / name
    p.write_bytes(body)
    return writer.object(p, suffix)


def restore_restricted_prices(writer, c, release, audit):
    """A use restriction is retained as a restriction, not inferred to be an invalid price."""
    result = []
    evidence = json.loads((audit / "hf-restriction-coverage.json").read_bytes())
    with release.metadata() as db:
        assets = {
            k: json.loads(v) for k, v in db.execute("select asset_id,body from source_assets")
        }
    for scope in evidence["scopes"]:
        chosen = [
            r
            for r in scope["rows"]
            if not {"nonpositive_price", "ohlc_envelope_error"} & set(r["rules"])
        ]
        if not chosen:
            continue
        matches = [
            (key, value)
            for key, value in assets.items()
            if value.get("path", "").endswith(scope["source_path"])
        ]
        assert len(matches) == 1
        asset_id, asset = matches[0]
        source = Path(asset["path"])
        assert sha_file(source) == asset["sha256"]
        instrument = asset["instrument_id"]
        batch = 600000 + instrument
        locators = ",".join(str(x["source_row"]) for x in chosen)
        query = f"""select 2::USMALLINT series_id,{instrument}::UINTEGER instrument_id,cast(timestamp as DATE) trade_date,
          timezone('Asia/Singapore',timestamp) label_at,open::DOUBLE open_price,high::DOUBLE high_price,
          low::DOUBLE low_price,close::DOUBLE close_price,volume::BIGINT volume_shares,turnover::DOUBLE turnover_cny,
          {batch}::UINTEGER batch_id from read_parquet({sql_literal(source)},file_row_number=true)
          where file_row_number in ({locators})"""
        c.execute("create or replace temp table restored_prices as " + query)
        assert (
            c.execute(
                "select count(*) from restored_prices where low_price<=0 or high_price<greatest(open_price,close_price) or low_price>least(open_price,close_price) or volume_shares<0 or turnover_cny<0"
            ).fetchone()[0]
            == 0
        )
        old = next(
            f
            for f in release.files
            if f["table"] == "bars_1m" and f["series_id"] == 2 and f["instrument_id"] == instrument
        )
        complete = (
            "select * from read_parquet("
            + sql_literal(release.verify(old))
            + ") UNION ALL select * from restored_prices ORDER BY label_at"
        )
        item = fact(
            writer,
            c,
            complete,
            "hf-research-restoration-" + str(instrument),
            "bars_1m",
            2,
            instrument,
        )
        updated_evidence = []
        for prior in release.manifest["evidence"]:
            if prior.get("series_id") != 2 or prior.get("instrument_id") != instrument:
                continue
            if prior.get("kind") == "issues":
                query = (
                    "select * replace(case when asset_id="
                    + str(asset_id)
                    + " and file_row_number in ("
                    + locators
                    + ") then 'adopted' else disposition end as disposition, case when asset_id="
                    + str(asset_id)
                    + " and file_row_number in ("
                    + locators
                    + ") then 'retained_price_conflict_with_execution_and_valuation_denial' else reason end as reason) from read_parquet("
                    + sql_literal(release.verify(prior))
                    + ")"
                )
            elif prior.get("kind") == "source_day_observations":
                query = (
                    "select * replace(observed_count+(select count(*) from restored_prices p where p.trade_date=o.trade_date) as observed_count) from read_parquet("
                    + sql_literal(release.verify(prior))
                    + ") o"
                )
            else:
                continue
            item_evidence = writer.parquet(
                c, query, [], "hf-restored-" + prior["kind"] + "-" + str(instrument)
            )
            item_evidence.update(
                kind=prior["kind"],
                series_id=2,
                instrument_id=instrument,
                check_version="hf-research-restrictions-v2",
            )
            updated_evidence.append(item_evidence)
        restrictions = [
            {
                "instrument_id": instrument,
                "series_id": 2,
                "label_at": label.isoformat(),
                "price_status": "conflict",
                "rule": "existing_daily_reference_price_conflict",
                "execution": "deny",
                "fresh_valuation": "deny",
            }
            for (label,) in c.execute("select label_at from restored_prices").fetchall()
        ]
        result.append(
            {
                "source": "hf_restriction_restore",
                "instrument_id": instrument,
                "batch_id": batch,
                "source_asset": asset_id,
                "source_rows": chosen,
                "files": [item],
                "evidence": updated_evidence,
                "restrictions": restrictions,
                "new_rows": len(chosen),
                "parser_sha256": sha_file(Path(__file__)),
            }
        )
    assert sum(r["new_rows"] for r in result) == 6
    return result


def rules(audit):
    return {
        "rule_version": "market-coverage-repair-v2",
        "early_daily": {
            "priority": "Sohu whole observation when OHLC and share volume corroborate Sina/Tencent within one source lot; amount/volume satisfies the documented rounded-unit envelope",
            "source_unit_mapping": {
                "Sohu_raw_volume_to_share": 100,
                "Sohu_raw_amount_to_CNY": 10000,
                "stored_SQLite_values": "already converted; no second scaling",
            },
            "unit_evidence_url": "https://q.stock.sohu.com/cn/000001/lshq.shtml",
            "evidence": "early-repair-summary.json",
            "unresolved": "Per-record historical denomination/unit corroboration failure; retained original observation remains available in the early research API",
            "price_basis": "unadjusted provider observations, corroborated in admitted scope; not exchange truth or strict PIT",
        },
        "stockdb": {
            "source_version": "b528f9ff8957ec38da1346e1e13bb823b377d8d0ea76a4638509229b15c3141a",
            "source_timezone": "Asia/Shanghai",
            "canonical_timezone": "UTC",
            "label_shift_seconds": 0,
            "scales": {"price": 1, "volume": 1, "amount": 1},
            "source_numbers": "native binary64 preserved; integral volume to BIGINT only with exact roundtrip",
            "units_evidence": "953797 matched 2025 minute labels in 17-security bounded sample share identical reported share volume and OHLC after accounting for source float32 representation; daily and 2026 aggregate checks retain rounding/regime limitations",
            "price_basis_evidence": "local native API documentation distinguishes raw values from dynamic qfq/hfq; bounded daily raw-source crosschecks retained",
            "source_document": "stockdb/调用方式/python/AI策略python开发接口文档.md, native raw retrieval section; pybao/stock_sdk.py adjustment implementation",
            "admission": "Valid native daily values fill dates absent from selected daily facts; before 2010 each missing record additionally requires independent OHLC/share-volume and amount-envelope corroboration. Valid minute values fill entire security-days absent from HF selected facts.",
            "time_boundaries": "Unknown, not guessed; overlaps are resolved by frozen whole-security-day priority, not treated as equivalent intervals",
            "precision_limitation": "Reported share volume is sometimes rounded, notably after 2026-01-21. Native source numbers are retained; no synthetic exact exchange volume is claimed.",
            "unknown_coverage": "HF partial final day is not filled intraday from a source with different label conventions",
        },
        "canonical_selection": {
            "daily_series": [1, 5],
            "daily_overlap_allowed": False,
            "minute_series": [2, 4],
            "minute_security_day_overlap_allowed": False,
            "tdx_series": 3,
        },
        "observations": "Explicit source reads use frozen original assets and keep quality restrictions; this is not an implicit canonical fallback",
        "hf_restrictions": "Restore six structurally valid price-conflict observations with explicit execution/fresh-valuation denial; invalid OHLC/nonpositive price records stay excluded from canonical facts",
        "calendar": "Retain prior frozen expectations. Newly admitted historical and supplemental dates have unknown expected coverage unless independently evidenced.",
        "pit": "No strict historical market PIT claim",
    }


def build(a, writer):
    r = MarketRelease(a.root, 1)
    assert r.sha256 == "99f0ed21634408c7b325f851518f9db5ed596484c1b0a143dc11c79e6c543391"
    pending = a.audit / "r2-candidate-manifest.json"
    if pending.exists():
        return json.loads(pending.read_bytes())
    stock_summary = json.loads((a.audit / "stockdb-repair-summary.json").read_bytes())
    assert stock_summary["complete"]
    independently_checked = json.loads((a.audit / "input-disposition-audit.json").read_bytes())
    assert independently_checked["passed"] and independently_checked["batches"] == 13169
    assert independently_checked["input_rows"] == 7348840 + 570743678
    source_research = json.loads((a.audit / "source-query-restoration.json").read_bytes())
    native_research = json.loads((a.audit / "native-hf-restoration.json").read_bytes())
    assert source_research["passed"] and source_research["all_original_fields_and_keys_equal"]
    assert native_research["passed"] and native_research["restored_original_keys"] == 30581
    assert (
        native_research["all_six_numeric_fields_equal"]
        and native_research["price_use_denials_preserved"]
    )
    with sqlite3.connect(
        (a.audit / "repair-checkpoint.sqlite3").as_uri() + "?mode=ro", uri=True
    ) as journal:
        batches = [
            (k, json.loads(b))
            for k, b in journal.execute(
                "select key,body from batches where key like 'early:%' or key like 'stockdb:%' order by key"
            )
        ]
    assert len([1 for k, v in batches if k.startswith("early:")]) == 1397
    assert len([1 for k, v in batches if k.startswith("stockdb:")]) == 11772
    c = connection(a.audit / "finalizer-spill", memory="1GB", threads=1)
    restored = restore_restricted_prices(writer, c, r, a.audit)
    batches += [("hf-research:" + str(v["instrument_id"]), v) for v in restored]
    new_rules = rules(a.audit)
    mapping_hash = hashlib.sha256(canonical(new_rules)).hexdigest()
    original = {(f["table"], f["series_id"], f["instrument_id"]): f for f in r.files}
    files = dict(original)
    changes = []
    early_ids = {v["instrument_id"] for k, v in batches if k.startswith("early:")}
    restored_ids = {v["instrument_id"] for v in restored}
    evidence = [
        e
        for e in r.manifest["evidence"]
        if not (
            (
                e.get("kind") == "input_dispositions"
                and e.get("series_id") == 1
                and e.get("instrument_id") in early_ids
            )
            or (
                e.get("kind") in ("issues", "source_day_observations")
                and e.get("series_id") == 2
                and e.get("instrument_id") in restored_ids
            )
        )
    ]
    counts = {key: Counter(v) for key, v in r.manifest["input_dispositions"].items()}
    early = Counter()
    stock = Counter()
    for key, batch in batches:
        if key.startswith("early:"):
            early.update(batch["input_dispositions"])
        if key.startswith("stockdb:"):
            stock.update(batch["input_dispositions"])
        evidence.extend(batch.get("evidence", []))
        for item in batch["files"]:
            k = (item["table"], item["series_id"], item["instrument_id"])
            files[k] = item
            changes.append({"key": k, "old": original.get(k), "new": item})
    assert early["input_rows"] == 7348840 and stock["input_rows"] == 570743678
    counts["daily_sqlite"]["quarantined"] -= early["input_rows"]
    for key in DISPOSITIONS:
        counts["daily_sqlite"][key] += early[key]
    counts["free_stockdb"] = stock
    counts["hf"]["quarantined"] -= 6
    counts["hf"]["adopted"] += 6
    assert sum(x["adopted"] for x in counts.values()) == sum(f["rows"] for f in files.values())
    # Validate every supplemental business key and every retained original value, bounded by instrument.
    checks = []
    for number, change in enumerate(changes):
        f = change["new"]
        p = str(r.verify(f))
        key = change["key"]
        old = change["old"]
        columns = "series_id,instrument_id,trade_date" + (
            ",label_at" if f["table"] == "bars_1m" else ""
        )
        duplicates = c.execute(
            "select count(*) from (select "
            + columns
            + ",count(*) n from read_parquet(?) group by "
            + columns
            + " having n!=1)",
            [p],
        ).fetchone()[0]
        assert duplicates == 0
        if old:
            missing = c.execute(
                "select count(*) from (select * from read_parquet(?) EXCEPT ALL select * from read_parquet(?))",
                [str(r.verify(old)), p],
            ).fetchone()[0]
            assert missing == 0, "Existing fact changed during coverage addition"
        if f["series_id"] in (4, 5):
            primary = files.get((f["table"], 2 if f["series_id"] == 4 else 1, f["instrument_id"]))
            if primary:
                overlap = c.execute(
                    "select count(*) from (select distinct trade_date from read_parquet(?) intersect select distinct trade_date from read_parquet(?))",
                    [p, str(r.verify(primary))],
                ).fetchone()[0]
                assert overlap == 0, "Supplement overlaps a primary security day"
        checks.append(
            {
                "key": key,
                "rows": f["rows"],
                "duplicate_keys": 0,
                "retained_original_row_differences": 0,
            }
        )
        if number % 100 == 0:
            print("VERIFY", number, len(changes), flush=True)
            writer.budget()
    report = {
        "schema": "market-coverage-repair-verification.v1",
        "changes": checks,
        "input_dispositions": counts,
        "target_unique_keys": {
            table: sum(f["rows"] for f in files.values() if f["table"] == table)
            for table in ("bars_1d", "bars_1m")
        },
        "all_native_input_response_hashes_match_r1_source_version": True,
        "all_staged_key_value_roundtrips": 0,
        "retained_original_row_differences": 0,
        "supplemental_day_overlaps": 0,
        "hf_restored_restricted_rows": 6,
    }
    atomic_write(a.audit / "r2-verification.json", canonical(report))
    # Freeze the journal without journal/WAL dependence and retain all producer versions.
    jcopy = writer.root / "work/coverage-repair-journal.sqlite3"
    with sqlite3.connect(
        (a.audit / "repair-checkpoint.sqlite3").as_uri() + "?mode=ro", uri=True
    ) as source:
        destination = sqlite3.connect(jcopy)
        source.backup(destination)
        destination.close()
    evidence.append({**writer.object(jcopy, "sqlite3"), "kind": "coverage_repair_journal"})
    producer = write_object(
        writer, "coverage-repair-finalizer.py", Path(__file__).read_bytes(), "py"
    )
    evidence.append({**producer, "kind": "coverage_repair_producer"})
    for path in (
        sorted(a.audit.glob("*.source"))
        + sorted(a.audit.glob("stockdb-parser-*.py"))
        + sorted(a.audit.glob("early-parser-*.py"))
    ):
        evidence.append(
            {
                **write_object(writer, "retained-producer-" + path.name, path.read_bytes(), "py"),
                "kind": "retained_parser",
            }
        )
    for name in [
        "r2-verification.json",
        "input-disposition-audit.json",
        "source-query-restoration.json",
        "native-hf-restoration.json",
        "early-repair-summary.json",
        "stockdb-repair-summary.json",
        "stockdb-semantics-sample.json",
        "stockdb-semantics-mixed-representation.json",
        "stockdb-divergence.json",
        "stockdb-basis-inspection.json",
        "sohu-history-unit-evidence.json",
        "hf-restriction-coverage.json",
    ]:
        evidence.append(
            {
                **write_object(writer, "coverage-" + name, (a.audit / name).read_bytes()),
                "kind": "coverage_repair_evidence",
                "name": name,
            }
        )
    metadata_path = writer.root / "work/coverage-repair-metadata.sqlite3"
    shutil.copyfile(r.metadata_path, metadata_path)
    doc = json.loads(a.entry.read_bytes())
    profile = doc["profiles"]["product"]
    data_root = Path(doc["data_root"])

    def full_path(value):
        p = Path(value).expanduser()
        return str((p if p.is_absolute() else data_root / p).resolve())

    native = profile["minute_sources"]["neigezhu"]
    native = json.loads(json.dumps(native))
    for name in ("manifest", "review"):
        retained = next(e for e in r.manifest["evidence"] if e["sha256"] == native[name]["sha256"])
        native[name]["path"] = str(r.verify(retained))
    source_readers = {
        "minute_sources": {"neigezhu": native},
        "assets": {
            native["asset"]: {
                **doc["assets"][native["asset"]],
                "path": full_path(doc["assets"][native["asset"]]["path"]),
            }
        },
    }
    with sqlite3.connect(metadata_path) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute(
            "insert or replace into release_contracts values ('coverage_repair',?)",
            (canonical(new_rules).decode(),),
        )
        db.execute(
            "insert or replace into release_contracts values ('source_observation_readers',?)",
            (canonical(source_readers).decode(),),
        )
        for series, code, freq in [
            (4, "stockdb_source_label_supplement", "1m"),
            (5, "stockdb_daily_supplement", "1d"),
        ]:
            db.execute("insert into series values (?,?,?)", (series, code, freq))
            body = {
                "series_id": series,
                "frequency": freq,
                "price_basis": "unadjusted",
                "basis_evidence_status": "verified_within_scope",
                "volume_unit": "share",
                "volume_basis": "source_reported_may_be_rounded",
                "amount_unit": "CNY",
                "semantic_status": "partially_verified",
                "numeric_storage": "binary64 prices/amount, exact integral reported share volume",
                "label_role": "unknown" if freq == "1m" else "trade_date",
                "interval_closure": "unknown",
            }
            db.execute(
                "insert into series_contracts values (?,?)", (series, canonical(body).decode())
            )
        db.execute(
            "CREATE TABLE release_row_restrictions(series_id INTEGER,instrument_id INTEGER,label_at TEXT,price_status TEXT,rule TEXT,execution TEXT,fresh_valuation TEXT,PRIMARY KEY(series_id,instrument_id,label_at))"
        )
        for group in restored:
            for v in group["restrictions"]:
                db.execute(
                    "insert into release_row_restrictions values (?,?,?,?,?,?,?)",
                    tuple(
                        v[k]
                        for k in [
                            "series_id",
                            "instrument_id",
                            "label_at",
                            "price_status",
                            "rule",
                            "execution",
                            "fresh_valuation",
                        ]
                    ),
                )
        for key, batch in batches:
            db.execute(
                "insert into release_batches values (?,?)",
                ("coverage-v2:" + key, canonical(batch).decode()),
            )
            if not batch["files"]:
                continue
            batch_id = batch["batch_id"]
            assert batch_id is not None
            db.execute(
                "insert into data_batches values (?,?,?,?,?,?)",
                (
                    batch_id,
                    "coverage-v2:" + key,
                    2,
                    batch["parser_sha256"],
                    mapping_hash,
                    canonical(
                        {
                            "source_asset": batch["source_asset"],
                            "inputs": batch.get("inputs"),
                            "source_rows": batch.get("source_rows"),
                            "input_values_sha256": batch.get("input_rows_sha256"),
                            "mapping_contract": "release_contracts.coverage_repair",
                            "retained_existing_values_keep_prior_batch": True,
                        }
                    ).decode(),
                ),
            )
            db.execute("insert into batch_inputs values (?,?)", (batch_id, batch["source_asset"]))
        # Summaries reflect all selected series, not stale pre-repair rows.
        db.execute("delete from release_daily_summary")
        summary = defaultdict(lambda: {"rows": 0, "first": None, "last": None})
        for f in files.values():
            if f["table"] != "bars_1d":
                continue
            for source, n, first, last in c.execute(
                "select source,count(*),min(trade_date),max(trade_date) from read_parquet(?) group by source",
                [str(r.verify(f))],
            ).fetchall():
                v = summary[(f["instrument_id"], source)]
                v["rows"] += n
                v["first"] = min(v["first"] or str(first), str(first))
                v["last"] = max(v["last"] or str(last), str(last))
        db.executemany(
            "insert into release_daily_summary values (?,?,?,?,?)",
            [(i, s, v["rows"], v["first"], v["last"]) for (i, s), v in summary.items()],
        )
        assert not db.execute("pragma foreign_key_check").fetchall()
        db.commit()
    db.close()
    metadata = writer.object(metadata_path, "sqlite3")
    metadata, evidence, coverage = refresh(writer, c, r, changes, evidence, metadata)
    with sqlite3.connect(writer.root / metadata["path"]) as db, r.metadata() as old_db:
        for table in ("data_batches", "source_assets"):
            old = {v[0]: tuple(v) for v in old_db.execute("select * from " + table)}
            new = {v[0]: tuple(v) for v in db.execute("select * from " + table)}
            assert all(new.get(k) == v for k, v in old.items())
        valid_batches = {
            row[0]: row[1] for row in db.execute("select batch_id,release_id from data_batches")
        }
        valid_instruments = {
            row[0] for row in db.execute("select instrument_id from release_instruments")
        }
    for change in changes:
        f = change["new"]
        assert f["instrument_id"] in valid_instruments
        ids = [
            x[0]
            for x in c.execute(
                "select distinct batch_id from read_parquet(?)", [str(r.verify(f))]
            ).fetchall()
        ]
        assert all(b in valid_batches and valid_batches[b] in (1, 2) for b in ids)
    rules_object = write_object(writer, "coverage-repair-rules.json", canonical(new_rules))
    evidence.append({**rules_object, "kind": "coverage_repair_rules"})
    value = {
        **r.manifest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": list(files.values()),
        "metadata": metadata,
        "evidence": list({x["path"]: x for x in evidence}.values()),
        "input_dispositions": counts,
        "target_unique_keys": report["target_unique_keys"],
        "coverage": coverage,
        "rule_version": "market-coverage-repair-v2",
        "rules": {**r.manifest["rules"], "coverage_repair": new_rules},
        "limitations": [
            "Local release reproducibility is supported; strict historical PIT remains unverified.",
            "Historical observations without per-record denomination/unit corroboration remain quarantined and queryable in preserved originals.",
            "Minute supplements select whole absent security days; native interval boundaries are unknown, and the HF final partial day is not filled intraday.",
            "Source-reported share counts may be rounded. Original binary values are retained without invented exchange precision.",
            "Explicit source research queries retain original observations and restrictions; canonical facts follow release-frozen selection.",
        ],
        "change_set": {
            "reason": "Repair overbroad historical/source exclusion and explicit source-query regression",
            "changes": changes,
        },
        "capacity": {
            "asset_budget_bytes": writer.budget_bytes,
            "free_reserve_bytes": writer.free_reserve_bytes,
            "current_owned_bytes": writer.budget(),
            "fact_bytes": sum(f["bytes"] for f in files.values()),
            "observed_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
    }
    value["initial_build_daily_key_reconciliation"] = value.pop("daily_key_reconciliation")
    c.close()
    atomic_write(pending, canonical(value))
    return value


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--entry", type=Path, required=True)
    p.add_argument("--publish", action="store_true")
    a = p.parse_args()
    with ReleaseWriter(a.root) as writer:
        candidate = build(a, writer)
        if a.publish:
            for item in candidate["evidence"]:
                assert sha_file(writer.root / item["path"]) == item["sha256"], (
                    "Evidence changed before publication"
                )
            with sqlite3.connect(writer.root / candidate["metadata"]["path"]) as db:
                daily = json.loads(
                    db.execute(
                        "select body from source_assets where asset_id='daily-sqlite'"
                    ).fetchone()[0]
                )
                for name, expected in daily["fingerprints"].items():
                    assert fingerprint(name) == expected, "Frozen daily source changed"
                stock = json.loads(
                    db.execute(
                        "select body from source_assets where asset_id='free-stockdb'"
                    ).fetchone()[0]
                )
            audit_record = next(
                e
                for e in candidate["evidence"]
                if e["sha256"] == "b98804b67e2038a975847e94db2c9069d0023ddd2e33f1462850927ae0cbb39c"
            )
            with sqlite3.connect(
                (writer.root / audit_record["path"]).as_uri() + "?mode=ro&immutable=1", uri=True
            ) as frozen:
                expected_inventory = json.loads(
                    frozen.execute(
                        "select value from metadata where key='source_inventory'"
                    ).fetchone()[0]
                )
            stock_root = Path(stock["root"])
            current_inventory = {
                str(p.relative_to(stock_root)): fingerprint(p)
                for name in ("data", "data1")
                for p in sorted((stock_root / name).glob("*"))
                if p.is_file() and p.name not in ("LOCK", "LOG", "LOG.old")
            }
            assert current_inventory == expected_inventory, "Frozen stockdb source changed"
            with daily_source(daily):
                pass
            existing = MarketRelease(a.root)
            if existing.release_id == 2:
                ignored = {
                    "schema",
                    "release_id",
                    "parent_release_id",
                    "parent_manifest_sha256",
                    "query_engine",
                    "snapshot_model",
                }
                assert all(
                    existing.manifest.get(k) == v for k, v in candidate.items() if k not in ignored
                )
                result = {"unchanged": True, "release": existing.receipt()}
            else:
                result = writer.publish(candidate, expected_parent=1)
            atomic_write(a.audit / "r2-publication.json", canonical(result))
            print(result)
        else:
            print("Verified candidate ready; pointer unchanged", candidate["target_unique_keys"])


if __name__ == "__main__":
    main()
