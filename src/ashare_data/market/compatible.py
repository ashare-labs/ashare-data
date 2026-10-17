"""Opt-in, read-only composition of normalized local daily observations.

Use exact Decimal agreement: differing display precision never licenses a
splice. A lower-priority whole observation remains visible even when rejected.
"""

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import itertools
from pathlib import Path

from ashare_data.market.client import _security
from ashare_data.storage.local_daily import BAR_FIELDS, digest, parse_day
from ashare_data.reference.status_evidence import query_status_evidence

PRIORITY = ("baostock", "eastmoney", "sina", "sohu", "tencent", "baostock_alias")
PRICES = ("open", "high", "low", "close")
NUMERIC = (*PRICES, "volume", "amount", "preclose")
EXTRA_COLUMNS = (
    "base_source",
    "base_observation_id",
    "field_sources",
    "filled_fields",
    "observations",
    "conflicts",
    "trade_state",
    "status_evidence",
    "completion",
    "has_conflicts",
)


def _number(value):
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("nonfinite value")
    return number


def _positive(row, key):
    try:
        return row.get(key) is not None and _number(row[key]) > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _identity_rejection(row):
    if row["source"] not in PRIORITY:
        return "unknown_raw_units_or_adjustment"
    if row["source"] == "baostock_alias" and row.get("origin") != "verified_alias":
        return "alias_requires_verified_capture"
    return None


def _invalid(row):
    identity_error = _identity_rejection(row)
    if identity_error:
        return identity_error
    try:
        values = {k: _number(row[k]) for k in NUMERIC if row.get(k) is not None}
        if any(values.get(k, Decimal(0)) <= 0 for k in PRICES):
            return "missing_or_invalid_ohlc"
        if not (
            values["low"]
            <= min(values["open"], values["close"])
            <= max(values["open"], values["close"])
            <= values["high"]
        ):
            return "invalid_ohlc_order"
        if any(values[k] < 0 for k in ("volume", "amount", "preclose") if k in values):
            return "negative_value"
        if row.get("tradestatus") not in (None, "0", "1") or row.get("is_st") not in (
            None,
            "0",
            "1",
        ):
            return "invalid_status_flag"
        # Detect incompatible units/turnover before fields enter a composed bar.
        # One cent accommodates rounded displayed prices; values are never changed.
        if "volume" in values and "amount" in values:
            v, a = values["volume"], values["amount"]
            if (v == 0 and a != 0) or (
                v > 0
                and not (values["low"] - Decimal("0.01")) * v
                <= a
                <= (values["high"] + Decimal("0.01")) * v
            ):
                return "turnover_outside_price_volume_bounds"
    except (InvalidOperation, TypeError, ValueError):
        return "invalid_decimal"
    return None


def _differences(left, right):
    return [
        k
        for k in BAR_FIELDS
        if left.get(k) is not None
        and right.get(k) is not None
        and ((_number(left[k]) != _number(right[k])) if k in NUMERIC else left[k] != right[k])
    ]


def _lineage(row):
    keys = (
        "observation_id",
        "source",
        "provider_symbol",
        "response_id",
        "row_hash",
        "raw_sha256",
        "observed_at",
        "capture_id",
        "capture",
        "code_event_id",
        "issuer_notice_sha256",
        "valid_from",
        "valid_to_exclusive",
    )
    return {k: row[k] for k in keys if row.get(k) is not None}


def _status(rows, evidence):
    halted = any(r.get("tradestatus") == "0" for r in rows)
    active = any(
        r.get("tradestatus") == "1" or _positive(r, "volume") or _positive(r, "amount")
        for r in rows
    )
    if (halted or evidence) and active:
        return "status_conflict"
    if evidence:
        if any(e["evidence_kind"] == "issuer_final_trading_day_inference" for e in evidence):
            return "inferred_nontrading"
        return "evidenced_nontrading"
    if halted:
        return "source_suspended"
    if any(_positive(r, "volume") for r in rows):
        return "source_activity"
    return "activity_unknown"


