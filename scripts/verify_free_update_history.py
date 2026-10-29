"""Preview or run a bounded, resumable live macro-history coverage trial."""
import argparse
import json
from pathlib import Path

from ashare_data.providers.free_update_coverage import recipe, run_trial


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--rows",type=int,nargs="+",required=True)
    parser.add_argument("--as-of",required=True)
    parser.add_argument("--seed-end",required=True)
    parser.add_argument("--delta-start",required=True)
    parser.add_argument("--page-size",type=int,default=100)
    parser.add_argument("--timeout",type=int,default=75)
    parser.add_argument("--execute",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    config=recipe(args.rows,as_of=args.as_of,seed_end=args.seed_end,delta_start=args.delta_start,page_size=args.page_size)
    result=run_trial(args.root,config,execute=args.execute,timeout=args.timeout,
        progress=lambda value:print(json.dumps({k:value[k] for k in ("stage","run_id","status","function","menu_rows","returned","inserted","error") if k in value},ensure_ascii=False),flush=True))
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:v for k,v in result.items() if k!="items"},ensure_ascii=False))
    return 0 if result.get("dry_run") or result.get("coverage_verified_count") == len(args.rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
