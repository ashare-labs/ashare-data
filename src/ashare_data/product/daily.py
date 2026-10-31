"""Read-only product access to real local observations, with no network or fallback."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import re

from ashare_data.core.model import DataError, day, digest, require
from ashare_data.market.client import _security
from ashare_data.storage.local_daily import LocalDailyStore

SOURCES = ("auto", "canonical", "baostock", "eastmoney", "sina", "sohu", "tencent", "free_stockdb", "cmes")
LIMITATIONS = [
    "Original prices and share volumes; no adjustment or cross-source field filling.",
    "Auto selects one source per security for the entire requested interval by observed days, "
    "amount coverage, latest date, then source name; this is not quality certification.",
    "Current listing facts and names are retrospective; historical PIT is not verified.",
    "The stored calendar is provider-scoped, not independently exchange-certified.",
    "Corporate actions, historical price limits, financials and factors are not included.",
    "Missing or zero-volume rows do not by themselves prove a suspension.",
]


def _sid(symbol):
    if symbol.startswith("bj"):
        return "BJ." + symbol[2:]
    return symbol[2:] + (".XSHG" if symbol.startswith("sh") else ".XSHE")


def _native_security(security):
    if isinstance(security, str) and re.fullmatch(r"BJ\.[0-9]{6}", security):
        return "bj" + security[3:]
    return _security(security)


def _selection(securities, start, end, source, *, native_security=_native_security):
    require(
        isinstance(securities, (list, tuple)) and bool(securities),
        "PRODUCT_SECURITY_SELECTION",
        "Provide a nonempty selection of securities",
    )
    symbols = [native_security(s) for s in securities]
    require(
        len(set(symbols)) == len(symbols), "PRODUCT_DUPLICATE_SECURITY", "Use unique securities"
    )
    start, end = day(start), day(end)
    require(start <= end, "PRODUCT_DATE_RANGE", "Start must be on or before end")
    require(source in SOURCES, "PRODUCT_SOURCE", "Choose an explicit supported source or auto")
    return sorted(securities), start, end


class LocalDailyProduct:
    """Open the selected library afresh for each consistent SQLite read transaction.

    A root is mandatory. No default evaluation store, collection or directory creation
    is performed. This object can be shared by API threads; connections cannot.
    """

    _native_security = staticmethod(_native_security)

    def __init__(self, root, *, references=None):
        self.root = Path(root).expanduser().resolve()
        self.references = references
        require(
            (self.root / "daily.sqlite3").is_file(),
            "PRODUCT_NOT_CONFIGURED",
            "The configured local daily library is missing; select its existing root",
        )

    @classmethod
    def from_deployment(cls, deployment=None, *, profile="product", reference_set=None):
        from ashare_data.product.configuration import resolve

        config = resolve(deployment, profile=profile, reference_set=reference_set)
        if config.get("market_release"):
            from ashare_data.product.market_release import ReleasedDailyProduct

            return ReleasedDailyProduct(config)
        return cls(config["daily_root"], references=config["references"])

    @contextmanager
    def _read(self):
        store = None
        try:
            store = LocalDailyStore(self.root, readonly=True)
            store.db.execute("PRAGMA query_only=ON")
            store.db.execute("BEGIN")
            yield store
        except sqlite3.Error as exc:
            raise DataError(
                "PRODUCT_LIBRARY_UNAVAILABLE",
                "Cannot read the configured daily library",
                {"reason": str(exc)},
            ) from exc
        finally:
            if store is not None:
                store.close()

    def _receipt(self, store, operation, request, rows, calendar, listing):
        response_ids = sorted(
            {r["response_id"] for r in rows if r.get("response_id")}
            | {r["receipt"] for r in calendar if r.get("receipt")}
        )
        responses = []
        for first in range(0, len(response_ids), 400):
            ids = response_ids[first : first + 400]
            responses.extend(
                dict(r)
                for r in store.db.execute(
                    "SELECT * FROM responses WHERE id IN ("
                    + ",".join("?" for _ in ids)
                    + ") ORDER BY id",
                    ids,
                )
            )
        # Listing receipts use the upstream body SHA rather than the responses ID.
        listing_hashes = sorted({r["receipt"] for r in listing if r.get("receipt")})
        for first in range(0, len(listing_hashes), 400):
            hashes = listing_hashes[first : first + 400]
            responses.extend(
                dict(r)
                for r in store.db.execute(
                    "SELECT * FROM responses WHERE sha256 IN ("
                    + ",".join("?" for _ in hashes)
                    + ") ORDER BY id",
                    hashes,
                )
            )
        responses = sorted({r["id"]: r for r in responses}.values(), key=lambda r: r["id"])
        value = {
            "schema": "ad-local-daily-read.v1",
            "operation": operation,
            "asset": {"kind": "local_daily", "root": str(self.root), "database": "daily.sqlite3"},
            "request": request,
            "transaction": "single SQLite read transaction; query_only",
            "rows_sha256": digest(rows),
            "calendar_sha256": digest(calendar),
            "listing_sha256": digest(listing),
            "responses": responses,
            "missing_response_ids": sorted(set(response_ids) - {r["id"] for r in responses}),
            "source_calendar_scope": store.meta("calendar_scope"),
            "historical_pit_verified": False,
            "network_used": False,
        }
        return {**value, "receipt_sha256": digest(value)}

    def catalogue(self, query="", limit=100, offset=0):
        """Page current names and actual stored per-source ranges, including empty entries."""
        require(isinstance(query, str) and len(query) <= 100, "PRODUCT_QUERY", "Query too long")
        require(
            type(limit) is int and 1 <= limit <= 500 and type(offset) is int and offset >= 0,
            "PRODUCT_PAGINATION",
            "Use limit 1..500 and nonnegative offset",
        )
        query = query.strip()
        needle = query.replace(".XSHG", "").replace(".XSHE", "")
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where = " WHERE symbol LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'"
        args = ("%" + escaped + "%",) * 2
        with self._read() as store:
            total = store.db.execute("SELECT count(*) FROM instruments" + where, args).fetchone()[0]
            listing = [
                dict(r)
                for r in store.db.execute(
                    "SELECT * FROM instruments" + where + " ORDER BY symbol LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]
            items = []
            for item in listing:
                sources = [
                    dict(r)
                    for r in store.db.execute(
                        "SELECT b.source,count(*) AS rows,min(b.date) AS first,max(b.date) AS last "
                        "FROM bars b JOIN calendar c ON c.date=b.date "
                        "WHERE b.symbol=? GROUP BY b.source ORDER BY b.source",
                        (item["symbol"],),
                    )
                ]
                coverage = store.db.execute(
                    "SELECT * FROM coverage WHERE symbol=?", (item["symbol"],)
                ).fetchone()
                items.append(
                    {
                        "security": _sid(item["symbol"]),
                        "name": item["name"],
                        "name_is_historical": False,
                        "ipo": item["ipo"],
                        "out": item["out"],
                        "board": item["board"],
                        "metadata_source": item["metadata_source"],
                        "first_bar": min((r["first"] for r in sources), default=None),
                        "last_bar": max((r["last"] for r in sources), default=None),
                        "sources": sources,
                        "coverage": dict(coverage) if coverage else None,
                        "coverage_semantics": "last maintenance audit; prices recheck requested scope",
                        "range_semantics": "observations on the stored calendar; listing/code filters are rechecked by prices",
                    }
                )
            receipt = self._receipt(
                store,
                "catalogue",
                dict(query=query, limit=limit, offset=offset),
                items,
                [],
                listing,
            )
            return {
                "schema": "ad-local-daily-catalogue.v1",
                "query": query,
                "limit": limit,
                "offset": offset,
                "total": total,
                "securities": items,
                "receipt": receipt,
                "limitations": list(LIMITATIONS),
            }

    def _prices(self, store, securities, start, end, source, *, selected_sources=None):
        require(
            source not in ("free_stockdb", "cmes") or hasattr(store, "release"),
            "MARKET_SOURCE_SCOPE",
            "File-source daily reads require a pinned market release",
        )
        calendar = [
            dict(r)
            for r in store.db.execute(
                "SELECT * FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
                (start, end),
            )
        ]
        days = [r["date"] for r in calendar]
        rows, gaps, coverage, listing = [], [], [], []
        if not days:
            gaps.append(
                {
                    "code": "CALENDAR_NO_SESSIONS",
                    "field": "calendar",
                    "detail": "The stored calendar has no sessions in this interval",
                }
            )
        for sid in securities:
            native = self._native_security(sid)
            facts = [
                dict(r)
                for r in store.db.execute(
                    "SELECT * FROM listing_facts WHERE symbol=? ORDER BY ipo,observed_at,id",
                    (native,),
                )
            ]
            listing.extend(facts)
            selected_source = selected_sources[sid] if selected_sources is not None else source
            frame = store.get_price(sid, start=start, end=end, source=selected_source)
            selected = frame.attrs["source"]
            records = frame.astype(object).where(frame.notna(), None).to_dict("records")
            converted = [{"security": sid, **r} for r in records]
            for row in converted:
                row.pop("symbol", None)
            rows.extend(converted)
            returned = {r["date"] for r in records}
            expected = set(store.expected(native, start, end, days))
            if not facts:
                gaps.append(
                    {
                        "code": "SECURITY_UNAVAILABLE",
                        "security": sid,
                        "detail": "No listing facts exist in the configured library",
                    }
                )
            for missing in sorted(set(days) - returned):
                gaps.append(
                    {
                        "code": "MISSING_DAILY" if missing in expected else "OUTSIDE_LISTING",
                        "security": sid,
                        "date": missing,
                        "field": "daily",
                        "detail": "Missing observation; suspension is not inferred"
                        if missing in expected
                        else "Outside the recorded listing/code-valid interval",
                    }
                )
            coverage.append(
                {
                    "security": sid,
                    "source": selected,
                    "expected_days": len(days),
                    "listing_days": len(expected),
                    "returned_days": len(records),
                    "missing_days": len(set(days) - returned),
                    "first": records[0]["date"] if records else None,
                    "last": records[-1]["date"] if records else None,
                    "source_metadata": frame.attrs,
                }
            )
        rows.sort(key=lambda r: (r["security"], r["date"]))
        request = dict(securities=securities, start=start, end=end, source=source)
        if selected_sources is not None:
            request["selected_sources"] = selected_sources
        receipt = self._receipt(store, "prices", request, rows, calendar, listing)
        return {
            "schema": "ad-local-daily-prices.v1",
            **request,
            "price_basis": "raw",
            "price_unit": "CNY/share",
            "volume_unit": "share",
            "amount_unit": "CNY",
            "rows": rows,
            "calendar": days,
            "calendar_evidence": calendar,
            "listing_evidence": listing,
            "gaps": gaps,
            "coverage": coverage,
            "receipt": receipt,
            "limitations": list(LIMITATIONS),
        }

    def prices(
        self, securities, start, end, source="auto", *,
        adjustment=None, basis_date=None, factors=None,
    ):
        from ashare_data.providers.cmes import adjust_prices

        securities, start, end = _selection(securities, start, end, source, native_security=self._native_security)
        with self._read() as store:
            raw = self._prices(store, securities, start, end, source)
        return adjust_prices(raw, factors=factors, adjustment=adjustment, basis_date=basis_date)

    def coverage(self, securities, start, end, source="auto", *, references=None):
        from ashare_data.product.publication import coverage

        securities, start, end = _selection(securities, start, end, source)
        return coverage(
            self,
            securities,
            start,
            end,
            source,
            self.references if references is None else references,
        )

    def publish(
        self,
        store_root,
        securities,
        start,
        end,
        source="auto",
        *,
        mode="covered_research",
        references=None,
    ):
        from ashare_data.product.publication import publish

        securities, start, end = _selection(securities, start, end, source)
        return publish(
            self,
            store_root,
            securities,
            start,
            end,
            source,
            mode=mode,
            references=self.references if references is None else references,
        )
