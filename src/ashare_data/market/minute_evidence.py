"""Offline minute evidence beside historical state; labels never grant intervals.

This is an inspection/replay API for a sealed source collection, not the AQ
execution input schema. It reuses the historical package and Sina decoders.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ashare_data.reference.historical_market import HistoricalMarketView, FIELDS, ordinary_limits
from ashare_data.market.client import KLINE_URL, _bars, _calendar, _security
from ashare_data.core.model import DataError, canonical, day, digest, require, timestamp

SCHEMA = "ad.minute-source-evidence.v1"
EXTRA_FIELDS = ("listed", "previous_close", "source_preclose")


class MinuteEvidenceView:
    """Detached source rows and dated facts with explicit execution blockers.

    HTTP/SDK receipt clocks are acquisition clocks. No method fabricates a
    historical availability clock, subdivides auction bars or adjusts prices.
    The underlying HistoricalMarketView remains usable for its original API.
    """

    def __init__(self, binding_path, *, expected_binding_sha256):
        self._market = HistoricalMarketView(
            binding_path, expected_binding_sha256=expected_binding_sha256
        )
        try:
            self._load()
        except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            raise DataError(
                "MINUTE_EVIDENCE_SCHEMA", "Malformed minute evidence extension"
            ) from exc

    def _load(self):
        from ashare_data.research.daily import _json

        m = self._market
        self._spec = spec = m._data["minute_evidence"]
        require(
            spec["schema"] == SCHEMA
            and spec["execution_permission"] is False
            and spec["historical_pit"] is False,
            "MINUTE_EVIDENCE_SCHEMA",
            "Source evidence cannot be promoted to execution/PIT",
        )
        self._symbols = spec["securities"]
        self._dates = spec["dates"]
        require(
            isinstance(self._symbols, list)
            and 1 <= len(self._symbols) <= 10
            and len(set(self._symbols)) == len(self._symbols)
            and all(s in m._instruments for s in self._symbols)
            and isinstance(self._dates, list)
            and 1 <= len(self._dates) <= 10
            and self._dates == sorted(set(self._dates)),
            "MINUTE_EVIDENCE_SCOPE",
            "Bounded explicit securities/dates required",
        )
        for label in self._dates:
            day(label)
        require(
            spec["sessions"] == [["09:30", "11:30"], ["13:00", "14:57"]],
            "MINUTE_EVIDENCE_SCOPE",
            "This profile describes continuous-session target labels through 14:57",
        )
        contract = spec.get("consumer_contract")
        if contract is not None:
            m._refs([contract["amendment_ref"]])
            amendment = _json(m._contents[contract["amendment_ref"]])
            scope = amendment["scope_unchanged"]
            require(
                amendment["schema"] == "aq-ad-minute-data-request-amendment.v1"
                and amendment["request_id"] == spec["request_id"]
                and amendment["aq_candidate_sha"] == contract["aq_candidate_sha"]
                and scope["securities"] == self._symbols
                and scope["days"] == self._dates
                and scope["sessions_local"] == spec["sessions"]
                and amendment["replaces_gate_G08_mechanism"]["caller_opt_in"]["default"] is False
                and all(
                    contract[k] is False
                    for k in (
                        "state_replay_default",
                        "source_clocks_modelled",
                        "historical_pit_verified",
                        "evidence_admitted",
                    )
                ),
                "MINUTE_EVIDENCE_SCHEMA",
                "Consumer opt-in does not establish source facts or PIT",
            )
        self._prices = {"1m": {}, "daily": {}}
        require(
            len(spec["price_sources"]) == 2 * len(self._symbols),
            "MINUTE_EVIDENCE_SOURCE",
            "One immutable minute and daily receipt per security",
        )
        for entry in spec["price_sources"]:
            symbol, frequency = entry["security"], entry["frequency"]
            require(
                symbol in self._symbols and frequency in self._prices,
                "MINUTE_EVIDENCE_SOURCE",
                "Foreign price source",
            )
            require(
                symbol not in self._prices[frequency],
                "MINUTE_EVIDENCE_SOURCE",
                "Ambiguous source observations",
            )
            m._refs([entry["body"], entry["receipt"]])
            raw = m._contents[entry["body"]]
            receipt = _json(m._contents[entry["receipt"]])
            core = dict(receipt)
            observation_id = core.pop("observation_id")
            require(
                digest(core) == observation_id
                and receipt["sha256"] == hashlib.sha256(raw).hexdigest()
                and receipt["http_status"] == 200
                and timestamp(receipt["request_started_at"])
                <= timestamp(receipt["observed_at"])
                <= m._received,
                "MINUTE_EVIDENCE_INTEGRITY",
                "Price receipt/hash/clock mismatch",
            )
            url = urlsplit(receipt["url"])
            scale = 1 if frequency == "1m" else 240
            require(
                url.scheme + "://" + url.netloc + url.path == KLINE_URL
                and not url.fragment
                and parse_qs(url.query)
                == {
                    "symbol": [_security(symbol)],
                    "scale": [str(scale)],
                    "ma": ["no"],
                    "datalen": ["1023"],
                },
                "MINUTE_EVIDENCE_SOURCE",
                "Source identity differs from receipt role",
            )
            rows = _bars(raw, scale)
            require(
                all(
                    all(isinstance(r[f], str) for f in ("open", "high", "low", "close", "volume"))
                    for r in rows
                ),
                "MINUTE_EVIDENCE_SOURCE",
                "Decimal source text required",
            )
            if frequency == "1m":
                require(
                    all(isinstance(r.get("amount"), str) for r in rows),
                    "MINUTE_EVIDENCE_SOURCE",
                    "Original minute amount text required",
                )
            self._prices[frequency][symbol] = {"rows": rows, "entry": entry, "receipt": receipt}
        expected = {(s, d) for s in self._symbols for d in self._dates}
        require(set(m._states) == expected, "MINUTE_EVIDENCE_SCOPE", "State scope differs")
        for row in m._states.values():
            for field in EXTRA_FIELDS:
                m._fact(row[field], "flag" if field == "listed" else "price")
            require(
                row["historical_available_at"] is None and row["replay_effective_at"] is None,
                "MINUTE_EVIDENCE_CLOCK",
                "A model clock must not masquerade as a source clock",
            )
            m._refs(row["source_quality"]["source_refs"])
            require(
                row["source_quality"]["execution_permission"] is False,
                "MINUTE_EVIDENCE_SCHEMA",
                "State source quality cannot grant execution",
            )
            previous = day(row["previous_session"])
            matches = [
                r for r in self._prices["daily"][row["security"]]["rows"] if r["day"] == previous
            ]
            require(
                len(matches) == 1
                and previous < row["date"]
                and Decimal(matches[0]["close"]) == Decimal(row["previous_close"]["value"]),
                "MINUTE_REFERENCE_MISMATCH",
                "Previous close must equal the bound raw daily row",
            )
            if "limit_model_candidate" in row:
                candidate = row["limit_model_candidate"]
                m._refs(candidate["source_refs"])
                require(
                    candidate["execution_usable"] is False
                    and candidate["ratio"] == "0.10"
                    and candidate["tick"] == "0.01"
                    and candidate["rounding"] == "ROUND_HALF_UP_minimum_one_tick"
                    and candidate["assumed_reference"] == row["previous_close"]["value"]
                    and isinstance(candidate["dependencies_unproved"], list)
                    and bool(candidate["dependencies_unproved"])
                    and (candidate["upper_limit"], candidate["lower_limit"])
                    == ordinary_limits(candidate["assumed_reference"], candidate["ratio"]),
                    "MINUTE_LIMIT_CANDIDATE",
                    "Unadmitted candidate must reproduce from the stated raw-close assumption",
                )
        checks = spec["assessments"]
        require(
            isinstance(checks, list)
            and {r["gate_id"] for r in checks} == {f"G{i:02d}" for i in range(12)}
            and len(checks) == 12,
            "MINUTE_EVIDENCE_SCHEMA",
            "Exactly one assessment per requested gate required",
        )
        for check in checks:
            require(
                check["status"]
                in {"obtained", "blocked", "requires_explicit_model", "consumer_pending"}
                and bool(check["reason"])
                and bool(check["owner"]),
                "MINUTE_EVIDENCE_SCHEMA",
                "Invalid gate assessment",
            )
            m._refs(check["source_refs"])
        # This source profile has no established source interval boundaries.
        # A root digest authenticates the package; it does not cure that fact.
        require(
            next(c for c in checks if c["gate_id"] == "G03")["status"] == "blocked",
            "MINUTE_INTERVAL_UNVERIFIED",
            "Source labels cannot satisfy G03",
        )
        require(
            spec["actions"]["absence_proved"] is False and spec["actions"]["complete"] is False,
            "MINUTE_ACTION_COVERAGE",
            "Date-column absence is not all-milestone absence",
        )
        m._refs(spec["actions"]["source_refs"])
        self._action_queries = self._read_action_queries()
        self._calendars = self._read_calendars()
        for row in m._states.values():
            calendar = self._calendars[row["security"].split(".")[1]]
            previous = [d for d, value in calendar["days"].items() if d < row["date"] and value]
            require(
                previous
                and max(previous) == row["previous_session"]
                and calendar["days"].get(row["date"]) is True,
                "MINUTE_REFERENCE_MISMATCH",
                "Previous close is not the source-calendar predecessor",
            )
        self._opening = {}
        for evidence in spec["opening_comparisons"]:
            key = evidence["security"], day(evidence["date"])
            require(
                key in expected and key not in self._opening,
                "MINUTE_EVIDENCE_SCOPE",
                "Invalid opening comparison scope",
            )
            m._refs(evidence["source_refs"])
            raw = [
                r for r in self._prices["1m"][key[0]]["rows"] if r["day"] == key[1] + " 09:31:00"
            ]
            opening, continuous = (
                int(evidence[f]) for f in ("timepoint_0930_volume", "timepoint_0931_volume")
            )
            require(
                len(raw) == 1
                and opening > 0
                and continuous >= 0
                and int(raw[0]["volume"]) == opening + continuous,
                "MINUTE_OPENING_MISMATCH",
                "Opening counterexample does not reproduce",
            )
            self._opening[key] = evidence
        require(set(self._opening) == expected, "MINUTE_EVIDENCE_SCOPE", "Missing opening evidence")

    def _read_calendars(self):
        from ashare_data.research.daily import _json

        start, end = (day(v) for v in self._spec["calendar_range"])
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
        require(0 <= (last - first).days <= 31, "MINUTE_EVIDENCE_SCOPE", "Calendar range too large")
        days = [(first + timedelta(days=n)).date() for n in range((last - first).days + 1)]
        result = {}
        for entry in self._spec["calendars"]:
            market = entry["market"]
            self._market._refs(entry["source_refs"])
            self._market._refs([entry["body"], entry["receipt"]])
            receipt = _json(self._market._contents[entry["receipt"]])
            raw = self._market._contents[entry["body"]]
            require(
                market in {"XSHG", "XSHE"} and market not in result,
                "MINUTE_EVIDENCE_SOURCE",
                "Invalid calendar market",
            )
            if market == "XSHG":
                core = dict(receipt)
                observation_id = core.pop("observation_id")
                require(
                    digest(core) == observation_id
                    and receipt["sha256"] == hashlib.sha256(raw).hexdigest()
                    and receipt["http_status"] == 200
                    and timestamp(receipt["request_started_at"])
                    <= timestamp(receipt["observed_at"])
                    <= self._market._received,
                    "MINUTE_EVIDENCE_INTEGRITY",
                    "Shanghai calendar observation mismatch",
                )
                require(
                    receipt["url"] == "https://www.sse.com.cn/disclosure/dealinstruc/closed/",
                    "MINUTE_EVIDENCE_SOURCE",
                    "Unexpected Shanghai annual source",
                )
                year, closed = _calendar(raw)
                require(
                    all(d.year == year for d in days),
                    "MINUTE_EVIDENCE_SCOPE",
                    "Calendar year mismatch",
                )
                flags = {str(d): d.weekday() < 5 and d not in closed for d in days}
            else:
                require(
                    entry["receipt"] == entry["body"] + ".receipt.json",
                    "MINUTE_EVIDENCE_INTEGRITY",
                    "Monthly calendar must use its checked raw-source receipt",
                )
                url = urlsplit(receipt["url"])
                require(
                    url.scheme == "https"
                    and url.netloc == "www.szse.cn"
                    and url.path == "/api/report/exchange/onepersistenthour/monthList"
                    and parse_qs(url.query) == {"month": [start[:7]]}
                    and start[:7] == end[:7],
                    "MINUTE_EVIDENCE_SOURCE",
                    "Unexpected Shenzhen monthly source",
                )
                rows = _json(raw)["data"]
                require(
                    len(rows) == len({r["jyrq"] for r in rows})
                    and all(r["jybz"] in {"0", "1"} for r in rows),
                    "MINUTE_EVIDENCE_SOURCE",
                    "Duplicate/unknown Shenzhen calendar flag",
                )
                by_date = {day(r["jyrq"]): r["jybz"] == "1" for r in rows}
                require(
                    all(str(d) in by_date for d in days),
                    "MINUTE_EVIDENCE_SCOPE",
                    "Calendar date missing",
                )
                flags = {str(d): by_date[str(d)] for d in days}
            result[market] = {
                "days": flags,
                "source_refs": deepcopy(entry["source_refs"]),
                "historical_available_at": None,
                "historical_pit": False,
            }
        require(
            {s.split(".")[1] for s in self._symbols} <= set(result),
            "MINUTE_EVIDENCE_SCOPE",
            "Exchange calendar missing",
        )
        return result

    def _read_action_queries(self):
        """Recheck actual query identities, pagination and milestone columns."""
        from ashare_data.research.daily import _json

        start, end = (day(self._spec["actions"][k]) for k in ("start", "end"))
        require(
            start <= self._dates[0] <= self._dates[-1] <= end
            and (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days <= 31,
            "MINUTE_ACTION_COVERAGE",
            "Action review must bound every requested day within 31 days",
        )
        results = []
        for query in self._spec["action_queries"]:
            report, symbol = query["report"], query["security"]
            require(
                report in {"RPT_SHAREBONUS_DET", "RPT_F10_DIVIDEND_MAIN"}
                and symbol in self._symbols,
                "MINUTE_ACTION_COVERAGE",
                "Foreign action query",
            )
            f10 = report == "RPT_F10_DIVIDEND_MAIN"
            filter_value = (
                '(SECUCODE="' + symbol[:6] + (".SH" if symbol.endswith("XSHG") else ".SZ") + '")'
                if f10
                else '(SECURITY_CODE="' + symbol[:6] + '")'
            )
            rows, counts = [], set()
            require(
                1 <= len(query["pages"]) <= 10, "MINUTE_ACTION_COVERAGE", "Unbounded action pages"
            )
            for n, page in enumerate(query["pages"], 1):
                self._market._refs([page["body"], page["receipt"]])
                require(
                    page["receipt"] == page["body"] + ".receipt.json",
                    "MINUTE_EVIDENCE_INTEGRITY",
                    "Action page must use its checked raw-source receipt",
                )
                receipt = _json(self._market._contents[page["receipt"]])
                url = urlsplit(receipt["url"])
                params = parse_qs(url.query)
                expected = {
                    "reportName": [report],
                    "columns": ["ALL"],
                    "pageSize": ["20"],
                    "pageNumber": [str(n)],
                    "sortColumns": ["REPORT_TIME,NOTICE_DATE" if f10 else "REPORT_DATE"],
                    "sortTypes": ["-1,-1" if f10 else "-1"],
                    "filter": [filter_value],
                    "source": ["HSF10" if f10 else "WEB"],
                    "client": ["PC" if f10 else "WEB"],
                }
                require(
                    params == expected
                    and url.scheme == "https"
                    and url.netloc
                    == ("datacenter.eastmoney.com" if f10 else "datacenter-web.eastmoney.com")
                    and url.path == ("/securities/api/data/v1/get" if f10 else "/api/data/v1/get"),
                    "MINUTE_ACTION_COVERAGE",
                    "Action query filtered or page identity differs",
                )
                response = _json(self._market._contents[page["body"]])
                result = response["result"]
                require(
                    response["success"] is True
                    and type(result["count"]) is int
                    and result["pages"] == len(query["pages"])
                    and isinstance(result["data"], list)
                    and 0 < len(result["data"]) <= 20
                    and (n == len(query["pages"]) or len(result["data"]) == 20),
                    "MINUTE_ACTION_COVERAGE",
                    "Incomplete action pagination",
                )
                rows.extend(result["data"])
                counts.add(result["count"])
            require(
                counts == {len(rows)}
                and len(rows) == len({digest(r) for r in rows})
                and all(r["SECURITY_CODE"] == symbol[:6] for r in rows),
                "MINUTE_ACTION_COVERAGE",
                "Missing, duplicate or foreign action rows",
            )
            fields = ["EQUITY_RECORD_DATE", "EX_DIVIDEND_DATE"] + (["PAY_CASH_DATE"] if f10 else [])
            coverage = {}
            for field in fields:
                require(
                    all(field in r for r in rows), "MINUTE_ACTION_COVERAGE", "Action field missing"
                )
                values = [day(r[field][:10]) for r in rows if r[field] is not None]
                coverage[field] = {
                    "nonnull_count": len(values),
                    "null_count": len(rows) - len(values),
                    "in_range_dates": [v for v in values if start <= v <= end],
                }
            results.append(
                {
                    "security": symbol,
                    "report": report,
                    "source_rows": len(rows),
                    "pages": len(query["pages"]),
                    "source_query_complete": True,
                    "coverage_start": start,
                    "coverage_end": end,
                    "milestones": coverage,
                    "absence_proved": False,
                    "source_refs": [p[k] for p in query["pages"] for k in ("body", "receipt")],
                }
            )
        require(
            {(r["security"], r["report"]) for r in results}
            == {
                (s, r)
                for s in self._symbols
                for r in ("RPT_SHAREBONUS_DET", "RPT_F10_DIVIDEND_MAIN")
            }
            and len(results) == 2 * len(self._symbols),
            "MINUTE_ACTION_COVERAGE",
            "Action source coverage inventory differs",
        )
        return results

    def descriptor(self):
        return {
            **self._market.descriptor(),
            "minute_schema": SCHEMA,
            "minute_dates": list(self._dates),
            "readiness": "blocked",
            "historical_available_at": None,
            "source_interval_verified": False,
            "price_unit": "CNY/share",
            "volume_unit": "share",
            "amount_unit": None,
            "provider_declared_amount_unit": "CNY",
            "amount_unit_independently_verified": False,
            "consumer_contract": deepcopy(self._spec.get("consumer_contract")),
        }

    def lineage(self):
        return self._market.lineage()

    def calendars(self):
        return deepcopy(self._calendars)

    def state(self, symbol, on_date, *, visibility="posthoc", knowledge_at=None, require_fields=()):
        require(
            isinstance(require_fields, (tuple, list))
            and set(require_fields) <= set(FIELDS + EXTRA_FIELDS),
            "INVALID_ARGUMENT",
            "Unknown required state field",
        )
        row = self._market.state(
            symbol,
            on_date,
            visibility=visibility,
            knowledge_at=knowledge_at,
            require_fields=[f for f in require_fields if f in FIELDS],
        )
        require(
            not require_fields or not row["dependency_conflicts"],
            "HISTORICAL_DEPENDENCY_CONFLICT",
            "Minute state has conflicting source dependencies",
            {"conflicts": row["dependency_conflicts"]},
        )
        missing = [f for f in require_fields if f in EXTRA_FIELDS and row[f]["value"] is None]
        require(
            not missing,
            "HISTORICAL_UNKNOWN",
            "Required minute state is unknown",
            {"fields": missing},
        )
        return row

    def instruments(self, symbols, **kwargs):
        return self._market.instruments(symbols, **kwargs)

    @staticmethod
    def _target_labels(label):
        labels = []
        for begin, end in (("09:30", "11:30"), ("13:00", "14:57")):
            clock = datetime.fromisoformat(label + "T" + begin) + timedelta(minutes=1)
            stop = datetime.fromisoformat(label + "T" + end)
            while clock <= stop:
                labels.append(clock.strftime("%Y-%m-%d %H:%M:%S"))
                clock += timedelta(minutes=1)
        return labels

    def bars(
        self, symbol, on_date, *, visibility="posthoc", knowledge_at=None, require_intervals=False
    ):
        self.state(symbol, on_date, visibility=visibility, knowledge_at=knowledge_at)
        require(type(require_intervals) is bool, "INVALID_ARGUMENT", "Boolean required")
        require(
            not require_intervals,
            "MINUTE_INTERVAL_UNVERIFIED",
            "09:31 contains the opening timepoint; remaining interval boundaries unverified",
        )
        source = self._prices["1m"][symbol]
        wanted = set(self._target_labels(on_date))
        rows = []
        for index, raw in enumerate(source["rows"]):
            if raw["day"][:10] != on_date:
                continue
            rows.append(
                {
                    "security": symbol,
                    "source_label": raw["day"],
                    "source_row": deepcopy(raw),
                    "source_row_index": index,
                    "response_sha256": source["receipt"]["sha256"],
                    "observation_id": source["receipt"]["observation_id"],
                    "source_refs": [source["entry"]["body"], source["entry"]["receipt"]],
                    "in_target_label_grid": raw["day"] in wanted,
                    "interval_start": None,
                    "interval_end": None,
                    "historical_available_at": None,
                    "replay_effective_at": None,
                    "execution_usable": False,
                }
            )
        return rows

    def label_coverage(self):
        result = []
        for symbol in self._symbols:
            for label in self._dates:
                rows = self.bars(symbol, label)
                expected = set(self._target_labels(label))
                actual = {r["source_label"] for r in rows}
                result.append(
                    {
                        "security": symbol,
                        "date": label,
                        "source_rows": len(rows),
                        "target_labels": len(expected),
                        "matched_labels": len(expected & actual),
                        "missing_labels": sorted(expected - actual),
                        "outside_labels": sorted(actual - expected),
                        "label_grid_complete": expected <= actual,
                        "interval_coverage_verified": False,
                        "opening_comparison": deepcopy(self._opening[(symbol, label)]),
                    }
                )
        return result

    def corporate_actions(self, symbols, *, start, end, visibility="posthoc", knowledge_at=None):
        self._market._visible(visibility, knowledge_at)
        require(
            isinstance(symbols, (tuple, list))
            and symbols
            and len(set(symbols)) == len(symbols)
            and set(symbols) <= set(self._symbols)
            and day(start) <= day(end),
            "HISTORICAL_SCOPE",
            "Invalid action scope",
        )
        bound = self._spec["actions"]
        require(
            bound["start"] <= start <= end <= bound["end"],
            "HISTORICAL_SCOPE",
            "Action query outside bounded review",
        )
        milestones = (
            "record_date",
            "ex_date",
            "payment_date",
            "share_credit_date",
            "new_share_trading_date",
        )
        rows = [
            deepcopy(a)
            for a in self._market._data["actions"]
            if a["security"] in symbols
            and any(a.get(k) and start <= a[k] <= end for k in milestones)
        ]
        return {
            "rows": rows,
            "linked_events": [
                deepcopy(a) for a in self._market._data["actions"] if a["security"] in symbols
            ],
            "coverage": "bounded_source_date_columns_only",
            "complete": False,
            "absence_proved": False,
            "historical_pit": False,
            "review": deepcopy(bound),
            "milestones_checked": list(milestones),
            "source_queries": [
                deepcopy(q) for q in self._action_queries if q["security"] in symbols
            ],
            "unknown_dates_are_not_no_event": True,
        }

    def admission_report(self):
        checks = deepcopy(self._spec["assessments"])
        return {
            "schema": "ad.minute-evidence-admission.v1",
            "request_id": self._spec["request_id"],
            "binding_sha256": self._market.binding_sha256,
            "ready_for_execution": False,
            "export_schema": None,
            "real_backtest_executed": False,
            "historical_available_at": None,
            "assessments": checks,
            "consumer_contract": deepcopy(self._spec.get("consumer_contract")),
            "blocking_gates": [c["gate_id"] for c in checks if c["status"] != "obtained"],
            "assessment_counts": dict(Counter(c["status"] for c in checks)),
            "label_coverage": self.label_coverage(),
            "network_used": False,
        }

    def export_backtest(self):
        raise DataError(
            "MINUTE_GATES_BLOCKED",
            "This evidence cannot truthfully populate ad-minute-backtest.v1",
            self.admission_report(),
        )


def seal_minute_evidence(source_root, destination):
    """Seal an explicitly curated file inventory and validate it before publication.

    Input ``inventory.json`` uses the existing historical binding shape, whose
    ``files`` hashes must already be fixed. Copying files never certifies their
    semantic claims. The v1 minute profile always reports its outstanding gates.
    """
    import os
    import shutil
    import tempfile
    from ashare_data.research.daily import _json

    root, out = Path(source_root).resolve(), Path(destination).resolve()
    require(not out.exists(), "DESTINATION_EXISTS", "Refuse to overwrite a sealed package")
    inventory = _json((root / "inventory.json").read_bytes())
    body = canonical(inventory)
    expected = hashlib.sha256(body).hexdigest()
    # The common loader enforces path confinement, file sizes/hashes and clocks.
    out.parent.mkdir(parents=True, exist_ok=True)
    require(
        isinstance(inventory.get("files"), list) and 1 <= len(inventory["files"]) <= 1000,
        "HISTORICAL_FILE",
        "Invalid inventory size",
    )
    with tempfile.TemporaryDirectory(prefix=".minute-validate-", dir=out.parent) as temporary:
        stage = Path(temporary)
        (stage / "binding.json").write_bytes(body)
        total = 0
        seen = set()
        for entry in inventory["files"]:
            relative = Path(entry["path"])
            path = root / relative
            require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and path.resolve().is_relative_to(root)
                and not path.is_symlink(),
                "HISTORICAL_FILE",
                "Unsafe input path",
            )
            require(
                relative.as_posix() not in {"binding.json", "inventory.json"}
                and relative.as_posix() not in seen,
                "HISTORICAL_FILE",
                "Duplicate or reserved input path",
            )
            seen.add(relative.as_posix())
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            require(
                path.stat().st_size <= 16 * 1024 * 1024,
                "HISTORICAL_FILE",
                "Evidence exceeds size limit",
            )
            total += path.stat().st_size
            require(total <= 64 * 1024 * 1024, "HISTORICAL_FILE", "Package exceeds size limit")
            shutil.copyfile(path, target)
        view = MinuteEvidenceView(stage / "binding.json", expected_binding_sha256=expected)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive destination reservation prevents replacing another package.
        out.mkdir()
        try:
            for path in sorted(stage.rglob("*")):
                target = out / path.relative_to(stage)
                if path.is_dir():
                    target.mkdir(exist_ok=True)
                else:
                    with target.open("xb") as file:
                        file.write(path.read_bytes())
                        file.flush()
                        os.fsync(file.fileno())
        except Exception:
            # Retain the partial directory for diagnosis; never replace/retry it.
            raise
    return {
        "binding_path": str(out / "binding.json"),
        "binding_sha256": expected,
        "admission": view.admission_report(),
    }
