"""Plan, collect, and inspect the frozen CMES free-source acceptance menu."""
import argparse
import csv
import json
from pathlib import Path

from ashare_data.providers.free_updates import catalog, plan
from ashare_data.storage.free_updates import collect, query, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog")
    p = sub.add_parser("run-manual", help="Collect reviewed source windows and export each successful row")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--as-of")
    p.add_argument("--execute", action="store_true")
    p = sub.add_parser("manual-query", help="Read a pinned native snapshot with stable pagination")
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("prepare-handoff",help="Prepare a verified bundle inside staging; never publish production")
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--rows",type=int,nargs="+",required=True)
    p.add_argument("--previous")
    p.add_argument("--execute",action="store_true")
    p = sub.add_parser("preview-intake",help="Read-only review of a bundle and existing writer coordination")
    p.add_argument("--bundle",type=Path,required=True)
    p.add_argument("--coordination",type=Path,required=True)
    p = sub.add_parser("prepare-queue", help="Preview or freeze an explicit-universe resumable queue")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--as-of")
    p.add_argument("--save", action="store_true")
    p = sub.add_parser("run-queue", help="Preview or resume a frozen queue; installs no timer")
    p.add_argument("--queue", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    p = sub.add_parser("run-scheduled", help="Preview or execute one bounded iteration; installs no timer")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--as-of")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    for name in ("plan", "collect"):
        p = sub.add_parser(name)
        p.add_argument("--rows", type=int, nargs="+")
        p.add_argument("--context", type=Path, help="Explicit symbol/board context JSON; not all-market coverage")
        p.add_argument("--as-of")
        p.add_argument("--lookback-days", type=int, default=14)
        p.add_argument("--source-start", help="Exact statistical date bound for reviewed macro histories")
        p.add_argument("--source-end", help="Exact statistical date bound for reviewed macro histories")
        p.add_argument("--page-size", type=int, help="Reviewed macro histories only; bounded source page size")
        if name == "collect":
            p.add_argument("--root", type=Path, required=True)
            p.add_argument("--timeout", type=int, default=90)
            p.add_argument("--max-jobs", type=int)
            p.add_argument("--workers", type=int, default=1, choices=[1,2,3])
            p.add_argument("--attempts-per-day",type=int,default=2,choices=[1,2,3])
            p.add_argument("--force", action="store_true", help="Repeat a completed job to verify idempotence")
    p = sub.add_parser("query")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--row", type=int, required=True)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--as-of", help="UTC received-by cutoff; never a historical announcement assumption")
    p.add_argument("--include-superseded", action="store_true", help="Audit known superseded projections; excluded by default")
    p = sub.add_parser("report")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, help="Optional JSON evidence report")
    p.add_argument("--csv", type=Path, help="Original Excel rows plus acceptance status")
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            result = catalog()
        elif args.command == "run-manual":
            from ashare_data.providers.free_updates.free_update_manual import run_manual
            result = run_manual(args.config, execute=args.execute, as_of=args.as_of,
                progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
        elif args.command == "manual-query":
            from ashare_data.providers.free_updates.free_update_manual import query_snapshot
            result = query_snapshot(args.snapshot, offset=args.offset, limit=args.limit)
        elif args.command == "prepare-handoff":
            from ashare_data.providers.free_updates.free_update_handoff import prepare_bundle
            result=prepare_bundle(args.root,args.rows,previous=args.previous,execute=args.execute)
        elif args.command == "preview-intake":
            from ashare_data.providers.free_updates.free_update_handoff import intake_preview
            result=intake_preview(args.bundle,args.coordination)
        elif args.command == "prepare-queue":
            from ashare_data.providers.free_updates.free_update_queue import prepare, save
            root, queue = prepare(args.config, as_of=args.as_of)
            result = {"queue_id":queue["queue_id"], "root":str(root), "jobs":len(queue["plan"]["jobs"]),
                      "as_of":queue["plan"]["as_of"], "limits":queue["limits"], "universe":queue["universe"], "saved":False}
            if args.save:
                result.update(saved=True, path=str(save(args.config, as_of=args.as_of)))
        elif args.command == "run-queue":
            from ashare_data.providers.free_updates.free_update_queue import run
            result = run(args.queue, execute=args.execute,
                progress=lambda value: print(json.dumps(value,ensure_ascii=False),flush=True))
        elif args.command == "run-scheduled":
            from ashare_data.providers.free_updates.free_update_schedule import run_scheduled
            result = run_scheduled(args.config, execute=args.execute, as_of=args.as_of,
                progress=lambda value: print(json.dumps(value,ensure_ascii=False),flush=True))
        elif args.command in {"plan", "collect"}:
            context = json.loads(args.context.read_text()) if args.context else None
            window={k:v for k,v in {"start_date":args.source_start,"end_date":args.source_end,"page_size":args.page_size}.items() if v is not None}
            result = plan(rows=args.rows,context=context,as_of=args.as_of,lookback_days=args.lookback_days,source_window=window or None)
            if args.command == "collect":
                def progress(item):
                    print(json.dumps(item,ensure_ascii=False),flush=True)
                collect(args.root,result,timeout=args.timeout,max_jobs=args.max_jobs,
                        force=args.force,progress=progress,workers=args.workers,attempts_per_day=args.attempts_per_day)
                evidence=report(args.root)
                result={k:v for k,v in evidence.items() if k!="items"}
        elif args.command == "query":
            result = query(args.root,args.row,limit=args.limit,as_of=args.as_of,include_superseded=args.include_superseded)
        else:
            result = report(args.root)
            if args.output:
                args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
            if args.csv:
                with args.csv.open("w",encoding="utf-8-sig",newline="") as out:
                    fields=["excel_row","category","item","candidate_status","candidate_source","note","source_url","status","missing_context"]
                    writer=csv.DictWriter(out,fieldnames=fields,extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(result["items"])
            result={k:v for k,v in result.items() if k!="items"}
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        if args.command in {"run-scheduled", "run-manual"} and args.execute and not result.get("all_requested_jobs_received",False):
            return 2
        return 0
    except (ValueError,OSError,RuntimeError) as exc:
        print(json.dumps({"error":str(exc)},ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
