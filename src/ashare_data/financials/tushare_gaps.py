"""Receipt-bound field inventory and gap-only plans; never scan a price database.

The compact inventory contains field presence, derivations and source pointers.
It is incrementally refreshed from changed normalized receipts, not copied raw
responses. A frozen plan is created only after the existing source pass closes.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, localcontext
import gzip
import json
import os
from pathlib import Path
from uuid import uuid4

from ashare_data.financials import expansion as e
from ashare_data.financials import tushare_reference as ts
from ashare_data.financials import tushare_fundamentals as f

KINDS = {"sina_income": "income", "sina_balance": "balancesheet", "sina_cashflow": "cashflow", "sina_actions": "dividend"}
META = {api: ts.FINANCIAL_ID.split(",") for api in f.STATEMENTS}
META["fina_indicator"] = ["ts_code", "ann_date", "end_date", "update_flag"]
META["dividend"] = ["ts_code", "end_date", "ann_date", "div_proc", "ex_date", "record_date", "cash_div_tax", "stk_bo_rate", "stk_co_rate"]
META["adj_factor"] = ts.FIELDS["adj_factor"].split(",")
DIV_GAPS = {"pay_date": "payment_date", "imp_ann_date": "implementation_announcement_date",
            "div_listdate": "new_shares_tradable_date", "base_date": "distribution_base_date", "base_share": "distribution_base_shares"}
# Financial-sector templates retain these amounts in fields but older normalized
# snapshots do not project them into metrics. Reuse the receipt-bound source
# value only when both its native identifier and title match, without rewriting
# source snapshots or borrowing publication evidence from another provider.
RAW_AMOUNT_ALIASES = {
    "income": {"n_income_attr_p": {
        "NETPARECOMPPROF": {"归属于母公司的净利润"},
        "NETPARESHARPROF": {"归属于母公司的净利润"},
    }},
    "cashflow": {"c_cash_equ_end_period": {"CASHEQUFINBALA": {"期末现金及现金等价物余额", "期末现金及现金等价物"}}},
}


def read(path, default=None):
    if not path.exists():
        return default
    return json.loads(gzip.decompress(path.read_bytes())) if path.suffix == ".gz" else json.loads(path.read_text())


def normalized(root, receipt):
    artifact = receipt["normalized"]
    body = (root / artifact["path"]).read_bytes()
    if e.digest(body) != artifact["sha256"]:
        raise ValueError("Normalized receipt checksum mismatch")
    return [json.loads(line) for line in gzip.decompress(body).splitlines() if line]


def derivation(row, numerator, denominator, name):
    metrics = row["metrics"]
    a, b = (metrics.get(k, {}) for k in (numerator, denominator))
    if row.get("scope") != "consolidated" or any(x.get("unit") != "CNY" or x.get("value") is None for x in (a, b)):
        return None
    if Decimal(b["value"]) == 0:
        return dict(field=name, value=None, state="undefined_zero_denominator", unit="percent")
    with localcontext() as ctx:
        ctx.prec = 34
        value = format(Decimal(a["value"]) / Decimal(b["value"]) * 100, "f")
    return dict(field=name, value=value, state="derived_current_source_version", unit="percent",
                formula=f"100 * {numerator} / {denominator}", source_revision_id=row["source_revision_id"],
                source_fields=[a["source_field"], b["source_field"]], historical_pit_certified=False)


def summarize(rows, api):
    periods, events, quarantined, distributions = [], [], 0, 0
    for row in rows:
        if api == "dividend":
            obs = row.get("observation", {})
            fields = obs.get("fields", {})
            if row.get("category") != "actions" or obs.get("type") != "distribution":
                continue
            distributions += 1
            if not fields.get("ex_date"):
                continue
            missing = [key for key, source in DIV_GAPS.items() if fields.get(source) is None]
            if all(fields.get(k) is not None and Decimal(fields[k]) == 0 for k in ("bonus_share_ratio", "capitalization_share_ratio")):
                missing = [key for key in missing if key != "div_listdate"]
            events.append(dict(date=fields["ex_date"], source_revision_id=row["source_revision_id"],
                locator=obs["locator"], missing=missing,
                anchors={"cash_div_tax": fields.get("cash_per_share"), "stk_bo_rate": fields.get("bonus_share_ratio"),
                         "stk_co_rate": fields.get("capitalization_share_ratio"), "record_date": fields.get("record_date")},
                cash_basis=obs.get("cash_basis")))
            continue
        if row.get("period_after_code_exit") or row.get("scope") != "consolidated" or row.get("currency") != "CNY":
            quarantined += 1
            continue
        metrics = row.get("metrics", {})
        present = [field for field, canonical in f.METRICS[api].items()
                   if metrics.get(canonical, {}).get("value") is not None and metrics[canonical].get("unit") == "CNY"]
        reused_raw_fields = {}
        for target, aliases in RAW_AMOUNT_ALIASES.get(api, {}).items():
            if target in present:
                continue
            candidates = [item for item in row.get("fields", []) if item.get("source_field") in aliases]
            if len(candidates) != 1:
                continue  # Duplicate/ambiguous native fields remain unresolved.
            item = candidates[0]
            if (item.get("source_title") not in aliases[item["source_field"]]
                    or item.get("state") != "source_observed" or item.get("value") is None
                    or e.numeric(item["value"])[1] != "source_observed"):
                continue
            present.append(target)
            reused_raw_fields[target] = {key: item[key] for key in ("source_field", "source_title", "ordinal")}
        derived = []
        if api == "income":
            value = derivation(row, "net_profit", "operating_revenue", "netprofit_margin")
            if value:
                derived.append(value)
        if api == "balancesheet":
            value = derivation(row, "total_liabilities", "total_assets", "debt_to_assets")
            if value:
                derived.append(value)
        periods.append(dict(period=row["period_end"], present=present, source_revision_id=row["source_revision_id"],
                            source_locator=row["source_locator"], announcement_date=row.get("announcement_date"), derived=derived,
                            reused_raw_fields=reused_raw_fields))
    return dict(periods=periods, events=events, quarantined_reports=quarantined, distribution_observations=distributions)


def inventory(baseline, output):
    baseline, manifest, tasks = e.load_batch(baseline)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    counts, entries, refreshed = Counter(), [], 0
    code = e.digest(Path(__file__).read_bytes() + Path(f.__file__).read_bytes())
    for task in tasks:
        api = KINDS.get(task["kind"])
        if api is None:
            continue
        receipt_path = baseline / "receipts" / (task["id"] + ".json")
        raw = receipt_path.read_bytes() if receipt_path.exists() else b"{}"
        receipt = json.loads(raw)
        key = e.digest(raw)
        cache = output / "entries" / (task["id"] + ".json.gz")
        entry = read(cache, {})
        if entry.get("receipt_sha256") != key or entry.get("inventory_code_sha256") != code:
            rows = normalized(baseline, receipt) if receipt.get("normalized") else []
            entry = dict(task_id=task["id"], symbol=task["symbol"], api=api, state=receipt.get("state", "pending"),
                         receipt_sha256=key, inventory_code_sha256=code, normalized=receipt.get("normalized"), **summarize(rows, api))
            e.atomic(cache, gzip.compress(e.encoded(entry), mtime=0))
            refreshed += 1
        entries.append(entry)
        counts[entry["state"]] += 1
    result = dict(schema="ad.tushare-field-inventory.v1", at=e.now(), baseline=str(baseline),
                  universe_sha256=manifest["universe_sha256"], entries=entries)
    artifact = e.artifact(output, "inventory.jsonl.gz", gzip.compress(b"".join(e.encoded(entry) for entry in entries), mtime=0))
    summary = dict(schema=result["schema"], at=result["at"], baseline=str(baseline), universe_sha256=result["universe_sha256"],
                   source_states=dict(counts), entries=len(entries), entries_refreshed=refreshed, inventory=artifact,
                   existing_amount_fields=sum(len(p["present"]) for v in entries for p in v["periods"]),
                   derived_indicator_values=sum(d["value"] is not None for v in entries for p in v["periods"] for d in p["derived"]),
                   distribution_events=sum(len(v["events"]) for v in entries),
                   quarantined_reports=sum(v["quarantined_reports"] for v in entries),
                   source_pass_closed=all(v["state"] in e.TERMINAL for v in entries),
                   no_database_scan=True, authenticated_requests=0)
    e.atomic(output / "summary.json", e.encoded(summary))
    return result, summary


def preview(baseline, root, factor_inventory=None):
    root = Path(root)
    snapshot, summary = inventory(baseline, root / "inventory")
    factors = read(Path(factor_inventory)) if factor_inventory else None
    plan = build_plan(baseline, snapshot, factor_inventory=factors)
    artifact = e.artifact(root, "gap-plan-preview.json.gz", gzip.compress(e.encoded(plan), mtime=0))
    result = dict(at=e.now(), state="offline_preview; not_enqueued", inventory=summary,
                  requests_by_api=dict(Counter(t["kind"] for t in plan["tasks"])),
                  requests_by_reason=dict(Counter(t["reason"] for t in plan["tasks"])),
                  deferred_by_reason=dict(Counter(t["state"] for t in plan["deferred"])),
                  plan=artifact, authenticated_requests=0, database_imported=False)
    e.atomic(root / "preview-summary.json", e.encoded(result))
    return result


def make_task(api, instrument, params, fields, manifest, **extra):
    code = instrument["symbol"][2:] + (".SH" if instrument["symbol"].startswith("sh") else ".SZ")
    selected = sorted(set(META[api] + list(fields)))
    url = ts.request_url(api, {"ts_code": code, **params}, selected)
    identity = "tushare_" + api + "-" + e.digest(url.encode())[:24]
    return dict(id=identity, gap_group=identity, kind="tushare_" + api,
                symbol=instrument["symbol"], out=instrument.get("out"), url=url,
                start=e.day(params.get("start_date") or params.get("period") or params.get("ex_date")) or manifest["target_start"],
                end=e.day(params.get("end_date") or params.get("period") or params.get("ex_date")) or manifest["target_end"],
                target_start=manifest["target_start"], target_end=manifest["target_end"], **extra)


def financial_tasks(entry, instrument, manifest, known_periods):
    api, periods, tasks = entry["api"], entry["periods"], []
    dates = sorted(known_periods)
    window = dict(start_date=manifest["target_start"].replace("-", ""), end_date=manifest["target_end"].replace("-", ""))
    if not periods:
        # Empty/failed responses are unknown coverage, not proof of no reports.
        return [make_task(api, instrument, window | {"report_type": "1"}, f.METRICS[api], manifest,
                          reason="unresolved_existing_source", gaps=list(f.METRICS[api]), periods=[])]
    tasks.append(make_task(api, instrument, window | {"report_type": "1"}, [], manifest, periods=dates,
                          reason="missing_version_and_actual_announcement_metadata", gaps=["f_ann_date", "update_flag"]))
    for kind in ("4", "5"):
        tasks.append(make_task(api, instrument, window | {"report_type": kind}, f.METRICS[api], manifest,
                              periods=dates, reason="prior_provider_versions_unknown", gaps=list(f.METRICS[api])))
    for p in periods:
        missing = sorted(set(f.METRICS[api]) - set(p["present"]))
        if missing:
            tasks.append(make_task(api, instrument, dict(period=p["period"].replace("-", ""), report_type="1"),
                                  missing, manifest, periods=[p["period"]], gaps=missing, reason="missing_current_amount_fields"))
    for period in sorted(known_periods - {p["period"] for p in periods}):
        tasks.append(make_task(api, instrument, dict(period=period.replace("-", ""), report_type="1"),
                              f.METRICS[api], manifest, periods=[period], gaps=list(f.METRICS[api]),
                              reason="report_period_observed_in_other_base_statement"))
    return tasks


def build_plan(baseline, snapshot, *, final=False, factor_inventory=None):
    baseline, manifest, baseline_tasks = e.load_batch(baseline)
    if snapshot["universe_sha256"] != manifest["universe_sha256"]:
        raise ValueError("Inventory universe mismatch")
    target_symbols = {task["symbol"] for task in baseline_tasks}
    instruments = {r["symbol"]: r for r in read(baseline / "universe.json")["instruments"] if r["symbol"] in target_symbols}
    known = {}
    for entry in snapshot["entries"]:
        known.setdefault(entry["symbol"], set()).update(p["period"] for p in entry["periods"])
    tasks, deferred, existing, indicators = {}, [], [], {}
    for entry in snapshot["entries"]:
        symbol, api = entry["symbol"], entry["api"]
        if entry["state"] not in e.TERMINAL:
            deferred.append(dict(task_id=entry["task_id"], state="pending_existing_source"))
            continue
        instrument = instruments[symbol]
        if api in f.STATEMENTS:
            for task in financial_tasks(entry, instrument, manifest, known[symbol]):
                tasks[task["id"]] = task
            for p in entry["periods"]:
                existing.append(dict(api=api, symbol=symbol, period=p["period"], fields=p["present"],
                                     state="covered_same_scope_current_source", source_revision_id=p["source_revision_id"],
                                     source_task=entry["task_id"], derived=p["derived"],
                                     reused_raw_fields=p.get("reused_raw_fields", {})))
                indicator = {"income": "netprofit_margin", "balancesheet": "debt_to_assets"}.get(api)
                if indicator and not p["derived"]:
                    indicators.setdefault((symbol, p["period"]), set()).add(indicator)
        else:
            for event in entry["events"]:
                if instrument.get("out") and event["date"] >= instrument["out"]:
                    deferred.append(dict(task_id=entry["task_id"], date=event["date"], state="event_after_code_exit_quarantined"))
                    continue
                if not event["missing"]:
                    continue
                task = make_task("dividend", instrument, {"ex_date": event["date"].replace("-", "")},
                                 event["missing"], manifest, reason="missing_existing_distribution_fields",
                                 gaps=event["missing"], matching_events=[event])
                if task["id"] in tasks:
                    tasks[task["id"]]["matching_events"].append(event)
                else:
                    tasks[task["id"]] = task
            if not entry.get("distribution_observations"):
                task = make_task("dividend", instrument, {}, DIV_GAPS, manifest,
                                 reason="unresolved_existing_distribution_source; no_existing_distributions",
                                 gaps=list(DIV_GAPS), matching_events=[])
                tasks[task["id"]] = task
            elif not entry["events"]:
                deferred.append(dict(task_id=entry["task_id"], state="no_event_date_anchor; no_full_history_duplicate_query"))
    for (symbol, period), fields in sorted(indicators.items()):
        task = make_task("fina_indicator", instruments[symbol], {"period": period.replace("-", "")}, fields,
                         manifest, periods=[period], gaps=sorted(fields), reason="base_inputs_missing_for_common_indicator")
        tasks[task["id"]] = task
    factor_state = "inventory_not_supplied; no_factor_requests"
    if factor_inventory is not None:
        if factor_inventory.get("schema") != "ad.existing-factor-inventory.v1" or factor_inventory.get("universe_sha256") != manifest["universe_sha256"]:
            raise ValueError("Explicit factor inventory for the same universe required")
        factor_state = "existing_source_history_reused; only_bounded_uncovered_tail_requested"
        for symbol, instrument in instruments.items():
            owned = factor_inventory.get("codes", {}).get(symbol, {})
            if not owned.get("covered_through"):
                deferred.append(dict(symbol=symbol, api="adj_factor", state="missing_existing_inventory; no_assumed_backfill"))
                continue
            a = max(date.fromisoformat(manifest["target_start"]), date.fromisoformat(owned["covered_through"]) + timedelta(days=1), date.fromisoformat(instrument["ipo"]))
            b = date.fromisoformat(manifest["target_end"])
            if instrument.get("out"):
                b = min(b, date.fromisoformat(instrument["out"]) - timedelta(days=1))
            while a <= b:
                end = min(b, a + timedelta(days=365))
                task = make_task("adj_factor", instrument, dict(start_date=a.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")),
                                 ["adj_factor"], manifest, gaps=["adj_factor"], reason="existing_factor_inventory_uncovered_tail",
                                 existing_factor_basis=owned.get("basis"), source_continuity_asserted=False)
                tasks[task["id"]] = task
                a = end + timedelta(days=1)
    if final and any(v["state"] == "pending_existing_source" for v in deferred):
        raise ValueError("Existing source requests are still pending; do not freeze overlapping financial queries")
    return dict(schema="ad.tushare-field-gap-plan.v1", universe_sha256=manifest["universe_sha256"],
                target_start=manifest["target_start"], target_end=manifest["target_end"], final=final,
                source_inventory_sha256=e.digest(e.encoded(snapshot["entries"])), tasks=list(tasks.values()),
                existing_fields=existing, deferred=deferred, factor_state=factor_state,
                database_imported=False, no_same_scope_amount_recollection=True,
                cross_provider_dates_bind_existing_values=False, historical_pit_certified=False)


def prepare(root, baseline, plan, *, calendar_root=None):
    if not plan["final"]:
        raise ValueError("Only a closed-source plan can enqueue requests")
    root = Path(root)
    tasks = plan["tasks"]
    if calendar_root:
        sessions = {"sh": set(), "sz": set()}
        calroot, _, caltasks = e.load_batch(calendar_root)
        for task in caltasks:
            receipt = read(calroot / "receipts" / (task["id"] + ".json"), {})
            if receipt.get("normalized"):
                for row in normalized(calroot, receipt):
                    if row["is_open"]:
                        sessions["sh" if row["exchange"] == "SSE" else "sz"].add(row["date"])
        tasks = [t | {"expected_sessions": sorted(d for d in sessions[t["symbol"][:2]] if t["start"] <= d <= t["end"])}
                 if t["kind"] == "tushare_adj_factor" else t for t in tasks]
    manifest = ts._prepare(root, baseline, tasks, {"gap_plan_sha256": e.digest(e.encoded(plan)),
                           "scope": "explicit_current_batch_field_gaps", "database_imported": False})
    e.immutable(root / "gap-plan.json", e.encoded(plan))
    return manifest


def prepare_splits(root, baseline, previous):
    previous, _, tasks = e.load_batch(previous)
    children, parents, unresolved = [], [], []
    for task in tasks:
        path = previous / "receipts" / (task["id"] + ".json")
        receipt = read(path, {})
        if receipt.get("coverage", {}).get("row_cap_reached"):
            split = f.split_task(task)
            if split:
                children.extend(split)
                parents.append(dict(task_id=task["id"], receipt_sha256=e.digest(path.read_bytes())))
            else:
                unresolved.append(task["id"])
    return ts._prepare(root, baseline, children, {"split_source": str(previous), "split_parents": parents,
                       "unsplittable_capped_tasks": unresolved, "split_semantics": "documented_date_dimension_only"})


def prepare_discovered_periods(root, baseline, source_roots):
    """Fill amounts only after an ordinary response proves a previously unseen period."""
    baseline, manifest, _ = e.load_batch(baseline)
    instruments = {r["symbol"]: r for r in read(baseline / "universe.json")["instruments"]}
    batches = [e.load_batch(path) for path in source_roots]
    existing_ids = {t["id"] for _, _, tasks in batches for t in tasks}
    tasks, evidence = {}, []
    replaced = {t["split_parent"] for _, _, leaves in batches for t in leaves if t.get("split_parent")}
    for source, _, leaves in batches:
        for task in leaves:
            if task["id"] in replaced or task["reason"] != "missing_version_and_actual_announcement_metadata":
                continue
            path = source / "receipts" / (task["id"] + ".json")
            receipt = read(path, {})
            if not receipt.get("normalized"):
                continue
            api = task["kind"].removeprefix("tushare_")
            discovered = {r["period_end"] for r in normalized(source, receipt)
                          if r["period_end"] not in task["periods"] and not r["period_after_code_exit"]}
            for period in sorted(discovered):
                new = make_task(api, instruments[task["symbol"]], dict(period=period.replace("-", ""), report_type="1"),
                                f.METRICS[api], manifest, periods=[period], gaps=list(f.METRICS[api]),
                                reason="new_period_observed_in_ordinary_metadata_response")
                if new["id"] not in existing_ids:
                    tasks[new["id"]] = new
                    evidence.append(dict(source_batch=str(source), source_task=task["id"], period=period,
                                         receipt_sha256=e.digest(path.read_bytes()), discovered_task=new["id"]))
    return ts._prepare(root, baseline, list(tasks.values()), {"period_discovery_evidence": evidence,
                       "quarterly_filing_obligation_inferred": False})


def dividend_matches(row, event):
    if event.get("cash_basis") != "gross_pre_tax_per_old_eligible_share" or not row.get("implemented"):
        return False
    for key, value in event["anchors"].items():
        actual = row["fields"].get(key, {}).get("value")
        if value is None or actual is None:
            return False
        if key == "record_date":
            if value != actual:
                return False
        elif Decimal(value) != Decimal(actual):
            return False
    return row["fields"]["ex_date"]["value"] == event["date"]


def reconciliation_rows(plan, roots):
    """Stream field outcomes; retain every source version without latest-only joins."""
    batches = [e.load_batch(root) for root in roots]
    replaced = {task["split_parent"] for _, _, tasks in batches for task in tasks if task.get("split_parent")}
    groups = {}
    for root, _, tasks in batches:
        for task in tasks:
            if task["id"] not in replaced:
                groups.setdefault(task.get("gap_group", task["id"]), []).append((root, task))
    for owned in plan["existing_fields"]:
        for name in owned["fields"]:
            yield {k: v for k, v in owned.items() if k not in {"fields", "derived"}} | {"field": name}
        for derived in owned["derived"]:
            yield dict(symbol=owned["symbol"], period=owned["period"], **derived)
    for identity, leaves in groups.items():
        task = leaves[0][1]
        rows, receipts = [], []
        for root, leaf in leaves:
            receipt = read(root / "receipts" / (leaf["id"] + ".json"), {})
            receipts.append(dict(task_id=leaf["id"], state=receipt.get("state", "pending"), coverage=receipt.get("coverage", {})))
            if receipt.get("normalized"):
                rows.extend(r | dict(source_task_id=r.get("source_task_id", leaf["id"]),
                                     source_batch=r.get("source_batch", str(root))) for r in normalized(root, receipt))
        api = task["kind"].removeprefix("tushare_")
        expected = (sorted(set(task.get("expected_sessions", [])) | {r["trade_date"] for r in rows})
                    if api == "adj_factor" else sorted(set(task.get("periods", [])) | {r["period_end"] for r in rows})
                    if api in {*f.STATEMENTS, "fina_indicator"} else []) or [None]
        for period in expected:
            candidates = [r for r in rows if period is None or r.get("trade_date" if api == "adj_factor" else "period_end") == period]
            for name in task.get("gaps", []):
                observed = []
                for row in candidates:
                    value = (row.get("actual_announcement_date") if name == "f_ann_date" else
                             row.get("source_update_flag") if name == "update_flag" else row.get("fields", {}).get(name, {}).get("value"))
                    observed.append(dict(source_revision_id=row["source_revision_id"], source_locator=row["source_locator"],
                        source_task_id=row["source_task_id"], source_batch=row["source_batch"],
                        report_type=row.get("report_type"), announcement_date=row.get("announcement_date"),
                        value=value, unit=row.get("fields", {}).get(name, {}).get("unit"),
                        quarantined_identity=row.get("period_after_code_exit", False)))
                matched = None
                if api == "dividend":
                    matched = []
                    for event in task["matching_events"]:
                        matches = [r for r in candidates if dividend_matches(r, event)]
                        matched.append(dict(source_revision_id=event["source_revision_id"], matching_provider_versions=len(matches),
                                            matched_versions=[e.digest(e.encoded([r["source_revision_id"], r["source_task_id"], r["source_locator"]])) for r in matches],
                                            values=[r["fields"][name]["value"] for r in matches]))
                state = "source_observed_separate_version" if any(v["value"] is not None and not v["quarantined_identity"] for v in observed) else "unresolved_source_missing_or_null"
                if state == "source_observed_separate_version" and any(v.get("unit") == "provider_native_amount_unit_unverified" for v in observed):
                    state = "observed_native_amount_unit_unverified"
                assignments = [key for v in matched or [] for key in v["matched_versions"]]
                if matched and (not all(v["matching_provider_versions"] == 1 for v in matched) or len(assignments) != len(set(assignments))):
                    state = "unresolved_ambiguous_event_match"
                elif matched and not all(v["values"][0] is not None for v in matched):
                    state = "unresolved_matched_event_field_missing"
                if any(r["coverage"].get("row_cap_reached") for r in receipts):
                    state = "partial_source_truncation"
                if any(r["state"] not in {"complete", "partial_source", "empty_unknown"} for r in receipts):
                    state = "unresolved_leaf_request_failure_or_pending"
                yield dict(gap_group=identity, symbol=task["symbol"], api=api, period=period, field=name,
                                    reason=task["reason"], state=state, observations=observed, event_matches=matched,
                                    leaf_receipts=receipts, original_value_overwritten=False, historical_pit_certified=False)

def reconcile(plan, roots, output):
    """Write an atomic compressed field ledger with bounded memory and cleanup."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "field-reconciliation.jsonl.gz"
    retained = e.PRESERVE_TEMPORARIES.get()
    temporary = path.with_name(path.name + ("." + uuid4().hex if retained else "") + ".tmp")
    counts, total = Counter(), 0
    try:
        with temporary.open("wb") as stream:
            with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressed:
                for row in reconciliation_rows(plan, roots):
                    compressed.write(e.encoded(row))
                    counts[row["state"]] += 1
                    total += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if not retained:
            temporary.unlink(missing_ok=True)
    artifact = dict(path=path.name, sha256=e.digest(path.read_bytes()), bytes=path.stat().st_size)
    result = dict(schema="ad.tushare-field-reconciliation.v1", at=e.now(), plan_sha256=e.digest(e.encoded(plan)),
                  states=dict(counts), artifact=artifact, records=total, full_history_complete=False,
                  database_imported=False, factor_basis_merge_performed=False)
    e.atomic(output / "reconciliation-summary.json", e.encoded(result))
    return result
