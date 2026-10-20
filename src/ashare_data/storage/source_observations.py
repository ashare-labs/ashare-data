"""Explicit research access to a release-pinned original SQLite snapshot.

This does not silently fill canonical facts. Existing raw values, original scope,
and restrictions remain queryable without another whole-source copy.
"""

from contextlib import contextmanager
import fcntl
from pathlib import Path
import threading

from ashare_data.core.model import require
from ashare_data.storage.local_daily import LocalDailyStore
from ashare_data.storage.market_release import fingerprint, sha_file

_verified = {}
_guard = threading.Lock()


@contextmanager
def daily_source(asset):
    files = asset["files"]
    candidates = [Path(p) for p in files if str(p).endswith("daily.sqlite3")]
    require(
        len(candidates) == 1, "MARKET_SOURCE_SNAPSHOT", "One frozen source database is required"
    )
    path = candidates[0]

    def check_unregistered_journals():
        for suffix in ("-wal", "-journal"):
            journal = Path(str(path) + suffix)
            require(
                str(journal) in files or not journal.exists() or journal.stat().st_size == 0,
                "MARKET_SOURCE_CHANGED",
                "The frozen source snapshot changed: unregistered SQLite journal",
            )

    with (path.parent / "writer.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        check_unregistered_journals()
        before = {}
        for name, expected in files.items():
            current = fingerprint(name)
            require(
                current == asset["fingerprints"][name],
                "MARKET_SOURCE_CHANGED",
                "The frozen source snapshot changed; select a new audited release",
            )
            key = (str(name), expected)
            with _guard:
                if _verified.get(key) != current:
                    require(
                        sha_file(name) == expected, "MARKET_SOURCE_CHANGED", "Source hash differs"
                    )
                    require(
                        fingerprint(name) == current,
                        "MARKET_SOURCE_CHANGED",
                        "Source changed during verification",
                    )
                    _verified[key] = current
            before[name] = current
        store = LocalDailyStore(path.parent, readonly=True)
        store.db.execute("PRAGMA query_only=ON")
        store.db.execute("BEGIN")
        try:
            yield store
        finally:
            store.close()
            check_unregistered_journals()
            require(
                all(fingerprint(p) == fp for p, fp in before.items()),
                "MARKET_SOURCE_CHANGED",
                "Source changed during the bounded research read",
            )


def incremental_daily(release, security, start, end, source, *, metadata_db=None):
    """Read only release-pinned recent observation assets, without a mutable glob."""
    import json
    import sqlite3
    import pandas as pd

    columns = [
        "symbol",
        "date",
        "source",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "preclose",
        "tradestatus",
        "is_st",
        "row_hash",
        "response_id",
        "observed_at",
    ]
    from contextlib import nullcontext

    with nullcontext(metadata_db) if metadata_db is not None else release.metadata() as db:
        row = None
        if db.execute(
            "select 1 from sqlite_master where type='table' and name='release_contracts'"
        ).fetchone():
            row = db.execute(
                "select body from release_contracts where name='incremental_observations'"
            ).fetchone()
        items = json.loads(row[0])["daily_assets"] if row else []
    frames, refs = [], []
    for item in items:
        if source not in item["sources"]:
            continue
        refs.append({k: item[k] for k in ("path", "sha256")})
        path = release.verify(item)
        with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
            frame = pd.read_sql_query(
                "SELECT "
                + ",".join(columns)
                + " FROM observations WHERE instrument_id=? AND source=? AND date BETWEEN ? AND ? ORDER BY date",
                db,
                params=(release.identifiers[security], source, start, end),
            )
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)
    # The ordered immutable asset list is a revision chain, never directory order.
    if not combined.empty:
        combined = combined.drop_duplicates(["date", "source"], keep="last").sort_values("date")
    return combined, refs
