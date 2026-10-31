"""Single-run all-market increments and optional scheduler configuration output."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import plistlib
import sys

from ashare_data.market.increment import DEFAULT_MINUTE_WINDOW, minute_window
from ashare_data.storage.daily_increment import initialize, IncrementStore


def parser():
    p = argparse.ArgumentParser(prog="ashare-data daily-update", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a separate updater root; no deployment changes")
    init.add_argument("--root", required=True)
    init.add_argument("--sdk-path", required=True)
    init.add_argument("--seed-directory")
    init.add_argument("--base-binding")
    init.add_argument("--minute-window", type=int, default=DEFAULT_MINUTE_WINDOW)
    init.add_argument("--interval", type=float, default=1)
    init.add_argument("--timeout", type=int, default=90)
    init.add_argument("--budget-gib", type=float, default=8)
    init.add_argument("--reserve-gib", type=float, default=50)
    init.add_argument("--ready-time", default="20:30", help="Asia/Shanghai; applies to --due")
    init.add_argument("--retry-window-days", type=int, default=10)
    init.add_argument("--daily-sources", nargs="+", choices=["baostock", "sohu"], default=["baostock", "sohu"])
    run = sub.add_parser("run", help="Run once; repeat the same command to recover")
    run.add_argument("--root", required=True)
    when = run.add_mutually_exclusive_group(required=True)
    when.add_argument("--date")
    when.add_argument("--due", action="store_true")
    run.add_argument("--phase", choices=["all", "prepare", "minute"], default="all")
    run.add_argument("--max-minute-jobs", type=int)
    run.add_argument("--retry-sources", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--root", required=True)
    status.add_argument("--date", required=True)
    schedule = sub.add_parser("schedule", help="Print a launchd configuration; never install or start it")
    schedule.add_argument("--root", required=True)
    schedule.add_argument("--interval-seconds", type=int, default=3600)
    candidate = sub.add_parser("candidate", help="Create an isolated standard release with pinned source rules")
    candidate.add_argument("--root", required=True)
    candidate.add_argument("--date", required=True)
    candidate.add_argument("--output", required=True)
    return p



def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command == "init":
        if args.interval < 1 or args.budget_gib <= 0 or args.reserve_gib < 0 or args.timeout <= 0 or not 1 <= args.retry_window_days <= 31:
            p.error("invalid resource/source limits")
        datetime.strptime(args.ready_time, "%H:%M")
        base = json.loads(Path(args.base_binding).read_bytes())["market_release"] if args.base_binding else None
        config = dict(sdk_path=str(Path(args.sdk_path).expanduser().resolve()),
                      seed_directory=str(Path(args.seed_directory).expanduser().resolve()) if args.seed_directory else None,
                      base_release=base, minute_window=minute_window(args.minute_window), interval=args.interval,
                      timeout=args.timeout, budget_bytes=int(args.budget_gib * 1024**3),
                      free_reserve_bytes=int(args.reserve_gib * 1024**3), ready_time=args.ready_time,
                      timezone="Asia/Shanghai", daily_sources=args.daily_sources,
                      retry_window_days=args.retry_window_days, minute_workers=2)
        print(json.dumps({"root": str(initialize(args.root, config)), "config": config}, ensure_ascii=False))
    elif args.command == "status":
        store = IncrementStore(args.root)
        try:
            print(json.dumps(store.report(args.date), ensure_ascii=False))
        finally:
            store.close()
    elif args.command == "schedule":
        if args.interval_seconds < 3600:
            p.error("scheduled invocation interval must be at least an hour")
        root = Path(args.root).expanduser().resolve()
        value = dict(Label="local.ashare-data.daily-update",
                     ProgramArguments=[sys.executable, "-B", "-m", "ashare_data.cli", "daily-update", "run", "--root", str(root), "--due"],
                     StartInterval=args.interval_seconds, RunAtLoad=False,
                     StandardOutPath=str(root / "scheduler.log"), StandardErrorPath=str(root / "scheduler-error.log"))
        print(plistlib.dumps(value).decode())
    elif args.command == "candidate":
        from ashare_data.storage.increment_release import build_candidate
        print(json.dumps(build_candidate(args.root, args.date, args.output), ensure_ascii=False))
    else:
        from ashare_data.market.daily_increment import run
        days = [args.date]
        capture_day = None
        if args.due:
            from ashare_data.market.increment_schedule import due_plan
            store = IncrementStore(args.root)
            try:
                with store.writer():
                    plan = due_plan(store)
                print(json.dumps({"event": "bounded_catchup_plan", **plan}), flush=True)
                days = plan["days"]
                capture_day = plan["latest_ready_day"]
            finally:
                store.close()
        for day in days:
            result = run(args.root, day, phase=args.phase, max_minute_jobs=args.max_minute_jobs,
                         retry_sources=args.retry_sources, capture_day=capture_day)
            if result["stopped"]:
                return 130
    return 0
