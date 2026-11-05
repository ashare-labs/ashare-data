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
    require(path.is_file(), "MARKET_SOURCE_CHANGED", "Source database not found")
    store = LocalDailyStore(path.parent, readonly=True)
    store.db.execute("PRAGMA query_only=ON")
    store.db.execute("BEGIN")
    try:
        yield store
    finally:
        store.close()


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
