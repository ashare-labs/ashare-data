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
