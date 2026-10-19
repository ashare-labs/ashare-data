"""Bounded real minute storage trial. Never modifies sources or drops OS caches."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time

import duckdb


def emit(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, default=str))
    os.replace(temp, path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--entry", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    args = p.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.root).free < 150 * 1024**3:
        raise RuntimeError("150 GiB free-space reserve")
    result = {
        "engine": duckdb.__version__,
        "started_at": time.time(),
        "limits": {
            "threads": 2,
            "memory": "4GB",
            "scratch": "12GB",
            "trial_budget_bytes": 20 * 1024**3,
            "free_reserve_bytes": 150 * 1024**3,
        },
        "cold_semantics": "new process/connection, OS page cache not flushed",
        "source_modified": False,
    }
    entry = json.loads(args.entry.read_text())
    spec = entry["profiles"]["product"]["minute_sources"]["neigezhu"]
    source = Path(entry["assets"][spec["asset"]]["path"])
    raw = Path(spec["manifest"]["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == spec["manifest"]["sha256"]
    files = [
        str(source / r["path"])
        for r in csv.DictReader(raw.decode().splitlines())
        if r["path"].startswith("data/stock_1m/")
    ]
    # First gate: writer ownership in independent processes.
    gate = args.root / "concurrency.duckdb"
    c = duckdb.connect(str(gate))
    c.execute("create or replace table t as select 7 as v")
    c.execute("checkpoint")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import duckdb,sys;print(duckdb.connect(sys.argv[1],read_only=True).execute('select * from t').fetchall())",
            str(gate),
        ],
        capture_output=True,
        text=True,
    )
    result["mutable_native_cross_process"] = {
        "reader_exit": probe.returncode,
        "diagnostic": probe.stderr[-1600:],
    }
    c.close()
    gate.unlink()
    print("GATE", result["mutable_native_cross_process"], flush=True)
    c = duckdb.connect(str(args.root / "instrument.duckdb"))
    c.execute("set threads=2; set memory_limit='4GB'; set preserve_insertion_order=false")
    c.execute("set temp_directory=?", [str(args.root / "spill")])
    c.execute("set max_temp_directory_size='12GB'")
    t = time.monotonic()
    existing = c.execute(
        "select count(*) from information_schema.tables where table_name='bars'"
    ).fetchone()[0]
    if not c.execute(
        "select count(*) from information_schema.tables where table_name='bars'"
    ).fetchone()[0]:
        c.execute(
            """create table bars as
            select exchange||symbol as security,timestamp,open,high,low,close,volume,turnover
            from read_parquet(?)
            where (timestamp >= timestamp '2024-07-01' and timestamp < timestamp '2024-08-01')
               or (exchange||symbol in ('SH600519','SH600000','SZ000001','SZ300750'))
            order by security,timestamp""",
            [files],
        )
        c.execute("checkpoint")
    result["instrument_load_seconds"] = None if existing else time.monotonic() - t
    result["sample_resumed"] = bool(existing)
    result["sample"] = dict(
        zip(
            ["rows", "securities", "first", "last"],
            c.execute(
                "select count(*), count(distinct security),min(timestamp),max(timestamp) from bars"
            ).fetchone(),
        )
    )
    print("SAMPLE", result["sample"], flush=True)
    t = time.monotonic()
    parquet = args.root / "instrument.parquet"
    if not parquet.exists():
        c.execute(
            "copy (select * from bars order by security,timestamp) to ? (format parquet, compression zstd, row_group_size 122880)",
            [str(parquet)],
        )
    result["parquet_export_seconds"] = time.monotonic() - t
    t = time.monotonic()
    dated = args.root / "date.duckdb"
    c.execute("attach '" + str(dated).replace("'", "''") + "' as dated")
    if not c.execute(
        "select count(*) from information_schema.tables where table_catalog='dated' and table_name='bars'"
    ).fetchone()[0]:
        c.execute("create table dated.bars as select * from bars order by timestamp,security")
    c.execute("detach dated")
    result["date_load_seconds"] = time.monotonic() - t
    # Key AND each value, both directions, one bounded sample, no checksum-only assertion.
    cols = "security,timestamp,open,high,low,close,volume,turnover"
    mismatches = 0
    securities = [
        r[0] for r in c.execute("select distinct security from bars order by security").fetchall()
    ]
    for offset in range(0, len(securities), 50):
        group = securities[offset : offset + 50]
        mismatches += c.execute(
            f"""select count(*) from (
          (select {cols} from bars where security=any(?) except all
           select {cols} from read_parquet(?) where security=any(?))
          union all (select {cols} from read_parquet(?) where security=any(?) except all
           select {cols} from bars where security=any(?)))""",
            [group, str(parquet), group, str(parquet), group, group],
        ).fetchone()[0]
    result["reconciliation_batch_securities"] = 50
    result["earlier_trial_failures"] = [
        "ATTACH parameter parser restriction fixed",
        "unbounded 32.86M-row EXCEPT ALL exceeded 4GB; bounded to 50 securities",
    ]
    assert mismatches == 0
    result["parquet_key_value_mismatches"] = mismatches
    c.close()
    queries = {
        "security_years": "select count(*),sum(volume),sum(turnover),min(low),max(high) from bars where security='SH600519'",
        "market_day": "select security,sum(volume),sum(turnover),min(low),max(high) from bars where timestamp>='2024-07-15' and timestamp<'2024-07-16' group by security",
        "market_month": "select security,sum(volume),sum(turnover),min(low),max(high) from bars where timestamp>='2024-07-01' and timestamp<'2024-08-01' group by security",
    }
    worker = """import duckdb,json,sys,time,resource
