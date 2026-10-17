"""Append-only pre-2010 research observations in the existing local daily library.

The modern calendar and validated-price API are intentionally not extended by
observing a date. This module exposes source observations with explicit quality,
unit and listing-phase limitations, never a synthesized historical price series.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import csv
from datetime import date
import gzip
import hashlib
import json
from pathlib import Path
import threading

from ashare_data.storage.local_daily import DEFAULT_ROOT, LocalDailyStore, digest, parse_day
from ashare_data.providers.daily_sources import (
    PublicHTTP,
    SourceBlocked,
    SourceUnavailable,
    decode_sina,
    sina_daily,
    sina_factors,
    sohu_daily,
    tencent_daily,
    now,
)
from ashare_data.storage.immutable import immutable_write

STAGES = {
    "2000-2009": ("2000-01-01", "2009-12-31"),
    "1992-1999": ("1992-01-01", "1999-12-31"),
    "1990-1991": ("1990-12-01", "1991-12-31"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS early_episodes (
 id TEXT PRIMARY KEY, symbol TEXT, ipo TEXT, out TEXT, body TEXT, receipt TEXT);
CREATE TABLE IF NOT EXISTS early_jobs (
 id TEXT PRIMARY KEY, stage TEXT, requested_source TEXT, capture_path TEXT,
 quality TEXT, replayed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS early_diagnostics (
 symbol TEXT, date TEXT, source TEXT, body TEXT, response_id TEXT,
 quality TEXT, PRIMARY KEY(symbol,date,source)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS early_calendar_observations (
 exchange TEXT, date TEXT, source TEXT, is_session TEXT, receipt TEXT,
 quality TEXT, PRIMARY KEY(exchange,date,source)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS early_evidence (
 id TEXT PRIMARY KEY, kind TEXT, body TEXT, receipt TEXT);
"""
LIMITATIONS = {
    "scope": "retrospective provider observations; not all-market history or PIT",
    "prices": "provider raw series; historical denomination/adjustment not certified",
    "volume": "adapter display convention only; early lot/share basis unverified",
    "calendar": "observed dates retained, including weekends; no fabricated dates",
    "factors": "separate immutable source versions; no automatic adjusted price",
    "baostock": "validation_unknown remains diagnostic, excluded from bars and usable coverage",
}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def csv_rows(path):
    with Path(path).open() as stream:
        return list(csv.DictReader(stream))


def add_job(store, stage, source, symbol, start, end, kind="bars"):
    parse_day(start)
    parse_day(end)
    if start > end or (kind == "bars" and (start < "1990-12-01" or end >= "2010-01-01")):
        raise ValueError("early job outside authorized pre-2010 range")
    job = dict(
        early_version=1, stage=stage, source=source, symbol=symbol, start=start, end=end, kind=kind
    )
    jid = digest(job)
    store.db.execute(
        "INSERT OR IGNORE INTO jobs(id,kind,symbol,start,end,source,updated_at) VALUES(?,?,?,?,?,?,?)",
        (jid, kind, symbol, start, end, "early_" + source, now()),
    )
    store.db.execute(
        "INSERT OR IGNORE INTO early_jobs(id,stage,requested_source) VALUES(?,?,?)",
        (jid, stage, source),
    )
    return jid


