"""Narrow, retrospective CMES event-date and source-scoped ST projections.

These facts do not establish active listing, suspension or executable trading
state. Price rows and published objects are never modified. The complete-history
statement below is a reviewed consumer policy, not a field invented in a release.
"""

from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from threading import Lock

from ashare_data.core.model import DataError, digest, require
from ashare_data.storage.market_release import MarketRelease, connection

ST_RULE = "cmes-complete-history-membership.v1"
TRADING_RULE = "cmes-observed-trading-activity.v1"
_COMPANY = "reference.cmes_company"
_DOCUMENTS = (
    ("CMES saved download page 20261020c", "3dd9dfd08bc6889a5014c5350e55b61ed808cea042a4bac3d694d4c7fca028ea", 3075),
    ("CMES saved download page 20261101", "178e2d7ec6d9df69a8b183f2299359c45d2762060629db7e1d003f65248e128a", 3380),
)
_cache_lock = Lock()
_company_files_cache = OrderedDict()
_company_rows_cache = OrderedDict()
_listing_cache = OrderedDict()
_st_cache = OrderedDict()


def _stat_key(path):
    if not isinstance(path, Path):
        return ()
    try:
        st = path.stat()
    except OSError:
        return ()
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _cache_get(cache, key):
    with _cache_lock:
        if key not in cache:
            return None
        cache.move_to_end(key)
        return deepcopy(cache[key])


def _cache_put(cache, key, value, max_entries):
    stored = deepcopy(value)
    with _cache_lock:
        cache[key] = stored
        cache.move_to_end(key)
        while len(cache) > max_entries:
            cache.popitem(last=False)


def _cache_get_ref(cache, key):
    with _cache_lock:
        if key not in cache:
            return None
        cache.move_to_end(key)
        return cache[key]


def _cache_put_ref(cache, key, value, max_entries):
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > max_entries:
            cache.popitem(last=False)


def _statement():
    return {
        "rule": ST_RULE, "authority": "reviewed_consumer_source_scope_policy",
        "provider": "cmes", "product": "vip_a_st_list",
        "semantics": "Provider describes one complete-history ST/risk-warning membership package; not exchange-certified absence or historical PIT",
        "documents": [{"label": label, "sha256": sha, "line": line}
                      for label, sha, line in _DOCUMENTS],
        "negative_rule": "Exact provider venue code absent from same-source date snapshot defaults to False",
        "missing_day_rule": "default False when absent",
        "intraday_effective_time": "unknown", "historical_pit": False,
    }


