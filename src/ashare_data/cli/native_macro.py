"""Prepare native macro candidates and query pinned releases; no publisher CLI."""

import argparse
import json
from pathlib import Path

from ashare_data.core.model import DataError
from ashare_data.storage.market_release import MarketRelease
from ashare_data.storage.native_macro_release import prepare, checked_bundle, current_parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--parent", type=int, required=True)
    p.add_argument("--parent-sha", required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    for name in ("query", "point"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, required=True)
        p.add_argument("--release", type=int, required=True)
        p.add_argument("--manifest-sha", required=True)
        p.add_argument("--row", type=int, required=True)
        p.add_argument("--received-by")
        if name == "query":
            p.add_argument("--start", required=True)
            p.add_argument("--end", required=True)
            p.add_argument("--history", action="store_true")
            p.add_argument("--limit", type=int, default=10000)
        else:
            p.add_argument("--field", required=True)
            p.add_argument("--period", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            parent = MarketRelease(args.root, args.parent, expected_manifest_sha256=args.parent_sha)
            if args.execute:
                value = prepare(parent, args.bundle, args.output)
            else:
                binding = current_parent(parent)
                manifest, rows, _, _ = checked_bundle(args.bundle)
                value = {
                    "dry_run": True,
                    "parent": binding,
                    "bundle_id": manifest["bundle_id"],
                    "input_rows": len(rows),
                    "output": str(args.output),
                    "production_write_attempted": False,
                }
        else:
            reader = MarketRelease(
                args.root, args.release, expected_manifest_sha256=args.manifest_sha
            ).native_macro()
            if args.command == "query":
                value = reader.records(
                    args.row,
                    args.start,
                    args.end,
                    received_by=args.received_by,
                    history=args.history,
                    limit=args.limit,
                )
            else:
                value = reader.point(
                    args.row, args.field, args.period, received_by=args.received_by
                )
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except (DataError, ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