def prepare(store, evidence):
    """Replay already captured official metadata; do not refresh modern metadata."""
    store.db.executescript(SCHEMA)
    evidence = Path(evidence)
    candidate = csv_rows(evidence / "candidate_listing_episodes.csv")
    bounds = {
        r["symbol"]: r
        for r in csv_rows(evidence / "estimated_security_days_by_code.csv")
        if r["board"] == "main"
    }
    early_exits = csv_rows(evidence / "official_pre2010_exits.csv")
    if len({r["symbol"] for r in early_exits}) < 64:
        raise ValueError("missing the documented 64 early exits")
    for record in candidate:
        symbol = record["symbol"]
        if symbol not in bounds:
            continue
        record = dict(record, out=record["out"] or None)
        rec = store.db.execute(
            "SELECT * FROM responses WHERE sha256=? LIMIT 1", (record["receipt"],)
        ).fetchone()
        # Existing legacy metadata may reference a native BaoStock capture.
        if (
            rec is None
            and store.db.execute("SELECT 1 FROM instruments WHERE symbol=?", (symbol,)).fetchone()
            is None
        ):
            raise ValueError("unverified new listing receipt: " + symbol)
        if rec:
            raw = gzip.decompress((store.root / "raw" / (rec["sha256"] + ".gz")).read_bytes())
            if hashlib.sha256(raw).hexdigest() != rec["sha256"]:
                raise ValueError("listing raw hash mismatch")
        body = json.dumps(record, ensure_ascii=False)
        store.db.execute(
            "INSERT OR IGNORE INTO early_episodes VALUES(?,?,?,?,?,?)",
            (digest(record), symbol, record["ipo"], record["out"], body, record["receipt"]),
        )
        observed = rec["received_at"] if rec else "2026-10-14T00:00:00+00:00"
        if not store.db.execute("SELECT 1 FROM instruments WHERE symbol=?", (symbol,)).fetchone():
            store.db.execute(
                "INSERT INTO instruments VALUES(?,?,?,?,?,?,?,?)",
                (
                    symbol,
                    record["name"],
                    record["ipo"],
                    record["out"],
                    record["board"],
                    record["source"],
                    observed,
                    record["receipt"],
                ),
            )
            store.db.execute(
                "INSERT OR IGNORE INTO listing_facts VALUES(?,?,?,?,?,?,?)",
                (
                    digest(dict(record, observed_at=observed)),
                    symbol,
                    record["ipo"],
                    record["out"],
                    body,
                    observed,
                    record["receipt"],
                ),
            )
    for symbol, boundary in bounds.items():
        for stage, (start, end) in STAGES.items():
            if boundary["start"] > end or boundary["end_exclusive"] <= start:
                continue
            # Broad requested windows retain source observations outside listing
            # boundaries for diagnostics. They are never counted as eligible days.
            for source in ("sina", "sohu", "baostock"):
                add_job(store, stage, source, symbol, start, end)
        if not store.db.execute(
            "SELECT 1 FROM factor_versions WHERE symbol=?", (symbol,)
        ).fetchone():
            add_job(store, "factors", "sina_hfq", symbol, "1990-01-01", "2009-12-31", "factor")
    # The archived Shanghai calendar is a proxy, not a Shenzhen calendar.
    receipt = store.db.execute(
        "SELECT * FROM responses WHERE url=? ORDER BY received_at DESC LIMIT 1",
        ("https://finance.sina.com.cn/realstock/company/klc_td_sh.txt",),
    ).fetchone()
    if receipt:
        raw = gzip.decompress((store.root / "raw" / (receipt["sha256"] + ".gz")).read_bytes())
        if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("calendar raw hash mismatch")
        days = [d[:10] for d in decode_sina(raw, "datelist") if d[:10] < "2010-01-01"]
        store.db.executemany(
            "INSERT OR IGNORE INTO early_calendar_observations VALUES(?,?,?,?,?,?)",
            [
                (
                    "SH",
                    d,
                    "sina_trading_dates_sh",
                    "1",
                    receipt["id"],
                    "provider_observed_not_exchange_certified",
                )
                for d in days
            ],
        )
    for name in (
        "pre2010_lineage_metadata_conflicts.csv",
        "existing_code_events.json",
        "historical_census_check.json",
    ):
        raw = (evidence / name).read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        immutable_write(store.root / "raw" / (sha + ".gz"), gzip.compress(raw, mtime=0))
        store.db.execute(
            "INSERT OR IGNORE INTO early_evidence VALUES(?,?,?,?)", (sha, name, raw.decode(), sha)
        )
    store.db.commit()
    return dict(
        candidate_symbols=len(bounds),
        early_exits=len(early_exits),
        jobs=store.db.execute("SELECT count(*) FROM early_jobs").fetchone()[0],
    )


