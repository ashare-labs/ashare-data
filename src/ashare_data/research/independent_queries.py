"""Pinned independent source observations and explicitly named research projections.

No network, JQ answers, implicit source merge, execution feed or historical PIT.
Decimal strings preserve the source's precision; unsupported is never NaN.
"""

from __future__ import annotations

from copy import deepcopy
import csv
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import io
import json
from pathlib import Path
import re

from ashare_data.core.model import DataError, day, require, symbol, timestamp, validate_ohlc
from ashare_data.storage.immutable import identifier

ROLE = "independent_retrospective_query_research"
SCHEMA = "ad.independent-query-binding.v1"
FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "money",
    "avg",
    "high_limit",
    "low_limit",
    "pre_close",
    "paused",
    "factor",
    "open_interest",
    "vwap",
)


def _json(raw):
    try:
        return json.loads(
            raw, parse_float=Decimal, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x))
        )
    except (ValueError, UnicodeError) as exc:
        raise DataError("INDEPENDENT_SCHEMA", "Invalid JSON or nonfinite source value") from exc


def _decimal(value):
    try:
        require(not isinstance(value, bool), "INDEPENDENT_VALUE", "Boolean numeric value")
        result = Decimal(str(value))
        require(
            result.is_finite() and 0 <= result < Decimal("1e17"),
            "INDEPENDENT_VALUE",
            "Invalid source number",
        )
        return result
    except (ValueError, InvalidOperation) as exc:
        raise DataError("INDEPENDENT_VALUE", "Invalid source number") from exc


def _value(value, origin="source", **metadata):
    return {
        "state": "value",
        "value": value if isinstance(value, bool) else str(value),
        "origin": origin,
        **metadata,
    }


def _fields(row):
    result = {
        f: {"state": "unsupported", "value": None, "reason": "not provided by this source contract"}
        for f in FIELDS
    }
    for key in ("open", "high", "low", "close", "volume", "money"):
        result[key] = _value(row[key])
    if row["volume"] > 0:
        with localcontext() as ctx:
            ctx.prec = 34
            result["avg"] = _value(
                row["money"] / row["volume"],
                "derived",
                formula="source money / source volume; Decimal precision 34",
                native_avg=False,
            )
        result["paused"] = _value(
            False, "derived", reason="positive day volume disproves a full-day halt"
        )
    return result


