"""Canonical facts and explicit source observations pinned to one immutable release."""

from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import sqlite3
import json
import re

import pandas as pd

from ashare_data.core.model import digest, require, timestamp
from ashare_data.product.daily import LocalDailyProduct, _sid
from ashare_data.product.minute_quality import released_invalid_slots, standard_quote
from ashare_data.storage.local_daily import LocalDailyStore
from ashare_data.storage.market_release import MarketRelease, FIELDS

LIMITATIONS = [
    "Whole daily observations follow the release-frozen source priority; conflicts remain in the disposition ledger.",
    "Only evidence-supported early observations are admitted; unresolved values retain exact source locators and restrictions.",
    "Minute labels, including 09:30, are preserved; interval boundaries and strict historical PIT are unverified.",
    "A frozen local release reproduces values, identifiers, calendar and rules; it does not certify historical market availability.",
    "Missing and zero-volume observations do not prove suspension. Quarantined values are not fabricated or filled.",
]


def open_release(config, *, release=None):
    selected = config["market_release"]
    if release is not None:
        require(
            isinstance(release, MarketRelease)
            and release.root == Path(selected["root"]).expanduser().resolve()
            and release.release_id == selected["release_id"]
            and release.sha256 == selected["manifest_sha256"],
            "PRODUCT_MARKET_RELEASE_CONFLICT", "Shared reader must use the same immutable release",
        )
        return release
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
        cmes = getattr(self.release, "manifest", {}).get("cmes")
        increment = getattr(self.release, "manifest", {}).get("free_daily_updates")
        exact_source = None
        if cmes or selected == "cmes":
            allowed = ("canonical", "cmes", *self.release.manifest.get("free_daily_contract", {}).get("source_priority", [])) if increment else ("canonical", "cmes")
            require(cmes and selected in allowed, "MARKET_SOURCE_SCOPE",
                    "CMES candidates contain only their explicit file source; no fallback")
            exact_source = selected if increment and selected != "canonical" else None
            selected = "canonical"
        if selected not in ("canonical", "free_stockdb"):
            from ashare_data.storage.source_observations import daily_source, incremental_daily

            incoming, observation_refs = incremental_daily(
                self.release, security, start, end, selected, metadata_db=self.db
            )
            if security.startswith("BJ."):
                require(
                    observation_refs,
                    "MARKET_SOURCE_SCOPE",
                    "This release has no Beijing observations for the selected source",
                )
                frame = pd.DataFrame(columns=incoming.columns)
            else:
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
            attrs = dict(frame.attrs)
            if not incoming.empty:
                # Each release explicitly fixes the latest retained source capture.
                # Canonical selection remains independent of this research overlay.
                frame = (
                    pd.concat(
                        [frame.loc[~frame.date.isin(incoming.date)], incoming], ignore_index=True
                    )
                    .sort_values("date")
                    .reset_index(drop=True)
                )
            frame.attrs = {
                **attrs,
                "release": self.release.receipt(),
                "access": "explicit_frozen_source_observation",
                "canonical": False,
                "source_asset_id": "daily-sqlite",
                "incremental_observation_refs": observation_refs,
                "source_selection": selected,
                "source": selected,
                "incremental_calendar_and_listing": "unknown unless independently evidenced",
            }
            return frame
        rows = []
        for series in (5,) if selected == "free_stockdb" else (1, 5):
            rows.extend(self.release.read("bars_1d", security, start, end, series_id=series))
        if exact_source:
            rows = [r for r in rows if r.get("source") == exact_source]
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
            if cmes:
                row.update({key: r.get(key) for key in ("source_asset_id", "source_member", "source_row")})
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
        if cmes:
            columns += ["source_asset_id", "source_member", "source_row"]
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
        if cmes:
            frame.attrs.update(source="cmes", source_selection=source,
                               mapping_version=cmes["mapping_version"],
                               mapping_sha256=cmes["mapping_sha256"], fallback="none")
        instrument = self.release.instruments.get(security, {})
        if instrument.get("asset_class") == "index":
            frame.attrs.update(asset_class="index", price_unit="index_point",
                               volume_unit=cmes.get("volume_unit") if cmes else None,
                               amount_unit=cmes.get("amount_unit") if cmes else None)
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
        # One date/search result per pinned reader: paging and same-date calls
        # reuse membership work without retaining a cache for every trading day.
        self._catalogue_rows = lru_cache(maxsize=1)(self._historical_catalogue)

    def _native_security(self, security):
        # Read access follows a verified publication identity, not a stock-code
        # prefix whitelist inherited from the live stock-only fetcher.
        if security in self.release.identifiers and re.fullmatch(r"[0-9]{6}\.(XSHG|XSHE)", security):
            return ("sh" if security.endswith(".XSHG") else "sz") + security[:6]
        return super()._native_security(security)

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
        missing = set(value["missing_response_ids"])
        ids = sorted({self.release.identifiers[s] for s in request.get("securities", [])
                      if s in self.release.identifiers})
        captured = {}
        for first in range(0, len(ids), 400):
            selected = ids[first:first + 400]
            if not missing:
                break
            assets = store.db.execute(
                "SELECT DISTINCT a.body FROM source_assets a JOIN source_aliases i "
                "ON i.source_version=a.asset_id WHERE a.asset_id LIKE 'response:%' "
                "AND i.instrument_id IN (" + ",".join("?" for _ in selected) + ")",
                selected,
            )
            for item in assets:
                asset = json.loads(item[0])
                receipt = asset.get("receipt")
                if receipt is None or digest(receipt) not in missing:
                    continue
                response_id = digest(receipt)
                response = captured.setdefault(response_id, {
                    "id": response_id, "error": None, "started_at": None,
                    **receipt, "frozen_source_assets": [],
                })
                response["frozen_source_assets"].append(
                    {key: val for key, val in asset.items() if key != "receipt"}
                )
        for response in captured.values():
            response["frozen_source_assets"].sort(key=lambda item: item["asset_id"])
        value["responses"] = sorted(
            value["responses"] + list(captured.values()), key=lambda item: item["id"]
        )
        value["missing_response_ids"] = sorted(missing - captured.keys())
        value.update(
            asset={"kind": "market_release", "root": str(self.root)},
            release=self.release.receipt(),
            transaction="one immutable release manifest; frozen SQLite metadata",
        )
        value["receipt_sha256"] = digest(value)
        return value

    def _prices(self, *args, **kwargs):
        value = super()._prices(*args, **kwargs)
        units = {r["security"]: r["source_metadata"]["price_unit"] for r in value["coverage"]}
        if "index_point" in units.values():
            value.update(price_units=units, price_unit="index_point" if set(units.values()) == {"index_point"} else "mixed")
            if set(units.values()) == {"index_point"}:
                value.update(volume_unit=None, amount_unit=None)
        value["limitations"] = list(LIMITATIONS)
        value["market_release"] = self.release.receipt()
        if value["source"] in ("auto", "canonical", "free_stockdb", "cmes") or value["rows"]:
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
        elif value["source"] not in ("auto", "canonical", "cmes"):
            value["access"] = "explicit_frozen_source_observation"
            value["limitations"] = [
                "Explicit source observations retain the original provider values and listing/calendar filters.",
                "These records are not silently promoted to canonical facts; historical PIT is unverified.",
            ]
        if self.release.manifest.get("cmes"):
            value["limitations"] = [
                ("CMES history with an explicitly frozen free-source continuation; per-row provenance and no field filling."
                 if self.release.manifest.get("free_daily_updates") else
                 "CMES raw prices with explicit file unit profiles; no fallback or field filling."),
                "ST membership is positive evidence only; absence, suspension and listing history remain unknown.",
                "Calendar flags are provider observations; common sessions require SH/SZ agreement; BJ is not inferred.",
                "Historical code changes and publication times are unverified; file retrieval is not historical PIT.",
            ]
        return value

    def _historical_catalogue(self, as_of, needle):
        items, unknown = [], []
        with self._read() as store:
            for row in store.db.execute(
                "SELECT r.instrument_id,r.security,i.name FROM release_identifiers r "
                "LEFT JOIN release_instruments i USING(instrument_id) ORDER BY r.security"
            ):
                item = dict(row)
                sid = item["security"]
                item["asset_class"] = self.release.instruments.get(sid, {}).get("asset_class", "equity")
                if needle not in sid and needle not in (item.get("name") or ""):
                    continue
                if not sid.endswith((".XSHG", ".XSHE")):
                    unknown.append(sid)
                    continue
                symbol = ("sh" if sid.endswith(".XSHG") else "sz") + sid[:6]
                facts = list(store.db.execute(
                    "SELECT ipo FROM listing_facts WHERE symbol=?", (symbol,)
                ))
                if not facts or any(not f[0] for f in facts):
                    unknown.append(sid)
                elif LocalDailyStore.expected(store, symbol, as_of, as_of, [as_of]):
                    # Membership uses this code's own interval, not the union
                    # of aliases that may be valid for price continuity.
                    items.append({
                        **item, "start_date": min(f[0] for f in facts),
                        "name_is_historical": False,
                        "coverage_semantics": "retrospective recorded listing/code intervals",
                    })
        return items, unknown

    def catalogue(self, query="", limit=100, offset=0, *, as_of=None):
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
        if as_of is not None:
            require(str(date.fromisoformat(as_of)) == as_of, "PRODUCT_DATE", "Use an ISO date")
            # Reuse the recorded listing/code intervals underlying prices(). This
            # is a retrospective directory, not a point-in-time index membership.
            self.release.verify(self.release.manifest["metadata"])
            items, unknown = self._catalogue_rows(as_of, needle)
            self.release.verify(self.release.manifest["metadata"])
            return {
                "schema": "ad-local-daily-catalogue.v1", "query": query,
                "limit": limit, "offset": offset, "total": len(items), "as_of": as_of,
                "securities": [dict(item) for item in items[offset:offset + limit]],
                "unknown_listing_securities": list(unknown),
                "historical_pit_verified": False, "receipt": self.release.receipt(),
                "limitations": list(LIMITATIONS) + [
                    "Directory membership uses recorded listing/code intervals; historical names, publication times and complete historical coverage are not certified.",
                    "Unknown listing intervals are reported separately, never assumed active.",
                ],
            }
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


