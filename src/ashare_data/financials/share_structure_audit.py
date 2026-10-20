"""Explain quarantined share cells using fixed responses, never substitute values."""

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import gzip
import json
import re

from ashare_data.corporate_actions.sources import parse_sina_float, parse_sina_structure
from ashare_data.financials import expansion as e


def printed_shares(value):
    match = re.fullmatch(r"(-?[\d,.]+) 万股", value or "")
    if not match:
        return None
    text = match[1].replace(",", "")
    amount = Decimal(text) * 10000
    quantum = Decimal(10000).scaleb(-len(text.split(".")[1]) if "." in text else 0)
    return amount, quantum


def diagnose(gap, task, series):
    """Arithmetic is evidence of a possible classification problem, not a correction."""
    native = gap["native_fields"]
    parts = [printed_shares(native.get(k)) for k in ("总股本", "限售A股", "高管股", "流通A股")]
    result = dict(group="unexplained_negative", arithmetic=None, replacement_value=None,
                  cause_is_inferred=True, negative_is_missing_marker=False,
                  explicit_unit="万股", field=gap["field"], source_value=gap["source_value"])
    residual = tolerance = None
    if gap["field"] == "float_a_shares" and all(p is not None for p in parts):
        (total, _), (restricted, _), (executive, _), (float_a, _) = parts
        residual = float_a - (total - restricted - executive)
        tolerance = sum(p[1] for p in parts) / 2
        if abs(residual) <= tolerance:
            result["group"] = ("restricted_equals_total" if total == restricted
                               else "overlapping_share_categories")
        result["arithmetic"] = dict(
            formula="float_a = total - restricted_a - executive",
            exact=residual == 0, within_printed_precision=abs(residual) <= tolerance,
            difference_shares=str(residual), rounding_tolerance_shares=str(tolerance),
        )
    day = gap["date"]
    result["before_source_code_ipo"] = bool(day and task.get("ipo") and day < task["ipo"])
    result["after_source_code_exit"] = bool(day and task.get("out") and day >= task["out"])
    matches = [row for row in series if row["date"] == day]
    result["series_comparison"] = dict(
        same_provider=True, independent_provider_confirmation=False,
        exact_date_candidates=len(matches), values=[dict(
            locator=row["locator"], value=row["fields"].get("float_a_shares"),
            quantum=row.get("source_quantum_shares", {}).get("float_a_shares"),
        ) for row in matches],
        matches_total_minus_restricted=None,
    )
    if len(matches) == 1 and all(p is not None for p in parts[:2]):
        row = matches[0]
        value = row["fields"].get("float_a_shares")
        quantum = row.get("source_quantum_shares", {}).get("float_a_shares")
        if value is not None and quantum is not None:
            delta = Decimal(value) - (parts[0][0] - parts[1][0])
            bound = (Decimal(quantum) + parts[0][1] + parts[1][1]) / 2
            result["series_comparison"].update(
                matches_total_minus_restricted=abs(delta) <= bound,
                difference_shares=str(delta), rounding_tolerance_shares=str(bound),
            )
    return result


def cached(root, task):
    path = root / "receipts" / (task["id"] + ".json")
    content = path.read_bytes()
    receipt = json.loads(content)
    if receipt["task_id"] != task["id"]:
        raise ValueError("Cached source receipt task identity changed")
    e.verify_receipt(root, receipt)
    raw = gzip.decompress((root / receipt["raw"]["path"]).read_bytes())
    if e.digest(raw) != receipt["attempts"][-1]["response_sha256"]:
        raise ValueError("Cached source response identity changed")
    return raw, receipt, dict(task_id=task["id"], receipt_sha256=e.digest(content),
                             response_sha256=e.digest(raw), observed_at=receipt["observed_at"])


def audit(root, float_root, *, captured_before=None):
    """Read only failed captured structure pages; verify and reuse existing float caches."""
    root, manifest, tasks = e.load_batch(root)
    float_root, _, float_tasks = e.load_batch(float_root)
    float_tasks = {t["symbol"]: t for t in float_tasks if t["kind"] == "sina_float"}
    cutoff = datetime.fromisoformat(captured_before) if captured_before else None
    if cutoff and cutoff.tzinfo is None:
        raise ValueError("captured_before must include a timezone")
    records, sources, excluded = [], [], []
    states, groups, symbol_groups = Counter(), Counter(), defaultdict(set)
    for task in tasks:
        if task["kind"] != "sina_structure":
            continue
        path = root / "receipts" / (task["id"] + ".json")
        if not path.exists():
            continue
        receipt = json.loads(path.read_text())
        if receipt["state"] != "parse_failed" or not receipt.get("raw"):
            continue
        if cutoff and datetime.fromisoformat(receipt["observed_at"]) > cutoff:
            excluded.append(task["id"])
            continue
        raw, receipt, source = cached(root, task)
        parsed = parse_sina_structure(raw, e.security(task["symbol"]))
        series, series_source = [], None
        float_task = float_tasks.get(task["symbol"])
        if float_task and (float_root / "receipts" / (float_task["id"] + ".json")).exists():
            float_receipt = json.loads((float_root / "receipts" / (float_task["id"] + ".json")).read_text())
            if float_receipt.get("raw"):
                float_raw, _, series_source = cached(float_root, float_task)
                series = parse_sina_float(float_raw, e.security(task["symbol"]))["shares"]
        source.update(series_source=series_source, original_error=receipt.get("parse_error"))
        sources.append(source)
        for gap in parsed["unparsed_records"]:
            diagnosis = diagnose(gap, task, series)
            groups[diagnosis["group"]] += 1
            symbol_groups[diagnosis["group"]].add(task["symbol"])
            states["before_ipo" if diagnosis["before_source_code_ipo"] else "not_before_ipo"] += 1
            arithmetic = diagnosis["arithmetic"]
            if arithmetic:
                states["exact_arithmetic" if arithmetic["exact"] else
                       "rounded_arithmetic" if arithmetic["within_printed_precision"] else
                       "inconsistent_arithmetic"] += 1
            comparison = diagnosis["series_comparison"]
            states["series_matches_total_minus_restricted" if comparison["matches_total_minus_restricted"]
                   else "series_unresolved"] += 1
            records.append(dict(task_id=task["id"], symbol=task["symbol"], ipo=task.get("ipo"),
                locator=gap["locator"], date=gap["date"], announcement_date=gap["announcement_date"],
                in_target_interval=bool(gap["date"] and manifest["target_start"] <= gap["date"] <= manifest["target_end"]),
                native_fields={k: gap["native_fields"].get(k) for k in
                               ("变动日期", "公告日期", "变动原因", "总股本", "限售A股", "高管股", "流通A股")},
                diagnosis=diagnosis))
    return dict(schema="ad.share-structure-source-audit.v1", at=e.now(), source=str(root),
        float_source=str(float_root), captured_before=captured_before,
        source_manifest_sha256=e.digest((root / "manifest.json").read_bytes()),
        float_manifest_sha256=e.digest((float_root / "manifest.json").read_bytes()),
        tasks=len(sources), negative_cells=len(records), groups=dict(groups), counts=dict(states),
        symbols_by_group={k: sorted(v) for k, v in symbol_groups.items()},
        sources=sources, cells=records, later_captures_excluded=excluded,
        network_requests=0, sources_modified=False, values_replaced=0,
        historical_pit_certified=False, database_imported=False)
