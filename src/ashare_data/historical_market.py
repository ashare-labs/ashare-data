"""Pinned historical state, security populations and corporate-action evidence.

Companion to DailyExecutionView. No network, broker, price adjustment or account
ledger lives here. Retrospective daily replay is an explicit consumer model.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
from pathlib import Path
import re

from .daily_research import _json, _number, TZ
from .model import DataError, day, require, timestamp
from .storage import identifier

SCHEMA = "ad.historical-market-binding.v1"
DATA_SCHEMA = "ad.historical-market-data.v1"
FIELDS = ("suspended", "is_st", "reference_price", "upper_limit", "lower_limit")
FACT_STATES = {"source_observed", "document_extracted", "derived", "unavailable", "not_applicable"}


def _required_state_known(row, field):
    fact = row[field]
    return fact["value"] is not None or (
        field in FIELDS[3:]
        and row["rule"]["kind"] == "unlimited"
        and fact["status"] == "not_applicable"
    )


def _event_state_claims(symbol, label, rule, events):
    """Yield only claims whose original date/rule bounds cover this key."""
    for event in events:
        if event["security"] != symbol:
            continue
        bounded_rule = (
            rule.get("event_id") == event["event_id"]
            and rule.get("start", label) <= label <= rule.get("end", label)
            and event["effective_date"] <= label
        )
        in_event = (
            label == event["effective_date"]
            or bounded_rule
            or (
                event.get("effective_end_date") is not None
                and event["effective_date"] <= label <= event["effective_end_date"]
            )
        )
        claims = {}
        if in_event and "is_st_from_effective_date" in event:
            claims["is_st"] = event["is_st_from_effective_date"]
        if event["event_type"] == "full_day_suspension":
            if event["suspension_start_date"] <= label <= event["suspension_end_date_inclusive"]:
                claims["suspended"] = True
            elif label == event.get("resume_date"):
                claims["suspended"] = False
        yield event, in_event, claims


def _state_dependency_conflicts(row, population, events):
    """Compare bound claims without selecting a winner or extending event scope."""
    conflicts = []

    def compare(field, left, right, left_origin, right_origin, left_refs, right_refs):
        if left is not None and right is not None and left != right:
            conflicts.append(
                {
                    "field": field,
                    "claims": [
                        {"origin": left_origin, "value": left, "source_refs": left_refs},
                        {"origin": right_origin, "value": right, "source_refs": right_refs},
                    ],
                }
            )

    rule = row["rule"]
    if rule["kind"] == "ordinary" and rule["depends_on_st"]:
        compare(
            "is_st",
            row["is_st"]["value"],
            rule["ratio"] == "0.05",
            "daily_state",
            "declared_rule",
            row["is_st"]["source_refs"],
            rule["source_refs"],
        )
    if population:
        for field in FIELDS[:2]:
            compare(
                field,
                row[field]["value"],
                population[field]["value"],
                "daily_state",
                "dated_population",
                row[field]["source_refs"],
                population[field]["source_refs"],
            )
    for event, in_event, claims in _event_state_claims(row["security"], row["date"], rule, events):
        for field, value in claims.items():
            compare(
                field,
                row[field]["value"],
                value,
                "daily_state",
                event["event_id"],
                row[field]["source_refs"],
                event["source_refs"],
            )
        if in_event and rule["kind"] == "ordinary":
            compare(
                "rule.ratio",
                rule["ratio"],
                event.get("daily_price_limit_ratio"),
                "declared_rule",
                event["event_id"],
                rule["source_refs"],
                event["source_refs"],
            )
    return conflicts


def _known_filter_flag(fact):
    return type(fact.get("value")) is bool and fact.get("status") in {
        "source_observed",
        "document_extracted",
        "derived",
    }


def _population_filter_conflicts(row, label, state, events):
    """Compare flags independently of whether a usable daily-state row exists.

    Missing claims do not vote for either boolean. In particular, a null daily
    fact cannot hide a contradiction between the population and a dated event.
    """
    claims = {field: [] for field in FIELDS[:2]}

    def add(field, value, origin, refs):
        claims[field].append(dict(origin=origin, value=value, source_refs=refs))

    for field in claims:
        if _known_filter_flag(row[field]):
            add(field, row[field]["value"], "dated_population", row[field]["source_refs"])
        if state and _known_filter_flag(state[field]):
            add(field, state[field]["value"], "daily_state", state[field]["source_refs"])
    rule = state["rule"] if state else {}
    if rule.get("kind") == "ordinary" and rule["depends_on_st"]:
        add("is_st", rule["ratio"] == "0.05", "declared_rule", rule["source_refs"])
    for event, _, flags in _event_state_claims(row["security"], label, rule, events):
        for field, value in flags.items():
            add(field, value, event["event_id"], event["source_refs"])
    return [
        dict(field=field, claims=[left, right])
        for field, observations in claims.items()
        for index, left in enumerate(observations)
        for right in observations[index + 1 :]
        if left["value"] != right["value"]
    ]


def security(value):
    require(
        isinstance(value, str) and re.fullmatch(r"\d{6}\.(XSHG|XSHE|XBSE)", value),
        "INVALID_SYMBOL",
        "Expected six digits and XSHG/XSHE/XBSE",
    )
    return value


def ordinary_limits(reference, ratio):
    """Cent tick model, independently of any archive's claimed daily bands."""
    ref = _number(reference, positive=True)
    require(
        ratio in ("0.05", "0.10", "0.20", "0.30") and ref < Decimal("1e12"),
        "HISTORICAL_LIMIT_MODEL",
        "Unsupported ordinary ratio/reference",
    )
    with localcontext() as context:
        context.prec = 32
        tick = Decimal("0.01")
        require(ref == ref.quantize(tick), "HISTORICAL_LIMIT_MODEL", "Cent reference required")
        upper = max((ref * (1 + Decimal(ratio))).quantize(tick, ROUND_HALF_UP), ref + tick)
        lower = min((ref * (1 - Decimal(ratio))).quantize(tick, ROUND_HALF_UP), ref - tick)
        require(lower > 0, "HISTORICAL_LIMIT_MODEL", "Nonpositive lower band unsupported")
    return str(upper), str(lower)


