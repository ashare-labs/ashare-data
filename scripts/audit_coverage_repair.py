"""Independent bounded verification of every repair input key and its exclusive disposition."""

from pathlib import Path
from collections import Counter, defaultdict
import argparse
import json
import sqlite3
import time
from ashare_data.storage.market_release import (
    connection,
    canonical,
    atomic_write,
    sha_file,
    DISPOSITIONS,
    sql_literal,
)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--audit", type=Path, required=True)
args = parser.parse_args()
root = args.root
out = args.audit
c = connection(out / "input-audit-spill", memory="512MB", threads=1)
checkpoint = sqlite3.connect(out / "input-disposition-audit.sqlite3")
checkpoint.execute("create table if not exists batches(key text primary key,body text)")
checkpoint.commit()
start = time.monotonic()
iterations = 0
while True:
    with sqlite3.connect((out / "repair-checkpoint.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        batches = [
            (k, json.loads(b))
            for k, b in db.execute(
                "select key,body from batches where key like 'early:%' or key like 'stockdb:%'"
            )
        ]
    for key, b in batches:
        if checkpoint.execute("select 1 from batches where key=?", (key,)).fetchone():
            continue
        expected = b["input_dispositions"]
        counts = Counter()
        groups = []
        for e in b.get("evidence", []):
            p = root / e["path"]
            assert sha_file(p) == e["sha256"]
            if key.startswith("early:"):
                relation = "select * from read_parquet(?) where date<'2010-01-01'"
                columns = "source_symbol,date,source"
                label = "date"
                reason = "reason"
                origin = "source"
            else:
                relation = "select * from read_parquet(?)"
                columns = "source_code,native_label"
                label = "native_label"
                reason = "disposition_reason"
                origin = "source_code"
            c.execute(
                "create or replace temp view current_input as "
                + relation.replace("?", sql_literal(p))
            )
            n, unique = c.execute(
                "select count(*),count(distinct (" + columns + ")) from current_input"
            ).fetchone()
            assert n == unique, (key, n, unique)
            for state, amount in c.execute(
                "select disposition,count(*) from current_input group by disposition"
            ).fetchall():
                assert state in DISPOSITIONS
                counts[state] += amount
            for state, why, who, year, n, first, last in c.execute(
                "select disposition,"
                + reason
                + ","
                + origin
                + ",substr("
                + label
                + ",1,4),count(*),min("
                + label
                + "),max("
                + label
                + ") from current_input group by all"
            ).fetchall():
                groups.append(
                    {
                        "disposition": state,
                        "reason": why,
                        "source": who,
                        "year": year,
                        "rows": n,
                        "first": first,
                        "last": last,
                    }
                )
        assert sum(counts.values()) == expected["input_rows"], key
        assert all(counts[k] == expected.get(k, 0) for k in DISPOSITIONS), key
        value = {
            "instrument_id": b["instrument_id"],
            "series_id": b.get("series_id", 1),
            "input_rows": expected["input_rows"],
            "counts": counts,
            "groups": groups,
            "duplicate_source_keys": 0,
            "exact_disposition_partition": True,
        }
        checkpoint.execute("insert into batches values (?,?)", (key, canonical(value).decode()))
        checkpoint.commit()
        iterations += 1
        if iterations % 250 == 0:
            print("VERIFIED", iterations, "seconds", round(time.monotonic() - start, 1), flush=True)
    summary = out / "stockdb-repair-summary.json"
    complete = summary.exists() and json.loads(summary.read_bytes()).get("complete")
    if complete:
        assert len(batches) == 13169
        assert checkpoint.execute("select count(*) from batches").fetchone()[0] == len(batches)
        totals = defaultdict(Counter)
        reasons = defaultdict(Counter)
        for key, body in checkpoint.execute("select key,body from batches"):
            v = json.loads(body)
            family = (
                "early_sqlite"
                if key.startswith("early:")
                else "stockdb_daily"
                if v["series_id"] == 5
                else "stockdb_minute"
            )
            totals[family].update(v["counts"])
            for group in v["groups"]:
                reasons[family + "|" + group["disposition"]][group["reason"]] += group["rows"]
        atomic_write(
            out / "input-disposition-audit.json",
            canonical(
                {
                    "passed": True,
                    "batches": len(batches),
                    "input_rows": sum(sum(v.values()) for v in totals.values()),
                    "by_family": totals,
                    "reasons": reasons,
                    "all_source_keys_unique_within_batch": True,
                    "all_input_keys_have_one_disposition": True,
                    "exact_per_security_source_year_reason_scopes": str(
                        out / "input-disposition-audit.sqlite3"
                    ),
                    "seconds": time.monotonic() - start,
                }
            ),
        )
        print("COMPLETE", dict(totals), flush=True)
        break
    assert time.monotonic() - start < 3 * 3600, "Bounded audit waiting timeout"
    time.sleep(20)
