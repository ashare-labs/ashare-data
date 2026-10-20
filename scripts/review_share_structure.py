"""Build or read an offline, evidence-bound resolution of negative share cells."""

import argparse
import json

from ashare_data.financials.share_resolution import ShareResolutionReader, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("prepare")
    build.add_argument("--root", required=True)
    build.add_argument("--structure-batch", required=True)
    build.add_argument("--float-batch", required=True)
    read = commands.add_parser("read")
    read.add_argument("--root", required=True)
    read.add_argument("--security", required=True)
    read.add_argument("--date", required=True)
    read.add_argument("--as-of")
    read.add_argument("--purpose", choices=["source_research", "listed_security", "historical_pit", "source_audit"], default="source_research")
    args = parser.parse_args()
    result = (prepare(args.root, args.structure_batch, args.float_batch) if args.command == "prepare" else
              ShareResolutionReader(args.root).read(args.security, args.date, purpose=args.purpose, as_of=args.as_of))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
