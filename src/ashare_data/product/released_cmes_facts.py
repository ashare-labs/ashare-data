"""Narrow, retrospective CMES event-date and source-scoped ST projections.

These facts do not establish active listing, suspension or executable trading
state. Price rows and published objects are never modified. The complete-history
statement below is a reviewed consumer policy, not a field invented in a release.
"""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re

from ashare_data.core.model import DataError, digest, require
from ashare_data.storage.market_release import MarketRelease, connection

ST_RULE = "cmes-complete-history-membership.v1"
TRADING_RULE = "cmes-observed-trading-activity.v1"
_ST_SHA = "c75f0048296e5223a82746ea30e0954db515b9ffa1aea0aa575229f47f422308"
_ST_ASSET = "cmes:" + _ST_SHA
_ST_IMPORT = "cmes-import:b29686ed8e88dde31dff1a975636b2de607d3ea2ec65a1914bbcf89391b1ef2f"
_ST_ROWS = 683228
_ST_BYTES = 2246778
_ST_FIRST, _ST_LAST = "2001-01-02", "2026-10-22"
_COMPANY = "reference.cmes_company"
_DOCUMENTS = (
    ("CMES saved download page 20261020c", "3dd9dfd08bc6889a5014c5350e55b61ed808cea042a4bac3d694d4c7fca028ea", 3075),
    ("CMES saved download page 20261101", "178e2d7ec6d9df69a8b183f2299359c45d2762060629db7e1d003f65248e128a", 3380),
)


