"""Mutable, resumable local daily library, separate from immutable Store publications.

SQLite WAL provides concurrent readers and one transactional writer. Raw response
objects and prior changed row versions remain available for provenance.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
import csv
from datetime import date, datetime, timedelta
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .live import _security
from .model import canonical
from .local_sources import HTTP_JOB_SOURCES, PublicHTTP, calendar, exchange_universe, fetch_job, now

DEFAULT_ROOT = Path.home() / "Workspace" / "ashare-data-local"
BAR_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "preclose",
    "tradestatus",
    "is_st",
)
HTTP_JOB_PREDICATE = (
    "(source IS NULL OR source IN (" + ",".join("?" for _ in HTTP_JOB_SOURCES) + "))"
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS responses (
 id TEXT PRIMARY KEY, url TEXT, started_at TEXT, received_at TEXT, sha256 TEXT,
 status INTEGER, error TEXT, bytes INTEGER);
CREATE TABLE IF NOT EXISTS instruments (
 symbol TEXT PRIMARY KEY, name TEXT, ipo TEXT, out TEXT, board TEXT,
 metadata_source TEXT, observed_at TEXT, receipt TEXT);
CREATE TABLE IF NOT EXISTS listing_facts (
 id TEXT PRIMARY KEY, symbol TEXT, ipo TEXT, out TEXT, body TEXT, observed_at TEXT, receipt TEXT);
CREATE INDEX IF NOT EXISTS listing_facts_symbol ON listing_facts(symbol,ipo,observed_at);
CREATE TABLE IF NOT EXISTS code_events (
 id TEXT PRIMARY KEY,old_symbol TEXT,new_symbol TEXT,effective_date TEXT,
 body TEXT,observed_at TEXT,receipt TEXT);
CREATE TABLE IF NOT EXISTS calendar (date TEXT PRIMARY KEY, source TEXT, receipt TEXT);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT, symbol TEXT, start TEXT, end TEXT,
 state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, updated_at TEXT,
 source TEXT, nrows INTEGER, error TEXT, response_ids TEXT);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state,kind,symbol,start);
CREATE TABLE IF NOT EXISTS bars (
 symbol TEXT, date TEXT, source TEXT, open TEXT, high TEXT, low TEXT, close TEXT,
 volume TEXT, amount TEXT, preclose TEXT, tradestatus TEXT, is_st TEXT,
 row_hash TEXT, response_id TEXT, observed_at TEXT,
 PRIMARY KEY(symbol,date,source)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bar_revisions (
 symbol TEXT, date TEXT, source TEXT, row_hash TEXT, body TEXT,
 response_id TEXT, observed_at TEXT, PRIMARY KEY(symbol,date,source,row_hash)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS factor_versions (
 id TEXT PRIMARY KEY, symbol TEXT, source TEXT, first_observed_at TEXT,
 last_observed_at TEXT, response_id TEXT, nrows INTEGER, basis TEXT);
CREATE TABLE IF NOT EXISTS factors (
 version TEXT, date TEXT, multiplier TEXT, PRIMARY KEY(version,date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS factor_observations (
 response_id TEXT PRIMARY KEY, version TEXT, observed_at TEXT);
CREATE TABLE IF NOT EXISTS gaps (
 symbol TEXT, start TEXT, end TEXT, trading_days INTEGER, kind TEXT,
 PRIMARY KEY(symbol,start,end,kind)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS coverage (
 symbol TEXT PRIMARY KEY, start TEXT, end TEXT, expected_days INTEGER, observed_days INTEGER,
 missing_days INTEGER, first_bar TEXT, last_bar TEXT, amount_missing INTEGER,
 explicit_suspended INTEGER, zero_volume_unknown INTEGER, factor_versions INTEGER,
 status TEXT, updated_at TEXT);
"""


