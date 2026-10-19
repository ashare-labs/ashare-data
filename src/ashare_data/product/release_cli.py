"""Inspect, query, verify, resume a backup, restore, or publish bounded replacements.

Replacement plans describe complete affected instrument/series chunks. Unaffected
objects and old release manifests are retained; no whole-market copy is created.
"""

import argparse
import json
from pathlib import Path
import shutil
import sqlite3

from ashare_data.core.model import require
from ashare_data.storage.market_release import (
    MarketRelease,
    ReleaseWriter,
    atomic_write,
    canonical,
    connection,
    sha_file,
    sql_literal,
)


def verify(release):
    objects = {
        item["path"]: item
        for item in [
            *release.files,
            *release.manifest.get("evidence", []),
            release.manifest["metadata"],
        ]
    }
    for item in objects.values():
        release.verify(item)
    return {
        "release": release.receipt(),
        "objects_checked": len(objects),
        "bytes_checked": sum(x["bytes"] for x in objects.values()),
    }


def backup(release, destination):
    destination = Path(destination).resolve()
    require(destination != release.root, "MARKET_BACKUP_TARGET", "Use a separate target")
    marker = {"source_manifest_sha256": release.sha256, "release_id": release.release_id}
    if destination.exists():
        require(
            (destination / "BACKUP-SOURCE.json").exists(),
            "MARKET_BACKUP_EXISTS",
            "Target is not this command's resumable backup",
        )
        require(
            json.loads((destination / "BACKUP-SOURCE.json").read_bytes()) == marker,
            "MARKET_BACKUP_EXISTS",
            "Target belongs to another release",
        )
    else:
        destination.mkdir(parents=True)
        atomic_write(destination / "BACKUP-SOURCE.json", canonical(marker))
    seen = {}
    for rid in range(1, release.release_id + 1):
        prior = MarketRelease(release.root, rid)
        for item in [*prior.files, *prior.manifest.get("evidence", []), prior.manifest["metadata"]]:
            seen[item["path"]] = item
        target = destination / "releases" / f"{rid:08d}.json"
        atomic_write(target, (release.root / "releases" / target.name).read_bytes())
    remaining = sum(
        item["bytes"] for item in seen.values() if not (destination / item["path"]).exists()
    )
    require(
        shutil.disk_usage(destination).free > remaining + 1024**3,
        "MARKET_CAPACITY",
        "Insufficient backup space plus 1 GiB reserve",
    )
    for item in seen.values():
        source = release.verify(item)
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and sha_file(target) == item["sha256"]:
            continue
        partial = target.with_name(target.name + ".partial")
        shutil.copyfile(source, partial)
        require(
            sha_file(partial) == item["sha256"], "MARKET_RESTORE_INTEGRITY", "Backup copy differs"
        )
        partial.replace(target)
    atomic_write(
        destination / "CURRENT.json",
        canonical(
            {
                "schema": "ad-market-release.v1",
                "release_id": release.release_id,
                "manifest": f"releases/{release.release_id:08d}.json",
                "sha256": release.sha256,
            }
        ),
    )
    return verify(MarketRelease(destination))


