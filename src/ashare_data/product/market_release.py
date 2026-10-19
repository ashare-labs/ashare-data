"""Canonical facts and explicit source observations pinned to one immutable release."""

from contextlib import contextmanager
from decimal import Decimal
import sqlite3
import json

import pandas as pd

from ashare_data.core.model import digest, require, timestamp
from ashare_data.product.daily import LocalDailyProduct, _sid
from ashare_data.storage.local_daily import LocalDailyStore
from ashare_data.storage.market_release import MarketRelease, FIELDS

LIMITATIONS = [
    "Whole daily observations follow the release-frozen source priority; conflicts remain in the disposition ledger.",
    "Only evidence-supported early observations are admitted; unresolved values retain exact source locators and restrictions.",
    "Minute labels, including 09:30, are preserved; interval boundaries and strict historical PIT are unverified.",
    "A frozen local release reproduces values, identifiers, calendar and rules; it does not certify historical market availability.",
    "Missing and zero-volume observations do not prove suspension. Quarantined values are not fabricated or filled.",
]


def open_release(config):
    selected = config["market_release"]
    return MarketRelease(
        selected["root"],
        selected["release_id"],
        expected_manifest_sha256=selected["manifest_sha256"],
    )


class FrozenDailyStore(LocalDailyStore):
    def __init__(self, release):
        self.release = release
        self.root = release.root
        self._code_events = None
        self._source_context = None
        self._source_store = None
        self.db = sqlite3.connect(release.metadata_path.as_uri() + "?mode=ro&immutable=1", uri=True)
        self.db.row_factory = sqlite3.Row

    def expected(self, symbol, start, end, days):
        security = _sid(symbol)
        instrument_id = self.release.identifiers.get(security)
        aliases = [key for key, value in self.release.identifiers.items() if value == instrument_id]
        expected = set()
        for alias in aliases:
            if alias.endswith((".XSHG", ".XSHE")):
                native = ("sh" if alias.endswith(".XSHG") else "sz") + alias[:6]
                expected.update(super().expected(native, start, end, days))
        return sorted(expected)

    def get_price(self, security, start="2010-01-01", end=None, source="auto", **unused):
        selected = "canonical" if source in ("auto", "canonical") else source
        if selected not in ("canonical", "free_stockdb"):
            require(
                not security.startswith("BJ."),
                "MARKET_SOURCE_SCOPE",
                "This frozen SQLite source has no Beijing daily observations; use canonical",
            )
            from ashare_data.storage.source_observations import daily_source

            if self._source_store is None:
                row = self.db.execute(
                    "select body from source_assets where asset_id='daily-sqlite'"
                ).fetchone()
                require(
                    row is not None,
                    "MARKET_SOURCE_SNAPSHOT",
                    "Frozen original source metadata is required",
                )
                self._source_context = daily_source(json.loads(row[0]))
                self._source_store = self._source_context.__enter__()
            frame = self._source_store.get_price(security, start, end, source=selected)
            frame.attrs.update(
                release=self.release.receipt(),
                access="explicit_frozen_source_observation",
                canonical=False,
                source_asset_id="daily-sqlite",
            )
            return frame
        rows = []
        for series in (5,) if selected == "free_stockdb" else (1, 5):
            rows.extend(self.release.read("bars_1d", security, start, end, series_id=series))
        rows.sort(key=lambda row: row["trade_date"])
        require(
            len({row["trade_date"] for row in rows}) == len(rows),
            "MARKET_DUPLICATE_KEY",
            "Canonical daily supplements must not overlap selected daily facts",
        )
        public = []
        for r in rows:
            row = {
                k: r.get(k)
                for k in (
                    "source",
                    "row_hash",
                    "response_id",
                    "observed_at",
                    "preclose",
                    "tradestatus",
                    "is_st",
                )
            }
            row.update(
                symbol=r["source_symbol"],
                date=str(r["trade_date"]),
                instrument_id=r["instrument_id"],
                batch_id=r["batch_id"],
                series_id=r["series_id"],
            )
            for name, field in zip(("open", "high", "low", "close", "volume", "amount"), FIELDS):
                value = r[field]
                row[name] = None if value is None else str(value)
            public.append(row)
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
            "row_hash",
            "response_id",
            "observed_at",
            "preclose",
            "tradestatus",
            "is_st",
            "instrument_id",
            "batch_id",
            "series_id",
        ]
        frame = pd.DataFrame(public, columns=columns)
        frame.attrs = {
            "source": selected,
            "source_selection": source,
            "access": "published_source_subset" if selected == "free_stockdb" else "canonical",
            "price_basis": "raw",
            "price_unit": "CNY/share",
            "volume_unit": "shares",
            "amount_unit": "CNY",
            "numeric_storage": "Series 1 exact Decimal(38,12); series 5 original binary64; integral share volume",
            "release": self.release.receipt(),
            "priority_frozen_for_entire_release": True,
            "no_request_window_reranking": True,
            "historical_pit_verified": False,
        }
        return frame

    def close(self):
        try:
            if self._source_context is not None:
                self._source_context.__exit__(None, None, None)
                self._source_context = None
        finally:
            self.db.close()