class ArchiveHTTP:
    """Replay immutable full-history bytes before making any public request."""

    def __init__(self, store, interval=1.0, network=True):
        self.root = store.root
        self.network = network
        self.http = PublicHTTP(
            store.root / "raw" / "early-transport", interval=interval, timeout=20
        )
        blocks = self.http.root / "source-blocks.json"
        if blocks.exists():
            # Explicitly evidenced source-side blocks persist across stages.
            # Do not rotate hosts/proxies or automatically bypass a WAF challenge.
            for host, item in json.loads(blocks.read_text()).items():
                self.http.blocked[host] = item["reason"]
        self.history = self.http.history
        self.lock = threading.Lock()
        self.saved = {}
        for row in store.db.execute(
            "SELECT * FROM responses WHERE sha256 IS NOT NULL ORDER BY received_at"
        ):
            receipt = {
                k: row[k]
                for k in ("url", "started_at", "received_at", "sha256", "status", "error", "bytes")
                if row[k] is not None
            }
            self.saved[receipt["url"]] = receipt
        self.replays = self.requests = 0

    def get(self, url, referer=None):
        # Only whole-history/whole-factor archives are range-independent.
        reusable = url.endswith(("/klc_kl.js", "/hfq.js"))
        with self.lock:
            saved = self.saved.get(url) if reusable else None
        if saved:
            body = gzip.decompress((self.root / "raw" / (saved["sha256"] + ".gz")).read_bytes())
            if hashlib.sha256(body).hexdigest() != saved["sha256"]:
                raise ValueError("archive replay hash mismatch")
            self.history.receipts.append(saved)
            self.history.replayed = True
            with self.lock:
                self.replays += 1
            return body, [saved]
        if not self.network:
            raise SourceUnavailable("offline: no reusable archive")
        self.history.cache_scope = "early-history-v1"
        body, receipts = self.http.get(url, referer)
        for rec in receipts:
            if rec.get("sha256"):
                source = self.http.root / "raw" / (rec["sha256"] + ".gz")
                immutable_write(self.root / "raw" / source.name, source.read_bytes())
        if reusable:
            with self.lock:
                self.saved[url] = receipts[-1]
        with self.lock:
            if getattr(self.http.history, "cache_hit", False):
                self.replays += 1
                self.history.replayed = True
            else:
                self.requests += 1
        return body, receipts


def fetch(http, job):
    http.history.receipts = []
    http.history.replayed = False
    source = job["requested_source"]
    quarantined = []
    try:
        if source == "sina_hfq":
            rows, receipts = sina_factors(http, job["symbol"])
        elif source == "sohu":
            rows, receipts = sohu_daily(
                http, job["symbol"], job["start"], job["end"], invalid_rows=quarantined
            )
        else:
            function = {"sina": sina_daily, "sohu": sohu_daily, "tencent": tencent_daily}[source]
            rows, receipts = function(http, job["symbol"], job["start"], job["end"])
        return dict(
            rows=rows,
            source=source,
            receipts=receipts,
            errors=[],
            replayed=http.history.replayed,
            quarantined=quarantined,
        )
    except Exception as exc:
        receipts = http.history.receipts + getattr(exc, "receipts", [])
        return dict(
            error=f"{type(exc).__name__}: {exc}",
            blocked=isinstance(exc, SourceBlocked),
            receipts=list({digest(r): r for r in receipts}.values()),
            replayed=http.history.replayed,
        )


def pending(store, stage, sources):
    marks = ",".join("?" for _ in sources)
    # Only this extension's interrupted jobs are resumed. Terminal failures are
    # recorded once; retries need an explicit, bounded repair decision.
    store.db.execute(
        "UPDATE jobs SET state='pending' WHERE state='running' AND id IN "
        "(SELECT id FROM early_jobs WHERE stage=?)",
        (stage,),
    )
    store.db.commit()
    return [
        dict(r)
        for r in store.db.execute(
            "SELECT j.*,e.stage,e.requested_source FROM jobs j JOIN early_jobs e USING(id) "
            f"WHERE e.stage=? AND e.requested_source IN ({marks}) AND j.state='pending' "
            "ORDER BY j.symbol,j.start,e.requested_source",
            (stage, *sources),
        )
    ]


def ingest_http(store, job, result):
    try:
        return _ingest_http(store, job, result)
    except BaseException:
        store.db.rollback()
        raise


