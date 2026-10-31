"""Resolve specific negative share cells from two fixed, explicit observations.

The original table stays quarantined. A supported point read uses the existing
float-series value only after the same-date embedded chart corroborates it.
This is source-scoped research evidence, never official historical visibility.
"""

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import gzip
import json
from pathlib import Path
import re

from ashare_data.corporate_actions.share_charts import chart_observation, structure_charts
from ashare_data.corporate_actions.sources import parse_sina_float, parse_sina_structure
from ashare_data.corporate_actions import share_charts
from ashare_data.financials import expansion as e
from ashare_data.financials.official_daily import SnapshotReader, SourceUseDenied, assess

SCHEMA = "ad.share-resolution-bundle.v1"


def listing_class(task, instrument, day, events):
    if (not instrument or not day or not task.get("ipo")
            or instrument.get("metadata_source") not in {"sse", "szse"}
            or any(task.get(k) != instrument.get(k) for k in ("symbol", "ipo", "out"))):
        return "identity_unresolved"
    for event in events:
        if ((event["old_symbol"] == task["symbol"] and day >= event["effective_date"])
                or (event["new_symbol"] == task["symbol"] and task["ipo"] <= day < event["effective_date"])):
            return "identity_unresolved"
    if day < task["ipo"]:
        return "before_listing"
    if task.get("out") and day >= task["out"]:
        return "identity_unresolved"
    return "after_listing"


def captured(root, task_id):
    path = root / "receipts" / (task_id + ".json")
    blob = path.read_bytes()
    receipt = json.loads(blob)
    if receipt.get("task_id") != task_id or receipt["state"] not in {"complete", "partial_source", "empty_unknown"}:
        raise ValueError("Unsupported source receipt identity or state")
    e.verify_receipt(root, receipt)
    raw = gzip.decompress((root / receipt["raw"]["path"]).read_bytes())
    if e.digest(raw) != receipt["attempts"][-1]["response_sha256"]:
        raise ValueError("Source response identity changed")
    rows = [json.loads(line) for line in gzip.decompress((root / receipt["normalized"]["path"]).read_bytes()).splitlines()]
    return raw, receipt, rows, e.digest(blob)


def prepare(root, structure, floats):
    root = Path(root).resolve()
    structure, sm, st = e.load_batch(structure)
    floats, fm, ft = e.load_batch(floats)
    if root.exists() or root.is_relative_to(structure) or root.is_relative_to(floats):
        raise ValueError("A new resolution directory outside the source batches is required")
    universe = json.loads((structure / "universe.json").read_text())
    instruments = {r["symbol"]: r for r in universe["instruments"]}
    ft = {r["symbol"]: r for r in ft if r["kind"] == "sina_float"}
    records, counts, states = [], Counter(), Counter()
    for task in st:
        if task["kind"] != "sina_structure":
            continue
        rp = structure / "receipts" / (task["id"] + ".json")
        if not rp.exists() or json.loads(rp.read_text())["state"] != "partial_source":
            continue
        raw, _, rows, sr_sha = captured(structure, task["id"])
        parser = structure_charts(raw, e.security(task["symbol"]))
        series, fr, fr_sha = [], None, None
        float_task = ft.get(task["symbol"])
        if float_task and (floats / "receipts" / (float_task["id"] + ".json")).exists():
            candidate = json.loads((floats / "receipts" / (float_task["id"] + ".json")).read_text())
            if candidate.get("raw") and candidate.get("normalized"):
                _, fr, series, fr_sha = captured(floats, float_task["id"])
        by_date = defaultdict(list)
        for row in series:
            if row["category"] == "shares":
                by_date[row["observation"].get("date")].append(row)
        for row in rows:
            if row["category"] != "shares":
                continue
            obs = row["observation"]
            for gap in obs.get("field_gaps", []):
                category = listing_class(task, instruments.get(task["symbol"]), obs.get("date"), universe["code_events"])
                counts[category] += 1
                item = dict(security=row["security"], symbol=task["symbol"], date=obs.get("date"),
                    field=gap["field"], listing_class=category, source_gap=gap,
                    structure_task=task["id"], structure_locator=obs["locator"],
                    structure_receipt_sha256=sr_sha, structure_response_sha256=e.digest(raw),
                    structure_observation_id=row["source_revision_id"],
                    original_table_value_unchanged=True, historical_pit_certified=False,
                    status=category if category != "after_listing" else "unresolved")
                if category == "after_listing":
                    try:
                        if (gap["field"] != "float_a_shares"
                                or gap.get("reason") != "negative_source_share_count"
                                or obs["fields"].get(gap["field"]) is not None):
                            raise ValueError("Only float A chart resolution is supported")
                        table, _, column = obs["locator"].split("/")
                        chart = chart_observation(parser, row["security"], table, int(column), obs["date"])
                        item["chart"] = chart
                        candidates = by_date[obs["date"]]
                        if len(candidates) != 1:
                            raise ValueError("No unique same-date captured float-series observation")
                        other = candidates[0]
                        value = other["observation"]["fields"].get("float_a_shares")
                        if value is None or Decimal(value) < 0 or Decimal(value) != Decimal(chart["value"]):
                            raise ValueError("Chart and captured series do not report the identical value")
                        item.update(status="source_value_recovered", value=value, unit="shares",
                            value_source="existing_sina_float_confirmed_by_same_date_embedded_chart",
                            float_task=float_task["id"], float_locator=other["observation"]["locator"],
                            float_receipt_sha256=fr_sha, float_response_sha256=fr["attempts"][-1]["response_sha256"],
                            float_observation_id=other["source_revision_id"], independent_provider_confirmation=False)
                    except (ValueError, KeyError) as exc:
                        item["reason"] = str(exc)
                states[item["status"]] += 1
                records.append(item)
    keys = [(r["security"], r["date"], r["field"]) for r in records]
    if len(keys) != len(set(keys)):
        raise ValueError("Ambiguous security/date/field resolution candidates")
    blob = gzip.compress(b"".join(e.encoded(r) for r in records), mtime=0)
    manifest = dict(schema=SCHEMA, at=e.now(), structure_root=str(structure), float_root=str(floats),
        source_manifest_sha256={"structure": e.digest((structure / "manifest.json").read_bytes()),
                                "float": e.digest((floats / "manifest.json").read_bytes())},
        source_universe_sha256=sm["universe_sha256"], float_universe_sha256=fm["universe_sha256"],
        implementation_sha256={"share_resolution.py": e.digest(Path(__file__).read_bytes()),
                               "share_charts.py": e.digest(Path(share_charts.__file__).read_bytes())},
        records=dict(path="resolutions.jsonl.gz", sha256=e.digest(blob), count=len(records)),
        classification=dict(counts), states=dict(states), sources_modified=False, network_requests=0,
        new_schedule=False, database_imported=False, historical_pit_certified=False,
        limitation="Exact point observations only; original negative cells remain. No carry forward, cross-code mapping or historical visibility is inferred.")
    root.mkdir(parents=True)
    with (root / "resolutions.jsonl.gz").open("xb") as stream:
        stream.write(blob)
    with (root / "manifest.json").open("xb") as stream:
        stream.write(e.encoded(manifest))
    return manifest


