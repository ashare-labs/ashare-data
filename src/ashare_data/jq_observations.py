"""Offline, sparse market observations from explicitly pinned local JQ exports.

No acquisition, order import, gap filling, trading engine, or historical PIT claim.
Numbers in the public JSON contract are exact decimal strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException
import hashlib
import json
from pathlib import Path
import re

from .model import DataError, TZ, canonical, day, digest, require, symbol, timestamp
from .storage import identifier, immutable_write

SCHEMA = "ad.jq-sparse-observations.v1"
MAX_BYTES = 8 * 1024 * 1024
MARKET_KINDS = {
    "current_data": "current", "market_bars": "minute",
    "daily_market": "daily", "call_auction_data": "auction",
}
ROLES = {
    "strategy": {"current", "minute", "daily", "auction"},
    "matching": {"current", "minute"},
    "early_order": {"current"},
    "opening_observation": {"current", "auction"},
    "final_valuation": {"daily"},
    "engine_capacity": {"daily"},
}
OHLC = {"open", "high", "low", "close"}
BAR_FIELDS = OHLC | {"volume", "money"}
CURRENT_FIELDS = {"last_price", "day_open", "high_limit", "low_limit", "paused", "is_st"}
BASE_FIELDS = {"case_id", "kind", "run_tag", "seq", "time"}


@dataclass(frozen=True)
class JQObservationInput:
    case_id: str
    security: str
    session_date: str
    events_path: str
    events_sha256: str
    script_path: str
    script_sha256: str


@dataclass(frozen=True)
class JQObservationRead:
    """Detached JSON projection; mutating returned dictionaries cannot change a view."""

    payload_json: str

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def rows(self):
        return tuple(self.to_dict()["rows"])

    @property
    def status(self):
        return self.to_dict()["status"]


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        require(key not in out, "JQ_SCHEMA", "JSON contains duplicate keys")
        out[key] = value
    return out


def _invalid_constant(value):
    raise ValueError("nonfinite JSON number")


def _json(raw, *, exact=False):
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant,
                          **({"parse_float": Decimal} if exact else {}))
    except (ValueError, UnicodeError, RecursionError, DecimalException) as exc:
        raise DataError("JQ_SCHEMA", "invalid bounded JSON") from exc


def _read_file(path, expected=None):
    try:
        p = Path(path).expanduser()
        require(p.is_file() and not p.is_symlink() and p.stat().st_size <= MAX_BYTES,
                "JQ_INPUT", "source must be a regular local file of at most 8 MiB")
        with p.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, "JQ_INPUT", "source exceeds 8 MiB")
    except (OSError, TypeError, ValueError) as exc:
        raise DataError("JQ_INPUT", "cannot read local source") from exc
    if expected is not None:
        require(_sha(raw) == identifier(expected), "JQ_SOURCE_HASH", "pinned source hash changed")
    return raw


def _source_time(value):
    require(type(value) is str, "JQ_SCHEMA", "source clock must be a string")
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise DataError("JQ_SCHEMA", "source clock must be YYYY-MM-DD HH:MM:SS") from exc
    require(dt.strftime("%Y-%m-%d %H:%M:%S") == value, "JQ_SCHEMA", "noncanonical source clock")
    return dt.replace(tzinfo=TZ).isoformat()


def _decimal(value):
    require(type(value) in (int, Decimal), "JQ_SCHEMA", "expected a JSON number")
    v = Decimal(value)
    require(v.is_finite() and 0 <= v < Decimal("1e17") and v.as_tuple().exponent >= -12,
            "JQ_SCHEMA", "invalid market number or more than 12 decimal places")
    return format(v, "f")


def _values(raw, fields):
    require(type(raw) is dict and set(raw) == fields, "JQ_SCHEMA", "unexpected market fields")
    out = {}
    for key, value in raw.items():
        if key in {"paused", "is_st"}:
            require(type(value) is bool or (type(value) in (int, Decimal) and value in (0, 1)),
                    "JQ_SCHEMA", "status flag must be boolean or source numeric 0/1")
            out[key] = bool(value)
        else:
            out["amount" if key == "money" else key] = _decimal(value)
    if OHLC <= fields:
        o, h, low, c = (Decimal(out[k]) for k in ("open", "high", "low", "close"))
        require(0 < low <= min(o, c) <= max(o, c) <= h, "JQ_SCHEMA", "invalid OHLC")
    for key in ("last_price", "day_open", "high_limit", "low_limit", "current"):
        if key in out:
            require(Decimal(out[key]) > 0, "JQ_SCHEMA", "price must be positive")
    if "volume" in out:
        v = Decimal(out["volume"])
        require(v == v.to_integral_value(), "JQ_SCHEMA", "volume must be whole shares")
    if "high_limit" in out:
        require(Decimal(out["low_limit"]) <= Decimal(out["high_limit"]),
                "JQ_SCHEMA", "inverted source limits")
    return out


def _market_rows(e, src, paused):
    kind = MARKET_KINDS[e["kind"]]
    extras = {
        "current": {"data", "symbol", "label"},
        "minute": {"rows", "symbol", "label", "frequency", "fq"},
        "daily": {"rows", "frequency", "fq"},
        "auction": {"rows", "source"},
    }[kind]
    require(set(e) == BASE_FIELDS | extras, "JQ_SCHEMA", "unexpected market event fields")
    observed = _source_time(e["time"])
    require(observed[:10] == src.session_date, "JQ_SCOPE", "observation outside bound session")
    if kind in {"current", "minute"}:
        require(e["symbol"] == src.security and type(e["label"]) is str,
                "JQ_SCOPE", "market event security mismatch")
    if kind in {"minute", "daily"}:
        require(e["fq"] is None and e["frequency"] == ("1m" if kind == "minute" else "daily"),
                "JQ_SCHEMA", "only source raw 1m/daily observations supported")
    if kind == "daily":
        require(observed >= src.session_date + "T15:00:00+08:00", "JQ_VISIBILITY",
                "intraday daily observations cannot stand in for whole-day/final data")
    if kind == "auction":
        require(e["source"] == "get_call_auction", "JQ_SCHEMA", "unsupported auction source")
    rows = [e["data"]] if kind == "current" else e["rows"]
    require(type(rows) is list and 1 <= len(rows) <= 32, "JQ_SCHEMA", "empty or unbounded market event")
    for raw in rows:
        require(type(raw) is dict, "JQ_SCHEMA", "market row must be an object")
        if kind == "current":
            label, values = observed, _values(raw, CURRENT_FIELDS)
        else:
            label_field = "time" if kind == "auction" else "index"
            require(label_field in raw, "JQ_SCHEMA", "missing source label")
            label = _source_time(raw[label_field])
            fields = BAR_FIELDS | ({"paused", "high_limit", "low_limit"} if kind == "daily" else set())
            if kind == "auction":
                require(raw.get("code") == src.security, "JQ_SCOPE", "auction security mismatch")
                fields = {"current", "volume", "money"}
            values = _values({k: v for k, v in raw.items() if k not in {label_field, "code"}}, fields)
            require(set(raw) == fields | {label_field} | ({"code"} if kind == "auction" else set()),
                    "JQ_SCHEMA", "unexpected source row fields")
        require(label <= observed, "JQ_VISIBILITY", "source label is after observation")
        if kind == "minute":
            require(label[17:19] == "00", "JQ_SCHEMA", "minute label must align to a whole minute")
        if kind == "daily":
            require(label[11:19] == "00:00:00", "JQ_SCHEMA", "daily source label must be midnight")
        require(0 <= (datetime.fromisoformat(observed) - datetime.fromisoformat(label)).days <= 7,
                "JQ_SCOPE", "market row outside bounded historical context")
        if kind in {"daily", "auction"}:
            require(label[:10] == src.session_date, "JQ_SCOPE", "row outside bound session")
        paused_at_observation = values.get("paused")
        if kind in {"minute", "auction"} and label[:10] == src.session_date:
            paused_at_observation = paused
        zero_volume = "volume" in values and Decimal(values["volume"]) == 0
        filled = kind in {"minute", "daily"} and paused_at_observation is True and zero_volume
        yield {
            "security": src.security, "kind": kind, "source_label_at": label,
            "available_at": observed, "observed_at": observed, "values": values,
            "source_paused_at_observation": paused_at_observation,
            "quality": "source_filled_suspended" if filled else "source_observed",
            "executable": False if paused_at_observation or zero_volume else None,
            "status_derivation": "same-clock current_data or daily source flag; not per-minute status history",
        }


def _extract(src):
    require(type(src) is JQObservationInput, "JQ_INPUT", "sources require JQObservationInput")
    require(type(src.case_id) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", src.case_id),
            "JQ_INPUT", "invalid case identifier")
    symbol(src.security)
    day(src.session_date)
    raw = _read_file(src.events_path, src.events_sha256)
    _read_file(src.script_path, src.script_sha256)
    lines = raw.splitlines()
    require(1 <= len(lines) <= 1000, "JQ_INPUT", "source must have 1..1000 events")
    events, run_tag = [], None
    for seq, line in enumerate(lines, 1):
        e = _json(line, exact=True)
        require(type(e) is dict and e.get("seq") == seq and type(e.get("seq")) is int
                and e.get("case_id") == src.case_id, "JQ_SCHEMA", "event identity/sequence mismatch")
        if seq == 1:
            require(e.get("kind") == "identity", "JQ_SCHEMA", "first event must identify the run")
            settings = e.get("settings", {})
            require(type(settings) is dict and settings.get("symbol") == src.security
                    and settings.get("start_date") == settings.get("end_date") == src.session_date
                    and settings.get("use_real_price") is True
                    and settings.get("frequency") == "minute", "JQ_SCOPE", "source run scope mismatch")
            run_tag = e.get("run_tag")
            require(type(run_tag) is str and 0 < len(run_tag) <= 200, "JQ_SCHEMA", "missing run tag")
        require(e.get("run_tag") == run_tag and type(e.get("kind")) is str,
                "JQ_SCHEMA", "run tag/kind mismatch")
        if e["kind"] in MARKET_KINDS:
            events.append((e, line))
        # Order, trade, portfolio, decision and reference outcomes never enter the product.
    status = {}
    for e, _ in events:
        if e["kind"] == "current_data":
            value = _values(e.get("data"), CURRENT_FIELDS)["paused"]
            key = e["time"]
            require(key not in status or status[key] == value, "JQ_CONFLICT", "conflicting source status")
            status[key] = value
    records, evidence = [], []
    for e, line in events:
        ref = {"case_id": src.case_id, "seq": e["seq"], "event_sha256": _sha(line),
               "events_sha256": src.events_sha256, "script_sha256": src.script_sha256,
               "run_tag": run_tag, "observed_at": _source_time(e["time"])}
        evidence.append({**ref, "raw_event_json": line.decode("utf-8")})
        for row in _market_rows(e, src, status.get(e["time"])):
            records.append({**row, "source_refs": [ref]})
    require(records, "JQ_INPUT", "no accepted market observations")
    source = {"case_id": src.case_id, "security": src.security, "session_date": src.session_date,
              "events_sha256": src.events_sha256, "script_sha256": src.script_sha256,
              "run_tag": run_tag, "source_event_count": len(lines), "market_event_count": len(events)}
    return source, records, evidence


def _merge(records):
    out = {}
    for row in records:
        key = (row["security"], row["kind"], row["source_label_at"])
        if key not in out:
            out[key] = row
            continue
        previous = out[key]
        compared = ("values", "source_paused_at_observation", "quality", "executable")
        require(all(previous[k] == row[k] for k in compared), "JQ_CONFLICT",
                "inconsistent observations; no last-write-wins", {"key": key})
        previous["available_at"] = previous["observed_at"] = min(previous["observed_at"], row["observed_at"])
        previous["source_refs"].extend(row["source_refs"])
    for row in out.values():
        row["source_refs"].sort(key=lambda r: (r["observed_at"], r["case_id"], r["seq"]))
    return [out[k] for k in sorted(out)]


def _coverage(records):
    out = []
    for security in sorted({r["security"] for r in records}):
        kinds = {}
        for kind in MARKET_KINDS.values():
            rows = [r for r in records if r["security"] == security and r["kind"] == kind]
            kinds[kind] = {"count": len(rows), "source_labels": [r["source_label_at"] for r in rows],
                           "first_observed_at": sorted({r["observed_at"] for r in rows})}
        out.append({"security": security, "coverage": "sparse_observations_only",
                    "complete_minute_session": False, "kinds": kinds})
    return out


def import_observations(store, sources, *, source_kind):
    """Seal only market events from bounded, hash-pinned exports. Never touches sources."""
    require(type(sources) in (list, tuple) and 1 <= len(sources) <= 10,
            "JQ_INPUT", "provide 1..10 pinned source files")
    require(type(source_kind) is str and source_kind in {"local_jq_backtest_observation", "synthetic_fixture"},
            "JQ_INPUT", "explicit source classification required")
    extracted = [_extract(src) for src in sources]
    metadata = sorted([s for s, _, _ in extracted], key=lambda s: s["case_id"])
    require(len({s["case_id"] for s in metadata}) == len(metadata), "JQ_INPUT", "duplicate case")
    records = _merge([r for _, rows, _ in extracted for r in rows])
    evidence = sorted([e for _, _, events in extracted for e in events],
                      key=lambda e: (e["case_id"], e["seq"]))
    body = {
        "schema": SCHEMA, "source_kind": source_kind, "sources": metadata,
        "records": records, "market_evidence": evidence, "coverage": _coverage(records),
        "timezone": "Asia/Shanghai", "price_basis": "source_raw_fq_none_use_real_price",
        "units": {"prices": "CNY/share", "volume": "shares", "amount": "CNY"},
        "unit_evidence": "JQ field convention; pinned scripts request OHLCV/money with fq=None",
        "visibility_basis": "earliest observed JQ simulated context clock, not historical publication time",
        "source_clock_domain": "JQ_backtest_context_current_dt", "physical_capture_at": None,
        "historical_pit": False, "network_used": False, "full_session_coverage": False,
        "live_execution_permission": False, "local_research_use": "conditional_sparse_native_execution",
        "read_roles": {k: sorted(v) for k, v in ROLES.items()},
        "limitations": [
            "Only explicitly observed labels; no forward/back filling or session expansion.",
            "Current-data and auction observations are not minute bars or order-book evidence.",
            "Suspended/zero-volume source fills are non-executable; other quotes are not certified tradable.",
            "Whole-day volume before observation is available only to explicit retrospective engine_capacity.",
            "Source prices/status/limits and simulation visibility are source claims, not historical PIT.",
            "No order, trade, portfolio, target quantity, reference answer or matching logic is imported.",
            "Security lifecycle, full exchange calendar, corporate actions and official rules remain unknown.",
        ],
    }
    dataset_id = digest(body)
    raw = canonical(body)
    require(len(raw) <= MAX_BYTES, "JQ_INPUT", "product exceeds 8 MiB")
    with store._writer():
        root = store.root / "jq_observations"
        root.mkdir(exist_ok=True)
        immutable_write(root / (dataset_id + ".json"), raw)
    return dataset_id


class JQObservationView:
    """Fixed data identity. All reads are local, verified and detached."""

    def __init__(self, store, dataset_id):
        self._root = store.root
        self.dataset_id = identifier(dataset_id)
        self._load()

    def _load(self):
        raw = _read_file(self._root / "jq_observations" / (self.dataset_id + ".json"))
        require(_sha(raw) == self.dataset_id, "JQ_INTEGRITY", "sealed product hash mismatch")
        body = _json(raw)
        require(body.get("schema") == SCHEMA, "JQ_SCHEMA", "unsupported product version")
        return body

    def describe(self):
        body = self._load()
        return {"dataset_id": self.dataset_id, **{k: v for k, v in body.items()
                                                 if k not in {"records", "market_evidence"}},
                "record_count": len(body["records"]), "market_event_count": len(body["market_evidence"])}

    def market_evidence(self):
        """Owner/audit read, not a strategy-clock API; contains market events only."""
        return self._load()["market_evidence"]

    def bootstrap(self, security, session_date):
        """Explicitly conditional one-session initialization, with unknown facts preserved."""
        symbol(security)
        day(session_date)
        body = self._load()
        sources = [s for s in body["sources"] if s["security"] == security and s["session_date"] == session_date]
        require(sources, "JQ_SCOPE", "security/session not bound by this product")
        clocks = sorted({r["observed_at"] for r in body["records"]
                         if r["security"] == security and r["observed_at"][:10] == session_date})
        return {
            "dataset_id": self.dataset_id, "security": security, "session_date": session_date,
            "source_observation_clocks": clocks, "sources": sources,
            "conditional_model": {
                "state": "explicit_research_assumption_not_market_fact",
                "security_type": "CS", "exchange": security.split(".")[1], "currency": "CNY",
                "round_lot": 100, "tick_size": "0.01", "session_dates": [session_date],
                "lifecycle_clocks": [session_date + "T09:00:00+08:00", session_date + "T15:30:00+08:00",
                                     session_date + "T23:59:59+08:00"],
                "scope": "single source-bound session with selected callbacks; no full-session schedule",
            },
            "unknown": {k: None for k in ("listing_date", "delisting_date", "exchange_calendar",
                                          "previous_session", "next_session", "corporate_actions",
                                          "official_price_limit_rule", "historical_eligibility")},
            "historical_pit": False, "live_execution_permission": False,
        }

    def read(self, security, *, kind, as_of, role="strategy", start=None, end=None):
        """Read exact current clocks or bounded label ranges; capacity exposes only volume."""
        symbol(security)
        require(type(role) is str and role in ROLES and type(kind) is str and kind in ROLES[role],
                "JQ_ROLE", "unsupported kind/read-role combination")
        clock = timestamp(as_of).isoformat()
        lower = timestamp(start).isoformat() if start is not None else None
        upper = timestamp(end).isoformat() if end is not None else None
        require(lower is None or upper is None or lower <= upper, "JQ_RANGE", "inverted label range")
        body = self._load()
        require(any(s["security"] == security and s["session_date"] == clock[:10] for s in body["sources"]),
                "JQ_SCOPE", "read clock outside source-bound security/session")
        candidates = [r for r in body["records"] if r["security"] == security and r["kind"] == kind
                      and (lower is None or r["source_label_at"] >= lower)
                      and (upper is None or r["source_label_at"] <= upper)]
        if kind == "current":
            candidates = [r for r in candidates if r["source_label_at"] == clock]
        if kind in {"daily", "auction"}:
            candidates = [r for r in candidates if r["source_label_at"][:10] == clock[:10]]
        retrospective = role == "engine_capacity"
        rows = [r for r in candidates if retrospective or r["available_at"] <= clock]
        for row in rows:
            if retrospective:
                row["values"] = {"volume": row["values"]["volume"]}
                row["strategy_visible"] = False
                row["use_restriction"] = "retrospective single-order capacity model only; not PIT or real capacity"
            else:
                row["source_refs"] = [ref for ref in row["source_refs"] if ref["observed_at"] <= clock]
                row["strategy_visible"] = True
            row["historical_pit"] = False
        reason = None if rows else ("NOT_OBSERVED_AS_OF" if candidates else "NO_SOURCE_OBSERVATION")
        result = {
            "dataset_id": self.dataset_id, "schema": SCHEMA, "security": security, "kind": kind,
            "role": role, "as_of": clock, "status": "available" if rows else "unavailable",
            "reason": reason, "rows": rows, "retrospective": retrospective,
            "coverage": "sparse_observations_only", "complete_minute_session": False,
            "source_kind": body["source_kind"], "price_basis": body["price_basis"], "units": body["units"],
            "visibility_basis": body["visibility_basis"], "historical_pit": False,
            "live_execution_permission": False, "network_used": False,
        }
        return JQObservationRead(canonical(result).decode())