class ReleasedDailyProduct(LocalDailyProduct):
    def __init__(self, config):
        self.release = open_release(config)
        self.root = self.release.root
        self.references = config.get("references")

    @contextmanager
    def _read(self):
        store = FrozenDailyStore(self.release)
        try:
            yield store
        finally:
            store.close()

    def _receipt(self, store, operation, request, rows, calendar, listing):
        value = super()._receipt(store, operation, request, rows, calendar, listing)
        value.pop("receipt_sha256")
        value.update(
            asset={"kind": "market_release", "root": str(self.root)},
            release=self.release.receipt(),
            transaction="one immutable release manifest; frozen SQLite metadata",
        )
        value["receipt_sha256"] = digest(value)
        return value

    def _prices(self, *args, **kwargs):
        value = super()._prices(*args, **kwargs)
        value["limitations"] = list(LIMITATIONS)
        value["market_release"] = self.release.receipt()
        if value["source"] in ("auto", "canonical", "free_stockdb"):
            known = set(value["calendar"])
            missing_listing = {
                gap["security"] for gap in value["gaps"] if gap["code"] == "SECURITY_UNAVAILABLE"
            }
            for gap in value["gaps"]:
                if gap["code"] == "OUTSIDE_LISTING" and gap["security"] in missing_listing:
                    gap.update(
                        code="LISTING_STATE_UNKNOWN",
                        detail="Historical listing state is unavailable in this release",
                    )
            for item in value["coverage"]:
                if item["security"] in missing_listing:
                    item["calendar_session_count"] = len(known)
                    item["expected_days"] = None
                    item["listing_days"] = None
                    item["missing_days"] = None
                    item["expected_state"] = "unknown"
                outside = sorted(
                    {
                        row["date"]
                        for row in value["rows"]
                        if row["security"] == item["security"] and row["date"] not in known
                    }
                )
                if outside:
                    item["observed_dates_outside_frozen_calendar"] = outside
                    item["expected_days"] = None
                    item["missing_days"] = None
                    item["expected_state"] = "partly_unknown"
                    value["gaps"].append(
                        {
                            "code": "EXPECTED_COVERAGE_UNKNOWN",
                            "security": item["security"],
                            "first": outside[0],
                            "last": outside[-1],
                            "detail": "Source observations exist outside the frozen calendar; expected sessions are not inferred from their presence",
                        }
                    )
        if value["source"] == "free_stockdb":
            value["access"] = "published_source_subset"
            value["limitations"].append(
                "This source filter returns only the release-admitted free-stockdb supplement, not all original provider observations."
            )
        elif value["source"] not in ("auto", "canonical"):
            value["access"] = "explicit_frozen_source_observation"
            value["limitations"] = [
                "Explicit source observations retain the original provider values and listing/calendar filters.",
                "These records are not silently promoted to canonical facts; historical PIT is unverified.",
            ]
        return value

    def catalogue(self, query="", limit=100, offset=0):
        require(
            isinstance(query, str)
            and len(query) <= 100
            and type(limit) is int
            and 1 <= limit <= 500
            and type(offset) is int
            and offset >= 0,
            "PRODUCT_PAGINATION",
            "Use bounded catalogue parameters",
        )
        needle = query.strip().replace(".XSHG", "").replace(".XSHE", "")
        with self.release.metadata() as db:
            candidates = [
                dict(r)
                for r in db.execute("""SELECT i.*,min(s.first) as first_bar,max(s.last) as last_bar
                FROM release_instruments i JOIN release_daily_summary s USING(instrument_id)
                GROUP BY i.instrument_id ORDER BY i.security""")
                if needle in r["security"] or needle in (r["name"] or "")
            ]
            items = []
            for item in candidates[offset : offset + limit]:
                sources = [
                    dict(r)
                    for r in db.execute(
                        "select source,rows,first,last from release_daily_summary where instrument_id=? order by source",
                        (item["instrument_id"],),
                    )
                ]
                item.update(
                    sources=sources,
                    name_is_historical=False,
                    coverage_semantics="fixed market release",
                )
                items.append(item)
        return {
            "schema": "ad-local-daily-catalogue.v1",
            "query": query,
            "limit": limit,
            "offset": offset,
            "total": len(candidates),
            "securities": items,
            "receipt": self.release.receipt(),
            "limitations": list(LIMITATIONS),
        }


