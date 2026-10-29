"""Bounded daily batches over explicit security lists, using existing checkpoints.

The list defines the delivered scope. Native snapshots are independent per
security/frequency; a failed or not-yet-attempted job cannot masquerade as fresh.
"""
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path

from ashare_data.financials.expansion import atomic, immutable
from ashare_data.providers.free_update_manual import checked_snapshot, write_snapshot
from ashare_data.providers.free_update_sina_minutes import parameters
from ashare_data.providers.free_updates import digest, encoded, implementation_identity, plan

ASSET_ROWS = {
    "stock": {8, 9, 20, 22, 23, 24, 48, 49, 60, 61, 62, 63, 111, 202, 203, 205, 210, 276, *range(12, 17)},
    "etf": {53, 276, *range(33, 38)},
    "index": {55, *range(43, 48)},
    "convertible_bond": {27, 38, 39, 40, 41, 42, 54},
}
ALLOWED_ROWS = {10}.union(*ASSET_ROWS.values())


def prepare(profile, *, as_of=None):
    path = Path(profile).resolve()
    config = json.loads(path.read_text())
    if config.get("schema") != "ad.security-update-batch.v1":
        raise ValueError("unsupported security batch profile")
    root = (path.parent / config["root"]).resolve()
    if any((root / name).exists() for name in ("CURRENT", "CURRENT.json", "releases")):
        raise ValueError("production root is forbidden")
    rows = config["rows"]
    if not rows or len(rows) != len(set(rows)) or not set(rows) <= ALLOWED_ROWS:
        raise ValueError("distinct reviewed security/calendar rows required")
    instruments = config.get("instruments", [])
    if len(instruments) > 10000 or not instruments and set(rows) != {10}:
        raise ValueError("explicit securities required, at most 10000 per frozen scope")
    seen, groups = set(), {kind: [] for kind in ASSET_ROWS}
    for instrument in instruments:
        symbol, kind = instrument["symbol"], instrument["asset_type"]
        if kind == "convertible_bond":
            import re
            if not re.fullmatch(r"(?:sh11|sz12)\d{4}", symbol):
                raise ValueError("explicit exchange-prefixed convertible bond identity required")
            if set(rows) & {38, 39, 40, 41, 42, 54}:
                from ashare_data.providers.free_update_tdx import parameters as native_parameters
                native_parameters("tdx_native_bond_bars", config.get("tdx", {}) | {
                    "symbol": symbol, "period": "1m", "count": 800})
            if not instrument.get("name") or not instrument.get("identity_evidence"):
                raise ValueError("bond identity requires name and source directory evidence")
        else:
            parameters(dict(symbol=symbol, asset_type=kind, period="1"))
        if (kind, symbol) in seen:
            raise ValueError("duplicate security in batch")
        seen.add((kind, symbol))
        if not set(rows) & ASSET_ROWS[kind]:
            raise ValueError("instrument has no requested data row")
        context = {kind+"_symbol": symbol, "stock_prefixed": symbol, "stock": symbol[2:]}
        if kind == "convertible_bond":
            context.update(bond_prefixed=symbol, bond=symbol[2:])
        if kind == "stock" and set(rows) & {23,24}:
            from ashare_data.providers.free_update_shareholders import request_identity
            report_date=instrument.get("report_date")
            if not isinstance(report_date,str):
                raise ValueError("shareholder batch requires each issuer's explicit report_date")
            request_identity({"symbol":symbol,"date":report_date})
            context["report_date"]=report_date
        if (kind == "convertible_bond" and set(rows) & {38, 39, 40, 41, 42, 54}
                or 205 in rows and kind == "stock" or 276 in rows and kind in {"stock", "etf"}):
            from ashare_data.providers.free_update_tdx import parameters as native_parameters
            settings = config.get("tdx", {})
            if kind == "stock" and 205 in rows:
                native_parameters("tdx_native_finance", settings | {"symbol": symbol})
            if 276 in rows and kind in {"stock", "etf"}:
                native_parameters("tdx_native_depth", settings | {"symbol":symbol,"asset_type":kind})
                context.update(quote_symbol=symbol, quote_asset_type=kind)
            context.update({"tdx_"+k: v for k, v in settings.items()})
            context["bond_prefixed"] = symbol
            context["bond"] = symbol[2:]
        groups[kind].append(context)
    budget = config.get("max_jobs", 20)
    timeout = config.get("timeout", 75)
    attempts = config.get("attempts_per_day", 2)
    if (not isinstance(budget, int) or isinstance(budget, bool) or not 1 <= budget <= 120
            or not 1 <= timeout <= 75 or not 1 <= attempts <= 2):
        raise ValueError("batch limits exceed 120 jobs, 75 seconds/job or 2 attempts/day")
    selections, jobs, prepared = {}, {}, None
    for kind, contexts in groups.items():
        selected = set(rows) & ASSET_ROWS[kind]
        if selected and not contexts:
            unique = selected - set().union(*(v for k,v in ASSET_ROWS.items() if k != kind))
            if unique:
                raise ValueError("selected asset rows have no identities")
            continue
        if not selected:
            continue
        prepared = plan(rows=sorted(selected), context=contexts, as_of=as_of,
                        lookback_days=config.get("lookback_days", 31))
        for item in prepared["items"]:
            row=item["excel_row"]
            if row in selections:
                item["job_ids"]=sorted(set(item["job_ids"]) | set(selections[row]["job_ids"]))
                item["missing_context"]=sorted(set(item["missing_context"]) | set(selections[row]["missing_context"]))
            selections[row]=item
        jobs.update({j["job_id"]: j for j in prepared["jobs"]})
    if 10 in rows:
        prepared = plan(rows=[10], as_of=as_of)
        selections[10] = prepared["items"][0]
        jobs.update({j["job_id"]: j for j in prepared["jobs"]})
    if set(selections) != set(rows) or any(i["missing_context"] for i in selections.values()):
        raise ValueError("incomplete batch context")
    prepared.update(items=[selections[r] for r in sorted(rows)], jobs=list(jobs.values()),
                    scope="explicit_configured_security_list")
    limits = dict(max_jobs=budget, timeout=timeout, workers=1, attempts_per_day=attempts)
    return root, config, prepared, limits


