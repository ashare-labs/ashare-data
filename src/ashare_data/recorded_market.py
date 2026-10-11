"""Bounded recorder-v2 market data. No accounts, trades or inferred bars.

All responses preserve source hashes and simulated observation clocks. Engine-only
retrospective capacity is a declared model, not historical availability evidence.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .jq_observations import (
    BASE_FIELDS, JQObservationInput, JQObservationRead, _json, _read_file,
    _sha, _source_time, _values,
)
from .jq_queries import FIELDS, _extract_queries, _parameters, validate_daily_visibility
from .model import canonical, day, digest, require, symbol, timestamp
from .storage import identifier, immutable_write

SCHEMA = "ad.recorded-market.v1"
CURRENT = {"last_price", "day_open", "high_limit", "low_limit", "paused"}


def import_recorded_market(store, sources, *, source_kind):
    require(source_kind in {"local_jq_backtest_observation", "synthetic_fixture"},
            "JQ_INPUT", "explicit source kind required")
    require(type(sources) in (list, tuple) and 1 <= len(sources) <= 10,
            "JQ_INPUT", "provide 1..10 sources")
    current, queries, calendars, metadata = [], [], [], []
    for src in sources:
        require(type(src) is JQObservationInput, "JQ_INPUT", "pinned source required")
        raw = _read_file(src.events_path, src.events_sha256)
        _read_file(src.script_path, src.script_sha256)
        lines = raw.splitlines()
        require(1 <= len(lines) <= 1000, "JQ_INPUT", "unbounded source")
        events = [_json(line, exact=True) for line in lines]
        settings = events[0].get("settings", {})
        start, end = day(src.session_date), day(src.session_end_date or src.session_date)
        require(0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 30,
                "JQ_SCOPE", "source range exceeds 31 days")
        require(events[0].get("kind") == "identity" and settings.get("symbol") == src.security
                and settings.get("start_date") == start and settings.get("end_date") == end
                and settings.get("frequency") == "minute" and settings.get("use_real_price") is True
                and settings.get("recorder_revision") == 2,
                "JQ_SCOPE", "recorder-v2 source identity mismatch")
        securities = {symbol(src.security)}
        if "second_symbol" in settings:
            securities.add(symbol(settings["second_symbol"]))
        run_tag = events[0]["run_tag"]
        meta = dict(case_id=src.case_id, events_sha256=src.events_sha256,
                    script_sha256=src.script_sha256, session_date=start, session_end_date=end,
                    securities=sorted(securities), run_tag=run_tag)
        metadata.append(meta)
        for seq, (e, line) in enumerate(zip(events, lines), 1):
            require(e.get("seq") == seq and type(e["seq"]) is int
                    and e.get("case_id") == src.case_id and e.get("run_tag") == run_tag,
                    "JQ_SCHEMA", "source identity or sequence mismatch")
            if e["kind"] not in {"current_data", "day_start"}:
                continue
            observed = _source_time(e["time"])
            require(start <= observed[:10] <= end, "JQ_SCOPE", "observation outside source")
            ref = dict(case_id=src.case_id, seq=seq, event_sha256=_sha(line),
                       events_sha256=src.events_sha256, script_sha256=src.script_sha256,
                       observed_at=observed)
            base = BASE_FIELDS | {"version"}
            if e["kind"] == "current_data":
                require(set(e) == base | {"symbol", "data", "label"}
                        and e["symbol"] in securities, "JQ_SCHEMA", "unexpected quote fields")
                current.append(dict(security=e["symbol"], source_label_at=observed,
                                    observed_at=observed, available_at=observed,
                                    values=_values(e["data"], CURRENT), source_refs=[ref]))
            else:
                require(set(e) == base | {"previous_date"}, "JQ_SCHEMA", "unexpected calendar fields")
                previous = day(e["previous_date"])
                require(previous < observed[:10], "JQ_SCHEMA", "invalid prior session")
                calendars.append(dict(session=observed[:10], previous=previous, source_refs=[ref]))
        if any(e["kind"] == "query_request" for e in events):
            _, rows, _ = _extract_queries(src)
            queries.extend(rows)
    # Reject conflicting same-time quotes and calendar anchors, including across runs.
    for items, key in [(current, lambda r: (r["security"], r["observed_at"])),
                       (calendars, lambda r: r["session"])]:
        seen = {}
        for row in items:
            value = row.get("values", row.get("previous"))
            require(key(row) not in seen or seen[key(row)] == value,
                    "JQ_CONFLICT", "conflicting source observation")
            seen[key(row)] = value
    body = dict(schema=SCHEMA, source_kind=source_kind,
                sources=sorted(metadata, key=lambda r: r["events_sha256"]),
                current=sorted(current, key=digest), queries=sorted(queries, key=digest),
                calendars=sorted(calendars, key=digest), historical_pit=False,
                source_clock_domain="JQ_backtest_context_current_dt", network_used=False,
                account_trade_oracle_imported=False)
    raw = canonical(body)
    require(len(raw) <= 8 * 1024 * 1024, "JQ_INPUT", "product too large")
    dataset_id = digest(body)
    with store._writer():
        root = store.root / "recorded_market"
        root.mkdir(exist_ok=True)
        immutable_write(root / (dataset_id + ".json"), raw)
    return dataset_id


class RecordedMarketView:
    def __init__(self, store, dataset_id):
        self._root = store.root
        self.dataset_id = identifier(dataset_id)
        self._load()

    def _load(self):
        raw = _read_file(self._root / "recorded_market" / (self.dataset_id + ".json"))
        require(_sha(raw) == self.dataset_id, "JQ_INTEGRITY", "recorded market hash mismatch")
        body = _json(raw)
        require(body["schema"] == SCHEMA, "JQ_SCHEMA", "unknown recording schema")
        return body

    def describe(self):
        body = self._load()
        return dict(dataset_id=self.dataset_id, sources=body["sources"],
                    calendars=body["calendars"], historical_pit=False,
                    counts={k: len(body[k]) for k in ("current", "queries")})

    def _result(self, rows, **extra):
        return JQObservationRead(canonical(dict(dataset_id=self.dataset_id,
            status="available" if rows else "unavailable", rows=rows,
            historical_pit=False, **extra)).decode())

    def current(self, security, *, as_of, role="strategy"):
        require(role in {"strategy", "matching"}, "JQ_ROLE", "quote role unsupported")
        sid, clock = symbol(security), timestamp(as_of).isoformat()
        rows = [r for r in self._load()["current"]
                if r["security"] == sid and r["observed_at"] == clock]
        return self._result(rows[:1], role=role, as_of=clock, strategy_visible=role == "strategy")

    def query(self, parameters, *, as_of):
        clock = timestamp(as_of).isoformat()
        params = _parameters("get_price", parameters)
        require(set(params["fields"]) <= FIELDS, "JQ_QUERY_FIELD", "unknown price field")
        require(params["end_date"] <= (clock[:10] if len(params["end_date"]) == 10 else clock),
                "JQ_FUTURE_QUERY", "requested end exceeds read clock")
        validate_daily_visibility(params, clock)
        found = [r for r in self._load()["queries"] if r["parameters"] == params
                 and r["available_at"] <= clock]
        if not found:
            return self._result([], reason="NO_OBSERVED_QUERY_AS_OF")
        newest = max(r["observed_at"] for r in found)
        found = [r for r in found if r["observed_at"] == newest]
        require(len({digest(r["result"]) for r in found}) == 1, "JQ_CONFLICT", "query conflict")
        value = found[0]
        return self._result(value["result"]["rows"], observation=value,
                            source_status=value["result"]["status"])

    def market(self, security, *, label, frequency, as_of, role):
        """Raw rows only. Daily capacity exposes volume alone, without time backdating."""
        sid, clock = symbol(security), timestamp(as_of).isoformat()
        label = timestamp(label).isoformat()
        roles = {"matching": {"1m"}, "valuation": {"daily"},
                 "initialization": {"daily"}, "retrospective_capacity": {"daily"},
                 "retrospective_minute": {"1m"}}
        require(role in roles and frequency in roles[role], "JQ_ROLE", "market role unsupported")
        if role == "initialization":
            require(label[:10] < clock[:10], "JQ_VISIBILITY", "initialization must be prior day")
        if role == "retrospective_capacity":
            require(label[:10] == clock[:10], "JQ_VISIBILITY", "capacity must be same session")
        elif role == "valuation":
            require(label[:10] <= clock[:10] and clock[11:19] >= "15:00:00",
                    "JQ_VISIBILITY", "daily valuation requires completed session")
        else:
            require(label <= clock, "JQ_VISIBILITY", "future row label")
        rows = []
        for q in self._load()["queries"]:
            p = q["parameters"]
            if (p["security"], p["frequency"], p["fq"]) != (sid, frequency, None):
                continue
            if role not in {"retrospective_capacity", "retrospective_minute"} and q["available_at"] > clock:
                continue
            for r in q["result"]["rows"]:
                if _source_time(r["index"]) != label:
                    continue
                required = {"volume"} if role == "retrospective_capacity" else {"close"} if frequency == "daily" else {"open", "high", "low", "close", "volume", "money"}
                if not required <= r.keys():
                    continue
                values = {k: r[k] for k in required}
                require(all(isinstance(v, str) and Decimal(v).is_finite() and Decimal(v) >= 0
                            for v in values.values()), "JQ_SCHEMA", "invalid market number")
                rows.append(dict(security=sid, source_label_at=label, values=values,
                                 observed_at=q["observed_at"], source_refs=q["source_refs"]))
        require(len({digest(r["values"]) for r in rows}) <= 1, "JQ_CONFLICT", "raw market conflict")
        return self._result(rows[:1], role=role, as_of=clock, strategy_visible=False,
                            retrospective=role in {"retrospective_capacity", "retrospective_minute"})

    def engine_quote(self, security, *, as_of):
        """Explicit retrospective minute mark with earlier observed daily limits.

        Never provided to strategy queries. No forward-fill of the last price.
        """
        sid, clock = symbol(security), timestamp(as_of).isoformat()
        exact = self.current(sid, as_of=clock, role='matching').to_dict()
        if exact['rows']:
            return self._result(exact['rows'], strategy_visible=False, retrospective=False)
        minute = self.market(sid, label=clock, frequency='1m', as_of=clock,
                             role='retrospective_minute').to_dict()
        earlier = [r for r in self._load()['current'] if r['security'] == sid
                   and r['observed_at'][:10] == clock[:10] and r['observed_at'] <= clock]
        if not minute['rows'] or not earlier:
            return self._result([], reason='NO_EXACT_QUOTE_OR_MINUTE_MARK')
        quote = max(earlier, key=lambda r:r['observed_at'])
        bar = minute['rows'][0]
        return self._result([dict(security=sid, source_label_at=clock,
            observed_at=bar['observed_at'], values={**quote['values'], 'last_price':bar['values']['close']},
            source_refs=bar['source_refs'] + quote['source_refs'],
            limits_model='carry earlier observed same-session limits; no new limit observation')],
            retrospective=True, strategy_visible=False)
