"""Durable per-security/day progress for finite public-source updates."""

from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from ashare_data.providers.baostock_receipt.capture import utc_now
from ashare_data.storage.market_release import FIELDS, MarketRelease, atomic_write, canonical

SCHEMA = "ad-daily-increment.v1"
SQL = """
CREATE TABLE IF NOT EXISTS instruments(security TEXT PRIMARY KEY,instrument_id INTEGER UNIQUE,
 name TEXT,venue TEXT,state TEXT,listed_on TEXT,delisted_on TEXT,first_seen TEXT,last_seen TEXT,body TEXT);
CREATE TABLE IF NOT EXISTS scopes(day TEXT,security TEXT,expected_state TEXT,source TEXT,
 PRIMARY KEY(day,security));
CREATE TABLE IF NOT EXISTS jobs(day TEXT,security TEXT,kind TEXT,state TEXT,attempts INTEGER DEFAULT 0,
 started_at TEXT,completed_at TEXT,error TEXT,PRIMARY KEY(day,security,kind));
CREATE TABLE IF NOT EXISTS coverage(day TEXT,security TEXT,kind TEXT,state TEXT,
 observed_count INTEGER,body TEXT,PRIMARY KEY(day,security,kind));
CREATE TABLE IF NOT EXISTS captures(sha256 TEXT PRIMARY KEY,body TEXT);
CREATE TABLE IF NOT EXISTS capture_requests(request_id TEXT PRIMARY KEY,asset_id TEXT,body TEXT);
CREATE INDEX IF NOT EXISTS capture_requests_asset ON capture_requests(asset_id);
CREATE TABLE IF NOT EXISTS observations(source TEXT,frequency TEXT,security TEXT,trade_date TEXT,
 label_at TEXT,open_price TEXT,high_price TEXT,low_price TEXT,close_price TEXT,volume_shares INTEGER,
 turnover_cny TEXT,preclose TEXT,tradestatus TEXT,is_st TEXT,asset_id TEXT,source_row INTEGER,
 observed_at TEXT,row_hash TEXT,
 PRIMARY KEY(source,frequency,security,trade_date,label_at,row_hash));
CREATE INDEX IF NOT EXISTS observations_day ON observations(trade_date,frequency,security);
CREATE TABLE IF NOT EXISTS factors(day TEXT,security TEXT,source TEXT,row_hash TEXT,body TEXT,
 PRIMARY KEY(day,security,source,row_hash));
CREATE TABLE IF NOT EXISTS rejections(day TEXT,security TEXT,kind TEXT,asset_id TEXT,
 source_row INTEGER,reason TEXT,native TEXT,PRIMARY KEY(day,security,kind,asset_id,source_row));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT,kind TEXT,body TEXT);
CREATE TABLE IF NOT EXISTS calendar(day TEXT PRIMARY KEY,is_open INTEGER,source TEXT);
"""
FINAL_JOBS = {"done", "not_applicable"}
COMPLETE_COVERAGE = {"complete", "suspended", "not_applicable", "no_factor_event"}


def initialize(root, config):
    root = Path(root).expanduser().resolve()
    if (root / "CURRENT.json").exists() or (root / "daily.sqlite3").exists():
        raise ValueError("Use a dedicated increment root, never a market or source library")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "CONFIG.json"
    value = dict(config, schema=SCHEMA)
    if path.exists():
        if json.loads(path.read_bytes()) != value:
            raise ValueError("Existing increment configuration differs; use an explicit new root")
    else:
        atomic_write(path, canonical(value))
    return root


class IncrementStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.config = json.loads((self.root / "CONFIG.json").read_bytes())
        if self.config.get("schema") != SCHEMA:
            raise ValueError("not a daily increment root")
        self.db = sqlite3.connect(self.root / "control.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SQL)

    def close(self):
        self.db.close()

    @contextmanager
    def writer(self):
        with (self.root / "collector.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Only this root's previous interrupted process; concurrent writers are excluded.
            with self.db:
                interrupted = self.db.execute("UPDATE jobs SET state='pending' WHERE state='running'").rowcount
                if interrupted:
                    self.event("recovered_jobs", {"count": interrupted})
            yield

    def budget(self):
        if shutil.disk_usage(self.root).free < self.config["free_reserve_bytes"]:
            raise RuntimeError("increment free-space reserve reached")
        size, seen = 0, set()
        base = self.config.get("base_release", {}).get("root") if self.config.get("base_release") else None
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            # Baseline immutable objects are linked, not newly allocated task data.
            original = Path(base) / "objects" / path.name if base and path.parent.name == "objects" else None
            if stat.st_nlink > 1 and original and original.is_file() and os.path.samefile(path, original):
                continue
            size += stat.st_size
        if size >= self.config["budget_bytes"]:
            raise RuntimeError("increment budget reached; completed tasks retained")
        return size

    def event(self, kind, value):
        self.db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)", (utc_now(), kind, canonical(value).decode()))

    def instrument(self, security, *, name=None, state="historical_observed", body=None,
                   listed_on=None, delisted_on=None, instrument_id=None):
        old = self.db.execute("SELECT * FROM instruments WHERE security=?", (security,)).fetchone()
        venue = "BJ" if security.startswith("BJ.") else "SH" if security.endswith(".XSHG") else "SZ"
        now = utc_now()
        if old:
            self.db.execute("UPDATE instruments SET name=coalesce(?,name),state=?,last_seen=?,"
                            "listed_on=coalesce(?,listed_on),delisted_on=coalesce(?,delisted_on),"
                            "body=coalesce(?,body) WHERE security=?", (
                                name, state, now, listed_on, delisted_on,
                                canonical(body).decode() if body else None, security))
            return old["instrument_id"]
        identifier = instrument_id or self.db.execute("SELECT coalesce(max(instrument_id),0)+1 FROM instruments").fetchone()[0]
        self.db.execute("INSERT INTO instruments VALUES(?,?,?,?,?,?,?,?,?,?)", (
            security, identifier, name, venue, state, listed_on, delisted_on, now, now,
            canonical(body or {}).decode()))
        return identifier

    def seed(self, inventory):
        if self.db.execute("SELECT count(*) FROM instruments").fetchone()[0]:
            return
        with self.db:
            binding = self.config.get("base_release")
            if binding:
                base = MarketRelease(binding["root"], binding["release_id"],
                                     expected_manifest_sha256=binding["manifest_sha256"])
                with base.metadata() as db:
                    for row in db.execute("SELECT * FROM release_instruments ORDER BY instrument_id"):
                        self.instrument(row["security"], name=row["name"], instrument_id=row["instrument_id"],
                                        body={"base_release": base.receipt()})
            for row in inventory:
                if self.db.execute("SELECT 1 FROM instruments WHERE security=?", (row["security"],)).fetchone() is None:
                    self.instrument(row["security"], body=row)
            self.event("seeded_historical_identities", {"count": self.db.execute("SELECT count(*) FROM instruments").fetchone()[0]})

    def plan(self, day, members, basics=()):
        from ashare_data.providers.daily_increment import equity_security
        current = {r["security"]: r for r in members}
        with self.db:
            prior = {r["security"] for r in self.db.execute("SELECT security FROM instruments WHERE state IN ('active','absent_unknown')")}
            for row in basics:
                security = equity_security(row.get("code")) if row.get("type") == "1" else None
                if not security:
                    continue
                retired = bool(row.get("outDate") and row["outDate"] < day)
                self.instrument(security, name=row.get("code_name"),
                                state="retired" if retired else "active" if security in current else "membership_unknown",
                                listed_on=row.get("ipoDate") or None, delisted_on=row.get("outDate") or None,
                                body={"source": "baostock_basic", "native": row})
            for security, row in current.items():
                self.instrument(security, name=row.get("name"), state="active", body=row)
                self.db.execute("INSERT OR REPLACE INTO scopes VALUES(?,?,'expected',?)", (day, security, row["source"]))
            for security in prior - current.keys():
                old = self.db.execute("SELECT * FROM instruments WHERE security=?", (security,)).fetchone()
                expected = "not_applicable" if old["delisted_on"] and old["delisted_on"] < day else "unknown"
                self.db.execute("INSERT OR REPLACE INTO scopes VALUES(?,?,?,?)", (day, security, expected, "prior_membership_absent_now"))
                self.db.execute("UPDATE instruments SET state=? WHERE security=?",
                                ("retired" if expected == "not_applicable" else "absent_unknown", security))
            for row in self.db.execute("SELECT * FROM scopes WHERE day=?", (day,)).fetchall():
                for kind in ("daily", "minute", "factor"):
                    state = "not_applicable" if row["expected_state"] == "not_applicable" else "pending"
                    self.db.execute("INSERT OR IGNORE INTO jobs(day,security,kind,state) VALUES(?,?,?,?)",
                                    (day, row["security"], kind, state))
                    if state == "not_applicable":
                        self.cover(day, row["security"], kind, {"state": state, "observed_count": 0})

    def capture(self, receipt):
        key = receipt.get("capture_sha256", receipt.get("sha256"))
        body = canonical(receipt)
        self.db.execute("INSERT OR IGNORE INTO captures VALUES(?,?)", (key, body.decode()))
        self.db.execute("INSERT OR IGNORE INTO capture_requests VALUES(?,?,?)",
                        (hashlib.sha256(body).hexdigest(), key, body.decode()))
        return key

    def register_request_cache(self, day):
        """Retain URL/time contexts even when different requests return identical bytes."""
        http = self.root / "runs" / day / "http"
        with self.db:
            for path in sorted((http / "raw" / "requests").glob("*.json")):
                receipt = json.loads(path.read_bytes())
                self.capture(dict(receipt, path=str(http / "raw" / (receipt["sha256"] + ".gz"))))

    def start(self, day, security, kind):
        with self.db:
            self.db.execute("UPDATE jobs SET state='running',attempts=attempts+1,started_at=?,error=NULL "
                            "WHERE day=? AND security=? AND kind=?", (utc_now(), day, security, kind))

    def finish(self, day, security, kind, *, error=None):
        self.db.execute("UPDATE jobs SET state=?,completed_at=?,error=? WHERE day=? AND security=? AND kind=?",
                        ("failed" if error else "done", utc_now(), error, day, security, kind))

    def cover(self, day, security, kind, value):
        value = dict(value)
        if kind == "daily":
            old = self.db.execute("SELECT body FROM coverage WHERE day=? AND security=? AND kind=?",
                                  (day, security, kind)).fetchone()
            evidence = json.loads(old[0]).get("trading_status_evidence") if old else None
            if evidence and "trading_status_evidence" not in value:
                value["trading_status_evidence"] = evidence
            status = value.get("trading_status_evidence", {}).get("tradestatus")
            if status == "0" and not value.get("observed_count") and value["state"] in {"empty_unknown", "quality_gap"}:
                value.update(state="suspended", price_rows_expected=0)
        self.db.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?)",
                        (day, security, kind, value["state"], value.get("observed_count", 0), canonical(value).decode()))

    def observe(self, source, frequency, security, day, row, asset, source_row, observed_at):
        values = [str(row[k]) if row.get(k) is not None else None for k in FIELDS]
        if values[4] is not None:
            values[4] = int(values[4])
        extra = [str(row[k]) if row.get(k) is not None else None for k in ("preclose", "tradestatus", "is_st")]
        label = str(row.get("label_at") or "")
        digest = hashlib.sha256(canonical([*values, *extra])).hexdigest()
        key = (source, frequency, security, day, label)
        old = self.db.execute("SELECT row_hash FROM observations WHERE source=? AND frequency=? AND security=? "
                              "AND trade_date=? AND label_at=?", key).fetchall()
        inserted = self.db.execute("INSERT OR IGNORE INTO observations VALUES(" + ",".join("?" for _ in range(18)) + ")",
                                   (*key, *values, *extra, asset, source_row, observed_at, digest)).rowcount
        return {"added": inserted, "duplicate": int(not inserted),
                "conflict": int(any(r[0] != digest for r in old))}

    def rejected(self, day, security, kind, asset, row):
        self.db.execute("INSERT OR IGNORE INTO rejections VALUES(?,?,?,?,?,?,?)", (
            day, security, kind, asset, row["source_row"], row["reason"], canonical(row["native"]).decode()))

    def pending(self, day, kind):
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE day=? AND kind=? "
                                               "AND state NOT IN ('done','not_applicable') ORDER BY day,security", (day, kind))]

    def cursors(self):
        """Contiguous known trading-day completion, separately for each data kind."""
        first = self.db.execute("SELECT min(day) FROM scopes").fetchone()[0]
        result = {}
        for kind in ("daily", "minute", "factor"):
            completed_through = None
            first_gap = None
            for (day,) in self.db.execute("SELECT day FROM calendar WHERE is_open=1 AND day>=? ORDER BY day", (first,)):
                rows = self.db.execute("SELECT s.expected_state,c.state FROM scopes s LEFT JOIN coverage c "
                                       "ON c.day=s.day AND c.security=s.security AND c.kind=? WHERE s.day=?", (kind, day)).fetchall()
                if not rows or any(r[0] == "unknown" or r[1] not in COMPLETE_COVERAGE for r in rows):
                    first_gap = day
                    break
                completed_through = day
            result[kind] = dict(complete_through=completed_through, first_incomplete_day=first_gap)
        return result

    def report(self, day):
        scope = [dict(r) for r in self.db.execute("SELECT s.*,i.venue FROM scopes s JOIN instruments i USING(security) WHERE day=?", (day,))]
        jobs = [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE day=?", (day,))]
        coverage = [dict(r) for r in self.db.execute("SELECT c.*,i.venue FROM coverage c JOIN instruments i USING(security) WHERE day=?", (day,))]
        by_kind = {}
        for kind in ("daily", "minute", "factor"):
            selected = [x for x in coverage if x["kind"] == kind]
            by_kind[kind] = dict(states=dict(Counter(x["state"] for x in selected)),
                                 rows=sum(x["observed_count"] for x in selected),
                                 jobs=dict(Counter(x["state"] for x in jobs if x["kind"] == kind)),
                                 venues={v: dict(Counter(x["state"] for x in selected if x["venue"] == v)) for v in ("SH", "SZ", "BJ")})
        complete = (bool(scope) and all(x["expected_state"] != "unknown" for x in scope)
                    and len(coverage) == len(scope) * 3 and all(x["state"] in COMPLETE_COVERAGE for x in coverage))
        value = dict(schema=SCHEMA, trade_date=day, at=utc_now(), scope_count=len(scope),
                     expected_states=dict(Counter(x["expected_state"] for x in scope)),
                     scope_by_venue=dict(Counter(x["venue"] for x in scope)), kinds=by_kind,
                     all_requested_data_complete=complete,
                     all_collection_jobs_finished=bool(jobs) and all(x["state"] in FINAL_JOBS for x in jobs),
                     raw_and_normalized_versions_retained=True,
                     strict_historical_pit=False, default_changed=False,
                     completion_cursors=self.cursors(),
                     observations=self.db.execute("SELECT count(*) FROM observations WHERE trade_date=?", (day,)).fetchone()[0],
                     rejected_rows=self.db.execute("SELECT count(*) FROM rejections WHERE day=?", (day,)).fetchone()[0])
        directory = self.root / "runs" / day
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write(directory / "report.json", canonical(value))
        return value