class HistoricalMarketView:
    """Immutable bounded evidence; every public return is a detached copy.

    A source-complete historical population is not a certified whole market.
    Publication dates, effective dates and this acquisition's clock stay separate.
    """

    def __init__(self, binding_path, *, expected_binding_sha256):
        try:
            self._load(binding_path, expected_binding_sha256)
        except (OSError, KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            raise DataError("HISTORICAL_SCHEMA", "Malformed historical evidence package") from exc

    def _load(self, binding_path, expected):
        self.path = Path(binding_path).resolve()
        self.binding_sha256 = identifier(expected)
        raw = self.path.read_bytes()
        require(
            len(raw) <= 2 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == expected,
            "HISTORICAL_INTEGRITY",
            "Binding digest mismatch",
        )
        self._binding = b = _json(raw)
        require(
            b.get("schema") == SCHEMA
            and b.get("historical_pit") is False
            and b.get("execution_permission") is False,
            "HISTORICAL_SCHEMA",
            "Unsupported schema or promoted execution claim",
        )
        require(
            isinstance(b["files"], list) and 1 <= len(b["files"]) <= 1000,
            "HISTORICAL_SCHEMA",
            "Invalid evidence inventory",
        )
        self._contents, size = {}, 0
        for entry in b["files"]:
            relative = Path(entry["path"])
            path = self.path.parent / relative
            require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and relative.as_posix() == entry["path"]
                and not path.is_symlink()
                and path.resolve().is_relative_to(self.path.parent)
                and path.is_file()
                and entry["path"] not in self._contents,
                "HISTORICAL_FILE",
                "Unsafe, duplicate or missing evidence path",
            )
            require(
                path.stat().st_size <= 16 * 1024 * 1024,
                "HISTORICAL_FILE",
                "Evidence exceeds 16 MiB",
            )
            content = path.read_bytes()
            size += len(content)
            require(size <= 64 * 1024 * 1024, "HISTORICAL_FILE", "Package exceeds 64 MiB")
            require(
                len(content) == entry["bytes"]
                and hashlib.sha256(content).hexdigest() == identifier(entry["sha256"]),
                "HISTORICAL_INTEGRITY",
                "Evidence digest mismatch",
                {"path": entry["path"]},
            )
            self._contents[entry["path"]] = content
        self._data = _json(self._contents[b["data"]])
        self._received = timestamp(b["received_at"])
        receipts = 0
        for name, raw in self._contents.items():
            if not name.endswith(".receipt.json"):
                continue
            receipt = _json(raw)
            source = name.removesuffix(".receipt.json")
            require(
                source in self._contents
                and receipt["status"] == 200
                and receipt["bytes"] == len(self._contents[source])
                and receipt["sha256"] == hashlib.sha256(self._contents[source]).hexdigest(),
                "HISTORICAL_INTEGRITY",
                "Receipt does not bind its raw source",
            )
            require(
                timestamp(receipt["captured_start"])
                <= timestamp(receipt["captured_end"])
                <= self._received,
                "HISTORICAL_CLOCK",
                "Invalid package receipt clock",
            )
            receipts += 1
        require(receipts > 0, "HISTORICAL_SOURCE", "Package requires source receipts")
        self._validate()

    def _refs(self, refs):
        require(
            isinstance(refs, list)
            and refs
            and len(refs) == len(set(refs))
            and set(refs) <= set(self._contents),
            "HISTORICAL_SOURCE",
            "Unbound evidence reference",
        )

    def _fact(self, fact, kind, *, allow_not_applicable=False):
        require(
            fact["status"] in FACT_STATES
            and (allow_not_applicable or fact["status"] != "not_applicable")
            and fact.get("historical_available_at") is None,
            "HISTORICAL_SCHEMA",
            "Invalid fact or unproved historical clock",
        )
        self._refs(fact["source_refs"])
        require(
            (fact["value"] is None) == (fact["status"] in {"unavailable", "not_applicable"}),
            "HISTORICAL_VALUE",
            "Unknown/not-applicable must remain null",
        )
        require(
            isinstance(fact["reason"], str) and bool(fact["reason"]),
            "HISTORICAL_VALUE",
            "Missing fact explanation",
        )
        value = fact["value"]
        if value is not None:
            if kind == "flag":
                require(type(value) is bool, "HISTORICAL_VALUE", "Expected boolean")
            elif kind == "price":
                _number(value, positive=True)
            elif kind == "date":
                day(value)

    def _validate(self):
        d = self._data
        require(
            d["schema"] == DATA_SCHEMA
            and isinstance(d["states"], list)
            and len(d["states"]) <= 10000,
            "HISTORICAL_SCHEMA",
            "Invalid state inventory",
        )
        self._instruments, self._states, self._universes = {}, {}, {}
        for row in d["instruments"]:
            key = security(row["security"])
            require(key not in self._instruments, "HISTORICAL_SCHEMA", "Duplicate instrument")
            self._refs(row["source_refs"])
            for field in ("initial_listing_date", "delisting_date"):
                self._fact(row[field], "date")
            start, end = row["initial_listing_date"]["value"], row["delisting_date"]["value"]
            require(
                not start or not end or start <= end,
                "HISTORICAL_VALUE",
                "Delisting predates listing",
            )
            self._instruments[key] = row
        require(
            1 <= len(self._instruments) <= 100,
            "HISTORICAL_SCHEMA",
            "Bounded instrument set required",
        )
        for row in d["states"]:
            key = security(row["security"]), day(row["date"])
            require(
                key[0] in self._instruments and key not in self._states,
                "HISTORICAL_SCHEMA",
                "Duplicate/foreign state",
            )
            for field in FIELDS:
                self._fact(
                    row[field],
                    "flag" if field in FIELDS[:2] else "price",
                    allow_not_applicable=field in FIELDS[3:] and row["rule"]["kind"] == "unlimited",
                )
            self._refs(row["rule"]["source_refs"])
            rule = row["rule"]
            require(
                rule["kind"] in {"ordinary", "unlimited", "unknown"},
                "HISTORICAL_SCHEMA",
                "Unknown limit regime",
            )
            require(
                type(rule["depends_on_st"]) is bool, "HISTORICAL_SCHEMA", "Invalid ST dependency"
            )
            claim = row["source_claimed_limits"]
            self._refs(claim["source_refs"])
            require(
                claim["execution_usable"] is False
                and claim["validation"]
                in {
                    "source_missing",
                    "model_unavailable",
                    "conflict_with_unlimited_rule",
                    "model_agreement",
                    "conflict_with_rule_model",
                    "conflict_with_dependencies",
                },
                "HISTORICAL_SCHEMA",
                "Unqualified source limit claim",
            )
            require(
                row["intraday_halts_verified"] is False,
                "HISTORICAL_SCHEMA",
                "Daily source cannot certify intraday halts",
            )
            self._states[key] = row
        for population in d["universes"]:
            label = day(population["date"])
            require(label not in self._universes, "HISTORICAL_SCHEMA", "Duplicate population date")
            self._refs(population["source_refs"])
            require(
                type(population["source_row_count"]) is int
                and 0 <= population["source_row_count"] <= 10000
                and population["source_query_complete"] is True
                and population["market_complete"] is False
                and len(population["rows"]) == population["source_row_count"],
                "HISTORICAL_COVERAGE",
                "Incomplete or promoted population",
            )
            seen = set()
            for row in population["rows"]:
                key = security(row["security"])
                require(key not in seen, "HISTORICAL_SCHEMA", "Duplicate population member")
                seen.add(key)
                for field in FIELDS[:2]:
                    self._fact(row[field], "flag")
            self._universes[label] = population
        action_ids = set()
        for event in d.get("security_events", []):
            require(
                event["event_id"] not in action_ids
                and event["security"] in self._instruments
                and event["historical_available_at"] is None,
                "HISTORICAL_SCHEMA",
                "Duplicate/foreign event or promoted historical clock",
            )
            action_ids.add(event["event_id"])
            day(event["effective_date"])
            day(event["publication_date"])
            self._refs(event["source_refs"])
            if "is_st_from_effective_date" in event:
                require(
                    type(event["is_st_from_effective_date"]) is bool,
                    "HISTORICAL_SCHEMA",
                    "Invalid event ST claim",
                )
            if event.get("daily_price_limit_ratio") is not None:
                require(
                    event["daily_price_limit_ratio"] in {"0.05", "0.10", "0.20", "0.30"},
                    "HISTORICAL_SCHEMA",
                    "Invalid event ratio",
                )
            if event.get("effective_end_date") is not None:
                require(
                    day(event["effective_end_date"]) >= event["effective_date"],
                    "HISTORICAL_SCHEMA",
                    "Invalid event range",
                )
            if event["event_type"] == "full_day_suspension":
                require(
                    day(event["suspension_start_date"])
                    <= day(event["suspension_end_date_inclusive"]),
                    "HISTORICAL_SCHEMA",
                    "Invalid suspension range",
                )
                if event.get("resume_date"):
                    day(event["resume_date"])
        populations = {
            (p["date"], r["security"]): r for p in self._universes.values() for r in p["rows"]
        }
        events = d.get("security_events", [])
        for row in self._states.values():
            rule = row["rule"]
            if rule.get("st_policy") == "fixed_event":
                applicable = [
                    e
                    for e in events
                    if e["event_id"] == rule.get("event_id")
                    and e["security"] == row["security"]
                    and e.get("is_st_from_effective_date") is not None
                    and e.get("daily_price_limit_ratio") is not None
                    and e["effective_date"] <= row["date"]
                ]
                require(
                    len(applicable) == 1 and day(rule["start"]) <= row["date"] <= day(rule["end"]),
                    "HISTORICAL_SOURCE",
                    "Fixed event rule lacks bounded event identity",
                )
            conflicts = _state_dependency_conflicts(
                row, populations.get((row["date"], row["security"])), events
            )
            require(
                row.get("dependency_conflicts", conflicts) == conflicts,
                "HISTORICAL_DEPENDENCY_CONFLICT",
                "Dependency conflict inventory differs",
            )
            row["dependency_conflicts"] = conflicts
            self._validate_limits(row)
        for event in d["actions"]:
            require(
                event["event_id"] not in action_ids and event["security"] in self._instruments,
                "HISTORICAL_SCHEMA",
                "Duplicate/foreign corporate action",
            )
            action_ids.add(event["event_id"])
            self._refs(event["source_refs"])
            require(
                event["action_type"] == "distribution"
                and event["currency"] == "CNY"
                and event["unit"] == "per_old_eligible_share"
                and event["historical_available_at"] is None
                and event["cash_available_at"] is None
                and event["share_credit_at"] is None,
                "HISTORICAL_ACTION",
                "Unsupported unit/type or unproved intraday clock",
            )
            require(
                day(event["record_date"]) < day(event["ex_date"]),
                "HISTORICAL_ACTION",
                "Invalid record/ex dates",
            )
            day(event["publication_date"])
            for f in ("payment_date", "new_share_trading_date", "share_credit_date"):
                if event[f] is not None:
                    require(
                        day(event[f]) >= event["ex_date"],
                        "HISTORICAL_ACTION",
                        "Action date precedes ex-date",
                    )
            for f in ("cash_per_share", "bonus_per_share", "transfer_per_share"):
                if event[f] is not None:
                    _number(event[f])
            if event["ex_price_cash_deduction"] is not None:
                _number(event["ex_price_cash_deduction"])
            require(
                all(
                    isinstance(event[f], str) and event[f]
                    for f in ("payment_date_scope", "share_credit_scope", "new_share_trading_scope")
                ),
                "HISTORICAL_ACTION",
                "Action dates require holder/distribution scope",
            )

    def _validate_limits(self, row):
        rule = row["rule"]
        if rule["kind"] == "ordinary":
            require(
                rule["ratio"] in {"0.05", "0.10", "0.20", "0.30"},
                "HISTORICAL_LIMIT_MODEL",
                "Unsupported rule",
            )
            if rule["depends_on_st"]:
                require(
                    row["is_st"]["value"] is not None and rule["ratio"] in {"0.05", "0.10"},
                    "HISTORICAL_LIMIT_MODEL",
                    "ST rule dependency unproved",
                )
        if row["dependency_conflicts"]:
            require(
                all(row[f]["status"] == "unavailable" for f in FIELDS[3:]),
                "HISTORICAL_DEPENDENCY_CONFLICT",
                "Conflicting inputs cannot produce usable limits",
            )
        elif rule["kind"] == "ordinary" and row["reference_price"]["value"] is not None:
            expected = ordinary_limits(row["reference_price"]["value"], rule["ratio"])
            require(
                tuple(row[f]["value"] for f in FIELDS[3:]) == expected
                and all(row[f]["status"] == "derived" for f in FIELDS[3:]),
                "HISTORICAL_LIMIT_MODEL",
                "Limit model does not reproduce",
            )
        elif rule["kind"] == "unlimited":
            require(
                all(row[f]["status"] == "not_applicable" for f in FIELDS[3:]),
                "HISTORICAL_LIMIT_MODEL",
                "Unlimited is not zero or unknown",
            )
        else:
            require(
                all(row[f]["status"] == "unavailable" for f in FIELDS[3:]),
                "HISTORICAL_LIMIT_MODEL",
                "Missing reference/rule cannot produce a band",
            )

    def _visible(self, visibility, knowledge_at):
        require(
            visibility in {"posthoc", "received", "verified"},
            "HISTORICAL_VISIBILITY",
            "Unknown visibility mode",
        )
        require(
            visibility != "verified",
            "PIT_UNAVAILABLE",
            "Historical first availability and revision chains are not established",
        )
        if visibility == "posthoc":
            require(
                knowledge_at is None, "HISTORICAL_CLOCK", "Choose received for a knowledge clock"
            )
        else:
            require(
                timestamp(knowledge_at) >= self._received,
                "RECEIPT_NOT_VISIBLE",
                "Knowledge clock precedes package receipt completion",
            )

    def descriptor(self):
        return deepcopy(
            {
                "binding_sha256": self.binding_sha256,
                "schema": SCHEMA,
                "scope": self._data["scope"],
                "state_rows": len(self._states),
                "securities": sorted(self._instruments),
                "universe_dates": sorted(self._universes),
                "action_events": len(self._data["actions"]),
                "received_at": self._received.isoformat(),
                "historical_pit": False,
                "execution_permission": False,
                "network_used": False,
            }
        )

    def lineage(self):
        return deepcopy(self._binding)

    def coverage(self):
        unknown = Counter(
            f for row in self._states.values() for f in FIELDS if row[f]["status"] == "unavailable"
        )
        return deepcopy(
            {
                "unknown_fields": dict(unknown),
                "state_rows": len(self._states),
                "source_limit_validation": dict(
                    Counter(
                        row["source_claimed_limits"]["validation"] for row in self._states.values()
                    )
                ),
                "universes": [
                    {
                        k: p[k]
                        for k in (
                            "date",
                            "source_row_count",
                            "source_query_complete",
                            "market_complete",
                        )
                    }
                    for p in self._universes.values()
                ],
                "actions_complete": False,
                "historical_pit": False,
            }
        )

    def state(self, symbol, on_date, *, visibility="posthoc", knowledge_at=None, require_fields=()):
        self._visible(visibility, knowledge_at)
        key = security(symbol), day(on_date)
        require(key in self._states, "HISTORICAL_SCOPE", "Security-day outside fixed sample")
        require(
            isinstance(require_fields, (list, tuple)) and set(require_fields) <= set(FIELDS),
            "INVALID_ARGUMENT",
            "Unknown required state field",
        )
        row = self._states[key]
        require(
            not require_fields or not row["dependency_conflicts"],
            "HISTORICAL_DEPENDENCY_CONFLICT",
            "Required state has conflicting source dependencies",
            {"conflicts": row["dependency_conflicts"]},
        )
        missing = [f for f in require_fields if not _required_state_known(row, f)]
        require(not missing, "HISTORICAL_UNKNOWN", "Required state is unknown", {"fields": missing})
        return deepcopy(row)

    def instruments(self, symbols, *, on_date, visibility="posthoc", knowledge_at=None):
        self._visible(visibility, knowledge_at)
        day(on_date)
        require(
            isinstance(symbols, (list, tuple))
            and 0 < len(symbols) <= 100
            and len(set(symbols)) == len(symbols),
            "INVALID_ARGUMENT",
            "Bounded unique symbols required",
        )
        result = []
        for code in symbols:
            require(security(code) in self._instruments, "HISTORICAL_SCOPE", "Unknown instrument")
            row = deepcopy(self._instruments[code])
            start, end = row["initial_listing_date"]["value"], row["delisting_date"]["value"]
            row["on_date"] = on_date
            row["lifecycle_state"] = (
                "not_yet_listed"
                if start and on_date < start
                else "delisted"
                if end and on_date >= end
                else "unknown"
            )
            row["continuous_listing_verified"] = False
            result.append(row)
        return result

    def universe(
        self,
        on_date,
        *,
        candidates=None,
        exclude_st=False,
        exclude_suspended=False,
        visibility="posthoc",
        knowledge_at=None,
        require_complete_market=False,
    ):
        self._visible(visibility, knowledge_at)
        label = day(on_date)
        require(
            all(type(x) is bool for x in (exclude_st, exclude_suspended, require_complete_market)),
            "INVALID_ARGUMENT",
            "Filters must be boolean",
        )
        require(
            not require_complete_market,
            "HISTORICAL_COVERAGE",
            "Archive query completeness does not certify full market coverage",
        )
        require(
            label in self._universes, "HISTORICAL_SCOPE", "Population date outside fixed sample"
        )
        population = self._universes[label]
        by_symbol = {r["security"]: r for r in population["rows"]}
        if candidates is not None:
            require(
                isinstance(candidates, (list, tuple))
                and len(candidates) <= 10000
                and len(set(candidates)) == len(candidates),
                "INVALID_ARGUMENT",
                "Invalid candidates",
            )
            for code in candidates:
                security(code)
        selected, excluded, unknown, conflicts = [], [], [], []
        required = [
            f for f, enabled in (("is_st", exclude_st), ("suspended", exclude_suspended)) if enabled
        ]
        for code in sorted(by_symbol) if candidates is None else candidates:
            row = by_symbol.get(code)
            if row is None:
                unknown.append({"security": code, "reason": "absent_from_captured_population"})
                continue
            evidence = _population_filter_conflicts(
                row, label, self._states.get((code, label)), self._data.get("security_events", [])
            )
            if evidence:
                conflicts.append({"security": code, "conflicts": evidence})
            active = [c for c in evidence if c["field"] in required]
            missing = [f for f in required if not _known_filter_flag(row[f])]
            if active:
                unknown.append(
                    {
                        "security": code,
                        "reason": "filter_dependency_conflict",
                        "fields": sorted({c["field"] for c in active}),
                        "conflicts": active,
                        "source_row": row,
                    }
                )
            elif missing:
                unknown.append(
                    {
                        "security": code,
                        "reason": "filter_field_unknown",
                        "fields": missing,
                        "source_row": row,
                    }
                )
            elif any(row[f]["value"] for f in required):
                excluded.append(row)
            else:
                selected.append(row)
        return deepcopy(
            {
                "date": label,
                "rows": selected,
                "excluded": excluded,
                "unknown": unknown,
                "filter_fields": required,
                "dependency_conflicts": conflicts,
                "source_row_count": population["source_row_count"],
                "source_query_complete": True,
                "market_complete": False,
                "source_refs": population["source_refs"],
                "coverage_state": "source_population_observed" if by_symbol else "unavailable",
                "survivor_filter_applied": False,
                "historical_pit": False,
            }
        )

    def corporate_actions(
        self,
        symbols,
        *,
        start,
        end,
        visibility="posthoc",
        knowledge_at=None,
        require_cash_time=False,
        require_share_credit_time=False,
    ):
        self._visible(visibility, knowledge_at)
        require(day(start) <= day(end), "INVALID_ARGUMENT", "Reversed action range")
        require(
            isinstance(symbols, (list, tuple))
            and 0 < len(symbols) <= 100
            and len(set(symbols)) == len(symbols)
            and set(symbols) <= set(self._instruments),
            "HISTORICAL_SCOPE",
            "Unknown action security",
        )
        require(
            type(require_cash_time) is bool and type(require_share_credit_time) is bool,
            "INVALID_ARGUMENT",
            "Time requirements must be boolean",
        )
        rows = [
            a
            for a in self._data["actions"]
            if a["security"] in symbols and start <= a["ex_date"] <= end
        ]
        issues = []
        for row in rows:
            if require_cash_time and (
                row["cash_per_share"] is None or _number(row["cash_per_share"]) > 0
            ):
                issues.append({"event_id": row["event_id"], "field": "cash_available_at"})
            if require_share_credit_time and any(
                row[f] is None or _number(row[f]) > 0
                for f in ("bonus_per_share", "transfer_per_share")
            ):
                issues.append({"event_id": row["event_id"], "field": "share_credit_at"})
        require(
            not issues,
            "HISTORICAL_ACTION_TIME_UNKNOWN",
            "Required intraday action clock unknown",
            {"issues": issues},
        )
        return deepcopy(
            {
                "rows": rows,
                "coverage": "known_events_only",
                "complete": False,
                "absence_proved": False,
                "historical_pit": False,
                "adjustment_basis": "raw_entitlements_no_price_adjustment",
                "tax_model": None,
            }
        )

    def security_events(self, symbols, *, start, end, visibility="posthoc", knowledge_at=None):
        self._visible(visibility, knowledge_at)
        require(
            day(start) <= day(end)
            and isinstance(symbols, (list, tuple))
            and 0 < len(symbols) <= 100
            and len(set(symbols)) == len(symbols)
            and set(symbols) <= set(self._instruments),
            "HISTORICAL_SCOPE",
            "Invalid event scope",
        )
        return deepcopy(
            {
                "rows": [
                    e
                    for e in self._data.get("security_events", [])
                    if e["security"] in symbols and start <= e["effective_date"] <= end
                ],
                "complete": False,
                "absence_proved": False,
                "historical_pit": False,
            }
        )

    def order_state(
        self,
        symbol,
        *,
        as_of,
        side,
        purpose="retrospective_simulation",
        accept_daily_replay=False,
        accept_limit_model=False,
    ):
        require(
            purpose in {"retrospective_simulation", "historical_pit", "live_execution"},
            "INVALID_ARGUMENT",
            "Unsupported order purpose",
        )
        require(
            purpose == "retrospective_simulation",
            "PIT_UNAVAILABLE",
            "Retrospective evidence cannot serve historical PIT or live execution",
        )
        require(
            side in {"BUY", "SELL"}
            and type(accept_daily_replay) is bool
            and type(accept_limit_model) is bool,
            "INVALID_ARGUMENT",
            "Invalid order context",
        )
        clock = timestamp(as_of)
        row = self.state(symbol, clock.date().isoformat())
        issues = []
        if not accept_daily_replay:
            issues.append("DAILY_REPLAY_NOT_ACCEPTED")
        if not accept_limit_model:
            issues.append("LIMIT_MODEL_NOT_ACCEPTED")
        if clock < datetime.combine(clock.date(), time(9), TZ):
            issues.append("BEFORE_REPLAY_CLOCK")
        fields = ["suspended", "reference_price", "upper_limit" if side == "BUY" else "lower_limit"]
        if row["rule"].get("depends_on_st"):
            fields.append("is_st")
        issues.extend("UNKNOWN_" + f.upper() for f in fields if not _required_state_known(row, f))
        if row["dependency_conflicts"]:
            issues.append("STATE_DEPENDENCY_CONFLICT")
        if row["suspended"]["value"]:
            issues.append("DAILY_SUSPENDED")
        lifecycle = self.instruments([symbol], on_date=row["date"])[0]["lifecycle_state"]
        if lifecycle in {"not_yet_listed", "delisted"}:
            issues.append(lifecycle.upper())
        return {
            "state": row,
            "ready": not issues,
            "issues": issues,
            "admission_scope": "one_order_daily_state_only",
            "required_fields": fields,
            "purpose": purpose,
            "as_of": clock.isoformat(),
            "side": side,
            "intraday_halts_verified": False,
            "execution_permission": False,
            "account_actions_checked": False,
            "historical_pit": False,
        }