def _ingest_http(store, job, result):
    if "error" not in result and job["kind"] == "bars":
        if any(
            not job["start"] <= r["date"] <= job["end"] or r["date"] >= "2010-01-01"
            for r in result["rows"]
        ):
            raise ValueError("refusing early import outside the requested pre-2010 interval")
        # A new run must not replace earlier observations, even when a vendor
        # revises history. The raw response remains available for later review.
        for row in result["rows"]:
            old = store.db.execute(
                "SELECT row_hash FROM bars WHERE symbol=? AND date=? AND source=?",
                (job["symbol"], row["date"], result["source"]),
            ).fetchone()
            if old and old[0] != digest(row):
                raise ValueError("append-only conflict: " + job["symbol"] + "/" + row["date"])
    # Prepare diagnostic and job annotations before ingest's atomic commit. A
    # checkpoint cannot report done while silently losing the quarantined rows.
    ids = store.receipts(result.get("receipts", []))
    for item in result.get("quarantined", []):
        row = item["row"]
        if not job["start"] <= row["date"] <= job["end"] or row["date"] >= "2010-01-01":
            raise ValueError("refusing diagnostic outside early job")
        body = json.dumps(row, ensure_ascii=False)
        old = store.db.execute(
            "SELECT body FROM early_diagnostics WHERE symbol=? AND date=? AND source=?",
            (job["symbol"], row["date"], result["source"]),
        ).fetchone()
        if old and old[0] != body:
            raise ValueError("diagnostic append conflict")
        store.db.execute(
            "INSERT OR IGNORE INTO early_diagnostics VALUES(?,?,?,?,?,?)",
            (
                job["symbol"],
                row["date"],
                result["source"],
                body,
                ids[-1] if ids else None,
                "invalid_source_row:" + item["error"],
            ),
        )
    quality = (
        "http_schema_valid_research_units_unverified"
        if "error" not in result
        else "request_or_parse_failure"
    )
    if result.get("quarantined"):
        quality += ";invalid_rows_quarantined=" + str(len(result["quarantined"]))
    store.db.execute(
        "UPDATE early_jobs SET quality=?,replayed=? WHERE id=?",
        (quality, int(result.get("replayed", False)), job["id"]),
    )
    store.ingest(job, result)


def collect_http(store, stage, sources, workers=3, interval=1.0, network=True, limit=None):
    jobs = pending(store, stage, sources)
    if limit is not None:
        jobs = jobs[:limit]
    http = ArchiveHTTP(store, interval, network)
    iterator, active = iter(jobs), {}
    count = 0
    service_failures, paused_sources = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:

        def submit():
            job = next(iterator, None)
            while job is not None and job["requested_source"] in paused_sources:
                job = next(iterator, None)
            if job is None or (store.root / "STOP_EARLY").exists():
                return
            store.db.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1,updated_at=? WHERE id=?",
                (now(), job["id"]),
            )
            store.db.commit()
            active[pool.submit(fetch, http, job)] = job

        for _ in range(workers):
            submit()
        while active:
            done, _ = wait(active, timeout=20, return_when=FIRST_COMPLETED)
            if not done:
                print(
                    json.dumps(dict(at=now(), stage=stage, finished=count, total=len(jobs))),
                    flush=True,
                )
            for future in done:
                job = active.pop(future)
                result = future.result()
                ingest_http(store, job, result)
                source = job["requested_source"]
                failures = result.get("receipts", [])
                service_error = bool(
                    result.get("error")
                    and failures
                    and all(500 <= r.get("status", 0) < 600 for r in failures)
                )
                service_failures[source] = (
                    service_failures.get(source, 0) + 1 if service_error else 0
                )
                if result.get("blocked") or service_failures[source] >= 3:
                    paused_sources[source] = result.get("error")
                count += 1
                print(
                    json.dumps(
                        dict(
                            at=now(),
                            stage=stage,
                            progress=count,
                            total=len(jobs),
                            symbol=job["symbol"],
                            source=job["requested_source"],
                            rows=len(result.get("rows", [])),
                            error=result.get("error"),
                            quarantined=len(result.get("quarantined", [])),
                            replayed=result.get("replayed"),
                        )
                    ),
                    flush=True,
                )
                submit()
    for source, reason in paused_sources.items():
        store.db.execute(
            "UPDATE jobs SET state='deferred_source',error=?,updated_at=? WHERE state='pending' "
            "AND id IN (SELECT id FROM early_jobs WHERE stage=? AND requested_source=?)",
            ("source circuit paused: " + reason, now(), stage, source),
        )
    store.db.commit()
    return dict(
        finished=count,
        replayed=http.replays,
        network_results=http.requests,
        paused_sources=paused_sources,
    )