def _statement():
    return {
        "rule": ST_RULE, "authority": "reviewed_consumer_source_scope_policy",
        "provider": "cmes", "product": "vip_a_st_list",
        "semantics": "Provider describes one complete-history ST/risk-warning membership package; not exchange-certified absence or historical PIT",
        "source_sha256": _ST_SHA, "source_bytes": _ST_BYTES,
        "input_rows": _ST_ROWS, "first_date": _ST_FIRST, "last_date": _ST_LAST,
        "documents": [{"label": label, "sha256": sha, "line": line}
                      for label, sha, line in _DOCUMENTS],
        "negative_rule": "Exact provider venue code absent from a nonempty, fully admitted same-source date snapshot",
        "missing_day_rule": "unknown; no forward/backward fill",
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
        self._company_files = tuple(deepcopy(x) for x in release.manifest.get("evidence", [])
                                    if x.get("kind") == "cmes_table" and x.get("table") == _COMPANY)
        self._proof = None

    def _fresh(self):
        require(self.release.receipt() == self._binding
                and self.release.manifest.get("metadata") == self._metadata
                and self.release.manifest.get("cmes") == self._cmes,
                "CMES_FACT_RELEASE", "Frozen CMES fact binding changed")
        self.release.verify(self._metadata)

    def _base(self, security, field):
        self._fresh()
        return {"security": security, "field": field, "value": None, "status": "unknown",
                "source": "cmes", "release": deepcopy(self._binding), "source_refs": [],
                "observed_at": None, "historical_available_at": None, "historical_pit": False,
                "identity_scope": "provider venue code; issuer lifecycle and historical validity unverified"}

    @staticmethod
    def _identity(db, security):
        require(type(security) is str, "CMES_FACT_SECURITY", "Use one canonical venue security code")
        # Restrict negative membership to the A-share code domains actually
        # examined in the reviewed package; do not infer coverage for indices/B shares.
        domain = (re.fullmatch(r"(?:600|601|603|605|688)\d{3}\.XSHG", security)
                  or re.fullmatch(r"(?:000|001|002|003|300|301)\d{3}\.XSHE", security)
                  or re.fullmatch(r"BJ\.(?:430|835|839|920)\d{3}", security))
        if not domain:
            return "unsupported_security_domain"
        if not {"release_identifiers", "release_instruments"} <= _tables(db):
            return "frozen_security_identity_missing"
        rows = list(db.execute(
            "SELECT r.instrument_id,r.namespace,i.asset_class,i.security FROM release_identifiers r "
            "JOIN release_instruments i ON i.instrument_id=r.instrument_id WHERE r.security=?", (security,)))
        if not rows:
            return "frozen_security_identity_missing"
        require(len(rows) == 1,
                "CMES_FACT_IDENTITY", "Frozen security identity is ambiguous")
        if rows[0]["security"] != security:
            return "historical_code_identity_unverified"
        if rows[0]["namespace"] != "venue_equity_code" or rows[0]["asset_class"] != "equity":
            return "unsupported_security_identity"
        return None

    def listing_date(self, security):
        """Project only the unique source-reported listed_date event, never active_at."""
        value = self._base(security, "source_reported_listing_date")
        with self.release.metadata() as db:
            reason = self._identity(db, security)
            if reason:
                return dict(value, reason=reason)
            if not {"cmes_business_selection", "cmes_business_conflicts"} <= _tables(db):
                return dict(value, reason="company_selection_missing")
            key = json.dumps({"security": security}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            selection = [dict(r) for r in db.execute(
                "SELECT * FROM cmes_business_selection WHERE table_name=? AND business_key=?",
                (_COMPANY, key))]
            conflicts = list(db.execute(
                "SELECT 1 FROM cmes_business_conflicts WHERE table_name=? AND business_key=? LIMIT 1",
                (_COMPANY, key)))
            if not selection:
                require(not conflicts, "CMES_FACT_CONFLICT", "Company key has unresolved conflicts")
                return dict(value, reason="company_selection_missing")
            require(len(selection) == 1 and type(selection[0]["conflict_count"]) is int
                    and selection[0]["conflict_count"] == 0 and not conflicts,
                    "CMES_FACT_CONFLICT", "Company listing date has unresolved source conflicts")
            require(type(selection[0]["record_id"]) is str and bool(selection[0]["record_id"]),
                    "CMES_FACT_PROVENANCE", "Company selected record identity is missing")
            if selection[0]["policy"] != "equal-values-collapse; conflict-retain-prior-accepted":
                return dict(value, reason="company_selection_policy_unreviewed", selection=selection[0])
        if not self._company_files:
            return dict(value, reason="company_table_missing")
        chosen = selection[0]
        candidates = []
        c = connection()
        try:
            seen = set()
            for item in self._company_files:
                path = self.release.verify(item)
                if (str(path), item["sha256"]) in seen:
                    continue
                seen.add((str(path), item["sha256"]))
                cursor = c.execute("SELECT record_id,security,listed_date,observed_at FROM read_parquet(?) "
                                   "WHERE security=?", (str(path), security))
                rows = cursor.fetchmany(1001)
                require(len(rows) <= 1000, "CMES_FACT_BOUND", "Too many company observations for one code")
                for record_id, sid, listed, observed in rows:
                    if type(listed) is date:
                        listed = listed.isoformat()
                    candidates.append({"record_id": record_id, "security": sid,
                                       "listed_date": listed, "observed_at": observed, "object": item})
                self.release.verify(item)
        finally:
            c.close()
        selected = [r for r in candidates if r["record_id"] == chosen["record_id"]]
        require(len(selected) == 1, "CMES_FACT_INTEGRITY", "Selected company record is absent or duplicated")
        row = selected[0]
        distinct = {r["listed_date"] for r in candidates if r["listed_date"] is not None}
        require(len(distinct) <= 1, "CMES_FACT_CONFLICT", "Typed company records disagree on listed_date")
        self._fresh()
        if row["listed_date"] is None or row["listed_date"] == "":
            return dict(value, reason="source_reported_listing_date_missing", record_id=row["record_id"],
                        selection=chosen)
        listed = _day(row["listed_date"])
        observed = _observed(row["observed_at"])
        ref = {"table": _COMPANY, "source_field": "listed_date", "record_id": row["record_id"],
               "object": deepcopy(row["object"]), "business_selection": chosen}
        return dict(value, value=listed, status="source_observed", observed_at=observed,
                    record_id=row["record_id"], source_refs=[ref], source_field="listed_date",
                    event_semantics="Source-reported listing date, not an absolute first lifecycle or historical active interval")

    def _complete_source(self, db):
        if self._proof is not None:
            return deepcopy(self._proof)
        required = {"source_assets", "cmes_imports", "cmes_ingested", "batch_inputs", "cmes_st"}
        if not required <= _tables(db):
            return None
        source = db.execute("SELECT body FROM source_assets WHERE asset_id=?", (_ST_ASSET,)).fetchone()
        imported = db.execute("SELECT body FROM cmes_imports WHERE import_id=?", (_ST_IMPORT,)).fetchone()
        if source is None or imported is None:
            return None
        asset, report = _json(source[0]), _json(imported[0])
        require(asset.get("asset_id") == _ST_ASSET and asset.get("source") == "cmes"
                and asset.get("kind") == "st" and asset.get("sha256") == _ST_SHA
                and asset.get("bytes") == _ST_BYTES,
                "CMES_FACT_INTEGRITY", "Reviewed ST source identity changed")
        require(type(report.get("dispositions")) is dict,
                "CMES_FACT_INTEGRITY", "ST source import has no disposition object")
        disposition = report["dispositions"].get(_ST_ASSET)
        require(type(disposition) is dict and all(type(v) is int for v in disposition.values())
                and disposition == {"input_rows": _ST_ROWS, "adopted": _ST_ROWS, "equal_duplicate": 0},
                "CMES_FACT_INTEGRITY", "Reviewed ST source was not completely admitted")
        batches = [r[0] for r in db.execute("SELECT batch_id FROM cmes_ingested WHERE asset_id=?", (_ST_ASSET,))]
        require(batches == [2] and db.execute(
            "SELECT 1 FROM batch_inputs WHERE batch_id=2 AND asset_id=?", (_ST_ASSET,)).fetchone(),
            "CMES_FACT_INTEGRITY", "ST complete source is not bound to its reviewed import batch")
        count = db.execute("SELECT count(*) FROM cmes_st WHERE source_asset_id=?", (_ST_ASSET,)).fetchone()[0]
        require(count == _ST_ROWS, "CMES_FACT_INTEGRITY", "Published ST membership row accounting differs")
        statement = _statement()
        self._proof = {"source_asset_id": _ST_ASSET, "source_sha256": _ST_SHA,
                       "import_id": _ST_IMPORT, "batch_id": 2, "disposition": disposition,
                       "published_membership_rows": count, "source_observed_at": _observed(asset.get("observed_at")),
                       "consumer_statement": statement, "consumer_statement_sha256": digest(statement)}
        return deepcopy(self._proof)

    def st_status(self, security, day):
        """Derive code/day membership only within one fully reviewed source snapshot."""
        day = _day(day)
        value = dict(self._base(security, "is_st"), date=day, rule=ST_RULE)
        with self.release.metadata() as db:
            reason = self._identity(db, security)
            if reason:
                return dict(value, reason=reason)
            if not _ST_FIRST <= day <= _ST_LAST:
                return dict(value, reason="outside_reviewed_source_dates")
            proof = self._complete_source(db)
            if proof is None:
                return dict(value, reason="complete_source_proof_missing")
            rows = [dict(r) for r in db.execute("SELECT * FROM cmes_st WHERE trade_date=? LIMIT 10001", (day,))]
            require(len(rows) <= 10000, "CMES_FACT_BOUND", "ST date snapshot exceeds reviewed code bound")
            if not rows:
                return dict(value, reason="date_snapshot_missing", proof=proof)
            if any(r["source_asset_id"] != _ST_ASSET for r in rows):
                return dict(value, reason="unsupported_snapshot_assets", proof=proof)
            require(all(r["status_type"] == "ST" and r["status_name"] == "风险警示板"
                        and type(r["source_row"]) is int and 2 <= r["source_row"] <= _ST_ROWS + 1
                        and r["source_member"] == "ST股票列表.csv" for r in rows),
                    "CMES_FACT_CONFLICT", "ST snapshot has invalid or conflicting membership provenance")
            require(len({r["security"] for r in rows}) == len(rows),
                    "CMES_FACT_CONFLICT", "ST date snapshot has conflicting memberships")
            snapshot = {"date": day, "member_count": len(rows), "source_asset_id": _ST_ASSET,
                        "membership_sha256": digest(sorted(rows, key=lambda r: r["security"])),
                        "semantics": "CMES source-scoped daily membership; intraday effective time unknown"}
            members = [r for r in rows if r["security"] == security]
        self._fresh()
        refs = [{"table": "cmes_st", **r} for r in members]
        refs.append({"table": "cmes_st", "consumer_statement_sha256": proof["consumer_statement_sha256"],
                     "source_asset_id": _ST_ASSET, "snapshot_sha256": snapshot["membership_sha256"]})
        return dict(value, value=bool(members), status="derived", proof=proof, snapshot=snapshot,
                    observed_at=proof["source_observed_at"], source_refs=refs)

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

