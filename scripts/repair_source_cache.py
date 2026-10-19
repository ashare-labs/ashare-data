"""Recover named failed source caches without network or live batch mutation."""

import argparse
import json

from ashare_data.financials.source_repair import apply_idle, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("prepare")
    build.add_argument("--root", required=True)
    build.add_argument("--financial-batch", required=True)
    build.add_argument("--official-batch", required=True)
    build.add_argument("--financial-task", action="append", default=[])
    build.add_argument("--official-symbol", action="append", default=[])
    adopt = sub.add_parser("apply-idle")
    adopt.add_argument("--root", required=True)
    adopt.add_argument("--source", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.root, args.financial_batch, args.official_batch, args.financial_task, args.official_symbol)
    else:
        result = apply_idle(args.root, args.source)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