def retry_invalid_sohu(store, stage):
    """Bounded replay after introducing per-row quarantine; retain failed ledger evidence."""
    jobs = list(
        store.db.execute(
            "SELECT j.* FROM jobs j JOIN early_jobs e USING(id) WHERE e.stage=? "
            "AND e.requested_source='sohu' AND j.state='failed' AND "
            "(j.error LIKE 'ValueError: negative %' OR j.error LIKE 'ValueError: invalid source OHLC%')",
            (stage,),
        )
    )
    for job in jobs:
        body = json.dumps(dict(job), ensure_ascii=False)
        sha = hashlib.sha256(body.encode()).hexdigest()
        immutable_write(store.root / "raw" / (sha + ".gz"), gzip.compress(body.encode(), mtime=0))
        store.db.execute(
            "INSERT OR IGNORE INTO early_evidence VALUES(?,?,?,?)",
            (sha, "failed_job_before_invalid_row_replay", body, sha),
        )
        store.db.execute(
            "UPDATE jobs SET state='pending',updated_at=? WHERE id=?", (now(), job["id"])
        )
    store.db.commit()
    return dict(requeued=len(jobs))


def observations(store, security, start, end, source=None, diagnostics=False):
    """Explicit source-level research query, without calendar deletion or filling."""
    from ashare_data.market.client import _security

    symbol = _security(security)
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='early_episodes'").fetchone():
        raise ValueError("early history has not been initialized in this library")
    parse_day(start)
    parse_day(end)
    if start > end or start < "1990-12-01" or end >= "2010-01-01":
        raise ValueError("research query requires a pre-2010 interval")
    if source not in (None, "sina", "sohu", "tencent", "baostock"):
        raise ValueError("unsupported early source")
    rows = [
        dict(r)
        for r in store.db.execute(
            "SELECT b.*,r.sha256 raw_sha256 FROM bars b LEFT JOIN responses r ON r.id=b.response_id "
            "WHERE b.symbol=? AND b.date BETWEEN ? AND ?"
            + (" AND b.source=?" if source else "")
            + " ORDER BY b.date,b.source",
            (symbol, start, end, source) if source else (symbol, start, end),
        )
    ]
    for row in rows:
        row["quality"] = "http_schema_valid_research_units_unverified"
        row["strict_usable"] = False
    if diagnostics:
        for item in store.db.execute(
            "SELECT d.*,r.sha256 raw_sha256,r.received_at observed_at FROM early_diagnostics d "
            "LEFT JOIN responses r ON r.id=d.response_id WHERE d.symbol=? AND d.date BETWEEN ? AND ?"
            + (" AND d.source=?" if source else ""),
            (symbol, start, end, source) if source else (symbol, start, end),
        ):
            rows.append(
                dict(
                    json.loads(item["body"]),
                    source=item["source"],
                    symbol=symbol,
                    response_id=item["response_id"],
                    raw_sha256=item["raw_sha256"],
                    observed_at=item["observed_at"],
                    quality=item["quality"],
                    strict_usable=False,
                )
            )
    episodes = [
        dict(r)
        for r in store.db.execute(
            "SELECT ipo,out,body,receipt FROM early_episodes WHERE symbol=?", (symbol,)
        )
    ]
    for row in rows:
        day = row["date"]
        row["weekend_observation"] = date.fromisoformat(day).weekday() >= 5
        row["listing_phase"] = (
            "within_candidate_episode"
            if any(r["ipo"] <= day and (not r["out"] or day < r["out"]) for r in episodes)
            else "outside_candidate_episode_unknown"
        )
        row["unit_convention"] = {
            "sina": "decoded values unchanged; original early share/denomination basis unknown",
            "sohu": "display lots x100; display ten-thousand CNY x10000; historical basis unverified",
            "tencent": "display lots x100; historical lot/share basis unverified",
            "baostock": "provider-declared CNY/shares/CNY; outer integrity and early basis unverified",
        }[row["source"]]
    return dict(
        symbol=symbol,
        start=start,
        end=end,
        limitations=LIMITATIONS,
        episodes=episodes,
        rows=sorted(rows, key=lambda r: (r["date"], r["source"])),
    )