def snapshot_scope(job):
    """Date windows select rows; they must not fork the security's history chain."""
    return {k: job[k] for k in ("function", "mode", "timezone", "implementation_sha256")} | {
        "parameters": {k: v for k, v in job["parameters"].items()
                       if k not in {"start_date", "end_date", "start_time", "end_time"}}}


def run(profile, *, execute=False, as_of=None, max_jobs=None, progress=None):
    from ashare_data.storage.free_updates import collect, writer
    root, config, prepared, limits = prepare(profile, as_of=as_of)
    if max_jobs is not None:
        if not isinstance(max_jobs, int) or isinstance(max_jobs, bool) or not 1 <= max_jobs <= limits["max_jobs"]:
            raise ValueError("override can only reduce the configured request budget")
        limits["max_jobs"] = max_jobs
    summary = {"schema": "ad.security-update-batch-result.v1", "as_of": prepared["as_of"],
        "root": str(root), "rows": sorted(config["rows"]), "instruments": config.get("instruments", []),
        "planned_jobs": len(prepared["jobs"]), "planned_menu_scopes": sum(len(j["menu_rows"]) for j in prepared["jobs"]),
        "limits": limits, "dry_run": not execute, "timer_installed": False, "formal_published": False,
        "implementation_sha256": implementation_identity()}
    if not execute:
        return summary
    with writer(root):
        pass
    with (root / "batch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("security batch already running") from None
        try:
            immutable(root / "batch-profile.json", encoded(config))
            queue = root / "batch-plans" / (prepared["as_of"]+"-"+digest(prepared)+".json")
            queue.parent.mkdir(exist_ok=True)
            immutable(queue, encoded(prepared))
            results = collect(root, prepared, progress=progress, **limits)
            summary.update(plan=str(queue), attempted=sum("run_id" in r for r in results),
                resumed_without_download=sum(r["status"] == "already_received" for r in results),
                stop_requested=(root / "STOP").exists())
            checks, scopes = {}, []
            with writer(root) as db:
                for job in prepared["jobs"]:
                    prior = db.execute("SELECT status FROM runs WHERE job_id=? AND as_of=? ORDER BY started_at DESC LIMIT 1",
                                       (job["job_id"], prepared["as_of"])).fetchone()
                    if not prior:
                        scopes.extend({"excel_row": row, "job_id": job["job_id"], "parameters": job["parameters"],
                                       "status": "未尝试"} for row in job["menu_rows"])
                        continue
                    try:
                        records, check = checked_snapshot(root, db, job, prepared["as_of"])
                        saved_scopes = []
                        for row in job["menu_rows"]:
                            saved_scopes.append(write_snapshot(root, row, snapshot_scope(job), records, check) | {
                                "job_id": job["job_id"], "parameters": job["parameters"]})
                        checks[job["job_id"]] = check
                        scopes.extend(saved_scopes)
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        scopes.extend({"excel_row": row, "job_id": job["job_id"], "parameters": job["parameters"],
                                       "status": "未完成", "error": str(exc), "old_snapshot_exported_as_new": False}
                                      for row in job["menu_rows"])
            summary.update(items=scopes, ready_jobs=len(checks), pending_jobs=len(prepared["jobs"])-len(checks),
                effective_records=sum(c["records"] for c in checks.values()),
                ready_menu_scopes=sum(s["status"] == "可读取" for s in scopes),
                all_requested_jobs_received=len(checks) == len(prepared["jobs"]),
                finished_at=datetime.now(timezone.utc).isoformat(),
                ready_menu_rows=[r for r in sorted(config["rows"]) if all(s["status"] == "可读取" for s in scopes if s["excel_row"] == r)])
            target = root / "batch-results" / (prepared["as_of"]+"-"+digest(summary)+".json")
            immutable(target, encoded(summary))
            atomic(root / "batch-latest.json", encoded(summary | {"result_path": str(target)}))
            return summary | {"result_path": str(target)}
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