class IndependentQueryView:
    """Read fixed owner products; recheck all linked files on each public call."""

    def __init__(self, binding_path, *, expected_binding_sha256):
        self.path = Path(binding_path).resolve()
        self.sha256 = identifier(expected_binding_sha256)

    def _checked(self, ref):
        require(
            type(ref) is dict and {"path", "sha256"} <= set(ref) and type(ref["path"]) is str,
            "INDEPENDENT_FILE",
            "Invalid file reference",
        )
        relative = Path(ref["path"])
        p = self.path.parent / relative
        require(
            not relative.is_absolute()
            and not p.is_symlink()
            and p.resolve().is_relative_to(self.path.parent),
            "INDEPENDENT_FILE",
            "Evidence must remain inside the binding directory",
        )
        require(
            p.is_file() and p.stat().st_size <= 16 * 1024 * 1024,
            "INDEPENDENT_FILE",
            "Missing or oversized evidence",
        )
        raw = p.read_bytes()
        require(
            hashlib.sha256(raw).hexdigest() == identifier(ref["sha256"]),
            "INDEPENDENT_INTEGRITY",
            "Evidence digest mismatch",
        )
        return raw

    def _open(self, product_id, security, role, kind):
        require(
            role == ROLE,
            "INDEPENDENT_ROLE",
            "Explicit independent retrospective query role required",
        )
        raw = self._checked({"path": self.path.name, "sha256": self.sha256})
        b = _json(raw)
        require(
            type(b) is dict
            and b.get("schema") == SCHEMA
            and b.get("role") == ROLE
            and b.get("historical_pit") is False
            and b.get("strategy_visible") is False
            and type(b.get("products")) is dict
            and type(b.get("sources")) is dict,
            "INDEPENDENT_SCHEMA",
            "Invalid independent binding",
        )
        require(
            type(product_id) is str and product_id in b["products"],
            "INDEPENDENT_SCOPE",
            "Unknown product",
        )
        p = b["products"][product_id]
        require(
            type(p) is dict and p.get("kind") == kind and p.get("security") == symbol(security),
            "INDEPENDENT_SCOPE",
            "Wrong security or product kind",
        )
        require(
            type(p.get("sources")) is list and 1 <= len(p["sources"]) <= 8,
            "INDEPENDENT_SCHEMA",
            "Invalid source list",
        )
        for name in p["sources"]:
            require(name in b["sources"], "INDEPENDENT_SCHEMA", "Missing source")
            s = b["sources"][name]
            require(
                s.get("security") == security
                and s.get("adjustment") == "none"
                and timestamp(s["captured_start"]) <= timestamp(s["captured_end"]),
                "INDEPENDENT_SCHEMA",
                "Invalid source identity/capture",
            )
            self._checked(s)
            require(
                type(s.get("evidence", [])) is list and len(s.get("evidence", [])) <= 12,
                "INDEPENDENT_SCHEMA",
                "Invalid evidence list",
            )
            for ref in s.get("evidence", []):
                self._checked(ref)
        for ref in p.get("evidence", []):
            self._checked(ref)
        return b, p

    def _daily(self, source):
        payload = _json(self._checked(source))
        rows = []
        if source["kind"] == "sina_daily_klc_decoded":
            require(
                payload.get("raw_sha256") == source["raw_sha256"]
                and payload.get("decoder_sha256") == source["decoder_sha256"]
                and type(payload.get("rows")) is list,
                "INDEPENDENT_SCHEMA",
                "Invalid decoder lineage",
            )
            for r in payload["rows"]:
                require(
                    type(r) is dict
                    and type(r.get("date")) is str
                    and re.fullmatch(r"\d{4}-\d\d-\d\dT00:00:00\.000Z", r["date"]),
                    "INDEPENDENT_SCHEMA",
                    "Unexpected source daily label",
                )
                rows.append(
                    {
                        "date": day(r["date"][:10]),
                        **{
                            k: _decimal(r[v])
                            for k, v in [(x, x) for x in ("open", "high", "low", "close", "volume")]
                            + [("money", "amount")]
                        },
                    }
                )
        elif source["kind"] == "eastmoney_daily_klines":
            d = payload.get("data")
            code = source["security"][:6]
            market = 0 if source["security"].endswith("XSHE") else 1
            require(
                payload.get("rc") == 0
                and type(d) is dict
                and d.get("code") == code
                and d.get("market") == market
                and type(d.get("klines")) is list,
                "INDEPENDENT_SCHEMA",
                "Invalid Eastmoney source identity",
            )
            for line in d["klines"]:
                parts = line.split(",")
                require(len(parts) == 11, "INDEPENDENT_SCHEMA", "Unexpected daily fields")
                r = {
                    "date": day(parts[0]),
                    **{
                        k: _decimal(v)
                        for k, v in zip(
                            ("open", "close", "high", "low", "volume", "money"),
                            parts[1:7],
                            strict=True,
                        )
                    },
                }
                r["volume"] *= 100  # Source lots; conversion does not recover share precision.
                rows.append(r)
        else:
            raise DataError("INDEPENDENT_SCHEMA", "Unsupported daily source")
        require(0 < len(rows) <= 20000, "INDEPENDENT_SCHEMA", "Unexpected row count")
        dates = [r["date"] for r in rows]
        require(
            dates == sorted(set(dates)), "INDEPENDENT_SCHEMA", "Duplicate or unordered daily dates"
        )
        for r in rows:
            validate_ohlc(
                *(r[k] for k in ("open", "high", "low", "close")), code="INDEPENDENT_VALUE"
            )
        return rows

    def _result(self, product_id, p, source, rows, **extra):
        return {
            "schema": "ad.independent-query-result.v1",
            "product_id": product_id,
            "binding_sha256": self.sha256,
            "security": p["security"],
            "role": ROLE,
            "historical_pit": False,
            "strategy_visible": False,
            "matching_feed": False,
            "source": deepcopy(source),
            "rows": rows,
            **extra,
        }

    def read_daily(self, product_id, security, *, source, role, standardize=False):
        b, p = self._open(product_id, security, role, "daily")
        require(type(standardize) is bool, "INDEPENDENT_ARGUMENT", "Standardize must be boolean")
        require(source in p["sources"], "INDEPENDENT_SCOPE", "Source not bound to this product")
        s = b["sources"][source]
        start, end = day(p["start_date"]), day(p["end_date"])
        require(start <= end, "INDEPENDENT_SCHEMA", "Reversed daily window")
        daily = self._daily(s)
        rows = [
            {"date": r["date"], "fields": _fields(r)} for r in daily if start <= r["date"] <= end
        ]
        if standardize:
            config = p.get("daily_standardization", {})
            require(
                type(config) is dict
                and config.get("policy") == "raw_identity_previous_session_close.v1"
                and "calendar" in config,
                "INDEPENDENT_SCOPE",
                "Daily standardization is not bound to this product",
            )
            calendar = _json(self._checked(config["calendar"]))
            dates = calendar.get("dates")
            require(
                type(dates) is list
                and 1 < len(dates) <= 20000
                and dates == sorted(set(day(d) for d in dates)),
                "INDEPENDENT_SCHEMA",
                "Invalid previous-session calendar",
            )
            previous = dict(zip(dates[1:], dates[:-1], strict=True))
            by_date = {r["date"]: r for r in daily}
            for row in rows:
                fields = row["fields"]
                fields["factor"] = _value(
                    1, "declared_raw_price_identity", corporate_action_factor=False
                )
                prior = previous.get(row["date"])
                if prior in by_date:
                    fields["pre_close"] = _value(
                        by_date[prior]["close"],
                        "derived_previous_session_raw_close",
                        previous_date=prior,
                        exchange_ex_reference=False,
                    )
                else:
                    fields["pre_close"] = {
                        "state": "unsupported",
                        "value": None,
                        "reason": "previous calendar session has no bound raw close",
                        "previous_date": prior,
                    }
        return self._result(
            product_id,
            p,
            s,
            rows,
            start_date=start,
            end_date=end,
            missing_row_policy="omit; absence reason unknown, never filled",
            full_session_coverage_certified=False,
            units={"price": "CNY/share", "volume": "share", "money": "CNY"},
            **(
                {
                    "standardization_policy": config["policy"],
                    "calendar_evidence": deepcopy(config["calendar"]),
                }
                if standardize
                else {}
            ),
        )

    def read_corporate_actions(self, product_id, security, *, source, role):
        """Expose separate CNinfo event observations, without certifying a factor chain."""
        b, p = self._open(product_id, security, role, "corporate_actions")
        require(source in p["sources"], "INDEPENDENT_SCOPE", "Unbound corporate action source")
        s = b["sources"][source]
        payload = _json(self._checked(s))
        records = payload.get("records")
        require(
            payload.get("resultcode") == 200
            and type(records) is list
            and type(payload.get("total")) is int
            and len(records) == payload["total"]
            and 0 < len(records) <= 1000,
            "INDEPENDENT_SCHEMA",
            "Incomplete corporate action response",
        )
        start, end = day(p["start_date"]), day(p["end_date"])
        require(start <= end, "INDEPENDENT_SCHEMA", "Reversed corporate action window")
        rows = []
        for item in records:
            if s["kind"] == "cninfo_dividends":
                label = day(item["F020D"])
                fields = {
                    "cash_per_10_shares": "F012N",
                    "bonus_shares_per_10": "F010N",
                    "capitalization_shares_per_10": "F011N",
                }
                dates = {
                    "announcement_date": "F006D",
                    "record_date": "F018D",
                    "cash_payment_date": "F023D",
                    "share_arrival_date": "F025D",
                }
                detail = {"source_plan": item["F007V"], "event_kind": "dividend"}
            elif s["kind"] == "cninfo_rights":
                require(
                    item.get("SECCODE") == security[:6],
                    "INDEPENDENT_SCHEMA",
                    "Wrong rights security",
                )
                label = day(item["F012D"])
                fields = {"rights_price": "F005N", "rights_shares_per_10": "F004N"}
                dates = {
                    "announcement_date": "DECLAREDATE",
                    "record_date": "F011D",
                    "payment_start_date": "F013D",
                    "payment_end_date": "F014D",
                    "rights_listing_date": "F038D",
                }
                detail = {"event_kind": "rights", "source_record_id": item["SUBID"]}
            else:
                raise DataError("INDEPENDENT_SCHEMA", "Unsupported corporate action source")
            if not start <= label <= end:
                continue
            rows.append(
                {
                    "ex_date": label,
                    **detail,
                    "fields": {
                        k: _value(_decimal(item[v]))
                        if item[v] is not None
                        else {
                            "state": "unsupported",
                            "value": None,
                            "reason": "source null; not assumed zero",
                        }
                        for k, v in fields.items()
                    },
                    **{k: day(item[v]) if item[v] is not None else None for k, v in dates.items()},
                }
            )
        rows.sort(key=lambda r: (r["ex_date"], r["event_kind"]))
        require(
            len({(r["ex_date"], r["event_kind"]) for r in rows}) == len(rows),
            "INDEPENDENT_SCHEMA",
            "Ambiguous same-day corporate actions",
        )
        return self._result(
            product_id,
            p,
            s,
            rows,
            start_date=start,
            end_date=end,
            historical_action_chain_complete=False,
            adjustment_factor_certified=False,
            source_publication_time=None,
            date_semantics="Source event/announcement labels; not verified historical receipt times",
            policy="Separate source ledgers; nulls preserved; no factor fitted or merged",
        )

    def read_halt_events(self, product_id, security, *, role):
        """Read explicit full-day provider events, never infer halts from absent bars."""
        b, p = self._open(product_id, security, role, "halt_events")
        s = b["sources"][p["sources"][0]]
        require(
            s["kind"] == "eastmoney_stock_calendar_html",
            "INDEPENDENT_SCHEMA",
            "Unsupported halt event source",
        )
        raw = self._checked(s).decode("utf8")
        marker = re.search(r"\bvar\s+pagedata\s*=\s*", raw)
        require(marker is not None, "INDEPENDENT_SCHEMA", "Missing event JSON envelope")
        # Decode a JSON value, never evaluate the page's JavaScript.
        try:
            payload, _ = json.JSONDecoder().raw_decode(raw[marker.end() :])
            records = payload["sjyl"]["result"]["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DataError("INDEPENDENT_SCHEMA", "Invalid event JSON") from exc
        require(
            type(records) is list and 0 < len(records) <= 20000,
            "INDEPENDENT_SCHEMA",
            "Unexpected event rows",
        )
        dates = p.get("dates")
        require(
            type(dates) is list
            and 0 < len(dates) <= 1000
            and dates == sorted(set(day(d) for d in dates)),
            "INDEPENDENT_SCHEMA",
            "Invalid halt scope",
        )
        rows = []
        for row in records:
            if row.get("EVENT_TYPE_CODE") != "023" or row.get("NOTICE_DATE", "")[:10] not in dates:
                continue
            require(
                row.get("SECURITY_CODE") == security[:6]
                and row.get("SECUCODE")
                == security[:6] + (".SZ" if security.endswith("XSHE") else ".SH"),
                "INDEPENDENT_SCHEMA",
                "Wrong halt security",
            )
            label = day(row["NOTICE_DATE"][:10])
            content = row["LEVEL1_CONTENT"]
            stamp = label[:4] + "年" + label[5:7] + "月" + label[8:] + "日"
            pattern = r"因(.+?)停牌一天，" + stamp + r"\s+9:30-" + stamp + r"\s+15:00停牌"
            match = re.fullmatch(pattern, content)
            require(
                match is not None,
                "INDEPENDENT_COVERAGE",
                "Event does not explicitly document a full-day halt",
            )
            rows.append(
                {
                    "date": label,
                    "full_day_paused": True,
                    "reason": match[1],
                    "source_content": content,
                    "source_notice_date": row["NOTICE_DATE"],
                    "source_quality": "provider historical event record",
                    "official_announcement_chain_verified": False,
                }
            )
        rows.sort(key=lambda r: r["date"])
        require(
            [r["date"] for r in rows] == dates,
            "INDEPENDENT_COVERAGE",
            "Missing or duplicate requested halt event",
        )
        return self._result(
            product_id,
            p,
            s,
            rows,
            source_publication_time=None,
            policy="Explicit event records; no price fills and no inference from missing daily bars",
        )

    def read_action_evidence(self, product_id, security, *, role):
        """Read reviewed issuer facts with pinned documents, without rebuilding factors."""
        b, p = self._open(product_id, security, role, "action_evidence")
        s = b["sources"][p["sources"][0]]
        fact = _json(self._checked(s))
        require(
            s["kind"] == "reviewed_issuer_actions"
            and fact.get("schema") == "ad.issuer-action-evidence.v1"
            and fact.get("security") == security,
            "INDEPENDENT_SCHEMA",
            "Invalid issuer action evidence",
        )
        documents, rows = fact.get("documents"), fact.get("rows")
        require(
            type(documents) is dict
            and 1 <= len(documents) <= 12
            and type(rows) is list
            and 1 <= len(rows) <= 100,
            "INDEPENDENT_SCHEMA",
            "Invalid issuer evidence coverage",
        )
        for document in documents.values():
            self._checked(document)
            day(document["document_date"])
            require(
                timestamp(document["captured_start"]) <= timestamp(document["captured_end"])
                and document.get("source_publication_time") is None,
                "INDEPENDENT_SCHEMA",
                "Document dates must not impersonate publication clocks",
            )
        result = []
        for row in rows:
            require(
                type(row) is dict
                and row.get("event_id") in p["event_ids"]
                and row.get("resolution") in {"resolved_schedule", "partial_history"}
                and re.fullmatch(r"\d{4}(?:-\d{2})?", row.get("period", "")) is not None
                and type(row.get("facts")) is dict
                and row["facts"]
                and type(row.get("citations")) is list
                and row["citations"],
                "INDEPENDENT_SCHEMA",
                "Invalid reviewed event",
            )
            for citation in row["citations"]:
                require(
                    citation.get("document") in documents
                    and type(citation.get("pages")) is list
                    and citation["pages"]
                    and all(type(n) is int and 1 <= n <= 1000 for n in citation["pages"]),
                    "INDEPENDENT_SCHEMA",
                    "Missing document/page citation",
                )
            label = row.get("ex_date")
            if label is not None:
                require(
                    day(p["start_date"]) <= day(label) <= day(p["end_date"]),
                    "INDEPENDENT_SCOPE",
                    "Action date outside bound window",
                )
            require(
                row["resolution"] != "resolved_schedule" or label is not None,
                "INDEPENDENT_SCHEMA",
                "Resolved schedule requires an ex-date",
            )
            result.append(deepcopy(row))
        require(
            sorted(r["event_id"] for r in result) == sorted(p["event_ids"])
            and len({r["event_id"] for r in result}) == len(result),
            "INDEPENDENT_COVERAGE",
            "Missing or duplicate reviewed event",
        )
        return self._result(
            product_id,
            p,
            s,
            result,
            documents=deepcopy(documents),
            historical_action_chain_complete=False,
            adjustment_factor_certified=False,
            source_publication_time=None,
            policy="Reviewed issuer facts only; no source-ledger replacement or factor synthesis",
        )

    def _archive_rows(self, p, s, security, *, calendar):
        require(s.get("kind") == "dolt_sql_snapshot", "INDEPENDENT_SCHEMA", "Unsupported archive")
        commit = s.get("archive_commit", "")
        require(
            type(commit) is str and re.fullmatch(r"[0-9a-v]{32}", commit) is not None,
            "INDEPENDENT_SCHEMA",
            "Archive must use an immutable Dolt commit",
        )
        start, end = day(p["start_date"]), day(p["end_date"])
        require(start <= end, "INDEPENDENT_SCOPE", "Reversed archive window")
        if calendar:
            exchange = p.get("exchange")
            require(
                exchange in {"SSE", "SZSE", "BSE"},
                "INDEPENDENT_SCHEMA",
                "Invalid calendar exchange",
            )
            query = (
                "SELECT exchange,date,HEX(is_open) AS is_open_hex FROM ts_trade_day_calendar "
                f"AS OF '{commit}' WHERE exchange='{exchange}' AND date BETWEEN '{start}' "
                f"AND '{end}' ORDER BY date LIMIT 20;"
            )
        else:
            code = security[:6] + (".SZ" if security.endswith("XSHE") else ".SH")
            query = (
                "SELECT tradedate,symbol,open,high,low,close,volume,amount FROM ts_a_stock_eod_price "
                f"AS OF '{commit}' WHERE symbol='{code}' AND tradedate BETWEEN '{start}' "
                f"AND '{end}' ORDER BY tradedate LIMIT 12;"
            )
        payload = _json(self._checked(s))
        require(
            payload.get("query_execution_status") == "Success"
            and payload.get("repository_owner") == s["repository_owner"]
            and payload.get("repository_name") == s["repository_name"]
            and payload.get("sql_query") == query
            and type(payload.get("rows")) is list,
            "INDEPENDENT_SCHEMA",
            "Archive response does not match the pinned query",
        )
        expected = p.get("expected_dates")
        require(
            type(expected) is list
            and 0 < len(expected) <= (20 if calendar else 12)
            and expected == sorted(set(day(d) for d in expected))
            and all(start <= d <= end for d in expected),
            "INDEPENDENT_SCHEMA",
            "Invalid archive date coverage",
        )
        rows = payload["rows"]
        require(
            all(type(r) is dict for r in rows)
            and [r.get("date" if calendar else "tradedate") for r in rows] == expected,
            "INDEPENDENT_COVERAGE",
            "Missing, duplicate or extra archive rows",
        )
        log = _json(self._checked(s["commit_log"]))
        require(
            log.get("query_execution_status") == "Success" and type(log.get("rows")) is list,
            "INDEPENDENT_SCHEMA",
            "Invalid archive commit log",
        )
        entries = [r for r in log["rows"] if r.get("commit_hash") == commit]
        require(
            len(entries) == 1 and type(entries[0].get("date")) is str,
            "INDEPENDENT_SCHEMA",
            "Archive commit absent from pinned log",
        )
        version = {
            "commit": commit,
            "commit_time_raw": entries[0]["date"],
            "commit_time_timezone": None,
            "original_source_capture_time": None,
            "source_publication_time": None,
            "source_revision_id": None,
            "semantics": "Database archive version; not an original provider capture or value revision",
        }
        return rows, version

    def read_archive_daily(self, product_id, security, *, source, role):
        """Read a bounded immutable archive, preserving native precision and clocks."""
        b, p = self._open(product_id, security, role, "archive_daily")
        require(source in p["sources"], "INDEPENDENT_SCOPE", "Unbound archive source")
        s = b["sources"][source]
        raw, version = self._archive_rows(p, s, security, calendar=False)
        require(
            s.get("unit_policy") == "tushare_daily_lots_100_and_amount_1000.v1"
            and s.get("evidence"),
            "INDEPENDENT_SCHEMA",
            "Missing archive unit evidence",
        )
        code = security[:6] + (".SZ" if security.endswith("XSHE") else ".SH")
        rows = []
        for item in raw:
            require(
                set(item)
                == {"tradedate", "symbol", "open", "high", "low", "close", "volume", "amount"}
                and item["symbol"] == code
                and item["amount"] is not None,
                "INDEPENDENT_SCHEMA",
                "Incomplete or wrong-security daily archive",
            )
            r = {k: _decimal(item[k]) for k in ("open", "high", "low", "close", "volume")}
            r["volume"] *= 100
            r["money"] = _decimal(item["amount"]) * 1000
            validate_ohlc(
                *(r[k] for k in ("open", "high", "low", "close")), code="INDEPENDENT_VALUE"
            )
            fields = _fields(r)
            for name in ("volume", "money"):
                fields[name]["origin"] = "documented_source_unit_conversion"
            rows.append(
                {"date": item["tradedate"], "fields": fields, "native_fields": deepcopy(item)}
            )
        return self._result(
            product_id,
            p,
            s,
            rows,
            archive_version=version,
            units={"price": "CNY/share", "volume": "share", "money": "CNY"},
            native_units={"volume": "100 shares", "amount": "1000 CNY"},
            full_session_coverage_certified=False,
            policy="Separate immutable archive snapshots; no fills or inferred value revisions",
        )

    def read_archive_calendar(self, product_id, security, *, source, role):
        """Read the archive's stated exchange calendar, without cross-exchange inference."""
        b, p = self._open(product_id, security, role, "archive_calendar")
        require(source in p["sources"], "INDEPENDENT_SCOPE", "Unbound archive source")
        s = b["sources"][source]
        raw, version = self._archive_rows(p, s, security, calendar=True)
        rows = []
        for item in raw:
            require(
                set(item) == {"exchange", "date", "is_open_hex"}
                and item["exchange"] == p["exchange"]
                and item["is_open_hex"] in {"30", "31"},
                "INDEPENDENT_SCHEMA",
                "Invalid archive calendar encoding or exchange",
            )
            rows.append(
                {
                    "date": item["date"],
                    "exchange": item["exchange"],
                    "is_open": item["is_open_hex"] == "31",
                    "native_fields": deepcopy(item),
                }
            )
        return self._result(
            product_id,
            p,
            s,
            rows,
            archive_version=version,
            calendar_exchange=p["exchange"],
            official_calendar_certified=False,
            policy="Observed exchange calendar only; not an original publication/receipt history",
        )

    def read_index_members(
        self, product_id, security, *, as_of_date, source, role, allow_uncertified=False
    ):
        """Read an explicit historical candidate, never certify an official/PIT set."""
        b, p = self._open(product_id, security, role, "index_members")
        require(
            allow_uncertified is True,
            "INDEPENDENT_UNCERTIFIED",
            "Historical candidates require allow_uncertified=True",
        )
        label = day(as_of_date)
        require(
            label in p["dates"] and source in p["sources"],
            "INDEPENDENT_SCOPE",
            "Unbound index date or source",
        )
        s = b["sources"][source]
        require(
            s.get("official_chain_verified") is False,
            "INDEPENDENT_SCHEMA",
            "Index reader only supports uncertified archives",
        )
        try:
            reader = csv.DictReader(io.StringIO(self._checked(s).decode("utf-8-sig")))
            rows = list(reader)
        except (UnicodeError, csv.Error) as exc:
            raise DataError("INDEPENDENT_SCHEMA", "Invalid index CSV") from exc
        require(0 < len(rows) <= 20000, "INDEPENDENT_SCHEMA", "Unexpected index rows")
        members = []
        for row in rows:
            require(
                None not in row and all(type(v) is str for v in row.values()),
                "INDEPENDENT_SCHEMA",
                "Malformed index row",
            )
            if s["kind"] == "index_membership_intervals":
                require(
                    set(row) == {"symbol", "name", "opt-in", "opt-out"},
                    "INDEPENDENT_SCHEMA",
                    "Unexpected interval fields",
                )
                code = row["symbol"]
                require(
                    re.fullmatch(r"(?:SH|SZ)\d{6}", code) is not None,
                    "INDEPENDENT_SCHEMA",
                    "Invalid source index member",
                )
                start = day(row["opt-in"])
                end = day(row["opt-out"]) if row["opt-out"] else None
                require(
                    end is None or start < end, "INDEPENDENT_SCHEMA", "Invalid membership interval"
                )
                if not (start <= label and (end is None or label < end)):
                    continue
                item = {
                    "security": code[2:] + (".XSHG" if code[:2] == "SH" else ".XSHE"),
                    "source_symbol": code,
                    "source_name": row["name"],
                    "source_opt_in": start,
                    "source_opt_out": end,
                }
            elif s["kind"] == "index_dated_snapshot":
                require(
                    set(row) == {"Symbol", "Name"} and label == s["snapshot_date"],
                    "INDEPENDENT_SCOPE",
                    "Snapshot date or fields do not match",
                )
                code = row["Symbol"]
                require(
                    re.fullmatch(r"\d{6}\.(?:SS|SZ)", code) is not None,
                    "INDEPENDENT_SCHEMA",
                    "Invalid snapshot index member",
                )
                item = {
                    "security": code[:6] + (".XSHG" if code[-2:] == "SS" else ".XSHE"),
                    "source_symbol": code,
                    "source_name": row["Name"],
                }
            else:
                raise DataError("INDEPENDENT_SCHEMA", "Unsupported index source")
            members.append(item)
        expected = p.get("expected_members")
        require(
            type(expected) is int
            and 0 < expected <= 10000
            and len(members) == expected
            and len({r["security"] for r in members}) == expected,
            "INDEPENDENT_COVERAGE",
            "Missing, duplicate or unexpected index members",
        )
        members.sort(key=lambda r: r["security"])
        return self._result(
            product_id,
            p,
            s,
            members,
            as_of_date=label,
            official_chain_verified=False,
            complete_membership_certified=False,
            historical_name_verified=False,
            source_quality="independent archival candidate",
            interval_semantics="opt-in inclusive; opt-out exclusive",
            warning="Counts and archive agreement do not certify official historical membership",
        )

    def read_paused(self, product_id, security, *, skip_paused, fill_paused, role):
        b, p = self._open(product_id, security, role, "paused_daily")
        require(
            type(skip_paused) is bool and type(fill_paused) is bool,
            "INDEPENDENT_ARGUMENT",
            "Pause options must be booleans",
        )
        s = b["sources"][p["sources"][0]]
        label = day(p["date"])
        fact = _json(self._checked(p["pause_fact"]))
        require(
            fact.get("security") == security
            and fact.get("full_day_paused") is True
            and fact.get("date") == label
            and fact.get("historical_pit") is False
            and type(fact.get("evidence")) is list
            and fact["evidence"],
            "INDEPENDENT_SCHEMA",
            "Missing independently documented halt",
        )
        for ref in fact["evidence"]:
            self._checked(ref)
        raw = self._daily(s)
        require(
            not any(r["date"] == label for r in raw),
            "INDEPENDENT_CONFLICT",
            "Documented pause conflicts with source daily bar",
        )
        before = [r for r in raw if r["date"] < label]
        require(
            before and before[-1]["date"] == p["last_trade_date"],
            "INDEPENDENT_COVERAGE",
            "Last true price anchor is missing or changed",
        )
        previous = before[-1]
        fields = {"paused": _value(True, "documented_fact")}
        for f in ("open", "high", "low", "close", "volume", "money"):
            if fill_paused:
                v = previous["close"] if f in ("open", "high", "low", "close") else Decimal(0)
                fields[f] = _value(v, "synthetic_paused_fill", traded=False)
            else:
                fields[f] = {
                    "state": "nan",
                    "value": None,
                    "origin": "missing_paused_row",
                    "traded": False,
                }
        rows = [] if skip_paused else [{"date": label, "fields": fields}]
        return self._result(
            product_id,
            p,
            s,
            rows,
            skip_paused=skip_paused,
            fill_paused=fill_paused,
            raw_row_present=False,
            pause_fact=deepcopy(fact),
            last_true_price={"date": previous["date"], "close": str(previous["close"])},
            policy="AD documented halt: skip wins; fill uses last raw close and zero flow; otherwise NaN",
            joinquant_equivalent=False,
        )

    def read_adjusted(self, product_id, security, *, basis_date, fq, role):
        b, p = self._open(product_id, security, role, "adjusted_daily")
        require(
            fq in (None, "pre", "post") and basis_date in p["basis_dates"],
            "INDEPENDENT_SCOPE",
            "Unsupported adjustment or basis date",
        )
        s = b["sources"][p["sources"][0]]
        raw = self._checked(p["factor_source"]).decode("utf8")
        prefix = "var " + ("sz" if security.endswith("XSHE") else "sh") + security[:6] + "hfq="
        require(raw.startswith(prefix), "INDEPENDENT_SCHEMA", "Factor source identity mismatch")
        factor_doc, _ = json.JSONDecoder().raw_decode(raw[len(prefix) :])
        fs = factor_doc["data"]
        require(
            len(fs) == factor_doc["total"] and len(fs) <= 1000,
            "INDEPENDENT_SCHEMA",
            "Incomplete provider factor array",
        )
        factors = [(day(r["d"]), _decimal(r["f"])) for r in fs]
        require(
            all(f > 0 for _, f in factors) and len({d for d, _ in factors}) == len(factors),
            "INDEPENDENT_SCHEMA",
            "Invalid provider factor history",
        )
        factors.sort()

        def factor_at(d):
            found = [(t, f) for t, f in factors if t <= d]
            require(found, "INDEPENDENT_COVERAGE", "Missing factor anchor")
            return found[-1][1]

        origin = day(p["normalization_date"])
        require(factor_at(origin) == 1, "INDEPENDENT_SCHEMA", "Normalization anchor is not one")
        start, end = day(p["start_date"]), day(p["end_date"])
        require(
            start <= end <= day(basis_date), "INDEPENDENT_SCOPE", "Data later than factor basis"
        )
        result = []
        with localcontext() as ctx:
            ctx.prec = 34
            for row in self._daily(s):
                if not start <= row["date"] <= end:
                    continue
                ratio = (
                    Decimal(1)
                    if fq is None
                    else factor_at(row["date"]) / factor_at(basis_date if fq == "pre" else origin)
                )
                projected = dict(row)
                for f in ("open", "high", "low", "close"):
                    projected[f] *= ratio
                fields = _fields(projected)
                for f in ("open", "high", "low", "close"):
                    fields[f]["origin"] = "source" if fq is None else "derived_sina_factor_price"
                fields["factor"] = _value(ratio, "declared_adjustment_convention")
                fields["avg"] = {
                    "state": "unsupported",
                    "value": None,
                    "reason": "adjusted avg not defined",
                }
                result.append(
                    {"date": row["date"], "fields": fields, "raw_close": str(row["close"])}
                )
        return self._result(
            product_id,
            p,
            s,
            result,
            fq=fq,
            basis_date=basis_date,
            normalization_date=origin,
            factor_source=deepcopy(p["factor_source"]),
            factor_history_through_basis=[
                {"date": d, "factor": str(f)} for d, f in factors if d <= basis_date
            ],
            adjustment_policy="Sina cumulative factor ratio; prices only; volume and money remain raw",
            decimal_precision=34,
            rounding=None,
            historical_action_chain_complete=False,
            joinquant_equivalent=False,
        )

    def read_opening(self, product_id, security, *, role):
        b, p = self._open(product_id, security, role, "opening_match")
        s = b["sources"][p["sources"][0]]
        payload = _json(self._checked(s))
        require(
            payload.get("code") == security[:6]
            and payload.get("exchange") == "sz"
            and payload.get("trading_date") == p["date"],
            "INDEPENDENT_SCHEMA",
            "Opening source identity mismatch",
        )
        rows = [
            r
            for r in payload["ticks"]
            if r.get("time_label") == "09:25"
            and r.get("event_kind") == "opening_match"
            and r.get("status_raw") != 8
        ]
        require(len(rows) == 1, "INDEPENDENT_COVERAGE", "Expected one actual opening match")
        r = rows[0]
        require(
            _decimal(r["price"]) == _decimal(r["price_milli"]) / 1000,
            "INDEPENDENT_SCHEMA",
            "Inconsistent opening price",
        )
        return self._result(
            product_id,
            p,
            s,
            [
                {
                    "date": p["date"],
                    "time": "09:25:00+08:00",
                    "price": str(r["price"]),
                    "volume_lots": str(r["volume"]),
                    "volume_shares": str(_decimal(r["volume"]) * 100),
                    "order_count_source": r["order_count"],
                    "status_raw": r["status_raw"],
                    "record_hex": r["record_hex"],
                    "money": None,
                }
            ],
            time_semantics="provider minute label; date from explicit request; not arrival time",
            amount_independently_observed=False,
            order_participation_verified=False,
            independent_matching_verified=False,
        )