def plan_tencent(store, stage):
    """Request every proxy-gap year plus the early Shenzhen calendar-risk years.

    A proxy-gap is a request-selection heuristic, not a claim of a missing trade.
    BaoStock diagnostic rows cannot make a gap disappear here.
    """
    start, end = STAGES[stage]
    calendar = [
        r[0]
        for r in store.db.execute(
            "SELECT date FROM early_calendar_observations WHERE exchange='SH' AND source='sina_trading_dates_sh' "
            "AND date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        )
    ]
    before = store.db.execute("SELECT count(*) FROM early_jobs").fetchone()[0]
    for (symbol,) in store.db.execute(
        "SELECT DISTINCT symbol FROM early_episodes ORDER BY symbol"
    ).fetchall():
        episodes = store.db.execute(
            "SELECT ipo,out FROM early_episodes WHERE symbol=?", (symbol,)
        ).fetchall()
        if not any(ipo <= end and (not out or start < out) for ipo, out in episodes):
            continue
        seen = {
            r[0]
            for r in store.db.execute(
                "SELECT DISTINCT date FROM bars WHERE symbol=? AND date BETWEEN ? AND ?",
                (symbol, start, end),
            )
        }
        years = {
            day[:4]
            for day in calendar
            if day not in seen
            and any(ipo <= day and (not out or day < out) for ipo, out in episodes)
        }
        if stage == "1990-1991":
            years.update(("1990", "1991"))
        if (
            stage == "1992-1999"
            and symbol.startswith("sz")
            and any(ipo < "1994-01-01" for ipo, _ in episodes)
        ):
            years.update(("1992", "1993"))
        for year in sorted(years):
            add_job(
                store,
                stage,
                "tencent",
                symbol,
                max(start, year + "-01-01"),
                min(end, year + "-12-31"),
            )
    # Suppliers may store the pre-1992 Yuyuan episode under another code; querying
    # this current supplier key is only a boundary probe, not a verified alias.
    if stage == "1990-1991":
        for year in ("1990", "1991"):
            add_job(
                store, stage, "tencent", "sh600655", max(start, year + "-01-01"), year + "-12-31"
            )
    store.db.commit()
    return dict(added=store.db.execute("SELECT count(*) FROM early_jobs").fetchone()[0] - before)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "http", "query", "plan-tencent", "retry-invalid-sohu")
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--stage", choices=(*STAGES, "factors"), default="2000-2009")
    parser.add_argument("--sources", default="sina,sohu")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--symbol")
    parser.add_argument("--start", default="1990-12-01")
    parser.add_argument("--end", default="2009-12-31")
    parser.add_argument("--source")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    store = LocalDailyStore(args.root, readonly=args.action == "query")
    try:
        if args.action == "query":
            result = observations(
                store, args.symbol, args.start, args.end, args.source, args.diagnostics
            )
        else:
            with store.writer():
                if args.action == "prepare":
                    result = prepare(store, args.evidence)
                elif args.action == "plan-tencent":
                    result = plan_tencent(store, args.stage)
                elif args.action == "retry-invalid-sohu":
                    result = retry_invalid_sohu(store, args.stage)
                else:
                    sources = args.sources.split(",")
                    if not sources or any(
                        s not in ("sina", "sohu", "tencent", "sina_hfq") for s in sources
                    ):
                        raise ValueError("unsupported HTTP source")
                    result = collect_http(
                        store,
                        args.stage,
                        sources,
                        args.workers,
                        args.interval,
                        not args.offline,
                        args.limit,
                    )
        if args.output:
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
