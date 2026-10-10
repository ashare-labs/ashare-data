"""Use only explicitly supplied private originals; no download or Store is required."""

import argparse
import json
from pathlib import Path

from ashare_data import reconcile_sina_day


def main():
    p = argparse.ArgumentParser()
    p.add_argument("security")
    p.add_argument("date")
    p.add_argument("one_minute", type=Path)
    p.add_argument("five_minute", type=Path)
    p.add_argument("quote", type=Path)
    args = p.parse_args()
    report = reconcile_sina_day(
        args.security,
        args.date,
        minute_body=args.one_minute.read_bytes(),
        five_minute_body=args.five_minute.read_bytes(),
        quote_body=args.quote.read_bytes(),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2  # Not a grant of market-data completeness or execution.


if __name__ == "__main__":
    raise SystemExit(main())
