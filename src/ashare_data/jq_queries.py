"""Fixed, source-observed query products; never a general JQ API emulator.

This adapter preserves request parameters, snapshot clocks, empty results and
tagged nonfinite values. Only whitelisted data evidence enters the sealed store.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import re

from .jq_observations import (BASE_FIELDS, JQObservationInput, JQObservationRead, MAX_BYTES,
                              _json, _read_file, _sha, _source_time)
from .model import canonical, day, digest, require, symbol, timestamp
from .storage import identifier, immutable_write

SCHEMA = "ad.jq-query-observations.v1"
FIELDS = {"open", "high", "low", "close", "volume", "money", "avg", "high_limit",
          "low_limit", "pre_close", "paused", "factor", "open_interest"}
EVENT_FIELDS = {
    "data_request": {"api", "label", "parameters", "symbol"},
    "data_result": {"api", "label", "columns", "dtypes", "rows", "symbol"},
    "query_request": {"label", "params"}, "query_return": {"label", "columns", "rows"},
    "query_exception": {"label", "error", "error_type", "native_repr"},
    "query_rows": {"label", "offset", "rows"},
    "field_request": {"field", "params"},
    "field_return": {"field", "columns", "dtypes", "rows"},
    "field_exception": {"field", "error", "error_type", "native_repr"},
    "coverage_request": {"api", "params"},
    "adjustment_asof": {"asof_date", "query_start", "query_end", "use_real_price"},
    "calendar_probe": {"previous_date", "trade_days"},
    "universe_summary": {"query_date", "native_columns", "native_count", "selected_fields"},
    "universe_rows": {"query_date", "offset", "total", "rows"},
    "security_info": {"symbol", "data"},
    "depth_summary": {"first_non_null", "native_count", "native_first", "native_last"},
    "depth_rows": {"offset", "total", "rows"},
    "index_members": {"index_symbol", "query_date", "native_count", "codes"},
    "trading_calendar": {"start_date", "end_date", "dates"},
    "freshness_rows": {"columns", "published_at_available", "revision_id_available", "rows",
                       "symbol", "timestamp_scope"},
    "capture_start": {"logical_time", "observed_wall_utc"},
    "capture_end": {"observed_wall_utc"},
}


def _text(value):
    require(type(value) is str and len(value) <= 2048, "JQ_QUERY_SCHEMA", "invalid bounded text")
    return value


def _date_param(value):
    _text(value)
    if len(value) == 10:
        return day(value)
    return _source_time(value) if " " in value else timestamp(value).isoformat()


def _parameters(api, params, *, security=None):
    require(type(api) is str, "JQ_QUERY_SCHEMA", "API must be a string")
    require(type(params) is dict, "JQ_QUERY_SCHEMA", "query parameters must be an object")
    out = dict(params)
    if security is not None:
        require("security" not in out or out["security"] == security,
                "JQ_SCOPE", "query security mismatch")
        out["security"] = security
    allowed = {
        "get_price": {"security", "fields", "fq", "frequency", "skip_paused", "fill_paused",
                      "start_date", "end_date", "count"},
        "get_all_securities": {"date", "types"},
        "get_index_stocks": {"date", "index_symbol"},
        "get_security_info": {"security"},
        "get_trade_days": {"start_date", "end_date"},
    }
    require(api in allowed and set(out) <= allowed[api], "JQ_QUERY_SCHEMA", "unsupported query shape")
    for key in ("security", "index_symbol"):
        if key in out:
            symbol(out[key])
    for key in ("start_date", "end_date", "date"):
        if key in out:
            out[key] = _date_param(out[key])
    for key in ("skip_paused", "fill_paused"):
        if key in out:
            require(type(out[key]) is bool, "JQ_QUERY_SCHEMA", "query flags must be boolean")
    if "count" in out:
        require(type(out["count"]) is int and 1 <= out["count"] <= 6000,
                "JQ_QUERY_SCHEMA", "query count out of bounds")
    if api == "get_price":
        require({"security", "fields", "frequency", "fq", "end_date"} <= set(out),
                "JQ_QUERY_SCHEMA", "incomplete fixed price query")
        require(out["frequency"] in ("daily", "1m") and out["fq"] in (None, "pre", "post"),
                "JQ_QUERY_SCHEMA", "unsupported frequency or adjustment")
        require(type(out["fields"]) is list and 1 <= len(out["fields"]) <= 20
                and all(type(f) is str and re.fullmatch(r"[A-Za-z_]{1,64}", f) for f in out["fields"])
                and len(set(out["fields"])) == len(out["fields"]), "JQ_QUERY_SCHEMA", "invalid fields")
    elif api == "get_all_securities":
        require(set(out) == {"date", "types"} and out["types"] == ["stock"],
                "JQ_QUERY_SCHEMA", "only the fixed stock metadata query is supported")
    else:
        require(set(out) == allowed[api], "JQ_QUERY_SCHEMA", "incomplete fixed query")
    return out


def _price_rows(rows, fields, observed):
    require(type(rows) is list and len(rows) <= 6000, "JQ_QUERY_SCHEMA", "unbounded price rows")
    require(set(fields) <= FIELDS, "JQ_QUERY_SCHEMA", "unsupported observed data field")
    out, labels = [], set()
    for row in rows:
        require(type(row) is dict and set(row) == set(fields) | {"index"},
                "JQ_QUERY_SCHEMA", "unexpected query row fields")
        label = _source_time(row["index"])
        require(label <= observed and label not in labels, "JQ_VISIBILITY", "future or duplicate data label")
        labels.add(label)
        normalized = {"index": row["index"]}
        for k in fields:
            value = row[k]
            if type(value) is dict:
                require(value in ({"nonfinite": "nan"}, {"nonfinite": "inf"}, {"nonfinite": "-inf"}),
                        "JQ_QUERY_SCHEMA", "invalid explicit nonfinite tag")
                normalized[k] = value
            else:
                require(type(value) in (int, Decimal), "JQ_QUERY_SCHEMA", "expected source number")
                number = Decimal(value)
                require(number.is_finite() and abs(number) < Decimal("1e30")
                        and number.as_tuple().exponent >= -64 and len(number.as_tuple().digits) <= 64,
                        "JQ_QUERY_SCHEMA", "source decimal exceeds bounded precision")
                normalized[k] = format(number, "f")
        out.append(normalized)
    return out


def _metadata_row(row, *, include_code=True):
    fields = {"start_date", "end_date", "type"} | ({"code"} if include_code else set())
    require(type(row) is dict and set(row) == fields and row["type"] == "stock",
            "JQ_QUERY_SCHEMA", "unsupported security metadata shape")
    if include_code:
        symbol(row["code"])
    day(row["start_date"])
    day(row["end_date"])
    return dict(row)


def _extract_queries(src):
    require(type(src) is JQObservationInput, "JQ_INPUT", "sources require JQObservationInput")
    require(type(src.case_id) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", src.case_id),
            "JQ_INPUT", "invalid case identifier")
    symbol(src.security)
    start, end = day(src.session_date), day(src.session_end_date or src.session_date)
    require(0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 30,
            "JQ_SCOPE", "source run must span at most 31 calendar dates")
    lines = _read_file(src.events_path, src.events_sha256).splitlines()
    _read_file(src.script_path, src.script_sha256)
    require(1 <= len(lines) <= 1000, "JQ_INPUT", "source must have 1..1000 events")
    accepted, run_tag, recorder_v2 = [], None, False
    for seq, line in enumerate(lines, 1):
        e = _json(line, exact=True)
        require(type(e) is dict and type(e.get("seq")) is int and e["seq"] == seq
                and e.get("case_id") == src.case_id, "JQ_QUERY_SCHEMA", "source sequence mismatch")
        if seq == 1:
            settings = e.get("settings", {})
            require(e.get("kind") == "identity" and type(settings) is dict
                    and settings.get("symbol") == src.security and settings.get("start_date") == start
                    and settings.get("end_date") == end and settings.get("frequency") == "minute"
                    and settings.get("use_real_price") is True, "JQ_SCOPE", "source identity mismatch")
            run_tag = _text(e.get("run_tag"))
            recorder_v2 = settings.get("recorder_revision") == 2
        require(e.get("run_tag") == run_tag and type(e.get("kind")) is str,
                "JQ_QUERY_SCHEMA", "source run mismatch")
        if e["kind"] not in EVENT_FIELDS:
            continue
        extras = EVENT_FIELDS[e["kind"]]
        if recorder_v2 and e["kind"] == "query_return":
            extras = {"label", "columns", "row_count"}
        base = BASE_FIELDS | ({"version"} if recorder_v2 else set())
        require(set(e) == base | extras,
                "JQ_QUERY_SCHEMA", "unexpected query event fields")
        observed = _source_time(e["time"])
        require(start <= observed[:10] <= end, "JQ_SCOPE", "event outside source run")
        ref = {"case_id": src.case_id, "seq": seq, "event_sha256": _sha(line),
               "events_sha256": src.events_sha256, "script_sha256": src.script_sha256,
               "run_tag": run_tag, "observed_at": observed}
        accepted.append((e, ref, line.decode("utf-8")))
    captures = {}
    for e, ref, _ in accepted:
        if e["kind"] in {"capture_start", "capture_end"}:
            wall = timestamp(e["observed_wall_utc"])
            if e["kind"] == "capture_start":
                require(e["logical_time"] == e["time"], "JQ_QUERY_SCHEMA", "capture clock mismatch")
            captures.setdefault(ref["observed_at"], {})[e["kind"]] = wall.isoformat()
    for capture in captures.values():
        require(set(capture) == {"capture_start", "capture_end"}
                and capture["capture_start"] <= capture["capture_end"],
                "JQ_QUERY_SCHEMA", "capture interval incomplete or inverted")
    records, pending, chunks = [], None, None

    def record(api, params, result, refs):
        observed = max(r["observed_at"] for r in refs)
        payload = {"api": api, "parameters": params, "result": result, "observed_at": observed,
                   "available_at": observed, "source_refs": refs,
                   "physical_capture_interval": captures.get(observed),
                   "historical_pit": False, "matching_eligible": False,
                   "adjustment_anchor": observed[:10] if api == "get_price" else None}
        records.append({"query_id": digest(payload), **payload})

    for e, ref, _ in accepted:
        k = e["kind"]
        if k in {"data_request", "query_request", "field_request", "coverage_request"}:
            require(pending is None, "JQ_QUERY_SCHEMA", "unresolved query before next request")
            api = e.get("api", "get_price")
            params = _parameters(api, e.get("parameters", e.get("params")),
                                 security=e.get("symbol"))
            pending = {"api": api, "params": params, "ref": ref,
                       "label": e.get("label", e.get("field"))}
        elif k in {"data_result", "query_return", "field_return", "query_exception", "field_exception",
                   "freshness_rows"}:
            require(pending is not None and pending["api"] == "get_price"
                    and pending["ref"]["observed_at"] == ref["observed_at"],
                    "JQ_QUERY_SCHEMA", "unbound price result")
            require(e.get("label", e.get("field")) == pending["label"],
                    "JQ_QUERY_SCHEMA", "query label mismatch")
            if "symbol" in e:
                require(e["symbol"] == pending["params"]["security"], "JQ_SCOPE", "result security mismatch")
            if "api" in e:
                require(e["api"] == pending["api"], "JQ_QUERY_SCHEMA", "result API mismatch")
            if recorder_v2 and k == "query_return":
                require(chunks is None and e["columns"] == pending["params"]["fields"]
                        and type(e["row_count"]) is int and 0 <= e["row_count"] <= 6000,
                        "JQ_QUERY_SCHEMA", "invalid chunked query summary")
                chunks = {"rows": [], "count": e["row_count"],
                          "refs": [pending["ref"], ref], "kind": "query_return"}
                if not e["row_count"]:
                    record(pending["api"], pending["params"],
                           {"status": "observed_empty", "rows": [],
                            "columns": e["columns"], "dtypes": None}, chunks["refs"])
                    pending = chunks = None
                continue
            if k.endswith("exception"):
                result = {"status": "observed_source_error", "rows": [],
                          "source_error": {"type": _text(e["error_type"]), "message": _text(e["error"])} }
                _text(e["native_repr"])
            else:
                require(e["columns"] == pending["params"]["fields"], "JQ_QUERY_SCHEMA", "columns mismatch")
                dtypes = e.get("dtypes")
                require(dtypes is None or (type(dtypes) is dict and set(dtypes) == set(e["columns"])
                        and all(v in {"float64", "int64", "object", "bool"} for v in dtypes.values())),
                        "JQ_QUERY_SCHEMA", "unexpected source dtypes")
                rows = _price_rows(e["rows"], e["columns"], ref["observed_at"])
                result = {"status": "observed_rows" if rows else "observed_empty", "rows": rows,
                          "columns": e["columns"], "dtypes": dtypes}
                if k == "freshness_rows":
                    require(e["published_at_available"] is False and e["revision_id_available"] is False,
                            "JQ_QUERY_SCHEMA", "unsupported publication/revision evidence")
                    result.update(published_at=None, revision_id=None, timestamp_scope=_text(e["timestamp_scope"]))
            record(pending["api"], pending["params"], result, [pending["ref"], ref])
            pending = None
        elif k == "query_rows":
            require(recorder_v2 and pending is not None and chunks is not None
                    and chunks["kind"] == "query_return" and e["label"] == pending["label"]
                    and ref["observed_at"] == pending["ref"]["observed_at"]
                    and type(e["offset"]) is int and e["offset"] == len(chunks["rows"])
                    and type(e["rows"]) is list and 1 <= len(e["rows"]) <= 20,
                    "JQ_QUERY_SCHEMA", "unbound, overlapping or missing query chunks")
            chunks["rows"].extend(_price_rows(e["rows"], pending["params"]["fields"],
                                            ref["observed_at"]))
            chunks["refs"].append(ref)
            require(len(chunks["rows"]) <= chunks["count"], "JQ_QUERY_SCHEMA", "chunk overflow")
            if len(chunks["rows"]) == chunks["count"]:
                require(len({r["index"] for r in chunks["rows"]}) == chunks["count"],
                        "JQ_QUERY_SCHEMA", "duplicate labels across chunks")
                record(pending["api"], pending["params"],
                       {"status": "observed_rows", "rows": chunks["rows"],
                        "columns": pending["params"]["fields"], "dtypes": None}, chunks["refs"])
                pending = chunks = None
        elif k in {"universe_summary", "depth_summary"}:
            require(pending is not None and chunks is None, "JQ_QUERY_SCHEMA", "unbound chunk summary")
            require(type(e["native_count"]) is int and 0 <= e["native_count"] <= 6000,
                    "JQ_QUERY_SCHEMA", "unbounded source row count")
            if k == "universe_summary":
                require(pending["api"] == "get_all_securities" and e["query_date"] == pending["params"]["date"]
                        and e["selected_fields"] == ["code", "start_date", "end_date", "type"]
                        and e["native_columns"] == ["display_name", "name", "start_date", "end_date", "type"],
                        "JQ_QUERY_SCHEMA", "metadata selection mismatch")
            else:
                require(pending["api"] == "get_price", "JQ_QUERY_SCHEMA", "depth API mismatch")
                require(type(e["first_non_null"]) is dict
                        and set(e["first_non_null"]) == set(pending["params"]["fields"]),
                        "JQ_QUERY_SCHEMA", "unexpected depth summary fields")
                for value in [e["native_first"], e["native_last"], *e["first_non_null"].values()]:
                    if value is not None:
                        require(_source_time(value) <= ref["observed_at"], "JQ_VISIBILITY", "future depth label")
            chunks = {"rows": [], "count": e["native_count"], "refs": [pending["ref"], ref], "kind": k}
        elif k in {"universe_rows", "depth_rows"}:
            require(chunks is not None and type(e["offset"]) is int and e["offset"] == len(chunks["rows"])
                    and e["total"] == chunks["count"] and type(e["rows"]) is list and 1 <= len(e["rows"]) <= 100,
                    "JQ_QUERY_SCHEMA", "missing, overlapping or unbounded chunks")
            if k == "universe_rows":
                require(chunks["kind"] == "universe_summary" and e["query_date"] == pending["params"]["date"],
                        "JQ_QUERY_SCHEMA", "universe query mismatch")
                rows = [_metadata_row(r) for r in e["rows"]]
            else:
                require(chunks["kind"] == "depth_summary", "JQ_QUERY_SCHEMA", "depth query mismatch")
                rows = _price_rows(e["rows"], pending["params"]["fields"], ref["observed_at"])
            chunks["rows"].extend(rows)
            chunks["refs"].append(ref)
            require(len(chunks["rows"]) <= chunks["count"], "JQ_QUERY_SCHEMA", "chunk overflow")
            if len(chunks["rows"]) == chunks["count"]:
                key = "code" if k == "universe_rows" else "index"
                require(len({r[key] for r in chunks["rows"]}) == chunks["count"],
                        "JQ_QUERY_SCHEMA", "duplicate rows across chunks")
                record(pending["api"], pending["params"],
                       {"status": "observed_rows", "rows": chunks["rows"], "source_count": chunks["count"],
                        "requested_result_complete": True, "entire_market_history_complete": False}, chunks["refs"])
                pending = chunks = None
        elif k == "index_members":
            require(pending is not None and pending["api"] == "get_index_stocks"
                    and e["query_date"] == pending["params"]["date"]
                    and e["index_symbol"] == pending["params"]["index_symbol"]
                    and type(e["codes"]) is list and len(e["codes"]) == e["native_count"]
                    and len(set(e["codes"])) == len(e["codes"]) <= 6000,
                    "JQ_QUERY_SCHEMA", "index member result mismatch")
            for code in e["codes"]:
                symbol(code)
            record(pending["api"], pending["params"], {"status": "observed_rows",
                   "rows": [{"code": code} for code in e["codes"]], "source_count": e["native_count"]},
                   [pending["ref"], ref])
            pending = None
        elif k == "security_info":
            symbol(e["symbol"])
            record("get_security_info", {"security": e["symbol"]},
                   {"status": "observed_rows", "rows": [_metadata_row(e["data"], include_code=False)]}, [ref])
        elif k in {"trading_calendar", "calendar_probe"}:
            dates = e.get("dates", e.get("trade_days"))
            require(type(dates) is list and 1 <= len(dates) <= 366 and dates == sorted(set(dates)),
                    "JQ_QUERY_SCHEMA", "invalid source calendar dates")
            for value in dates:
                day(value)
            params = {"start_date": e.get("start_date", start), "end_date": e.get("end_date", end)}
            params = _parameters("get_trade_days", params)
            require(all(params["start_date"] <= v <= params["end_date"] for v in dates),
                    "JQ_QUERY_SCHEMA", "calendar rows outside query")
            result = {"status": "observed_rows", "rows": [{"date": v} for v in dates],
                      "full_exchange_calendar": False}
            if k == "calendar_probe":
                result["source_previous_date"] = day(e["previous_date"])
                result["previous_date_anchor"] = ref["observed_at"][:10]
            record("get_trade_days", params, result, [ref])
        elif k == "adjustment_asof":
            require(day(e["asof_date"]) == ref["observed_at"][:10] and e["use_real_price"] is True,
                    "JQ_QUERY_SCHEMA", "unsupported adjustment anchor")
            day(e["query_start"])
            day(e["query_end"])
    require(pending is None and chunks is None and records, "JQ_QUERY_SCHEMA", "incomplete or empty query source")
    source = {"case_id": src.case_id, "security": src.security, "session_date": start, "session_end_date": end,
              "events_sha256": src.events_sha256, "script_sha256": src.script_sha256,
              "run_tag": run_tag, "source_event_count": len(lines), "query_count": len(records)}
    evidence = [{**ref, "raw_event_json": raw} for _, ref, raw in accepted]
    return source, records, evidence


def import_queries(store, sources, *, source_kind):
    require(type(sources) in (tuple, list) and 1 <= len(sources) <= 10, "JQ_INPUT", "provide 1..10 pinned sources")
    require(source_kind in {"local_jq_backtest_observation", "synthetic_fixture"}, "JQ_INPUT", "source kind required")
    parts = [_extract_queries(s) for s in sources]
    metadata = sorted([s for s, _, _ in parts], key=lambda x: x["case_id"])
    require(len({s["case_id"] for s in metadata}) == len(metadata), "JQ_INPUT", "duplicate case")
    records = sorted([r for _, rows, _ in parts for r in rows], key=lambda r: r["query_id"])
    body = {"schema": SCHEMA, "source_kind": source_kind, "sources": metadata, "queries": records,
            "evidence": sorted([e for _, _, es in parts for e in es], key=lambda e: (e["case_id"], e["seq"])),
            "historical_pit": False, "network_used": False, "live_execution_permission": False,
            "source_clock_domain": "JQ_backtest_context_current_dt",
            "numeric_encoding": "exact decimal strings; explicit source nonfinite tags; no zero substitution",
            "units": {"prices": "CNY/share", "volume": "shares", "money": "CNY", "factor": "source factor"},
            "unit_basis": "source field convention, not independent unit certification",
            "limitations": ["Exact observed requests only; no gap fill or invented adjustment factors.",
                            "Source clock visibility is not historical market publication time.",
                            "Future metadata end dates are source observations, not PIT delisting facts.",
                            "Errors in evidence are source observations, never raised as AD runtime errors.",
                            "Coverage is the requested source response, not verified full market history.",
                            "No order, trade, portfolio or reference result is an input."]}
    raw = canonical(body)
    require(len(raw) <= MAX_BYTES, "JQ_INPUT", "query product exceeds 8 MiB")
    dataset_id = digest(body)
    with store._writer():
        root = store.root / "jq_queries"
        root.mkdir(exist_ok=True)
        immutable_write(root / (dataset_id + ".json"), raw)
    return dataset_id


class JQQueryView:
    def __init__(self, store, dataset_id):
        self._root = store.root
        self.dataset_id = identifier(dataset_id)
        self._load()

    def _load(self):
        raw = _read_file(self._root / "jq_queries" / (self.dataset_id + ".json"))
        require(_sha(raw) == self.dataset_id, "JQ_INTEGRITY", "sealed query product hash mismatch")
        body = _json(raw)
        require(body.get("schema") == SCHEMA, "JQ_QUERY_SCHEMA", "unsupported query product")
        return body

    def describe(self):
        """Owner inventory, not a strategy read: includes future snapshot descriptors."""
        body = self._load()
        return {"dataset_id": self.dataset_id, **{k: v for k, v in body.items() if k not in {"queries", "evidence"}},
                "queries": [{k: v for k, v in r.items() if k != "result"} | {
                    "source_status": r["result"]["status"], "row_count": len(r["result"]["rows"])}
                    for r in body["queries"]]}

    def source_evidence(self):
        """Owner audit only; includes source query errors as data, not AD exceptions."""
        return self._load()["evidence"]

    def read(self, query_id, *, as_of):
        identifier(query_id)
        clock = timestamp(as_of).isoformat()
        body = self._load()
        found = [r for r in body["queries"] if r["query_id"] == query_id]
        require(len(found) == 1, "JQ_QUERY_MISSING", "query identity is not in this product")
        record = found[0]
        visible = record["available_at"] <= clock
        result = {"dataset_id": self.dataset_id, "query_id": query_id, "as_of": clock,
                  "status": "available" if visible else "unavailable",
                  "reason": None if visible else "NOT_OBSERVED_AS_OF", "rows": [],
                  "historical_pit": False, "matching_eligible": False, "network_used": False}
        if visible:
            result.update(record)
            result["rows"] = record["result"]["rows"]
            if record["api"] in {"get_all_securities", "get_security_info"}:
                for row in result["rows"]:
                    row["future_end_date_observation"] = row["end_date"] > record["observed_at"][:10]
                    row["end_date_is_historical_pit_fact"] = False
            result["use_restriction"] = "fixed source query observation; not certified execution input or historical PIT"
        return JQObservationRead(canonical(result).decode())

    def query(self, api, parameters, *, as_of, observed_at=None):
        """Match an exact recorded request; own field/future guards run independently."""
        clock = timestamp(as_of).isoformat()
        params = _parameters(api, parameters)
        if api == "get_price":
            require(set(params["fields"]) <= FIELDS, "JQ_QUERY_FIELD", "unsupported price field")
            end = params["end_date"]
            require(end <= (clock[:10] if len(end) == 10 else clock),
                    "JQ_FUTURE_QUERY", "requested price end is later than the read clock")
        if api == "get_price":
            validate_daily_visibility(params, clock)
        anchor = timestamp(observed_at).isoformat() if observed_at is not None else None
        found = [r for r in self._load()["queries"] if r["api"] == api and r["parameters"] == params
                 and r["available_at"] <= clock and (anchor is None or r["observed_at"] == anchor)]
        if not found:
            return JQObservationRead(canonical({"dataset_id": self.dataset_id, "status": "unavailable",
                "reason": "NO_OBSERVED_QUERY_AS_OF", "rows": [], "historical_pit": False}).decode())
        newest = max(r["observed_at"] for r in found)
        found = [r for r in found if r["observed_at"] == newest]
        require(len({digest(r["result"]) for r in found}) == 1, "JQ_CONFLICT", "conflicting query observations")
        return self.read(sorted(found, key=lambda r: r["query_id"])[0]["query_id"], as_of=clock)

    def read_initialization(self, query_id, security, *, valuation_date, as_of):
        """Only the explicitly selected prior raw daily close, never today's quote."""
        security, label_date = symbol(security), day(valuation_date)
        clock = timestamp(as_of)
        require(1 <= (clock.date() - date.fromisoformat(label_date)).days <= 31,
                "JQ_INITIALIZATION_SCOPE", "initialization requires an explicit prior date within 31 days")
        result = self.read(query_id, as_of=clock).to_dict()
        out = {"dataset_id": self.dataset_id, "query_id": query_id, "security": security,
               "valuation_date": label_date, "as_of": clock.isoformat(), "role": "initialization",
               "status": "unavailable", "reason": result.get("reason"), "rows": [],
               "historical_pit": False, "strategy_visible": False, "matching_eligible": False,
               "current_quote": False, "source_preclose": False, "network_used": False,
               "adjacent_session_certified": False}
        if result["status"] != "available":
            return JQObservationRead(canonical(out).decode())
        params = result.get("parameters", {})
        require(result.get("api") == "get_price" and params.get("security") == security
                and params.get("frequency") == "daily" and params.get("fq") is None
                and "close" in params.get("fields", []),
                "JQ_INITIALIZATION_SOURCE", "requires a matching raw daily close query")
        matches = [r for r in result["rows"] if r.get("index") == label_date + " 00:00:00"]
        require(len(matches) <= 1, "JQ_CONFLICT", "duplicate daily valuation label")
        if not matches or type(matches[0].get("close")) is not str:
            out["reason"] = "NO_FINITE_SOURCE_CLOSE"
        else:
            close = Decimal(matches[0]["close"])
            require(close.is_finite() and close > 0, "JQ_INITIALIZATION_SOURCE", "invalid close")
            out.update(status="available", reason=None, observed_at=result["observed_at"],
                       available_at=result["available_at"], source_refs=result["source_refs"],
                       physical_capture_interval=result.get("physical_capture_interval"),
                       rows=[{"security": security, "source_label_at": label_date + "T00:00:00+08:00",
                              "values": {"close": matches[0]["close"]}}])
        return JQObservationRead(canonical(out).decode())


def validate_daily_visibility(params, clock):
    """Independent field-level guard, including an end timestamp equal to now."""
    if (params["frequency"] == "daily" and params["end_date"][:10] == clock[:10]
            and clock[11:19] < "15:00:00"):
        require(set(params["fields"]) <= {"open"} and clock[11:19] >= "09:30:00",
                "JQ_INCOMPLETE_DAILY", "unfinished session daily fields are not available")
