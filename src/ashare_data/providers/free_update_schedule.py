"""One bounded scheduling iteration; creating a timer or publishing is separate.

The default is a read-only preview. Contexts and menu rows are explicit, successful
jobs resume from the observation store, and missed dates widen the overlap window.
"""
from datetime import date
import json
from pathlib import Path

from ashare_data.providers.free_updates import plan


def scheduled_plan(config_path, *, as_of=None):
    from ashare_data.storage.free_updates import connect
    path = Path(config_path).resolve()
    config = json.loads(path.read_text())
    if config.get("schema") != "ad.free-update-schedule.v1":
        raise ValueError("unsupported free-update schedule config")
    if not isinstance(config.get("rows"), list) or not config["rows"]:
        raise ValueError("schedule requires explicit nonempty menu rows")
    root = Path(config["root"])
    root = (path.parent/root).resolve() if not root.is_absolute() else root.resolve()
    if root.exists() and any(root.iterdir()):
        with connect(root, readonly=True) as db:
            previous = db.execute("""SELECT mr.excel_row,MAX(r.as_of) AS last_day
                FROM menu_runs mr JOIN runs r USING(run_id)
                WHERE r.status='received' GROUP BY mr.excel_row""").fetchall()
    else:
        previous = []
    lookback = config.get("lookback_days", 14)
    prepared = plan(rows=config["rows"], context=config.get("context"), as_of=as_of,
                    lookback_days=lookback)
    selected = set(config["rows"])
    days = [r["last_day"] for r in previous if r["excel_row"] in selected]
    if days:
        gap = (date.fromisoformat(prepared["as_of"])-date.fromisoformat(min(days))).days
        lookback = max(lookback, gap+config.get("overlap_days", 7))
        if lookback > 366:
            raise ValueError("resume gap exceeds 366 days; use explicit dated backfill batches first")
        prepared = plan(rows=config["rows"], context=config.get("context"), as_of=as_of,
                        lookback_days=lookback)
    options = {"timeout": config.get("timeout", 60), "max_jobs": config.get("max_jobs", 20),
               "workers": config.get("workers", 2), "attempts_per_day": config.get("attempts_per_day", 2)}
    if not 1 <= options["timeout"] <= 300 or not 1 <= options["workers"] <= 3:
        raise ValueError("invalid schedule timeout or workers")
    if not 1 <= options["attempts_per_day"] <= 3 or options["max_jobs"] < 1:
        raise ValueError("invalid schedule attempt budget")
    missing = [{"excel_row": i["excel_row"], "fields": i["missing_context"]}
               for i in prepared["items"] if i["missing_context"]]
    discontinued = {i["excel_row"] for i in prepared["items"]
                    if i.get("source_lifecycle", {}).get("status") == "discontinued"}
    prepared["jobs"] = [j for j in prepared["jobs"] if set(j["menu_rows"])-discontinued]
    summary = {"as_of": prepared["as_of"], "root": str(root), "menu_rows": len(prepared["items"]),
               "jobs": len(prepared["jobs"]), "missing_context": missing,
               "lookback_days": lookback, "limits": options,
               "production_publication": False, "timer_installed": False,
               "discontinued_history_only_rows": sorted(discontinued),
               "scope": "explicit contexts; no full-universe coverage claim"}
    return prepared, summary


def run_scheduled(config_path, *, execute=False, as_of=None, progress=None):
    from ashare_data.storage.free_updates import collect
    prepared, summary = scheduled_plan(config_path, as_of=as_of)
    summary["dry_run"] = not execute
    if execute:
        if summary["missing_context"]:
            raise ValueError("schedule is missing explicit sample/universe contexts")
        results = collect(summary["root"], prepared, progress=progress, **summary["limits"])
        summary["attempted"] = sum("run_id" in r for r in results)
        summary["resumed_without_download"] = sum(r["status"] == "already_received" for r in results)
        summary["all_requested_jobs_received"] = bool(prepared["jobs"]) and len(results)==len(prepared["jobs"]) and all(r["status"] in {"received","already_received"} for r in results)
        config=json.loads(Path(config_path).read_text())
        if config.get("prepare_handoff") and summary["all_requested_jobs_received"]:
            from ashare_data.providers.free_update_handoff import prepare_bundle
            summary["handoff"]=prepare_bundle(summary["root"],config["rows"],previous=config.get("previous_bundle"),execute=True,resume_latest=True)
    return summary