def digest(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def parse_day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


def target_end():
    local = datetime.now(ZoneInfo("Asia/Shanghai"))
    # End-of-day sources may publish after close. Do not call a partial session complete.
    return (local.date() - timedelta(days=local.hour < 18)).isoformat()


def contiguous(days, calendar_days):
    indexes = {d: i for i, d in enumerate(calendar_days)}
    groups = []
    for day in sorted(days):
        if groups and indexes[day] == indexes[groups[-1][-1]] + 1:
            groups[-1].append(day)
        else:
            groups.append([day])
    return groups


class LocalDailyStore:
    def __init__(self, root=DEFAULT_ROOT, readonly=False):
        self.root = Path(root).expanduser().resolve()
        if readonly:
            self.db = sqlite3.connect((self.root / "daily.sqlite3").as_uri() + "?mode=ro", uri=True)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.root / "daily.sqlite3")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(SCHEMA)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self._code_events = None

    def close(self):
        self.db.close()

    @contextmanager
    def writer(self):
        with (self.root / "writer.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another local-daily writer is active") from exc
            yield

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def receipts(self, receipts):
        ids = []
        for receipt in receipts:
            rid = digest(receipt)
            ids.append(rid)
            self.db.execute(
                "INSERT OR IGNORE INTO responses VALUES (?,?,?,?,?,?,?,?)",
                (
                    rid,
                    *[
                        receipt.get(k)
                        for k in (
                            "url",
                            "started_at",
                            "received_at",
                            "sha256",
                            "status",
                            "error",
                            "bytes",
                        )
                    ],
                ),
            )
        return ids

    def save_universe(self, records, receipts, start, end):
        self.receipts(receipts)
        received = {r.get("sha256"): r.get("received_at") for r in receipts}
        seen = {}
        observed_times = {}
        for record in records:
            symbol, ipo, out = record["symbol"], record["ipo"], record["out"]
            parse_day(ipo)
            if out and parse_day(out) < parse_day(ipo):
                raise ValueError("termination before IPO: " + symbol)
            if ipo > end or (out and out <= start):
                continue
            # Retain multiple episodes (e.g. a terminated security relisted later).
            fact_observed = record.get("observed_at") or received.get(record["receipt"]) or now()
            fid = digest(dict(record, observed_at=fact_observed))
            self.db.execute(
                "INSERT OR IGNORE INTO listing_facts VALUES (?,?,?,?,?,?,?)",
                (
                    fid,
                    symbol,
                    ipo,
                    out,
                    json.dumps(record, ensure_ascii=False),
                    fact_observed,
                    record["receipt"],
                ),
            )
            old = seen.get(symbol)
            if old and old != record:
                # Current listing wins the display record; facts retain both episodes.
                if old["out"] is None or (out is not None and old["ipo"] >= ipo):
                    continue
            seen[symbol] = record
            observed_times[symbol] = fact_observed
        for symbol, record in seen.items():
            old = self.db.execute(
                "SELECT ipo,metadata_source,observed_at FROM instruments WHERE symbol=?", (symbol,)
            ).fetchone()
            if old:
                previous_priority = old["metadata_source"] == "issuer_exit_notice"
                current_priority = record["source"] == "issuer_exit_notice"
                # A reviewed issuer correction applies only to the same listing
                # episode. A later IPO remains a separate fact and display record.
                if old["ipo"] == record["ipo"] and previous_priority != current_priority:
                    if previous_priority:
                        continue
                elif old["observed_at"] > observed_times[symbol]:
                    continue
            self.db.execute(
                "INSERT OR REPLACE INTO instruments VALUES (?,?,?,?,?,?,?,?)",
                (
                    symbol,
                    record["name"],
                    record["ipo"],
                    record["out"],
                    record["board"],
                    record["source"],
                    observed_times[symbol],
                    record["receipt"],
                ),
            )
        if receipts or not self.meta("universe_observed_at"):
            self.set_meta("universe_observed_at", now())
        self.set_meta(
            "universe_definition",
            "Official SSE main A + SZSE 00xxxx current/terminated lineage, supplemented by observed BaoStock legacy codes/memberships when available; "
            "SME 002xxx/003xxx before 2021-04-06 excluded from main-board-at-date queries; retrospective facts, names/PIT not certified",
        )
        self.db.commit()

    def save_calendar(self, days, receipts):
        ids = self.receipts(receipts)
        self.db.executemany(
            "INSERT OR REPLACE INTO calendar VALUES (?,?,?)",
            [(d, "sina_trading_dates_sh", ids[-1]) for d in days],
        )
        self.set_meta(
            "calendar_scope",
            "Sina Shanghai trading-date list used as common SH/SZ session calendar; "
            "not independently exchange-certified; closed days are not bar observations",
        )
        self.db.commit()

    def code_events(self):
        if self._code_events is None:
            exists = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='code_events'"
            ).fetchone()
            self._code_events = (
                [
                    json.loads(r[0])
                    for r in self.db.execute(
                        "SELECT body FROM (SELECT *,row_number() OVER (PARTITION BY old_symbol,new_symbol ORDER BY observed_at DESC,id DESC) AS rank FROM code_events) WHERE rank=1"
                    )
                ]
                if exists
                else []
            )
        return self._code_events

    def save_code_events(self, events, receipts):
        self.receipts(receipts)
        received = {r["sha256"]: r["received_at"] for r in receipts}
        additions = []
        for event in events:
            parse_day(event["effective_date"])
            observed = received.get(event["receipt"])
            if observed is None:
                previous = self.db.execute(
                    "SELECT observed_at FROM code_events WHERE id=?", (digest(event),)
                ).fetchone()
                if previous is None:
                    raise ValueError("new code event requires a verified receipt")
                observed = previous["observed_at"]
            self.db.execute(
                "INSERT OR IGNORE INTO code_events VALUES (?,?,?,?,?,?,?)",
                (
                    digest(event),
                    event["old_symbol"],
                    event["new_symbol"],
                    event["effective_date"],
                    json.dumps(event, ensure_ascii=False),
                    observed,
                    event["receipt"],
                ),
            )
            if not self.db.execute(
                "SELECT 1 FROM instruments WHERE symbol=?", (event["old_symbol"],)
            ).fetchone():
                additions.append(
                    dict(
                        symbol=event["old_symbol"],
                        name=event["old_name"],
                        ipo=event["issuer_ipo"],
                        out=event["effective_date"],
                        board="main",
                        source="issuer_code_change",
                        receipt=event["receipt"],
                        observed_at=observed,
                        out_inclusive=False,
                    )
                )
        self._code_events = None
        if additions:
            self.save_universe(additions, [], self.meta("start"), self.meta("end"))
        self.db.commit()

    def code_bounds(self, symbol):
        events = self.code_events()
        begins = [e["effective_date"] for e in events if e["new_symbol"] == symbol]
        ends = [e["effective_date"] for e in events if e["old_symbol"] == symbol]
        return max(begins) if begins else None, min(ends) if ends else None

    def announced_suspensions(self, symbol, days):
        return {
            d
            for e in self.code_events()
            if symbol in (e["old_symbol"], e["new_symbol"]) and e.get("halt_start")
            for d in days
            if e["halt_start"] <= d <= e["halt_end"]
        }

    def expected(self, symbol, start, end, days):
        facts = self.db.execute(
            "SELECT ipo,out,coalesce(json_extract(body,'$.out_inclusive'),0) AS inclusive FROM "
            "(SELECT *,row_number() OVER (PARTITION BY ipo ORDER BY "
            "(json_extract(body,'$.source')='issuer_exit_notice') DESC,observed_at DESC,id DESC) AS rank "
            "FROM listing_facts WHERE symbol=?) WHERE rank=1",
            (symbol,),
        ).fetchall()
        code_start, code_end = self.code_bounds(symbol)
        return [
            d
            for d in days
            if start <= d <= end
            and (not code_start or d >= code_start)
            and (not code_end or d < code_end)
            and any(
                f["ipo"] <= d
                and (not f["out"] or d < f["out"] or (f["inclusive"] and d == f["out"]))
                for f in facts
            )
        ]

    def add_job(self, kind, symbol, start, end):
        body = dict(kind=kind, symbol=symbol, start=start, end=end)
        self.db.execute(
            "INSERT OR IGNORE INTO jobs (id,kind,symbol,start,end,updated_at) VALUES (?,?,?,?,?,?)",
            (digest(body), kind, symbol, start, end, now()),
        )

    def plan(self, start, end, repair=False, retry_failed=False, symbols=None):
        parse_day(start)
        parse_day(end)
        if start > end:
            raise ValueError("start after end")
        selection = (
            "" if symbols is None else " AND symbol IN (" + ",".join("?" for _ in symbols) + ")"
        )
        prior_start, prior_end = self.meta("start"), self.meta("end")
        self.set_meta("start", min(start, prior_start or start))
        self.set_meta("end", max(end, prior_end or end))
        days = [
            r[0]
            for r in self.db.execute(
                "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
            )
        ]
        if not days:
            raise ValueError("no source calendar for requested scope")
        for instrument in self.db.execute(
            "SELECT * FROM instruments ORDER BY out IS NULL,symbol"
        ).fetchall():
            symbol = instrument["symbol"]
            if symbols is not None and symbol not in symbols:
                continue
            expected = self.expected(symbol, start, end, days)
            if not expected:
                continue
            existing = self.db.execute(
                "SELECT start,end FROM jobs WHERE symbol=? AND kind='bars'", (symbol,)
            ).fetchall()
            if repair:
                observed = {
                    r[0]
                    for r in self.db.execute(
                        "SELECT DISTINCT date FROM bars WHERE symbol=?", (symbol,)
                    )
                }
                needed = [d for d in expected if d not in observed]
            else:
                needed = [
                    d for d in expected if not any(j["start"] <= d <= j["end"] for j in existing)
                ]
            for group in contiguous(needed, days):
                # Whole history is one bounded HTTP request; provider truncation is exposed by gaps.
                self.add_job("bars", symbol, group[0], group[-1])
            self.add_job("factor", symbol, "1990-01-01", end)
        for source in self.meta("supplement_sources", []):
            self.plan_source(source, start, end, symbols=symbols)
        if retry_failed or repair:
            self.db.execute(
                "UPDATE jobs SET state='pending',attempts=0 WHERE state IN ('failed','blocked','empty_unknown')"
                + " AND "
                + HTTP_JOB_PREDICATE
                + selection,
                (*HTTP_JOB_SOURCES, *(symbols or [])),
            )
        # Only the process holding the writer lock invokes plan; interrupted jobs are safe to resume.
        self.db.execute(
            "UPDATE jobs SET state='pending' WHERE state='running' AND "
            + HTTP_JOB_PREDICATE
            + selection,
            (*HTTP_JOB_SOURCES, *(symbols or [])),
        )
        self.db.commit()

    def plan_source(self, source, start, end, symbols=None):
        if source not in {"sohu", "tencent"}:
            raise ValueError("unsupported HTTP supplement source")
        days = [
            r[0]
            for r in self.db.execute(
                "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
            )
        ]
        for (symbol,) in self.db.execute(
            "SELECT symbol FROM instruments ORDER BY symbol"
        ).fetchall():
            if symbols is not None and symbol not in symbols:
                continue
            requested = self.db.execute(
                "SELECT start,end FROM jobs WHERE symbol=? AND kind='bars' AND source=?",
                (symbol, source),
            ).fetchall()
            needed = [
                d
                for d in self.expected(symbol, start, end, days)
                if not any(a <= d <= b for a, b in requested)
            ]
            groups = contiguous(needed, days)
            if source == "tencent":
                # Each job has one raw response and its own provenance. The API's
                # 640-row limit cannot truncate a single calendar year.
                groups = [
                    [d for d in group if d[:4] == year]
                    for group in groups
                    for year in sorted({d[:4] for d in group})
                ]
            for group in groups:
                job = dict(supplement=source, symbol=symbol, start=group[0], end=group[-1])
                self.db.execute(
                    "INSERT OR IGNORE INTO jobs (id,kind,symbol,start,end,source,updated_at) VALUES (?,'bars',?,?,?,?,?)",
                    (digest(job), symbol, group[0], group[-1], source, now()),
                )

    def ingest(self, job, result):
        """Records, lineage and job completion commit together; duplicates do not multiply."""
        try:
            ids = self.receipts(result.get("receipts", []))
            if "error" in result:
                self.db.execute(
                    "UPDATE jobs SET state=?,error=?,response_ids=?,updated_at=? WHERE id=?",
                    (
                        "blocked" if result.get("blocked") else "failed",
                        result["error"],
                        json.dumps(ids),
                        now(),
                        job["id"],
                    ),
                )
                self.db.commit()
                return
            rows, source = result["rows"], result["source"]
            rid = ids[-1] if ids else None
            received = [
                r.get("received_at")
                for r in result.get("receipts", [])
                if r.get("sha256") and r.get("received_at")
            ]
            observed = received[-1] if received else now()
            if job["kind"] == "bars":
                for row in rows:
                    rh = digest(row)
                    old = self.db.execute(
                        "SELECT * FROM bars WHERE symbol=? AND date=? AND source=?",
                        (job["symbol"], row["date"], source),
                    ).fetchone()
                    if old and old["row_hash"] == rh:
                        continue
                    if old and datetime.fromisoformat(old["observed_at"]) > datetime.fromisoformat(
                        observed
                    ):
                        incoming = dict(
                            row,
                            symbol=job["symbol"],
                            source=source,
                            row_hash=rh,
                            response_id=rid,
                            observed_at=observed,
                        )
                        self.db.execute(
                            "INSERT OR IGNORE INTO bar_revisions VALUES (?,?,?,?,?,?,?)",
                            (
                                job["symbol"],
                                row["date"],
                                source,
                                rh,
                                json.dumps(incoming),
                                rid,
                                observed,
                            ),
                        )
                        continue
                    if old:
                        old = dict(old)
                        self.db.execute(
                            "INSERT OR IGNORE INTO bar_revisions VALUES (?,?,?,?,?,?,?)",
                            (
                                old["symbol"],
                                old["date"],
                                source,
                                old["row_hash"],
                                json.dumps(old),
                                old["response_id"],
                                old["observed_at"],
                            ),
                        )
                    self.db.execute(
                        "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            job["symbol"],
                            row["date"],
                            source,
                            *[row.get(k) for k in BAR_FIELDS],
                            rh,
                            rid,
                            observed,
                        ),
                    )
            else:
                version = digest(dict(symbol=job["symbol"], source=source, rows=rows))
                self.db.execute(
                    "INSERT OR IGNORE INTO factor_versions VALUES (?,?,?,?,?,?,?,?)",
                    (
                        version,
                        job["symbol"],
                        source,
                        observed,
                        observed,
                        rid,
                        len(rows),
                        "Sina hfq: raw price multiplied by effective-date factor; full source snapshot; retrospective",
                    ),
                )
                self.db.execute(
                    "UPDATE factor_versions SET first_observed_at=min(first_observed_at,?),last_observed_at=max(last_observed_at,?) WHERE id=?",
                    (observed, observed, version),
                )
                self.db.executemany(
                    "INSERT OR IGNORE INTO factors VALUES (?,?,?)",
                    [(version, row["date"], row["factor"]) for row in rows],
                )
                if rid:
                    self.db.execute(
                        "INSERT OR IGNORE INTO factor_observations VALUES (?,?,?)",
                        (rid, version, observed),
                    )
            self.db.execute(
                "UPDATE jobs SET state=?,source=?,nrows=?,error=?,response_ids=?,updated_at=? WHERE id=?",
                (
                    "done" if rows else "empty_unknown",
                    source,
                    len(rows),
                    "; ".join(result.get("errors", [])) or None,
                    json.dumps(ids),
                    now(),
                    job["id"],
                ),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def run(self, http, workers=3, limit=None, only_source=None, symbols=None):
        selection = (
            "" if symbols is None else " AND j.symbol IN (" + ",".join("?" for _ in symbols) + ")"
        )
        pending = [
            dict(r)
            for r in self.db.execute(
                "SELECT j.* FROM jobs j JOIN instruments i ON i.symbol=j.symbol "
                "WHERE state='pending' AND (? IS NULL OR j.source=?)"
                + " AND "
                + HTTP_JOB_PREDICATE
                + selection
                + " ORDER BY i.out IS NULL,j.symbol,j.kind",
                (only_source, only_source, *HTTP_JOB_SOURCES, *(symbols or [])),
            )
        ]
        if limit is not None:
            pending = pending[:limit]
        total, finished = len(pending), 0
        report_every = max(500, total // 10)
        iterator = iter(pending)
        active = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:

            def submit():
                job = next(iterator, None)
                if job is None or (self.root / "STOP").exists():
                    return False
                # One full-history source snapshot can satisfy multiple missing
                # intervals in the same maintenance horizon without refetching it.
                job["cache_scope"] = self.meta("end")
                self.db.execute(
                    "UPDATE jobs SET state='running',attempts=attempts+1,updated_at=? WHERE id=?",
                    (now(), job["id"]),
                )
                self.db.commit()
                active[pool.submit(fetch_job, http, job)] = job
                return True

            for _ in range(workers):
                submit()
            while active:
                completed, _ = wait(active, timeout=20, return_when=FIRST_COMPLETED)
                if not completed:
                    print(
                        json.dumps(
                            dict(at=now(), progress=finished, total=total, active=len(active))
                        ),
                        flush=True,
                    )
                for future in completed:
                    job = active.pop(future)
                    result = future.result()
                    self.ingest(job, result)
                    finished += 1
                    if finished % report_every == 0:
                        self.report()
                    print(
                        json.dumps(
                            dict(
                                at=now(),
                                progress=finished,
                                total=total,
                                symbol=job["symbol"],
                                kind=job["kind"],
                                rows=len(result.get("rows", [])),
                                source=result.get("source"),
                                error=result.get("error"),
                            ),
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    submit()
        return self.summary()

    def summary(self):
        owns_snapshot = not self.db.in_transaction
        if owns_snapshot:
            self.db.execute("BEGIN")
        try:
            query = self.db.execute
            return dict(
                root=str(self.root),
                database=str(self.root / "daily.sqlite3"),
                at=now(),
                start=self.meta("start"),
                end=self.meta("end"),
                securities=query("SELECT count(*) FROM instruments").fetchone()[0],
                delisted_securities=query(
                    "SELECT count(*) FROM instruments WHERE out IS NOT NULL"
                ).fetchone()[0]
                - sum(
                    bool(
                        query(
                            "SELECT 1 FROM instruments WHERE symbol=? AND out IS NOT NULL",
                            (e["old_symbol"],),
                        ).fetchone()
                    )
                    for e in self.code_events()
                ),
                retired_codes=query(
                    "SELECT count(*) FROM instruments WHERE out IS NOT NULL"
                ).fetchone()[0],
                verified_code_changes=len(self.code_events()),
                bar_rows=query("SELECT count(*) FROM bars").fetchone()[0],
                unique_security_days=query(
                    "SELECT count(*) FROM (SELECT symbol,date FROM bars GROUP BY symbol,date)"
                ).fetchone()[0],
                securities_with_bars=query("SELECT count(DISTINCT symbol) FROM bars").fetchone()[0],
                factor_rows=query("SELECT count(*) FROM factors").fetchone()[0],
                securities_with_factors=query(
                    "SELECT count(DISTINCT symbol) FROM factor_versions WHERE nrows>0"
                ).fetchone()[0],
                bar_revisions=query("SELECT count(*) FROM bar_revisions").fetchone()[0],
                jobs=[
                    dict(r)
                    for r in query(
                        "SELECT kind,state,count(*) AS count FROM jobs GROUP BY kind,state"
                    )
                ],
                sources=[
                    dict(r)
                    for r in query(
                        "SELECT source,count(*) AS rows,min(date) AS start,max(date) AS end FROM bars GROUP BY source"
                    )
                ],
                database_bytes=(self.root / "daily.sqlite3").stat().st_size,
            )
        finally:
            if owns_snapshot:
                self.db.rollback()

    def report(self):
        start, end = self.meta("start"), self.meta("end")
        days = [
            r[0]
            for r in self.db.execute(
                "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
            )
        ]
        day_set = set(days)
        self.db.execute("DELETE FROM gaps")
        self.db.execute("DELETE FROM coverage")
        boundary_rows = []
        main_coverage, main_gaps = [], []
        code_exits = {e["old_symbol"] for e in self.code_events()}
        for item in self.db.execute("SELECT * FROM instruments ORDER BY symbol").fetchall():
            symbol = item["symbol"]
            expected = self.expected(symbol, start, end, days)
            rows = self.db.execute(
                "SELECT date,source,amount,tradestatus,volume FROM bars WHERE symbol=? AND date BETWEEN ? AND ?",
                (symbol, start, end),
            ).fetchall()
            expected_set = set(expected)
            boundary_rows.extend(
                dict(
                    symbol=symbol,
                    **dict(r),
                    reason="outside_source_calendar"
                    if r["date"] not in day_set
                    else "outside_listing_or_code_bounds",
                )
                for r in rows
                if r["date"] not in expected_set
            )
            rows = [r for r in rows if r["date"] in expected_set]
            observed = {r["date"] for r in rows}
            missing = set(expected) - observed
            announced = missing & self.announced_suspensions(symbol, expected)
            main_days = {
                d
                for d in expected
                if not symbol.startswith(("sz002", "sz003")) or d >= "2021-04-06"
            }
            main_coverage.append(
                dict(
                    symbol=symbol,
                    expected_days=len(main_days),
                    observed_days=len(main_days & observed),
                    missing_days=len(main_days - observed),
                    amount_missing=len(
                        (main_days & observed)
                        - {r["date"] for r in rows if r["amount"] is not None}
                    ),
                    delisted=bool(item["out"] and symbol not in code_exits),
                )
            )
            pending = self.db.execute(
                "SELECT start,end FROM jobs WHERE symbol=? AND kind='bars' AND state IN ('pending','running')",
                (symbol,),
            ).fetchall()
            awaiting = {d for d in missing if any(r["start"] <= d <= r["end"] for r in pending)}
            for kind, dates in (
                ("awaiting_collection", awaiting),
                ("announced_suspension_without_bar", announced - awaiting),
                ("missing_unknown_not_proven_suspended", missing - awaiting - announced),
            ):
                for group in contiguous(dates, days):
                    self.db.execute(
                        "INSERT INTO gaps VALUES (?,?,?,?,?)",
                        (symbol, group[0], group[-1], len(group), kind),
                    )
                for group in contiguous(set(dates) & main_days, days):
                    main_gaps.append(
                        dict(
                            symbol=symbol,
                            start=group[0],
                            end=group[-1],
                            trading_days=len(group),
                            kind=kind,
                        )
                    )
            factor_count = self.db.execute(
                "SELECT count(*) FROM factor_versions WHERE symbol=? AND nrows>0", (symbol,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO coverage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    symbol,
                    expected[0] if expected else None,
                    expected[-1] if expected else None,
                    len(expected),
                    len(observed & set(expected)),
                    len(missing),
                    min(observed) if observed else None,
                    max(observed) if observed else None,
                    len(observed - {r["date"] for r in rows if r["amount"] is not None}),
                    len(
                        {r["date"] for r in rows if r["tradestatus"] == "0"}
                        | self.announced_suspensions(symbol, expected)
                    ),
                    len(
                        {
                            r["date"]
                            for r in rows
                            if r["volume"] in ("0", "0.0") and r["tradestatus"] is None
                        }
                        - {r["date"] for r in rows if r["tradestatus"] is not None}
                    ),
                    factor_count,
                    "awaiting_collection"
                    if awaiting
                    else "has_gaps"
                    if missing
                    else "observed_calendar_coverage",
                    now(),
                ),
            )
        self.db.commit()
        reports = self.root / "reports"
        reports.mkdir(exist_ok=True)
        for filename, data, fields in (
            (
                "main-board-coverage.csv",
                main_coverage,
                [
                    "symbol",
                    "expected_days",
                    "observed_days",
                    "missing_days",
                    "amount_missing",
                    "delisted",
                ],
            ),
            ("main-board-gaps.csv", main_gaps, ["symbol", "start", "end", "trading_days", "kind"]),
        ):
            with (reports / filename).open("w") as output:
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                writer.writerows(data)
        with (reports / "source-boundary-observations.csv").open("w") as output:
            writer = csv.DictWriter(
                output,
                fieldnames=[
                    "symbol",
                    "date",
                    "source",
                    "amount",
                    "tradestatus",
                    "volume",
                    "reason",
                ],
            )
            writer.writeheader()
            writer.writerows(boundary_rows)
        tables = ["coverage", "gaps", "instruments", "jobs"]
        for optional in ("metadata_conflicts", "source_captures", "code_events"):
            if self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (optional,)
            ).fetchone():
                tables.append(optional)
        for table in tables:
            cursor = self.db.execute("SELECT * FROM " + table)
            with (reports / (table + ".csv")).open("w") as output:
                writer = csv.writer(output)
                writer.writerow([d[0] for d in cursor.description])
                writer.writerows(cursor)
        membership_boundaries = []
        if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe_membership'"
        ).fetchone():
            candidates = self.db.execute(
                "SELECT m.date,m.symbol,m.trade_status,m.capture_id,i.ipo,i.out FROM universe_membership m "
                "JOIN instruments i USING(symbol) WHERE m.date<i.ipo OR (i.out IS NOT NULL AND m.date>=i.out) "
                "OR m.symbol IN (SELECT new_symbol FROM code_events)"
            ).fetchall()
            membership_boundaries = [
                dict(r)
                for r in candidates
                if not self.expected(r["symbol"], r["date"], r["date"], [r["date"]])
            ]
        with (reports / "membership-boundary-observations.csv").open("w") as output:
            writer = csv.DictWriter(
                output, fieldnames=["date", "symbol", "trade_status", "capture_id", "ipo", "out"]
            )
            writer.writeheader()
            writer.writerows(membership_boundaries)
        result = self.summary()
        result["coverage"] = dict(
            self.db.execute(
                "SELECT sum(expected_days) AS expected_security_days, "
                "sum(observed_days) AS observed_security_days,sum(missing_days) AS missing_security_days,"
                "sum(amount_missing) AS amount_missing_rows,"
                'sum(status="observed_calendar_coverage") AS symbols_without_calendar_gaps FROM coverage'
            ).fetchone()
        )
        result["gap_classes"] = [
            dict(r)
            for r in self.db.execute(
                "SELECT kind,sum(trading_days) AS security_days,count(*) AS intervals FROM gaps GROUP BY kind"
            )
        ]
        result["main_board_at_date"] = dict(
            definition="Only main-board sessions; former SME 002/003 begins 2021-04-06; verified code boundaries applied",
            securities=sum(r["expected_days"] > 0 for r in main_coverage),
            delisted_securities=sum(
                r["expected_days"] > 0 and r["delisted"] for r in main_coverage
            ),
            expected_security_days=sum(r["expected_days"] for r in main_coverage),
            observed_security_days=sum(r["observed_days"] for r in main_coverage),
            missing_security_days=sum(r["missing_days"] for r in main_coverage),
            amount_missing_rows=sum(r["amount_missing"] for r in main_coverage),
        )
        result["outside_listing_source_rows"] = sum(
            r["reason"] == "outside_listing_or_code_bounds" for r in boundary_rows
        )
        result["outside_calendar_source_rows"] = sum(
            r["reason"] == "outside_source_calendar" for r in boundary_rows
        )
        result["outside_listing_membership_rows"] = len(membership_boundaries)
        result["limitations"] = [
            self.meta("universe_definition"),
            self.meta("calendar_scope"),
            "Missing bars are unknown, never inferred suspended. Historical name changes and historical publication/PIT are not certified.",
            "Eastmoney volume is source lots * 100 (rounded lot precision), amount CNY; Sina volume shares, amount unavailable when absent.",
            "Sohu volume source lots * 100 and amount ten-thousand CNY * 10000; rounded display precision with observed source disagreements, not exchange-certified exactness.",
            "Factors retain entire observed source versions; empty factors do not prove no corporate actions. No cross-source automatic adjusted series.",
        ]
        (reports / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        return result

    def universe(self, day, main_board_only=True):
        """Dated source membership when available; otherwise retrospective listing facts."""
        parse_day(day)
        source_rows = []
        if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe_membership'"
        ).fetchone():
            source_rows = self.db.execute(
                "SELECT * FROM universe_membership WHERE date=? ORDER BY symbol", (day,)
            ).fetchall()
        observed_members = {r["symbol"]: dict(r) for r in source_rows}
        instruments = {r["symbol"]: dict(r) for r in self.db.execute("SELECT * FROM instruments")}
        for event in self.code_events():
            old, new = event["old_symbol"], event["new_symbol"]
            if (
                observed_members
                and new in observed_members
                and old not in observed_members
                and day < event["effective_date"]
            ):
                observed_members[old] = dict(
                    observed_members[new],
                    symbol=old,
                    name=event["old_name"],
                    trade_status=None,
                    remapped_from=new,
                )
        symbols = sorted(observed_members if observed_members else instruments)
        result = []
        for symbol in symbols:
            if symbol in instruments and not self.expected(symbol, day, day, [day]):
                continue
            row = dict(instruments.get(symbol, dict(symbol=symbol, ipo=None, out=None)))
            row["board_at_date"] = (
                "sme" if symbol.startswith(("sz002", "sz003")) and day < "2021-04-06" else "main"
            )
            # BaoStock dated membership can backfill the current/final display name
            # (e.g. sz000001 returns 平安银行 for 2010). A dated row is not a dated name.
            row["name_is_historical"] = False
            row["historical_pit"] = False
            row["code_valid_from"], row["code_valid_until_exclusive"] = self.code_bounds(symbol)
            row["membership_basis"] = (
                "dated_source_snapshot_with_listing_boundaries"
                if observed_members
                else "retrospective_listing_facts"
            )
            row["listing_metadata_known"] = symbol in instruments
            if observed_members:
                member = observed_members[symbol]
                row.update(
                    name=member["name"],
                    source_trade_status=member["trade_status"],
                    membership_capture=member["capture_id"],
                )
                if member.get("remapped_from"):
                    row["membership_basis"] = "issuer_code_change_from_backfilled_snapshot"
                    row["source_symbol"] = member["remapped_from"]
            if not main_board_only or row["board_at_date"] == "main":
                result.append(row)
        return result

    def factor_snapshots(self, security):
        symbol = _security(security)
        code_start, code_end = self.code_bounds(symbol)
        result = []
        for row in self.db.execute(
            "SELECT * FROM factor_versions WHERE symbol=? ORDER BY first_observed_at", (symbol,)
        ):
            item = dict(row, code_valid_from=code_start, code_valid_until_exclusive=code_end)
            item["source_rows_after_code_end"] = (
                self.db.execute(
                    "SELECT count(*) FROM factors WHERE version=? AND date>=?",
                    (row["id"], code_end),
                ).fetchone()[0]
                if code_end
                else 0
            )
            item["snapshot_scope"] = (
                "Entire observed source snapshot, including any provider alias continuation; "
                "factor dates after code_valid_until_exclusive are not events of this retired quote code."
            )
            result.append(item)
        return result

    def get_factors(self, version):
        """Return one explicit immutable observed factor version; no silent latest selection."""
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM factors WHERE version=? ORDER BY date", (version,)
            )
        ]

    def get_price(
        self,
        security,
        start="2010-01-01",
        end=None,
        source="auto",
        *,
        mode="single",
        alias_captures=(),
    ):
        if mode not in ("single", "compatible"):
            raise ValueError("mode must be single or compatible")
        if mode == "compatible":
            from .local_compatible import get_price

            return get_price(self, security, start, end, source, alias_captures)
        if alias_captures:
            raise ValueError("alias_captures requires mode='compatible'")
        import pandas as pd

        symbol = _security(security)
        end = end or self.meta("end")
        parse_day(start)
        parse_day(end)
        if start > end or source not in (
            "auto",
            "eastmoney",
            "sina",
            "baostock",
            "sohu",
            "tencent",
        ):
            raise ValueError("invalid date interval or source")
        selection = source
        frame = pd.read_sql_query(
            "SELECT * FROM bars WHERE symbol=? AND date BETWEEN ? AND ?"
            + ("" if source == "auto" else " AND source=?")
            + " ORDER BY date,source",
            self.db,
            params=(symbol, start, end) if source == "auto" else (symbol, start, end, source),
        )
        calendar_days = {
            r[0]
            for r in self.db.execute(
                "SELECT date FROM calendar WHERE date BETWEEN ? AND ?", (start, end)
            )
        }
        outside_calendar = frame[~frame.date.isin(calendar_days)]
        frame = frame[frame.date.isin(calendar_days)]
        # Keep source placeholders in storage, but not as post-delisting prices in
        # the default scope. The official exit date is exclusive unless the only
        # known boundary is a BaoStock legacy-code outDate explicitly marked inclusive.
        valid = set(self.expected(symbol, start, end, sorted(set(frame.date))))
        outside = frame[~frame.date.isin(valid)]
        frame = frame[frame.date.isin(valid)]
        if source == "auto":
            # Select a whole source series after applying listing episodes.
            if frame.empty:
                source = None
            else:
                candidates = (
                    frame.groupby("source")
                    .agg(
                        observed_days=("date", "count"),
                        amount_rows=("amount", "count"),
                        latest=("date", "max"),
                    )
                    .reset_index()
                )
                source = candidates.sort_values(
                    ["observed_days", "amount_rows", "latest", "source"],
                    ascending=[False, False, False, True],
                ).iloc[0]["source"]
            frame = frame[frame.source == source]
        frame = frame.reset_index(drop=True)
        frame.attrs = dict(
            source=source,
            source_selection=selection,
            price_basis="raw",
            price_unit="CNY/share",
            volume_unit="shares",
            amount_unit="CNY",
            timezone="Asia/Shanghai",
            numeric_storage="decimal strings",
            listing_filter="issuer-confirmed corrections then latest observed listing episodes; official exit exclusive; legacy-only Bao boundary inclusive",
            calendar_filter=self.meta("calendar_scope"),
            excluded_source_rows_outside_listing=len(outside)
            if source is None
            else int((outside.source == source).sum()),
            excluded_source_rows_outside_calendar=len(outside_calendar)
            if source is None
            else int((outside_calendar.source == source).sum()),
            coverage=[
                dict(r) for r in self.db.execute("SELECT * FROM coverage WHERE symbol=?", (symbol,))
            ],
        )
        return frame

    def get_alias_price(self, security, capture, start="2010-01-01", end=None):
        """Explicitly query an archived later-code backfill; native bars stay unchanged."""
        from .local_alias import get_alias_price

        return get_alias_price(self, security, capture, start, end)

    def get_early_observations(
        self, security, start="1990-12-01", end="2009-12-31", source=None, *, diagnostics=False
    ):
        """Read pre-2010 source observations and their unresolved historical basis."""
        from .local_early import observations

        return observations(self, security, start, end, source, diagnostics)