def released_minute_sources(release):
    """Small immutable metadata determines source selection for this release."""
    sources = {"neigezhu": 2, "tdx": 3, "free_stockdb": 4}
    canonical_series = [2, 4]
    with release.metadata() as db:
        if db.execute(
            "select 1 from sqlite_master where type='table' and name='release_contracts'"
        ).fetchone():
            row = db.execute(
                "select body from release_contracts where name='minute_selection'"
            ).fetchone()
            if row:
                value = json.loads(row[0])
                sources = value["sources"]
                canonical_series = value["canonical_series"]
    require(
        isinstance(sources, dict)
        and bool(canonical_series)
        and len(set(canonical_series)) == len(canonical_series)
        and all(type(v) is int and v > 0 for v in sources.values())
        and set(canonical_series) <= set(sources.values()),
        "MARKET_MINUTE_CONTRACT",
        "Invalid frozen minute selection",
    )
    return sources, canonical_series


def minute_prices(config, security, start, end, source="canonical", *, release=None):
    release = open_release(config, release=release)
    sources, canonical_series = released_minute_sources(release)
    chosen = canonical_series if source == "canonical" else [sources.get(source)]
    require(
        all(s is not None for s in chosen),
        "PRODUCT_MINUTE_SOURCE_UNKNOWN",
        "Choose a source registered in this release",
    )
    series = chosen[0]
    a, b = timestamp(start, minute=True), timestamp(end, minute=True)
    raw, observed_days = [], {}
    for chosen_series in chosen:
        part = release.read("bars_1m", security, a, b, series_id=chosen_series)
        days = {r["trade_date"] for r in part}
        require(
            not (days & observed_days.keys()),
            "MARKET_MINUTE_SCOPE",
            "Supplements must use separate security days, without guessing label alignment",
        )
        observed_days.update(dict.fromkeys(days, chosen_series))
        raw.extend(part)
    raw.sort(key=lambda row: row["label_at"])
    names = {value: key for key, value in sources.items()}
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
            "source": names[row["series_id"]],
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
        rows.append(standard_quote(value))
    present = {timestamp(r["source_label"], minute=True) for r in rows}
    for row in released_invalid_slots(release, security, chosen, a, b):
        label = timestamp(row["source_label"], minute=True)
        # Respect a published whole-day replacement; never mask its good quote
        # with an excluded observation from another source.
        if label.date() not in observed_days:
            day_start = label.replace(hour=0, minute=0)
            for other in chosen:
                if other != row["series_id"] and release.read(
                    "bars_1m", security, day_start, day_start + timedelta(days=1, minutes=-1),
                    series_id=other,
                ):
                    observed_days[label.date()] = other
                    break
            observed_days.setdefault(label.date(), row["series_id"])
        if (
            label not in present
            and observed_days.get(label.date(), row["series_id"]) == row["series_id"]
        ):
            rows.append(row)
    rows.sort(key=lambda row: timestamp(row["source_label"], minute=True))
    return {
        "schema": "ad-local-minute-prices.v1",
        "source": source,
        "binding": {
            "reader": "market-release",
            "series_id": series,
            "series_ids": chosen,
            **release.receipt(),
        },
        "rows": rows,
        "report": {
            "rows": len(rows),
            "range_inclusive": [a.isoformat(), b.isoformat()],
            "release": release.receipt(),
            "native_labels_preserved": True,
            "repaired_rows": 0,
            "unavailable_rows": sum(bool(r.get("missing_fields")) for r in rows),
            "strict_execution_admitted": False,
            "historical_pit": False,
            "limits": LIMITATIONS,
            "input_dispositions": release.manifest["input_dispositions"],
        },
    }