path,kind,queries=json.loads(sys.argv[1])
c=duckdb.connect(path,read_only=True) if kind=='native' else duckdb.connect()
c.execute("set threads=2;set memory_limit='4GB'")
if kind=='parquet': c.execute("create view bars as select * from read_parquet('"+path.replace("'","''")+"')")
result={}
for name,sql in queries.items():
 timings=[]
 for i in range(3):
  t=time.monotonic();rows=c.execute(sql).fetchall();timings.append(time.monotonic()-t)
 result[name]={'seconds':timings,'result_rows':len(rows)}
print(json.dumps({'queries':result,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
"""
    result["queries"] = {}
    for name, kind in [
        ("instrument.duckdb", "native"),
        ("date.duckdb", "native"),
        ("instrument.parquet", "parquet"),
    ]:
        proc = subprocess.run(
            [sys.executable, "-c", worker, json.dumps([str(args.root / name), kind, queries])],
            capture_output=True,
            text=True,
            check=True,
        )
        result["queries"][name] = json.loads(proc.stdout)
        result["queries"][name]["bytes"] = (args.root / name).stat().st_size
        print(name, result["queries"][name], flush=True)
    # A process writes new files while old readers continue using the frozen path.
    c = duckdb.connect()
    c.execute("set threads=2;set memory_limit='4GB'")
    c.execute(
        "create view bars as select * from read_parquet('" + str(parquet).replace("'", "''") + "')"
    )
    before = c.execute(queries["security_years"]).fetchall()
    newer = args.root / "revision.parquet"
    t = time.monotonic()
    c.execute(
        "copy (select * replace(close+0.01 as close) from bars where security='SH600519') to ? (format parquet, compression zstd)",
        [str(newer)],
    )
    result["bounded_security_revision_seconds"] = time.monotonic() - t
    result["old_snapshot_unchanged"] = before == c.execute(queries["security_years"]).fetchall()
    result["revision_bytes"] = newer.stat().st_size
    result["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["completed_at"] = time.time()
    result["total_trial_bytes"] = sum(x.stat().st_size for x in args.root.rglob("*") if x.is_file())
    assert result["total_trial_bytes"] < result["limits"]["trial_budget_bytes"]
    emit(args.root / "report.json", result)
    print("COMPLETE", args.root / "report.json", flush=True)


if __name__ == "__main__":
    main()