def add_parser(sub):
    p = sub.add_parser(
        "local-daily",
        help="批量本地日线维护、断点续取和覆盖报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "query-early 只读逐来源研究观察，不是完整可回测行情或全市场历史 PIT。\n"
            "来源行按 (symbol,date,source) 计数；唯一证券日按 (symbol,date) 去重，仍不认证成交日。\n"
            "2026-10-15 验收快照（非当前库实时统计）：7,348,840 HTTP 来源行，"
            "3,654,381 唯一证券日，\n"
            "164,076 个上海代理日历缺口。代理不是深圳早年/PT 完整日历；"
            "缺口不等于已证实漏采。\n"
            "未认证 BaoStock 停牌/成交声明不消除缺口；validation_unknown 仅在 --diagnostics 展示。\n"
            "早期价格/股数/手数单位、证券身份阶段和复权依据仍有限制；"
            "strict_usable=false，不自动缩放、复权或生成总收益。\n"
            "范围、缺口归因和使用限制见 docs/local-early-history.md。"
        ),
    )
    p.add_argument(
        "action",
        choices=[
            "update",
            "resume",
            "report",
            "status",
            "query",
            "query-alias",
            "query-early",
            "universe",
            "factors",
            "import-bao",
            "supplement",
        ],
    )
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument(
        "--start",
        default=None,
        help="起始日；query-early 默认 1990-12-01，其他操作默认 2010-01-01",
    )
    p.add_argument("--end", default=None, help="结束日；query-early 默认 2009-12-31")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--repair-gaps", action="store_true")
    p.add_argument("--refresh-metadata", action="store_true")
    p.add_argument("--symbol", action="append")
    p.add_argument("--capture", type=Path, help="Explicit immutable capture for query-alias")
    p.add_argument(
        "--diagnostics",
        action="store_true",
        help="query-early: include unadmitted BaoStock and anomalous HTTP rows with validation status",
    )
    p.add_argument(
        "--mode",
        choices=["single", "compatible"],
        default="single",
        help="query: opt in to audited per-day/field completion",
    )
    p.add_argument(
        "--alias-capture",
        action="append",
        type=Path,
        default=[],
        help="query --mode compatible: explicit verified alias archive (repeatable)",
    )
    p.add_argument(
        "--include-sme-history",
        action="store_true",
        help="历史名单中包括原中小板时期；默认按当时主板归属筛选",
    )
    p.add_argument("--limit-jobs", type=int, help="受控验证可用；正常全量无需指定")
    p.add_argument(
        "--source",
        default="auto",
        choices=["auto", "eastmoney", "sina", "baostock", "sohu", "tencent"],
    )