def apply(release, plan):
    """Publish complete touched chunks under a parent hash, with explicit correction evidence."""
    require(
        plan["schema"] == "ad-market-replacement-plan.v1", "MARKET_PLAN", "Unknown replacement plan"
    )
    require(
        plan["parent_manifest_sha256"] == release.sha256,
        "MARKET_PARENT_CHANGED",
        "Plan must name the fixed parent hash",
    )
    require(
        bool(plan.get("change_reason")) and bool(plan.get("evidence")),
        "MARKET_CHANGE_EVIDENCE",
        "Supply correction evidence and reason",
    )
    with ReleaseWriter(release.root) as writer:
        writer.budget()
        c = connection(writer.root / "work/spill")
        files = {(f["table"], f["series_id"], f["instrument_id"]): f for f in release.files}
        changes = []
        for number, change in enumerate(plan["chunks"]):
            key = (change["table"], change["series_id"], change["instrument_id"])
            require(key[0] in ("bars_1d", "bars_1m"), "MARKET_TABLE", "Unknown fact table")
            original = files.get(key)
            if change.get("delete"):
                require(original is not None, "MARKET_DELETE_KEY", "Cannot delete an absent chunk")
                del files[key]
                changes.append({"key": key, "deleted_rows": original["rows"]})
                continue
            source = Path(change["parquet"]).resolve()
            require(
                sha_file(source) == change["sha256"],
                "MARKET_INPUT_INTEGRITY",
                "Replacement input differs",
            )
            query = "SELECT * FROM read_parquet(" + sql_literal(source) + ")"
            group = "series_id,instrument_id,trade_date" + (
                ",label_at" if key[0] == "bars_1m" else ""
            )
            counts = c.execute(
                "select count(*),min(series_id),max(series_id),min(instrument_id),max(instrument_id),min(trade_date),max(trade_date) from ("
                + query
                + ")"
            ).fetchone()
            require(
                counts[0] > 0 and counts[1:5] == (key[1], key[1], key[2], key[2]),
                "MARKET_CHUNK_SCOPE",
                "One complete instrument/series per replacement",
            )
            duplicates = c.execute(
                "select count(*) from (select "
                + group
                + ",count(*) n from ("
                + query
                + ") group by "
                + group
                + " having n!=1)"
            ).fetchone()[0]
            require(
                duplicates == 0, "MARKET_DUPLICATE_KEY", "Replacement has duplicate logical keys"
            )
            # A byte-different encoding of the exact same rows is still a no-op.
            if original:
                old = "SELECT * FROM read_parquet(" + sql_literal(release.verify(original)) + ")"
                difference = c.execute(
                    "select count(*) from (("
                    + query
                    + " EXCEPT ALL "
                    + old
                    + ") UNION ALL ("
                    + old
                    + " EXCEPT ALL "
                    + query
                    + "))"
                ).fetchone()[0]
                if difference == 0:
                    continue
            chunk = writer.parquet(c, query, [], "replacement-" + str(number))
            chunk.update(
                table=key[0],
                series_id=key[1],
                instrument_id=key[2],
                rows=counts[0],
                first_date=str(counts[5]),
                last_date=str(counts[6]),
                duplicate_keys=0,
            )
            files[key] = chunk
            changes.append({"key": key, "old": original, "new": chunk})
        if not changes:
            c.close()
            return {"unchanged": True, "release": release.receipt()}

        def import_object(spec, suffix):
            source = Path(spec["path"]).resolve()
            require(
                sha_file(source) == spec["sha256"], "MARKET_INPUT_INTEGRITY", "Plan input differs"
            )
            target = writer.root / "work" / ("replacement-input." + suffix)
            shutil.copyfile(source, target)
            return writer.object(target, suffix)

        metadata = import_object(plan["metadata"], "sqlite3")
        evidence = [*release.manifest.get("evidence", [])]
        evidence.extend(
            import_object(item, Path(item["path"]).suffix.lstrip(".") or "bin")
            for item in plan["evidence"]
        )
        with sqlite3.connect(writer.root / metadata["path"]) as db, release.metadata() as old_db:
            instruments = {
                r[0] for r in db.execute("select instrument_id from release_instruments")
            }
            batches = dict(db.execute("select batch_id,release_id from data_batches"))
            require(
                not db.execute("pragma foreign_key_check").fetchall(),
                "MARKET_REFERENCE",
                "Invalid metadata references",
            )
            # Existing source/batch identities cannot be repointed when publishing.
            for table, key_column in (("data_batches", "batch_id"), ("source_assets", "asset_id")):
                old_rows = {r[0]: tuple(r) for r in old_db.execute("select * from " + table)}
                new_rows = {r[0]: tuple(r) for r in db.execute("select * from " + table)}
                require(
                    all(new_rows.get(key) == row for key, row in old_rows.items()),
                    "MARKET_IMMUTABLE_METADATA",
                    "Existing source/batch metadata changed",
                )
                if table == "data_batches":
                    require(
                        all(
                            batches[key] == release.release_id + 1
                            for key in new_rows.keys() - old_rows.keys()
                        ),
                        "MARKET_BATCH",
                        "New batches belong to the next release",
                    )
        for change in changes:
            if "new" not in change:
                continue
            f = change["new"]
            require(f["instrument_id"] in instruments, "MARKET_REFERENCE", "Unknown instrument")
            new_relation = "read_parquet(" + sql_literal(writer.root / f["path"]) + ")"
            ids = {
                r[0] for r in c.execute("select distinct batch_id from " + new_relation).fetchall()
            }
            require(
                ids <= batches.keys()
                and all(0 < batches[i] <= release.release_id + 1 for i in ids),
                "MARKET_BATCH",
                "Unknown or future batch",
            )
            fresh_ids = [i for i in ids if batches[i] == release.release_id + 1]
            fresh = (
                "n.batch_id IN (" + ",".join(str(i) for i in fresh_ids) + ")"
                if fresh_ids
                else "false"
            )
            old = change.get("old")
            if old:
                old_relation = "read_parquet(" + sql_literal(release.verify(old)) + ")"
                original_schema = c.execute("describe select * from " + old_relation).fetchall()
                require(
                    original_schema
                    == c.execute("describe select * from " + new_relation).fetchall(),
                    "MARKET_NUMERIC_CONTRACT",
                    "Replacement must retain the physical numeric/schema contract",
                )
                keys = ["series_id", "instrument_id", "trade_date"] + (
                    ["label_at"] if f["table"] == "bars_1m" else []
                )
                join = " AND ".join("n." + k + "=o." + k for k in keys)
                same = " AND ".join(
                    'n."' + r[0] + '" IS NOT DISTINCT FROM o."' + r[0] + '"'
                    for r in original_schema
                    if r[0] not in {*keys, "batch_id"}
                )
                bad = c.execute(
                    "select count(*) from "
                    + new_relation
                    + " n left join "
                    + old_relation
                    + " o on "
                    + join
                    + " where case when o.batch_id is not null and ("
                    + same
                    + ") then n.batch_id IS DISTINCT FROM o.batch_id else NOT ("
                    + fresh
                    + ") end"
                ).fetchone()[0]
            else:
                bad = c.execute(
                    "select count(*) from " + new_relation + " n where NOT (" + fresh + ")"
                ).fetchone()[0]
            require(
                bad == 0,
                "MARKET_BATCH_HISTORY",
                "Changed values require a new batch; unchanged values retain the old batch",
            )
        value = {
            **release.manifest,
            "files": list(files.values()),
            "metadata": metadata,
            "evidence": evidence,
            "input_dispositions": plan["input_dispositions"],
            "change_set": {"reason": plan["change_reason"], "changes": changes},
            "target_unique_keys": {
                table: sum(f["rows"] for f in files.values() if f["table"] == table)
                for table in ("bars_1d", "bars_1m")
            },
        }
        c.close()
        return writer.publish(value, expected_parent=release.release_id)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--release-id", type=int)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("describe")
    sub.add_parser("verify")
    q = sub.add_parser("query")
    q.add_argument("--table", choices=["bars_1d", "bars_1m"], required=True)
    q.add_argument("--security", required=True)
    q.add_argument("--start", required=True)
    q.add_argument("--end", required=True)
    q.add_argument("--series-id", type=int)
    for name in ("backup", "restore"):
        s = sub.add_parser(name)
        s.add_argument("--destination", type=Path, required=True)
    s = sub.add_parser("apply")
    s.add_argument("--plan", type=Path, required=True)
    a = p.parse_args(argv)
    release = MarketRelease(a.root, a.release_id)
    if a.command == "describe":
        result = release.manifest
    elif a.command == "verify":
        result = verify(release)
    elif a.command in ("backup", "restore"):
        result = backup(release, a.destination)
    elif a.command == "apply":
        result = apply(release, json.loads(a.plan.read_bytes()))
    else:
        result = {
            "release": release.receipt(),
            "rows": release.read(a.table, a.security, a.start, a.end, series_id=a.series_id),
        }
    print(json.dumps(result, ensure_ascii=False, default=str, allow_nan=False))


if __name__ == "__main__":
    main()
