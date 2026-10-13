"""One frozen six-session research replay, never the JQ VROC6 implementation.

Only existing public snapshots enter this module. Price-factor-rescaled quantity
is a declared local experiment, not a documented Sina volume transformation.
There is no network, event-factor reconstruction, normalization, or precision fit.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re

DEFINITION = "sina_native_pricefactor_rescaled_volume_lag5.research.v1"
ROLE = "retrospective_research"
FLAGS = {"jq_equivalent": False, "historical_pit": False,
         "strategy_visible": False, "production_eligible": False,
         "supplier_volume_transform_verified": False}


class InputError(ValueError):
    """Reject unusable evidence; do not fill, infer, or silently rescale it."""


def require(condition, message):
    if not condition:
        raise InputError(message)


def day(value):
    require(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
            "ISO date required")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise InputError("Invalid date") from exc
    return value


def decimal_fraction(value):
    require(type(value) is str and re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", value)
            and len(value) <= 80, "Exact nonnegative decimal string required")
    return Fraction(value)


def checked_bytes(root, ref):
    require(type(ref) is dict and type(ref.get("path")) is str,
            "Evidence reference required")
    rel = Path(ref["path"])
    require(not rel.is_absolute() and ".." not in rel.parts,
            "Evidence path must remain within the package")
    path = root / rel
    require(not path.is_symlink() and path.resolve().is_relative_to(root.resolve())
            and path.is_file() and path.stat().st_size <= 2_000_000,
            "Missing, external, symlinked, or oversized evidence")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == ref.get("sha256"),
            "Evidence SHA256 mismatch")
    return raw


def load_json(raw):
    def reject_constant(value):
        raise InputError("Nonfinite JSON constant")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(raw, parse_float=str, parse_constant=reject_constant,
                          object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError) as exc:
        raise InputError("Invalid evidence JSON") from exc


def javascript_payload(raw, prefix):
    try:
        text = raw.decode("utf-8").strip()
        require(text.startswith(prefix), "Unexpected snapshot symbol or format")
        # This is a data reader. No JavaScript or eval is executed.
        body = text[len(prefix):].split("/*", 1)[0].strip().rstrip(";")
        return load_json(body)
    except UnicodeError as exc:
        raise InputError("Invalid UTF-8 snapshot") from exc


def calendar_sessions(calendar, start, end):
    require(type(calendar) is dict and calendar.get("venue") == "SSE",
            "Explicit SSE calendar required")
    n = (date.fromisoformat(end) - date.fromisoformat(start)).days
    require(0 <= n <= 31, "One bounded calendar interval required")
    expected = [(date.fromisoformat(start) + timedelta(days=i)).isoformat()
                for i in range(n + 1)]
    rows = calendar.get("days")
    require(type(rows) is list and all(type(row) is dict for row in rows),
            "Calendar rows required")
    require([row.get("date") for row in rows] == expected,
            "Calendar must include every civil day exactly once")
    require(all(type(row.get("open")) is bool for row in rows),
            "Unknown calendar state")
    sessions = [row["date"] for row in rows if row["open"]]
    require(len(sessions) == 6, "Exactly six uncompressed sessions required")
    return sessions


def factor_rows(payload, ipo):
    require(type(payload) is dict, "Factor ledger object required")
    rows = payload.get("data")
    require(type(rows) is list and rows and payload.get("total") == len(rows),
            "Factor ledger count mismatch")
    require(all(type(row) is dict for row in rows), "Invalid factor rows")
    labels = [day(row.get("d")) for row in rows]
    require(len(labels) == len(set(labels)), "Duplicate factor effective date")
    require(labels == sorted(labels, reverse=True), "Unexpected ledger order")
    factors = [(label, decimal_fraction(row.get("f")), row["f"])
               for label, row in zip(labels, rows)]
    require(all(factor > 0 for _, factor, _ in factors), "Nonpositive factor")
    require(any(label == ipo and factor == 1 for label, factor, _ in factors),
            "Explicit IPO=1 row required; no synthetic anchor")
    return sorted(factors)


def select_factor(factors, session):
    available = [row for row in factors if row[0] <= session]
    require(bool(available), "Missing historical factor; backfill forbidden")
    return available[-1]


def research_result(sessions, volumes, factors):
    require(len(sessions) == len(volumes) == 6, "Six sessions required")
    require([day(item) for item in sessions] == sorted(set(sessions)),
            "Unique ordered session dates required")
    require(all(type(v) is int and 0 < v < 10**17 for v in volumes),
            "This active-session demo rejects missing, zero, or invalid raw shares")
    rows = []
    for session, volume in zip(sessions, volumes):
        effective, factor, original = select_factor(factors, session)
        # Python Fraction.__round__ is exact nearest integer, ties to even.
        quantity = round(Fraction(volume) / factor)
        rows.append({"date": session, "raw_volume_shares": volume,
                     "native_factor_text": original, "factor_effective_date": effective,
                     "local_research_quantity": quantity})
    first, last = rows[0]["local_research_quantity"], rows[-1]["local_research_quantity"]
    require(first > 0, "Zero rounded denominator")
    result = Fraction(100 * (last - first), first)
    with localcontext() as context:
        context.prec = 34
        decimal = str(Decimal(result.numerator) / Decimal(result.denominator))
    return {"definition": DEFINITION, "role": ROLE, **FLAGS,
            "security": "601012.XSHG", "unit": "percentage_points",
            "formula": "100 * (Q[t] / Q[t-5] - 1); Q = half_even(V / native_F)",
            "factor_precision": "source decimal text used exactly; no quantization",
            "quantity_tie_policy": "locally declared half-even; not vendor-certified",
            "rows": rows, "value_decimal_34digits": decimal,
            "value_exact": {"numerator": str(result.numerator),
                            "denominator": str(result.denominator)}}


def replay(root, *, expected_binding_sha256, definition, role):
    require(definition == DEFINITION and role == ROLE,
            "Explicit research definition and role required; VROC6 alias unavailable")
    root = Path(root)
    binding = load_json(checked_bytes(root, {"path": "binding.json",
                                           "sha256": expected_binding_sha256}))
    require(type(binding) is dict and binding.get("schema") == "vroc6.native-research-binding.v1"
            and binding.get("definition") == DEFINITION
            and binding.get("role") == ROLE
            and all(binding.get(key) is False for key in FLAGS),
            "Invalid research contract")
    require(binding.get("security") == "601012.XSHG"
            and binding.get("input_start") == "2012-06-19"
            and binding.get("input_end") == "2012-06-27"
            and binding.get("ipo_date") == "2012-04-11", "Outside bounded demo")
    require(binding.get("volume_unit") == "share"
            and binding.get("raw_adjustment") == "none"
            and binding.get("factor_convention") == "sina_native_hfq"
            and binding.get("anchor_policy") == "source_ipo_row_no_rescaling"
            and binding.get("factor_origin") == "independent_provider_snapshot",
            "Wrong unit, adjustment, anchor, or input origin")
    evidence = binding.get("evidence")
    require(type(evidence) is dict and set(evidence) == {
        "sina_decoded", "sina_hfq", "tencent_raw", "calendar", "calendar_notice", "listing"
    }, "Unexpected evidence set")
    saved = {name: checked_bytes(root, ref) for name, ref in evidence.items()}
    sessions = calendar_sessions(load_json(saved["calendar"]), binding["input_start"],
                                 binding["input_end"])
    sina = load_json(saved["sina_decoded"])
    require(type(sina) is list and all(type(row) is dict for row in sina),
            "Invalid decoded Sina daily snapshot")
    selected = [row for row in sina if row.get("date", "")[:10] in sessions]
    require([row["date"][:10] for row in selected] == sessions,
            "Missing, duplicated, or reordered raw session")
    volumes = [row.get("volume") for row in selected]
    tencent = javascript_payload(saved["tencent_raw"], "kline_day2012=")
    require(tencent.get("code") == 0, "Tencent snapshot unavailable")
    try:
        cross = tencent["data"]["sh601012"]["day"]
        require([row[0] for row in cross] == sessions, "Cross-source session mismatch")
        cross_volumes = [decimal_fraction(row[5]) * 100 for row in cross]
        require(cross_volumes == volumes, "Independent raw-volume mismatch")
    except (KeyError, IndexError, TypeError) as exc:
        raise InputError("Malformed cross-source snapshot") from exc
    factors = factor_rows(javascript_payload(saved["sina_hfq"], "var sh601012hfq="),
                          binding["ipo_date"])
    result = research_result(sessions, volumes, factors)
    result.update(binding_sha256=expected_binding_sha256,
                  raw_cross_source_matches=len(sessions),
                  source_factor_ledger_entries=len(factors),
                  provider_snapshot_is_historical_pit=False,
                  source_evidence_sha256={key: ref["sha256"] for key, ref in evidence.items()})
    return result