def command(args):
    if getattr(args, "start", None) is None:
        args.start = "1990-12-01" if args.action == "query-early" else "2010-01-01"
    mode = getattr(args, "mode", "single")
    aliases = getattr(args, "alias_capture", ())
    if args.action != "query" and (mode != "single" or aliases):
        raise ValueError("--mode / --alias-capture are query options")
    if getattr(args, "diagnostics", False) and args.action != "query-early":
        raise ValueError("--diagnostics is a query-early option")
    readonly = args.action in (
        "status",
        "query",
        "query-alias",
        "query-early",
        "universe",
        "factors",
    )
    store = LocalDailyStore(args.root, readonly=readonly)
    try:
        if args.action == "status":
            return store.summary()
        if args.action == "universe":
            return store.universe(
                args.end or store.meta("end"), main_board_only=not args.include_sme_history
            )
        if args.action == "factors":
            if not args.symbol or len(args.symbol) != 1:
                raise ValueError("factors needs one --symbol")
            return store.factor_snapshots(args.symbol[0])
        if args.action == "query":
            if not args.symbol or len(args.symbol) != 1:
                raise ValueError("query needs one --symbol")
            frame = store.get_price(
                args.symbol[0],
                args.start,
                args.end,
                args.source,
                mode=mode,
                alias_captures=aliases,
            )
            if mode == "compatible":
                return dict(rows=frame.to_dict("records"), metadata=frame.attrs)
            return frame.to_dict("records")
        if args.action == "query-alias":
            if not args.symbol or len(args.symbol) != 1 or not args.capture:
                raise ValueError("query-alias needs one --symbol and an explicit --capture")
            if args.source not in ("auto", "baostock"):
                raise ValueError("query-alias supports only the explicit BaoStock capture")
            return store.get_alias_price(
                args.symbol[0], args.capture, args.start, args.end
            ).to_dict("records")
        if args.action == "query-early":
            if not args.symbol or len(args.symbol) != 1:
                raise ValueError("query-early needs one --symbol")
            return store.get_early_observations(
                args.symbol[0],
                args.start,
                args.end or "2009-12-31",
                None if args.source == "auto" else args.source,
                diagnostics=getattr(args, "diagnostics", False),
            )
        with store.writer():
            if args.action == "import-bao":
                from .local_baostock import import_captures

                result = import_captures(store)
                store.report()
                return result
            if args.action == "report":
                return store.report()
            if args.workers < 1 or args.interval < 0 or args.timeout <= 0:
                raise ValueError("workers/interval/timeout invalid")
            if args.action == "supplement":
                if args.source not in {"sohu", "tencent"}:
                    raise ValueError("supplement requires --source sohu or tencent")
                if not args.symbol:
                    store.set_meta(
                        "supplement_sources",
                        sorted(set(store.meta("supplement_sources", [])) | {args.source}),
                    )
            http = PublicHTTP(store.root, interval=args.interval, timeout=args.timeout)
            http.history.refresh = args.refresh_metadata
            end = args.end or (store.meta("end") if args.action == "resume" else target_end())
            if not end:
                end = target_end()
            parse_day(args.start)
            parse_day(end)
            if args.start > end:
                raise ValueError("start after end")
            horizon_advanced = end > (store.meta("end") or "")
            http.history.refresh = args.refresh_metadata or horizon_advanced
            if args.action == "update" and (
                args.refresh_metadata
                or horizon_advanced
                or not store.meta("universe_observed_at")
                or store.meta("universe_observed_at")[:10] != now()[:10]
            ):
                records, receipts = exchange_universe(http)
                store.save_universe(records, receipts, args.start, end)
            latest_calendar = store.db.execute("SELECT max(date) FROM calendar").fetchone()[0]
            if not latest_calendar or latest_calendar < end or args.refresh_metadata:
                days, receipts = calendar(http, end)
                store.save_calendar(days, receipts)
            http.history.refresh = False
            if args.action == "update" and end > (store.meta("end") or ""):
                recent = (parse_day(end) - timedelta(days=7)).isoformat()
                symbols = [_security(s) for s in args.symbol] if args.symbol else []
                selection = (
                    " AND symbol IN (" + ",".join("?" for _ in symbols) + ")" if symbols else ""
                )
                store.db.execute(
                    "UPDATE jobs SET state='pending' WHERE state IN ('failed','empty_unknown') AND end>=? AND "
                    + HTTP_JOB_PREDICATE
                    + selection,
                    (recent, *HTTP_JOB_SOURCES, *symbols),
                )
                store.db.commit()
            if args.action in ("update", "supplement"):
                from .local_code_history import refresh_code_history

                # Resolve retrospective identifiers before creating any jobs:
                # current provider codes may be backfilled before their lifetime.
                store.set_meta("start", min(args.start, store.meta("start") or args.start))
                store.set_meta("end", max(end, store.meta("end") or end))
                refresh_code_history(store, http)
            store.plan(
                args.start,
                end,
                args.repair_gaps,
                args.retry_failed,
                [_security(s) for s in args.symbol] if args.symbol else None,
            )
            if args.action == "supplement":
                # A targeted source repair does not silently subscribe the entire
                # market to that additional provider on the next daily update.
                store.plan_source(
                    args.source,
                    args.start,
                    end,
                    symbols=[_security(s) for s in args.symbol] if args.symbol else None,
                )
                store.db.commit()
            print(json.dumps(dict(planned=store.summary()), ensure_ascii=False), flush=True)
            store.run(
                http,
                workers=args.workers,
                limit=args.limit_jobs,
                only_source=args.source if args.action == "supplement" else None,
                symbols=[_security(s) for s in args.symbol] if args.symbol else None,
            )
            return store.report()
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub)
    args = parser.parse_args(argv)
    print(json.dumps(command(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
