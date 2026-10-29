"""Daily, bounded reuse of the existing financial collector and snapshot reader.

Each actual collection day has an immutable batch. Same-day reruns resume it;
later days preserve older versions. This recipe never publishes production data.
"""
import argparse
from datetime import datetime
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

from ashare_data.financials import expansion as source
from ashare_data.financials.expansion_access import SnapshotReader


def prepare_scope(profile, *, today=None):
    config = json.loads(Path(profile).read_text())
    if config.get("schema") != "ad.legacy-financial-scope.v1":
        raise ValueError("unsupported financial profile")
    root = Path(config["root"]).resolve()
    if any((root / name).exists() for name in ("CURRENT", "CURRENT.json", "releases")):
        raise ValueError("production root is forbidden")
    symbols = config["symbols"]
    if not 1 <= len(symbols) <= 10000 or len(set(symbols)) != len(symbols):
        raise ValueError("one to 10000 distinct explicit securities required")
    budget = config.get("max_jobs", min(18, 3 * len(symbols)))
    if not isinstance(budget, int) or isinstance(budget, bool) or not 1 <= budget <= 120:
        raise ValueError("financial batch budget must be 1..120 requests")
    raw = Path(config["source_universe"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != config["source_universe_sha256"]:
        raise ValueError("source universe changed")
    baseline = json.loads(raw)
    instruments = [i for i in baseline["instruments"] if i["symbol"] in symbols]
    if {i["symbol"] for i in instruments} != set(symbols):
        raise ValueError("security not present in the evidenced source directory")
    day = today or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    source.day(day)
    source.day(config["start"])
    if day < config["start"]:
        raise ValueError("report window is reversed")
    root.mkdir(parents=True, exist_ok=True)
    source.immutable(root / "profile.json", source.encoded(config))
    database = root / "scoped-universe.sqlite"
    # This is a small, evidenced directory projection for the old prepare API,
    # not a second copy of any market database or an invented security universe.
    with sqlite3.connect(database) as db:
        for table, rows in (("instruments", instruments), ("code_events", baseline["code_events"])):
            keys = sorted(set().union(*(r.keys() for r in rows))) if rows else ["id"]
            if not all(re.fullmatch(r"[a-z_]+", k) for k in keys):
                raise ValueError("invalid source directory column")
            columns = ",".join('"'+k+'"' for k in keys)
            db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
            existing = [dict(zip(keys, r)) for r in db.execute(f"SELECT {columns} FROM {table}")]
            if existing and existing != [{k: r.get(k) for k in keys} for r in rows]:
                raise ValueError("scoped directory differs from its fixed source")
            if not existing:
                db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in keys)})",
                               [[r.get(k) for k in keys] for r in rows])
        db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT OR IGNORE INTO metadata VALUES (?,?)", baseline["daily_metadata"].items())
    batch = root / "days" / day
    source.prepare(batch, database, config["start"], day, list(source.STATEMENTS))
    return config, batch


def shared_host_state(root, batch):
    """A new collection day must not clear a prior refusal or active cooldown."""
    hosts = {}
    paths = [root / "source-state.json", *sorted((root / "days").glob("*/source-state.json"))]
    for path in paths:
        if not path.exists():
            continue
        for host, state in json.loads(path.read_text()).items():
            prior = hosts.get(host, {})
            merged = dict(state)
            for key in ("cooldown_until", "interval"):
                merged[key] = max(prior.get(key, 0), state.get(key, 0))
            if prior.get("blocked"):
                merged["blocked"] = prior["blocked"]
            hosts[host] = merged
    source.atomic(root / "source-state.json", source.encoded(hosts))
    source.atomic(batch / "source-state.json", source.encoded(hosts))


def _run_profile(profile, *, execute=False, today=None, fetcher=None, max_jobs=None):
    config, batch = prepare_scope(profile, today=today)
    budget = config.get("max_jobs", min(18, 3 * len(config["symbols"])))
    if max_jobs is not None:
        if not isinstance(max_jobs, int) or isinstance(max_jobs, bool) or not 1 <= max_jobs <= budget:
            raise ValueError("override can only reduce the configured request budget")
        budget = max_jobs
    before = source.audit(batch, verify=True)
    if execute:
        shared_host_state(Path(config["root"]).resolve(), batch)
        try:
            source.run(batch, max_jobs=budget, interval=2.0, fetcher=fetcher,
                       stop_paths=[Path(config["root"]) / "STOP"])
        finally:
            shared_host_state(Path(config["root"]).resolve(), batch)
    audit = source.audit(batch, verify=True)
    count = 0
    reader = SnapshotReader(batch)
    temporary = batch / "reports.jsonl.next"
    output = temporary.open("wb") if execute else None
    try:
        for receipt_path in sorted((batch / "receipts").glob("*.json")):
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("state") not in {"complete", "partial_source"}:
                continue
            with gzip.open(batch / receipt["normalized"]["path"], "rt") as stream:
                for line in stream:
                    record = json.loads(line)
                    readback = reader.read(receipt["task_id"], period=record["period_end"], purpose="source_audit")
                    if readback["source_record"] != record:
                        raise ValueError("financial readback differs")
                    count += 1
                    if output:
                        output.write(source.encoded(record))
        if output:
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.replace(temporary, batch / "reports.jsonl")
    finally:
        if output:
            output.close()
        temporary.unlink(missing_ok=True)
    summary = {"batch": str(batch), "symbols": config["symbols"], "execute": execute,
               "reports": count, "readback_verified": bool(count), "max_jobs_this_run": budget,
               "planned_jobs": 3 * len(config["symbols"]),
               "previous_task_states": before["task_states"], "audit": audit,
               "source_announcement_dates_preserved": True, "published_at_invented": False,
               "same_day_checkpoint_resume": True, "older_daily_batches_preserved": True,
               "timer_installed": False, "formal_published": False}
    if execute:
        source.atomic(batch / "result.json", source.encoded(summary))
    return summary


def run_profile(profile, **kwargs):
    root = Path(json.loads(Path(profile).read_text())["root"]).resolve()
    if any((root / name).exists() for name in ("CURRENT", "CURRENT.json", "releases")):
        raise ValueError("production root is forbidden")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "scope.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("financial scope already running") from None
        try:
            return _run_profile(profile, **kwargs)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-jobs", type=int)
    args = parser.parse_args()
    result = run_profile(args.profile, execute=args.execute, max_jobs=args.max_jobs)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not args.execute or result["audit"]["all_tasks_have_source_results"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