def minute_prices(config, security, start, end, source="canonical"):
    release = open_release(config)
    series = {"canonical": 2, "neigezhu": 2, "tdx": 3, "free_stockdb": 4}.get(source)
    require(
        series is not None, "PRODUCT_MINUTE_SOURCE_UNKNOWN", "Choose a registered released source"
    )
    a, b = timestamp(start, minute=True), timestamp(end, minute=True)
    raw = release.read("bars_1m", security, a, b, series_id=series, max_rows=15000)
    if source == "canonical":
        supplement = release.read("bars_1m", security, a, b, series_id=4, max_rows=15000)
        require(
            not ({r["trade_date"] for r in raw} & {r["trade_date"] for r in supplement}),
            "MARKET_MINUTE_SCOPE",
            "Supplements must use separate security days, without guessing label alignment",
        )
        raw.extend(supplement)
        raw.sort(key=lambda row: row["label_at"])
        require(len(raw) <= 15000, "MARKET_QUERY_BOUND", "Narrow the requested range")
    restrictions = {}
    with release.metadata() as db:
        if db.execute(
            "select 1 from sqlite_master where type='table' and name='release_row_restrictions'"
        ).fetchone():
            for record in db.execute(
                "select * from release_row_restrictions where instrument_id=?",
                (release.identifiers[security],),
            ):
                restrictions[(record["series_id"], record["label_at"])] = dict(record)
    rows = []
    for row in raw:
        value = {
            "security": security,
            "instrument_id": row["instrument_id"],
            "series_id": row["series_id"],
            "source": "free_stockdb"
            if row["series_id"] == 4
            else "neigezhu"
            if row["series_id"] == 2
            else "tdx",
            "trade_date": str(row["trade_date"]),
            "source_label": row["label_at"].isoformat(),
            "label_at": row["label_at"].isoformat(),
            "batch_id": row["batch_id"],
            "price_status": "unverified",
            "interval_status": "unknown",
            "historical_pit": False,
        }
        for field, column in zip(("open", "high", "low", "close", "volume", "amount"), FIELDS):
            value[field] = str(row[column]) if isinstance(row[column], Decimal) else row[column]
        restriction = restrictions.get((row["series_id"], row["label_at"].isoformat()))
        value["diagnostics"] = []
        if restriction:
            value["price_status"] = restriction["price_status"]
            value["diagnostics"].append(restriction["rule"])
        for field in ("volume", "amount"):
            if value[field] is None:
                value["diagnostics"].append("null_" + field)
        from ashare_data.research.minute_policy import native_denials

        value["use_decision"] = native_denials(value)
        rows.append(value)
    return {
        "schema": "ad-local-minute-prices.v1",
        "source": source,
        "binding": {"reader": "market-release", "series_id": series, **release.receipt()},
        "rows": rows,
        "report": {
            "rows": len(rows),
            "range_inclusive": [a.isoformat(), b.isoformat()],
            "release": release.receipt(),
            "native_labels_preserved": True,
            "repaired_rows": 0,
            "strict_execution_admitted": False,
            "historical_pit": False,
            "limits": LIMITATIONS,
            "input_dispositions": release.manifest["input_dispositions"],
        },
    }
