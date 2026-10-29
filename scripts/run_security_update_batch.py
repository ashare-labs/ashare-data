"""Run one bounded portion of a configured security list; repeat to resume."""
import argparse
import json

from ashare_data.providers.free_update_batch import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--max-jobs", type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    result = run(args.profile, execute=args.execute, max_jobs=args.max_jobs,
                 progress=lambda r: print(json.dumps(r, ensure_ascii=False), flush=True))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not args.execute or result["all_requested_jobs_received"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