class ShareResolutionReader:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = json.loads((self.root / "manifest.json").read_text())
        if self.manifest.get("schema") != SCHEMA:
            raise ValueError("Unsupported share resolution schema")
        artifact = (self.root / self.manifest["records"]["path"]).resolve()
        if not artifact.is_relative_to(self.root):
            raise ValueError("Resolution artifact escapes bundle")
        blob = artifact.read_bytes()
        if e.digest(blob) != self.manifest["records"]["sha256"]:
            raise ValueError("Resolution artifact changed")
        self.records = [json.loads(line) for line in gzip.decompress(blob).splitlines()]
        self.structure = SnapshotReader(self.manifest["structure_root"])
        self.floats = SnapshotReader(self.manifest["float_root"])
        for name, reader in (("structure", self.structure), ("float", self.floats)):
            if e.digest((reader.root / "manifest.json").read_bytes()) != self.manifest["source_manifest_sha256"][name]:
                raise ValueError("Resolution source manifest changed")

    def read(self, security, day, *, field="float_a_shares", purpose="source_research", as_of=None):
        selected = [r for r in self.records if (r["security"], r["date"], r["field"]) == (security, day, field)]
        if len(selected) != 1:
            raise SourceUseDenied(["exact_resolution_point_missing_or_ambiguous"])
        item = selected[0]
        raw, _, _, receipt_sha = captured(self.structure.root, item["structure_task"])
        if receipt_sha != item["structure_receipt_sha256"]:
            raise ValueError("Resolution structure receipt changed")
        original = self.structure.read(item["structure_task"], locator=item["structure_locator"], purpose="source_audit")
        row = original["source_record"]
        if (row["source_revision_id"] != item["structure_observation_id"]
                or row["security"] != security or row["observation"].get("date") != day
                or item["source_gap"] not in row["observation"].get("field_gaps", [])):
            raise ValueError("Resolution structure observation changed")
        if purpose == "source_audit":
            return dict(resolution=item, source_record=row, calculation_value_exposed=False)
        if item["status"] != "source_value_recovered":
            raise SourceUseDenied([item["status"], item.get("reason", "no_confirmed_listed_point_value")])
        decision = assess(row, self.structure.tasks[item["structure_task"]], purpose=purpose, as_of=as_of, field=field)
        if not decision["allowed"]:
            raise SourceUseDenied(decision["reasons"], decision)
        _, _, _, float_sha = captured(self.floats.root, item["float_task"])
        if float_sha != item["float_receipt_sha256"]:
            raise ValueError("Resolution float receipt changed")
        float_audit = self.floats.read(item["float_task"], locator=item["float_locator"], purpose="source_audit")
        float_row = float_audit["source_record"]
        if float_row["security"] != security or float_row["observation"].get("date") != day:
            raise ValueError("Resolution float security or date changed")
        table, _, column = item["structure_locator"].split("/")
        chart = chart_observation(structure_charts(raw, security), security, table, int(column), day)
        if chart != item["chart"]:
            raise ValueError("Resolution chart evidence changed")
        value = self.floats.read(item["float_task"], locator=item["float_locator"], field=field,
                                 purpose=purpose, as_of=as_of)
        if (value["provenance"]["source_revision_id"] != item["float_observation_id"]
                or value["value"] != item["value"] or Decimal(value["value"]) != Decimal(chart["value"])):
            raise ValueError("Resolution float value changed")
        return value | dict(resolution=item, structure_decision=decision, carry_forward_authorized=False,
                            historical_pit_certified=False, original_table_value_unchanged=True)


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