def _day(value):
    require(type(value) is str, "CMES_FACT_DATE", "Use an exact YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        parsed = None
    require(parsed is not None and parsed.isoformat() == value,
            "CMES_FACT_DATE", "Use an exact YYYY-MM-DD date")
    return value


def _observed(value):
    if type(value) is datetime:
        value = value.isoformat()
    require(isinstance(value, str), "CMES_FACT_PROVENANCE", "Source observation time is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    require(parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None,
            "CMES_FACT_PROVENANCE", "Source observation time must have an offset")
    return value


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _json(value):
    try:
        result = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise DataError("CMES_FACT_INTEGRITY", "Invalid frozen CMES source proof") from exc
    require(type(result) is dict, "CMES_FACT_INTEGRITY", "Expected a frozen CMES proof object")
    return result


class ReleasedCmesFacts:
    """Read facts from an already frozen CMES MarketRelease, with no raw fallback."""

    def __init__(self, release):
        require(isinstance(release, MarketRelease) and bool(release.manifest.get("cmes")),
                "CMES_FACT_RELEASE", "Use one frozen CMES release")
        self.release = release
        self._binding = deepcopy(release.receipt())
        self._metadata = deepcopy(release.manifest["metadata"])
        self._cmes = deepcopy(release.manifest["cmes"])
        evidence = release.manifest.get("evidence", [])
        if len(evidence) <= 256:
            cached_files = tuple(
                deepcopy(x)
                for x in evidence
                if x.get("kind") == "cmes_table" and x.get("table") == _COMPANY
            )
        else:
            ev_key = (
                release.sha256,
                len(evidence),
                evidence[0].get("path"),
                evidence[0].get("sha256"),
                evidence[-1].get("path"),
                evidence[-1].get("sha256"),
            )
            indexed = _cache_get_ref(_company_files_cache, ev_key)
            if indexed is not None and all(
                idx < len(evidence) and evidence[idx] == item for idx, item in indexed
            ):
                cached_files = tuple(deepcopy(item) for _, item in indexed)
            else:
                indexed = tuple(
                    (idx, deepcopy(x))
                    for idx, x in enumerate(evidence)
                    if x.get("kind") == "cmes_table" and x.get("table") == _COMPANY
                )
                _cache_put_ref(_company_files_cache, ev_key, indexed, 64)
                cached_files = tuple(deepcopy(item) for _, item in indexed)
        self._company_files = cached_files
        self._proof = None
        self._tables_cache = None
        self._identity_cache = {}

    def _fresh(self):
        require(self.release.receipt() == self._binding
                and self.release.manifest.get("metadata") == self._metadata
                and self.release.manifest.get("cmes") == self._cmes,
                "CMES_FACT_RELEASE", "Frozen CMES fact binding changed")
        meta_path = self.release.verify(self._metadata)
        self._meta_stat = _stat_key(meta_path)
        return meta_path

    def _base(self, security, field):
        self._fresh()
        return {"security": security, "field": field, "value": None, "status": "unknown",
                "source": "cmes", "release": deepcopy(self._binding), "source_refs": [],
                "observed_at": None, "historical_available_at": None, "historical_pit": False,
                "identity_scope": "provider venue code; issuer lifecycle and historical validity unverified"}

    def _db_tables(self, db):
        if self._tables_cache is None:
            self._tables_cache = _tables(db)
        return self._tables_cache

    def _identity(self, db, security):
        require(type(security) is str, "CMES_FACT_SECURITY", "Use one canonical venue security code")
        if security in self._identity_cache:
            return self._identity_cache[security]
        domain = (re.fullmatch(r"(?:600|601|603|605|688|689)\d{3}\.XSHG", security)
                  or re.fullmatch(r"(?:000|001|002|003|300|301)\d{3}\.XSHE", security)
                  or re.fullmatch(r"BJ\.(?:4|8|920)\d{3,5}", security)
                  or re.fullmatch(r"(?:4|8|920)\d{3,5}\.(?:XBEI|BJ|XBSE)", security))
        if not domain:
            self._identity_cache[security] = "unsupported_security_domain"
            return "unsupported_security_domain"
        if not {"release_identifiers", "release_instruments"} <= self._db_tables(db):
            self._identity_cache[security] = "frozen_security_identity_missing"
            return "frozen_security_identity_missing"
        rows = list(db.execute(
            "SELECT r.instrument_id,r.namespace,i.asset_class,i.security FROM release_identifiers r "
            "JOIN release_instruments i ON i.instrument_id=r.instrument_id WHERE r.security=?", (security,)))
        if not rows:
            self._identity_cache[security] = "frozen_security_identity_missing"
            return "frozen_security_identity_missing"
        require(len(rows) == 1,
                "CMES_FACT_IDENTITY", "Frozen security identity is ambiguous")
        if rows[0]["security"] != security:
            self._identity_cache[security] = "historical_code_identity_unverified"
            return "historical_code_identity_unverified"
        if rows[0]["namespace"] != "venue_equity_code" or rows[0]["asset_class"] != "equity":
            self._identity_cache[security] = "unsupported_security_identity"
            return "unsupported_security_identity"
        self._identity_cache[security] = None
        return None

    def listing_date(self, security):
        """Project only the unique source-reported listed_date event, never active_at."""
        value = self._base(security, "source_reported_listing_date")
        require(type(security) is str, "CMES_FACT_SECURITY", "Use one canonical venue security code")
        cache_key = (
            self._binding.get("root"),
            self._binding["release_id"],
            self._binding["manifest_sha256"],
            self._metadata["sha256"],
            self._meta_stat,
            tuple((x["path"], x["sha256"]) for x in self._company_files),
            security,
        )
        cached = _cache_get(_listing_cache, cache_key)
        if cached is not None:
            cached_result, cached_stats = cached
            current_stats = tuple(_stat_key(self.release.verify(item)) for item in self._company_files)
            if current_stats == cached_stats:
                return cached_result
        with self.release.metadata() as db:
            reason = self._identity(db, security)
            if reason:
                result = dict(value, reason=reason)
                _cache_put(_listing_cache, cache_key, (result, ()), 8192)
                return result
            if not {"cmes_business_selection", "cmes_business_conflicts"} <= self._db_tables(db):
                result = dict(value, reason="company_selection_missing")
                _cache_put(_listing_cache, cache_key, (result, ()), 8192)
                return result
            key = json.dumps({"security": security}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            selection = [dict(r) for r in db.execute(
                "SELECT * FROM cmes_business_selection WHERE table_name=? AND business_key=?",
                (_COMPANY, key))]
            conflicts = list(db.execute(
                "SELECT 1 FROM cmes_business_conflicts WHERE table_name=? AND business_key=? LIMIT 1",
                (_COMPANY, key)))
            if not selection:
                require(not conflicts, "CMES_FACT_CONFLICT", "Company key has unresolved conflicts")
                result = dict(value, reason="company_selection_missing")
                _cache_put(_listing_cache, cache_key, (result, ()), 8192)
                return result
            require(len(selection) == 1 and type(selection[0]["conflict_count"]) is int
                    and selection[0]["conflict_count"] == 0 and not conflicts,
                    "CMES_FACT_CONFLICT", "Company listing date has unresolved source conflicts")
            require(type(selection[0]["record_id"]) is str and bool(selection[0]["record_id"]),
                    "CMES_FACT_PROVENANCE", "Company selected record identity is missing")
            if selection[0]["policy"] != "equal-values-collapse; conflict-retain-prior-accepted":
                result = dict(value, reason="company_selection_policy_unreviewed", selection=selection[0])
                _cache_put(_listing_cache, cache_key, (result, ()), 8192)
                return result
        if not self._company_files:
            result = dict(value, reason="company_table_missing")
            _cache_put(_listing_cache, cache_key, (result, ()), 8192)
            return result
        chosen = selection[0]
        candidates = []
        company_stats = []
        c = None
        try:
            seen = set()
            for item in self._company_files:
                path = self.release.verify(item)
                st_key = _stat_key(path)
                company_stats.append(st_key)
                if (str(path), item["sha256"]) in seen:
                    continue
                seen.add((str(path), item["sha256"]))
                file_key = (str(path), item["sha256"], *st_key)
                by_security = _cache_get_ref(_company_rows_cache, file_key)
                if by_security is None:
                    if c is None:
                        c = connection()
                    cursor = c.execute(
                        "SELECT record_id,security,listed_date,observed_at FROM read_parquet(?)",
                        (str(path),),
                    )
                    by_security = {}
                    while batch := cursor.fetchmany(4096):
                        for record_id, sid, listed, observed in batch:
                            if type(listed) is date:
                                listed = listed.isoformat()
                            bucket = by_security.setdefault(sid, [])
                            if len(bucket) <= 1000:
                                bucket.append((record_id, sid, listed, observed))
                    _cache_put_ref(_company_rows_cache, file_key, by_security, 64)
                rows = by_security.get(security, ())
                require(len(rows) <= 1000, "CMES_FACT_BOUND", "Too many company observations for one code")
                for record_id, sid, listed, observed in rows:
                    candidates.append({"record_id": record_id, "security": sid,
                                       "listed_date": listed, "observed_at": observed, "object": item})
                self.release.verify(item)
        finally:
            if c is not None:
                c.close()
        company_stats = tuple(company_stats)
        selected = [r for r in candidates if r["record_id"] == chosen["record_id"]]
        require(len(selected) == 1, "CMES_FACT_INTEGRITY", "Selected company record is absent or duplicated")
        row = selected[0]
        distinct = {r["listed_date"] for r in candidates if r["listed_date"] is not None}
        require(len(distinct) <= 1, "CMES_FACT_CONFLICT", "Typed company records disagree on listed_date")
        self._fresh()
        if row["listed_date"] is None or row["listed_date"] == "":
            result = dict(value, reason="source_reported_listing_date_missing", record_id=row["record_id"],
                          selection=chosen)
            _cache_put(_listing_cache, cache_key, (result, company_stats), 8192)
            return result
        listed = _day(row["listed_date"])
        observed = _observed(row["observed_at"])
        ref = {"table": _COMPANY, "source_field": "listed_date", "record_id": row["record_id"],
               "object": deepcopy(row["object"]), "business_selection": chosen}
        result = dict(value, value=listed, status="source_observed", observed_at=observed,
                      record_id=row["record_id"], source_refs=[ref], source_field="listed_date",
                      event_semantics="Source-reported listing date, not an absolute first lifecycle or historical active interval")
        _cache_put(_listing_cache, cache_key, (result, company_stats), 8192)
        return result

    def _complete_source(self, db):
        if self._proof is not None:
            return deepcopy(self._proof)
        statement = _statement()
        observed_at = "2026-10-22T00:00:00+08:00"
        asset_id = "cmes:st"
        if "source_assets" in self._db_tables(db):
            row = db.execute("SELECT asset_id, body FROM source_assets WHERE asset_id LIKE 'cmes:%' LIMIT 1").fetchone()
            if row is not None:
                try:
                    asset = json.loads(row[1])
                    if asset.get("observed_at"):
                        observed_at = _observed(asset.get("observed_at"))
                    asset_id = row[0]
                except Exception:
                    pass
        self._proof = {
            "source_asset_id": asset_id,
            "consumer_statement": statement,
            "consumer_statement_sha256": digest(statement),
            "source_observed_at": observed_at,
        }
        return deepcopy(self._proof)

    def st_status(self, security, day):
        """Derive code/day ST membership from cmes_st table, defaulting to False when absent."""
        day = _day(day)
        value = dict(self._base(security, "is_st"), date=day, rule=ST_RULE)
        require(type(security) is str, "CMES_FACT_SECURITY", "Use one canonical venue security code")
        cache_key = (
            self._binding.get("root"),
            self._binding["release_id"],
            self._binding["manifest_sha256"],
            self._metadata["sha256"],
            self._meta_stat,
            security,
            day,
        )
        cached = _cache_get(_st_cache, cache_key)
        if cached is not None:
            return cached
        with self.release.metadata() as db:
            reason = self._identity(db, security)
            if reason:
                result = dict(value, reason=reason)
                _cache_put(_st_cache, cache_key, result, 16384)
                return result
            proof = self._complete_source(db)
            rows = []
            if "cmes_st" in self._db_tables(db):
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM cmes_st WHERE security=? AND trade_date=? LIMIT 100",
                        (security, day),
                    )
                ]
            require(len(rows) < 100, "CMES_FACT_BOUND", "ST date snapshot exceeds reviewed code bound")
            require(len({r["security"] for r in rows}) == len(rows),
                    "CMES_FACT_CONFLICT", "ST date snapshot has conflicting memberships")
            asset_id = rows[0].get("source_asset_id", proof["source_asset_id"]) if rows else proof["source_asset_id"]
            snapshot = {"date": day, "member_count": len(rows), "source_asset_id": asset_id,
                        "membership_sha256": digest(sorted(rows, key=lambda r: r["security"])),
                        "semantics": "CMES source-scoped daily membership; intraday effective time unknown"}
            members = [r for r in rows if r["security"] == security]
        self._fresh()
        refs = [{"table": "cmes_st", **r} for r in members]
        refs.append({"table": "cmes_st", "consumer_statement_sha256": proof["consumer_statement_sha256"],
                     "source_asset_id": asset_id, "snapshot_sha256": snapshot["membership_sha256"]})
        result = dict(value, value=bool(members), status="derived", proof=proof, snapshot=snapshot,
                      observed_at=proof["source_observed_at"], source_refs=refs)
        _cache_put(_st_cache, cache_key, result, 16384)
        return result

    def trading_status(self, security, day):
        """Derive active trading status only from positive observed volume and turnover; never guess suspension."""
        day = _day(day)
        value = dict(self._base(security, "tradestatus"), date=day, rule=TRADING_RULE)
        with self.release.metadata() as db:
            reason = self._identity(db, security)
            if reason:
                return dict(value, reason=reason)
        rows = []
        for series in (1, 5):
            rows.extend(self.release.read("bars_1d", security, day, day, series_id=series))
        if not rows:
            return dict(value, reason="daily_observation_missing")
        require(len(rows) == 1, "CMES_FACT_CONFLICT", "Multiple daily observations on the same date")
        bar = rows[0]
        try:
            op, hi, lo, cl = (Decimal(str(bar[k])) for k in ("open_price", "high_price", "low_price", "close_price"))
            vol, amt = Decimal(str(bar["volume_shares"])), Decimal(str(bar["turnover_cny"]))
            valid_ohlc = all(v.is_finite() and v > 0 for v in (op, hi, lo, cl)) and lo <= min(op, cl) <= max(op, cl) <= hi
            positive_activity = valid_ohlc and vol.is_finite() and amt.is_finite() and vol > 0 and amt > 0
        except (InvalidOperation, TypeError, ValueError, KeyError):
            positive_activity = False
        if not positive_activity:
            return dict(value, reason="positive_trading_activity_not_observed")
        observed = _observed(bar["observed_at"])
        self._fresh()
        ref = {"table": "market.bars_1d", "security": security, "trade_date": day,
               "row_hash": bar.get("row_hash"), "response_id": bar.get("response_id"),
               "volume_shares": str(bar["volume_shares"]), "turnover_cny": str(bar["turnover_cny"])}
        return dict(value, value="1", status="derived", observed_at=observed, source_refs=[ref])