def _query(store, symbol, start, end, source, captures):
    import pandas as pd

    priority = list(PRIORITY)
    if source != "auto":
        priority.remove(source)
        priority.insert(0, source)
    calendar = [
        r[0]
        for r in store.db.execute(
            "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
        )
    ]
    expected = set(store.expected(symbol, start, end, calendar))
    evidence = query_status_evidence(store, symbol)
    native = [
        dict(r)
        for r in store.db.execute(
            "SELECT b.*,r.sha256 AS raw_sha256 FROM bars b LEFT JOIN responses r "
            "ON r.id=b.response_id WHERE b.symbol=? AND b.date BETWEEN ? AND ? ORDER BY b.date,b.source",
            (symbol, start, end),
        )
    ]
    outside = Counter(
        "outside_calendar" if r["date"] not in calendar else "outside_listing"
        for r in native
        if r["date"] not in expected
    )
    candidates = [
        dict(r, provider_symbol=symbol, origin="native") for r in native if r["date"] in expected
    ]
    alias_metadata = []
    for capture in captures:
        security = symbol[2:] + (".XSHG" if symbol.startswith("sh") else ".XSHE")
        frame = store.get_alias_price(security, capture, start, end)
        alias_metadata.append(frame.attrs)
        for row in frame.to_dict("records"):
            candidates.append(
                dict(
                    row,
                    origin="verified_alias",
                    capture=frame.attrs["capture"],
                    valid_from=frame.attrs["valid_from"],
                    valid_to_exclusive=frame.attrs["valid_to_exclusive"],
                )
            )
    by_day = defaultdict(list)
    seen = set()
    for row in candidates:
        row["observation_id"] = digest(row)
        if row["observation_id"] in seen:
            continue
        seen.add(row["observation_id"])
        row["rejection"] = _invalid(row)
        by_day[row["date"]].append(row)
    result, unfilled, field_counts = [], [], Counter()
    for day in sorted(expected):
        observations = sorted(
            by_day[day],
            key=lambda r: (
                priority.index(r["source"]) if r["source"] in priority else len(priority),
                r["observation_id"],
            ),
        )
        facts = [e for e in evidence if e["start"] <= day < e["end_exclusive"]]
        # Unknown units/adjustment and unverified aliases cannot supply status.
        # A known source's explicit halt still matters if its OHLC is invalid.
        status_rows = [r for r in observations if _identity_rejection(r) is None]
        state = _status(status_rows, facts)
        valid = [r for r in observations if r["rejection"] is None]
        conflicts = [
            dict(
                kind="invalid_observation",
                observation_id=r["observation_id"],
                reason=r["rejection"],
            )
            for r in observations
            if r["rejection"]
        ]
        for left, right in itertools.combinations(valid, 2):
            for field in _differences(left, right):
                conflicts.append(
                    dict(
                        kind="value_conflict",
                        field=field,
                        left=_lineage(left),
                        left_value=left[field],
                        right=_lineage(right),
                        right_value=right[field],
                    )
                )
        if state == "status_conflict":
            conflicts.append(
                dict(
                    kind="status_conflict",
                    observation_ids=[r["observation_id"] for r in status_rows],
                    evidence=facts,
                )
            )
        # Alias suspension placeholders explain a missing native day, but do not
        # manufacture native coverage or a historical trading row.
        eligible = [
            r for r in valid if r["source"] != "baostock_alias" or state == "source_activity"
        ]
        if not eligible:
            unfilled.append(
                dict(
                    date=day,
                    trade_state=state,
                    status_evidence=facts,
                    observations=observations,
                    conflicts=conflicts,
                )
            )
            continue
        base = eligible[0]
        out = {
            k: base.get(k)
            for k in (
                "symbol",
                "date",
                "source",
                *BAR_FIELDS,
                "row_hash",
                "response_id",
                "observed_at",
            )
        }
        field_sources = {k: _lineage(base) if base.get(k) is not None else None for k in BAR_FIELDS}
        filled = []
        if state == "source_activity":
            for field in ("volume", "amount", "preclose", "tradestatus", "is_st"):
                if out[field] is not None:
                    continue
                donors = [
                    r
                    for r in valid
                    if r is not base
                    and r.get(field) is not None
                    and not _differences(out, r)
                    and r.get("tradestatus") != "0"
                ]
                # Quantity must be observed, but an amount-only donor can use
                # the base's known quantity. The combined bar is checked below.
                if field == "volume":
                    donors = [r for r in donors if _positive(r, "volume")]
                if field == "amount":
                    donors = [
                        r
                        for r in donors
                        if _positive(out, "volume")
                        and (r.get("volume") is None or _positive(r, "volume"))
                    ]
                values = {_number(r[field]) if field in NUMERIC else r[field] for r in donors}
                if len(values) > 1:
                    conflicts.append(
                        dict(
                            kind="donor_disagreement",
                            field=field,
                            observation_ids=[r["observation_id"] for r in donors],
                        )
                    )
                elif donors:
                    donor = donors[0]
                    trial = dict(out, origin=base["origin"], **{field: donor[field]})
                    if _invalid(trial) is None:
                        out[field] = donor[field]
                        field_sources[field] = _lineage(donor)
                        filled.append(field)
                        field_counts[field] += 1
        if filled:
            # These identifiers belong to source observations, not derived bars.
            out.update(source="composed", row_hash=None, response_id=None, observed_at=None)
        native_sources = {r["source"] for r in observations if r["source"] != "baostock_alias"}
        completion = (
            "alias_day"
            if base["source"] == "baostock_alias"
            else "fallback_day"
            if source != "auto" and source not in native_sources
            else "fields"
            if filled
            else "original"
        )
        out.update(
            base_source=base["source"],
            base_observation_id=base["observation_id"],
            field_sources=field_sources,
            filled_fields=filled,
            observations=observations,
            conflicts=conflicts,
            trade_state=state,
            status_evidence=facts,
            completion=completion,
            has_conflicts=bool(conflicts),
        )
        result.append(out)
    columns = (
        "symbol",
        "date",
        "source",
        *BAR_FIELDS,
        "row_hash",
        "response_id",
        "observed_at",
        *EXTRA_COLUMNS,
    )
    frame = pd.DataFrame(result, columns=columns)
    native_days = {r["date"] for r in native if r["date"] in expected}
    frame.attrs = dict(
        mode="compatible",
        policy_version="local-compatible-v1",
        requested_range=dict(symbol=symbol, start=start, end=end),
        source_selection=source,
        source_priority=priority,
        source_priority_reason=(
            "Explicit source first, then BaoStock status/quantity, Eastmoney, Sina, Sohu, Tencent; "
            "explicitly selected retrospective alias captures last. Per security-day."
        ),
        compatibility="raw OHLC and every overlapping non-null field must agree as exact Decimals; "
        "conflicting donors leave the target field null; no rounding tolerance for splices",
        price_basis="raw",
        price_unit="CNY/share",
        volume_unit="shares",
        amount_unit="CNY",
        timezone="Asia/Shanghai",
        numeric_storage="decimal strings",
        historical_pit=False,
        source_contract="local daily import adapters normalize raw units; native raw receipts are "
        "referenced, not redecoded by this query; alias wire and issuer notice reverified",
        calendar_filter=store.meta("calendar_scope"),
        excluded_source_rows=dict(outside),
        alias_captures=alias_metadata,
        coverage=dict(
            expected_days=len(expected),
            native_observed_days=len(native_days),
            returned_days=len(result),
            missing_days=len(unfilled),
            filled_fields=dict(field_counts),
            days_with_conflicts=sum(r["has_conflicts"] for r in result),
            completion_counts=dict(Counter(r["completion"] for r in result)),
        ),
        unfilled_days=unfilled,
        interpretation="Source observations and retrospective completion, not proof of trading. "
        "Suspensions, status conflicts and unknown activity never receive field fills. "
        "Native coverage/storage are unchanged; field_sources identify each returned value.",
    )
    return frame


def get_price(store, security, start, end, source, alias_captures):
    symbol = _security(security)
    end = end or store.meta("end")
    parse_day(start)
    parse_day(end)
    if start > end or source not in ("auto", *PRIORITY[:-1]):
        raise ValueError("invalid date interval or source")
    if not isinstance(alias_captures, (tuple, list)):
        raise ValueError("alias_captures must be an explicit list/tuple of archive paths")
    captures = [Path(p).expanduser().resolve() for p in alias_captures]
    owns_transaction = not store.db.in_transaction
    if owns_transaction:
        store.db.execute("BEGIN")
    store._code_events = None
    try:
        return _query(store, symbol, start, end, source, captures)
    finally:
        if owns_transaction:
            store.db.rollback()
