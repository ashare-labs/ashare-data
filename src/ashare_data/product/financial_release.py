"""Frozen public financial/reference tables and bounded point queries.

SQL exposes retained source observations; calculation requires an explicit mode,
field, source and cutoff. No source priority, capture-time revision ordering,
unit guessing, filling or cross-code propagation.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3

from ashare_data.core.model import require
from ashare_data.financials import tables as t
from ashare_data.storage.financial_release import (
    CONTRACT_NAME, table_views, metadata_views, verify_object, checked_path,
)
from ashare_data.storage.market_release import MarketRelease, fingerprint

SHANGHAI = timezone(timedelta(hours=8))


def timestamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    require(result.tzinfo is not None, "FIN_TIME", "Use a timezone-aware cutoff")
    return result


class FinancialTables:
    def __init__(self, release):
        require(isinstance(release, MarketRelease), "FIN_RELEASE", "Use a frozen MarketRelease")
        require(CONTRACT_NAME in release.manifest, "FIN_RELEASE", "Financial tables are not published in this release")
        self.release = release
        self.candidate_root = None
        self.candidate = None
        self.files = [x for x in release.manifest.get("evidence", []) if x.get("kind") == "financial_table"]

    @classmethod
    def prepared(cls, root, parent):
        """Explicitly inspect an unpublished candidate without changing CURRENT."""
        obj = object.__new__(cls)
        obj.candidate_root = Path(root).resolve()
        obj.candidate = json.loads((obj.candidate_root / "candidate.json").read_text())
        obj._verified = {}
        require(obj.candidate["parent_manifest_sha256"] == parent.sha256,
                "FIN_PARENT", "Prepared query requires its frozen market parent")
        obj.release = parent
        obj.files = obj.candidate["files"]
        return obj

    def _candidate_object(self, item):
        path = checked_path(self.candidate_root, item["path"])
        before = fingerprint(path)
        key = (item["sha256"], str(path))
        if self._verified.get(key) != before:
            verify_object(self.candidate_root, item)
            require(fingerprint(path) == before, "FIN_OBJECT", "Candidate changed during verification")
            self._verified[key] = before
        return path

    def receipt(self):
        return dict(self.release.receipt(), financial_schema=t.VERSION,
                    financial_published=self.candidate is None,
                    candidate_id=self.candidate["candidate_id"] if self.candidate else None,
                    full_history_complete=False)

    def sql(self, *, market_tables=(), securities=None):
        c = self.release.sql(tables=market_tables, securities=securities)
        try:
            verify = self._candidate_object if self.candidate else self.release.verify
            table_views(c, self.files, verify)
            if self.candidate:
                p = self._candidate_object(self.candidate["metadata_delta"])
                db = sqlite3.connect(p.as_uri() + "?mode=ro&immutable=1", uri=True)
                try:
                    metadata_views(c, db)
                finally:
                    db.close()
            else:
                with self.release.metadata() as db:
                    metadata_views(c, db)
            c.execute("""CREATE OR REPLACE VIEW reference.security_lifetimes AS
              SELECT * FROM reference.instrument_identifiers""")
            c.execute("""CREATE OR REPLACE VIEW financial.capture_times AS
              SELECT l.from_type,l.from_id,min(CAST(c.observed_at AS TIMESTAMPTZ)) AS first_captured_at
              FROM financial.links l JOIN meta.financial_captures c ON c.capture_id=l.to_id
              WHERE l.relation='captured_in' AND l.to_type='capture' GROUP BY ALL""")
            if "bars_1d" in market_tables:
                names = {r[0] for r in c.execute("DESCRIBE market.bars_1d").fetchall()}
                if {"tradestatus", "is_st"} <= names:
                    c.execute("""CREATE OR REPLACE VIEW reference.daily_attached_status AS
                      SELECT instrument_id,trade_date,series_id,tradestatus,is_st,batch_id
                      FROM market.bars_1d""")
            return c
        except BaseException:
            c.close()
            raise

    def point(self, security, field, period, *, source, as_of, mode="historical_pit"):
        require(mode in {"historical_pit", "official_daily", "source_research", "listed_security"},
                "FIN_MODE", "Choose an explicit supported financial query mode")
        cutoff = timestamp(as_of)
        require(security in self.release.identifiers, "FIN_SECURITY", "Unknown frozen security identifier")
        c = self.sql()
        try:
            rows = c.execute("""SELECT o.*,r.source,i.valid_from,i.valid_to,i.identity_status,
              ct.first_captured_at FROM financial.observations o
              JOIN financial.report_versions r USING(report_version_id)
              JOIN reference.issuer_instruments i ON i.issuer_id=o.issuer_id
              LEFT JOIN financial.capture_times ct ON ct.from_type='report' AND ct.from_id=o.report_version_id
              WHERE i.instrument_id=? AND json_extract_string(i.body,'$.security')=?
               AND r.source=? AND o.canonical_field=? AND o.period_end=?
              LIMIT 10001""", [self.release.identifiers[security], security, source, field, period]).fetchall()
            require(len(rows) <= 10000, "FIN_QUERY_BOUND", "Narrow the financial point query")
            names = [d[0] for d in c.description]
            records = [dict(zip(names, row)) for row in rows]
            result = dict(self.receipt(), security=security, field=field, period_end=period,
                          source=source, as_of=as_of, mode=mode, value=None, status="missing",
                          reasons=[], observation_ids=[])
            if not records:
                result["reasons"] = ["no_observed_field"]
                return result
            accepted, rejected = [], set()
            for row in records:
                reasons = []
                if row["quality_state"] != "usable_current_source" or row["canonical_value_decimal_text"] is None:
                    reasons.extend(json.loads(row["quality_reasons_json"]) or ["field_not_calculation_ready"])
                if mode in {"listed_security", "historical_pit", "official_daily"}:
                    if row["valid_from"] is None:
                        reasons.append("code_lifetime_unknown")
                    elif period < row["valid_from"]:
                        reasons.append("period_before_listing")
                    if row["valid_to"] and period >= row["valid_to"]:
                        reasons.append("period_after_code_exit")
                events = c.execute("""SELECT d.* FROM financial.links l
                  JOIN financial.disclosure_events d ON d.event_id=l.to_id
                  WHERE l.from_type='report' AND l.from_id=? AND l.to_type='disclosure'""",
                                   [row["report_version_id"]]).fetchall()
                event_names = [d[0] for d in c.description]
                events = [dict(zip(event_names, x)) for x in events]
                if mode in {"historical_pit", "official_daily"}:
                    # Only evidence-bound historical records can qualify. Provider
                    # date claims never become historical merely because time passed.
                    proved = c.execute("""SELECT v.policy,v.available_from FROM financial.visibility v
                      JOIN reference.issuer_instruments i ON i.relation_id=v.issuer_relation_id
                      WHERE v.observation_id=? AND i.instrument_id=?
                       AND json_extract_string(i.body,'$.security')=?
                       AND i.identity_status='verified_report_security_relation'
                       AND (v.policy='verified_exact_publication' OR ?='official_daily')""",
                                       [row["observation_id"], self.release.identifiers[security], security, mode]).fetchall()
                    if not proved:
                        reasons.append("historical_visibility_unproven")
                    elif not any(timestamp(gate) <= cutoff for _, gate in proved):
                        reasons.append("not_yet_visible")
                else:
                    captured = timestamp(row["first_captured_at"]) if row["first_captured_at"] else None
                    if not captured or captured > cutoff:
                        reasons.append("snapshot_not_captured_at_cutoff")
                    if not events:
                        reasons.append("announcement_date_unknown")
                    for event in events:
                        day = event["published_date"]
                        if day and day > cutoff.astimezone(SHANGHAI).date().isoformat():
                            reasons.append("announcement_after_cutoff")
                        if day and captured and day > captured.astimezone(SHANGHAI).date().isoformat():
                            reasons.append("source_announcement_after_capture_requires_review")
                if reasons:
                    rejected.update(reasons)
                else:
                    accepted.append(row)
            if not accepted:
                result.update(status="missing" if rejected == {"not_yet_visible"} else
                              "visibility_unproven" if any("unproven" in x for x in rejected) else "unavailable",
                              reasons=sorted(rejected))
                return result
            ids = {x["observation_id"] for x in accepted}
            replacements = c.execute("""SELECT from_id,to_id,available_at FROM financial.links
               WHERE relation='replaces' AND issuer_id IN
                (SELECT issuer_id FROM reference.issuer_instruments WHERE instrument_id=?
                 AND json_extract_string(body,'$.security')=?)
                AND field=? AND period_end=?""",
                                     [self.release.identifiers[security], security, field, period]).fetchall()
            replaced = {old for new, old, at in replacements if new in ids and (at is None or timestamp(at) <= cutoff)}
            accepted = [x for x in accepted if x["observation_id"] not in replaced]
            # Distinct source versions are never silently selected by latest capture.
            versions = {x["report_version_id"] for x in accepted}
            values = {(x["canonical_value_decimal_text"], x["canonical_unit"], x["context_id"]) for x in accepted}
            blockers = c.execute("""SELECT count(*) FROM financial.links WHERE relation='unresolved_revision'
              AND issuer_id IN (SELECT issuer_id FROM reference.issuer_instruments WHERE instrument_id=?
               AND json_extract_string(body,'$.security')=?)
              AND period_end=? AND (field IS NULL OR field=?) AND
              (available_at IS NULL OR CAST(available_at AS TIMESTAMPTZ)<=CAST(? AS TIMESTAMPTZ))""",
                                 [self.release.identifiers[security], security, period, field, as_of]).fetchone()[0]
            if len(versions) != 1 or len(values) != 1 or blockers:
                result.update(status="conflict", reasons=["unresolved_version_or_extraction_conflict"],
                              observation_ids=sorted(x["observation_id"] for x in accepted))
                return result
            row = accepted[0]
            result.update(status="ok", value=row["canonical_value_decimal_text"], unit=row["canonical_unit"],
                          issuer_id=row["issuer_id"], context_id=row["context_id"],
                          scope=row["scope"], basis=row["basis"], currency=row["currency"],
                          report_version_id=row["report_version_id"],
                          observation_ids=sorted({x["observation_id"] for x in accepted}),
                          daily_pit_usable=mode == "official_daily",
                          time_basis="verified_official_daily" if mode == "official_daily" else
                          "verified_exact_publication" if mode == "historical_pit" else "captured_source_snapshot")
            return result
        finally:
            c.close()

    def common_indicator(self, security, name, period, *, source, as_of, mode="historical_pit"):
        formulas = {
            "netprofit_margin": ("net_profit", "operating_revenue"),
            "debt_to_assets": ("total_liabilities", "total_assets"),
        }
        require(name in formulas, "FIN_INDICATOR", "Only reviewed common formulas are supported")
        inputs = [self.point(security, field, period, source=source, as_of=as_of, mode=mode)
                  for field in formulas[name]]
        result = dict(indicator=name, formula_version="ratio-percent.v1", period_end=period,
                      value=None, unit="percent", inputs=inputs, status="missing")
        if any(x["status"] != "ok" for x in inputs):
            return result
        if any(inputs[0][key] != inputs[1][key] for key in
               ("unit", "issuer_id", "scope", "basis", "currency", "report_version_id")):
            result["status"] = "scope_conflict"
        elif Decimal(inputs[1]["value"]) == 0:
            result["status"] = "undefined_zero_denominator"
        else:
            with localcontext() as ctx:
                ctx.prec = 38
                result.update(value=format(100 * Decimal(inputs[0]["value"]) / Decimal(inputs[1]["value"]), "f"), status="ok")
        return result

    def reference_point(self, family, day, *, source, as_of, security=None, exchange=None, field=None):
        """Exact captured-source point; never daily fill or historical availability."""
        kinds = {
            "share": ("reference.share_observations", "effective_date", "share"),
            "factor": ("reference.factor_observations", "trade_date", "factor"),
            "price_limit": ("reference.price_limits", "trade_date", "limit"),
            "calendar": ("reference.exchange_calendar", "date", "calendar"),
            "status": ("reference.security_status", "trade_date", "status"),
            "corporate_action": ("corporate_action.events", "ex_date", "action"),
        }
        require(family in kinds, "FIN_REFERENCE", "Unknown reference family")
        cutoff = timestamp(as_of)
        table, date_column, endpoint = kinds[family]
        require(day <= cutoff.astimezone(SHANGHAI).date().isoformat(),
                "FIN_TIME", "Reference effective date is after cutoff")
        if family == "calendar":
            require(exchange in {"XSHE", "XSHG"} and security is None,
                    "FIN_REFERENCE", "Calendar point requires an exchange")
            condition, scope = "r.exchange=?", [exchange]
        else:
            require(security in self.release.identifiers and exchange is None,
                    "FIN_REFERENCE", "Reference point requires one frozen security")
            condition, scope = "r.instrument_id=? AND r.security=?", [self.release.identifiers[security], security]
        c = self.sql()
        try:
            key = t.KEYS[table]
            rows = c.execute(f"""SELECT r.*,ct.first_captured_at FROM {table} r
               LEFT JOIN financial.capture_times ct ON ct.from_type=? AND ct.from_id=r.{key}
               WHERE {condition} AND r.{date_column}=? AND r.source=? LIMIT 10001""",
                             [endpoint, *scope, day, source]).fetchall()
            require(len(rows) <= 10000, "FIN_QUERY_BOUND", "Reference point has too many observations")
            names = [x[0] for x in c.description]
            records = [dict(zip(names, row)) for row in rows]
            selected, reasons = [], set()
            for row in records:
                captured = row["first_captured_at"]
                if not captured or timestamp(captured) > cutoff:
                    reasons.add("evidence_not_captured_at_cutoff")
                    continue
                announcement = row.get("announcement_date")
                if announcement and (announcement > cutoff.astimezone(SHANGHAI).date().isoformat()
                                     or announcement > timestamp(captured).astimezone(SHANGHAI).date().isoformat()):
                    reasons.add("announcement_after_capture_or_cutoff_requires_review")
                    continue
                unit, value, basis = None, None, None
                if family == "share":
                    require(field and field.endswith("_shares"), "FIN_REFERENCE", "Select a share class")
                    if row["field"] != field:
                        continue
                    if row["value_state"] == "source_observed":
                        value, unit = row["value_decimal_text"], "shares"
                    else:
                        reasons.add(row["value_state"])
                elif family == "factor":
                    value, unit, basis = row["factor_value"], "ratio", row["basis"]
                elif family == "calendar":
                    value, unit = row["is_open"], "boolean"
                elif family == "price_limit":
                    require(field in {"up_limit", "down_limit"}, "FIN_REFERENCE", "Choose up_limit or down_limit")
                    if row[field] is not None and Decimal(row[field]) > 0:
                        value, unit = row[field], row["unit"]
                    else:
                        reasons.add("missing_or_zero_limit_semantics_unverified")
                elif family == "status":
                    require(field in {"is_st", "is_paused"}, "FIN_REFERENCE", "Choose one status field")
                    if row[field] in {"0", "1"}:
                        value, unit = row[field], "source_flag"
                    else:
                        reasons.add("unknown_source_status")
                else:
                    require(field in {"gross_cash", "net_cash", "bonus_ratio", "capitalization_ratio", "base_shares"},
                            "FIN_REFERENCE", "Choose one explicit action field")
                    if row["implemented"] is True:
                        value = row[field]
                        unit = "CNY/eligible_share" if "cash" in field else (
                            "shares" if field == "base_shares" else "new_shares/eligible_share")
                        if "cash" in field:
                            native = json.loads(row["source_fields_json"])
                            native_name = "cash_div_tax" if field == "gross_cash" else "cash_div"
                            basis = native.get(native_name, {}).get("tax_basis") or row["tax_basis"]
                    else:
                        reasons.add("event_not_implemented")
                if value is not None:
                    selected.append((value, unit, basis, row[key]))
            distinct = {(value, unit, basis) for value, unit, basis, _ in selected}
            result = dict(self.receipt(), family=family, day=day, source=source, security=security,
                          exchange=exchange, field=field, as_of=as_of, value=None,
                          status="missing", reasons=sorted(reasons), carry_forward_authorized=False,
                          time_basis="captured_source_point_not_historical_public_availability")
            if len(distinct) == 1:
                value, unit, basis = next(iter(distinct))
                result.update(value=value, unit=unit, status="ok",
                              observation_ids=sorted({x[3] for x in selected}))
                if family == "factor":
                    result["factor_basis"] = basis
                elif family == "corporate_action" and "cash" in field:
                    result["tax_basis"] = basis
            elif distinct:
                result.update(status="conflict", reasons=["conflicting_source_point_values"])
            return result
        finally:
            c.close()


def main(argv=None):
    import argparse
    from ashare_data.storage.financial_release import prepare
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-root", required=True, type=Path)
    parser.add_argument("--release-id", required=True, type=int)
    parser.add_argument("--manifest-sha256", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("prepare")
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--batch", action="append", type=Path, default=[])
    build.add_argument("--daily-review-spec", action="append", type=Path, default=[])
    build.add_argument("--official-batch", action="append", type=Path, default=[])
    build.add_argument("--official-repair", action="append", type=Path, default=[])
    build.add_argument("--share-resolutions", action="append", type=Path, default=[])
    query = sub.add_parser("query")
    query.add_argument("--candidate", type=Path)
    query.add_argument("--security", required=True)
    query.add_argument("--field", required=True)
    query.add_argument("--period", required=True)
    query.add_argument("--source", required=True)
    query.add_argument("--as-of", required=True)
    query.add_argument("--mode", default="historical_pit")
    args = parser.parse_args(argv)
    parent = MarketRelease(args.market_root, args.release_id, expected_manifest_sha256=args.manifest_sha256)
    if args.action == "prepare":
        value = prepare(parent, args.output, args.batch, daily_reviews=args.daily_review_spec,
                        official_batches=args.official_batch, share_resolutions=args.share_resolutions,
                        official_repairs=args.official_repair)
        print(json.dumps({"candidate_id": value["candidate_id"], "published": False,
                          "validation": value["validation"]}, ensure_ascii=False))
    else:
        reader = FinancialTables.prepared(args.candidate, parent) if args.candidate else FinancialTables(parent)
        print(json.dumps(reader.point(args.security, args.field, args.period,
                                      source=args.source, as_of=args.as_of, mode=args.mode), ensure_ascii=False))


if __name__ == "__main__":
    main()
