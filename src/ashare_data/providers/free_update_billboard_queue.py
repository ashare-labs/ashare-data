"""Resume broker details from the already collected daily billboard directory."""
from datetime import date
import fcntl
import json
from pathlib import Path

from ashare_data.providers.free_update_coverage import require
from ashare_data.providers.free_update_derived import source_run
from ashare_data.providers.free_update_manual import checked_snapshot, write_snapshot
from ashare_data.providers.free_updates import catalog, digest, encoded, implementation_identity, source_date, source_families
from ashare_data.storage.free_updates import collect, writer
from ashare_data.storage.immutable import immutable_write


def detail_plan(source, as_of):
    run, source_job, result = source
    require(source_job["parameters"]["dataset"] == "billboard", "wrong event directory")
    require(run["as_of"] >= as_of, "daily directory has not run for the requested day")
    pairs = set()
    for record in result["records"]:
        day = source_date(record.get("TRADE_DATE"))
        code = str(record.get("SECURITY_CODE", ""))
        require(day and day <= as_of and len(code) == 6 and code.isdigit(),
                "billboard directory security/date invalid")
        pairs.add((day, code))
    require(pairs, "daily directory has no published security identities")
    latest = max(day for day, _ in pairs)
    symbols = sorted(code for day, code in pairs if day == latest)
    require(len(symbols) <= 10000, "billboard identity list exceeds reviewed scope")
    item = next(i for i in catalog()["items"] if i["excel_row"] == 67)
    jobs = []

    def append(function, parameters):
        job = dict(function=function, parameters=parameters, mode=item["mode"],
                   timezone=item["timezone"], source_families=source_families(function),
                   implementation_sha256=implementation_identity())
        jobs.append(dict(job, job_id=digest(job), menu_rows=[67]))

    day = latest.replace("-", "")
    append("stock_lhb_jgmmtj_em", {"start_date": day, "end_date": day})
    for symbol in symbols:
        for side in ("买入", "卖出"):
            append("stock_lhb_stock_detail_em", {"symbol": symbol, "date": day, "flag": side})
    return dict(schema="ad.cmes-free-plan.v1", as_of=as_of, items=[item], jobs=jobs,
                source_run_id=run["run_id"], source_observed_at=run["finished_at"],
                source_trade_date=latest, securities=symbols, scope="all securities in latest published daily directory")


def run_billboard_queue(profile, *, as_of, execute=False):
    config = json.loads(Path(profile).read_text())
    require(config.get("schema") == "ad.billboard-queue.v1" and config["rows"] == [67],
            "reviewed daily billboard profile required")
    date.fromisoformat(as_of)
    root = Path(config["root"]).resolve()
    summary = dict(dry_run=not execute, rows=[67], attempted=0,
                   all_requested_jobs_received=False, formal_published=False)
    if not execute:
        return summary
    with writer(root):
        pass
    with (root / "queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        folder = root / "daily-queues"
        folder.mkdir(exist_ok=True)
        queue = folder / (as_of + ".json")
        if queue.exists():
            prepared = json.loads(queue.read_text())
            require(all(j["implementation_sha256"] == implementation_identity() for j in prepared["jobs"]),
                    "frozen billboard queue implementation changed")
        else:
            source = source_run(config["source_root"], "security_events_em")
            if source is None or source[0]["as_of"] < as_of:
                return summary | {"deferred": True, "reason": "daily billboard directory pending"}
            prepared = detail_plan(source, as_of)
            # Weekend or publication lag: reuse a completed queue for the same
            # actual trading date. A different observation day is not new data.
            for previous in sorted(folder.glob('*.json'), reverse=True):
                prior = json.loads(previous.read_text())
                if prior.get('source_trade_date') != prepared['source_trade_date']:
                    continue
                reused = []
                with writer(root) as db:
                    for job in prior['jobs']:
                        try:
                            records, check = checked_snapshot(root, db, job, prior['as_of'])
                            reused.append(write_snapshot(root, 67, job, records, check))
                        except (ValueError, OSError, KeyError, TypeError):
                            break
                if len(reused) == len(prior['jobs']):
                    return summary | dict(all_requested_jobs_received=True, items=reused,
                        source_trade_date=prior['source_trade_date'], reused_queue=str(previous),
                        reason='same published trading date already complete')
                prepared = prior
                break
            immutable_write(queue, encoded(prepared))
        # Retain old unfinished dates, but do not let a failed job that has
        # exhausted today's attempt prevent collecting the next day's queue.
        seen, results = set(), []
        selected = prepared
        for path in sorted(folder.glob('*.json')):
            candidate = json.loads(path.read_text())
            identity = digest(candidate)
            if candidate['as_of'] > as_of or identity in seen:
                continue
            seen.add(identity)
            attempts = collect(root, candidate, timeout=75, workers=1, max_jobs=1, attempts_per_day=1)
            if any('run_id' in r for r in attempts):
                selected, results = candidate, attempts
                break
        active_as_of = selected['as_of']
        items = []
        with writer(root) as db:
            for job in selected["jobs"]:
                try:
                    records, check = checked_snapshot(root, db, job, active_as_of)
                    items.append(write_snapshot(root, 67, job, records, check))
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    items.append(dict(job_id=job["job_id"], status="未完成", error=str(exc)))
        complete = selected == prepared and all(i["status"] == "可读取" for i in items)
        attempted = sum("run_id" in r for r in results)
        return summary | dict(attempted=attempted, all_requested_jobs_received=complete,
            deferred=not complete and attempted == 0, items=items,
            jobs=len(selected["jobs"]), securities=len(selected["securities"]),
            source_trade_date=selected["source_trade_date"], source_run_id=selected["source_run_id"],
            scope=selected["scope"], queue=str(queue), active_queue_as_of=active_as_of)
