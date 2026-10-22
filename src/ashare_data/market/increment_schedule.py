"""Bounded outage catch-up: oldest known open days first, no historical rebuild."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ashare_data.storage.daily_increment import COMPLETE_COVERAGE
from ashare_data.storage.market_release import MarketRelease


def calendar_from_batches(store, batches):
    batch = batches.get("query_trade_dates", {})
    if batch.get("status") == "ok":
        with store.db:
            for row in batch["rows"]:
                day = date.fromisoformat(row["calendar_date"]).isoformat()
                value = row["is_trading_day"]
                if value not in ("0", "1"):
                    raise ValueError("unknown BaoStock calendar status")
                store.db.execute("INSERT OR REPLACE INTO calendar VALUES(?,?,?)",
                                 (day, int(value), "baostock:" + batch["capture_sha256"]))


def load_base_calendar(store):
    binding = store.config.get("base_release")
    if not binding:
        return
    release = MarketRelease(binding["root"], binding["release_id"], expected_manifest_sha256=binding["manifest_sha256"])
    with release.metadata() as db, store.db:
        for row in db.execute("SELECT date FROM calendar"):
            store.db.execute("INSERT OR IGNORE INTO calendar VALUES(?,1,?)", (row[0], "base:" + release.sha256))
        # Explicit closed days exist in CMES metadata; absence in the open-day table is not closure.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='cmes_calendar'").fetchone():
            for day, opened in db.execute("SELECT trade_date,min(is_open) FROM cmes_calendar WHERE venue IN ('XSHG','XSHE') "
                                          "GROUP BY trade_date HAVING count(DISTINCT venue)=2 AND min(is_open)=max(is_open)"):
                store.db.execute("INSERT OR IGNORE INTO calendar VALUES(?,?,?)", (day, opened, "base:" + release.sha256))


def due_plan(store, now=None, *, fetch_calendar=True):
    now = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
    latest = now.date() - timedelta(days=int(now.strftime("%H:%M") < store.config["ready_time"]))
    if fetch_calendar:
        from ashare_data.market.daily_increment import bao_batches
        calendar_from_batches(store, bao_batches(store, latest.isoformat()))
    load_base_calendar(store)
    first = store.db.execute("SELECT min(day) FROM scopes").fetchone()[0]
    start = max(date.fromisoformat(first), latest - timedelta(days=store.config["retry_window_days"] - 1)) if first else latest
    days, unknown = [], []
    for offset in range(max(0, (latest - start).days + 1)):
        day = (start + timedelta(days=offset)).isoformat()
        known = store.db.execute("SELECT is_open FROM calendar WHERE day=?", (day,)).fetchone()
        if known is None:
            unknown.append(day)
            continue
        if not known[0]:
            continue
        scopes = store.db.execute("SELECT count(*) FROM scopes WHERE day=?", (day,)).fetchone()[0]
        states = [r[0] for r in store.db.execute("SELECT state FROM coverage WHERE day=?", (day,))]
        jobs = [r[0] for r in store.db.execute("SELECT state FROM jobs WHERE day=?", (day,))]
        # A captured partial rolling window cannot be repaired by redownloading the same window hourly.
        # Failed transports and data that were not yet available remain eligible for a bounded retry.
        retryable = {"not_yet_available", "source_unavailable"}
        if not scopes or len(jobs) != scopes * 3 or any(s not in {"done", "not_applicable"} for s in jobs) or any(s in retryable for s in states):
            days.append(day)
    deferred = [r[0] for r in store.db.execute("SELECT DISTINCT day FROM jobs WHERE day<? AND state NOT IN ('done','not_applicable') ORDER BY day", (start.isoformat(),))]
    return dict(days=days, priority="oldest_first_before_rolling_window_expires", calendar_unknown=unknown,
                before_source_ready_time=now.strftime("%H:%M") < store.config["ready_time"],
                deferred_older_days=deferred, deferred_is_not_proof_of_source_expiry=True,
                lookback_calendar_days=store.config["retry_window_days"], latest_ready_day=latest.isoformat())


def historical_members(store, day, current, batches):
    """Use a dated source list when available; label extrapolated membership unknown."""
    from ashare_data.providers.daily_increment import equity_security
    dated = batches.get("query_all_stock", {})
    proven = {}
    if dated.get("status") == "ok":
        for row in dated["rows"]:
            security = equity_security(row.get("code"))
            if security:
                proven[security] = dict(security=security, name=row.get("code_name"), source="baostock_dated_membership")
    candidates = {r["security"]: r for r in current}
    prior = store.db.execute("SELECT max(day) FROM scopes WHERE day<?", (day,)).fetchone()[0]
    if prior:
        for row in store.db.execute("SELECT i.security,i.name FROM scopes s JOIN instruments i USING(security) WHERE s.day=? AND s.expected_state!='not_applicable'", (prior,)):
            candidates.setdefault(row["security"], dict(row, source="prior_membership"))
    candidates.update(proven)
    with store.db:
        for security, row in candidates.items():
            old = store.db.execute("SELECT * FROM instruments WHERE security=?", (security,)).fetchone()
            if old is None:
                store.instrument(security, name=row.get("name"), body=row)
                old = store.db.execute("SELECT * FROM instruments WHERE security=?", (security,)).fetchone()
            outside = old["listed_on"] and day < old["listed_on"] or old["delisted_on"] and day > old["delisted_on"]
            expected = "not_applicable" if outside else "expected" if security in proven else "unknown"
            store.db.execute("INSERT OR IGNORE INTO scopes VALUES(?,?,?,?)", (day, security, expected, row["source"]))
            for kind in ("daily", "minute", "factor"):
                state = "not_applicable" if outside else "pending"
                store.db.execute("INSERT OR IGNORE INTO jobs(day,security,kind,state) VALUES(?,?,?,?)", (day, security, kind, state))
                if outside:
                    store.cover(day, security, kind, dict(state="not_applicable", observed_count=0))


def retry_not_ready(store, day):
    with store.db:
        store.db.execute("UPDATE jobs SET state='pending' WHERE day=? AND EXISTS(SELECT 1 FROM coverage c "
                         "WHERE c.day=jobs.day AND c.security=jobs.security AND c.kind=jobs.kind AND c.state='not_yet_available')", (day,))


def known_complete(store, day, kind):
    rows = store.db.execute("SELECT s.expected_state,c.state FROM scopes s LEFT JOIN coverage c ON c.day=s.day "
                            "AND c.security=s.security AND c.kind=? WHERE s.day=?", (kind, day)).fetchall()
    return bool(rows) and all(r[0] != "unknown" and r[1] in COMPLETE_COVERAGE for r in rows)
