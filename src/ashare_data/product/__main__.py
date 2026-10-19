"""Offline catalogue, price queries and bounded publication from the shared manifest."""

import argparse
import json
import sys

from ashare_data.core.model import DataError
from ashare_data.product import LocalDailyProduct, LocalMinuteProduct
from ashare_data.product.configuration import resolve
from ashare_data.product.daily import SOURCES


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment")
    parser.add_argument("--profile", default="product")
    parser.add_argument("--root", help="Explicit existing local-daily root overrides the manifest")
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("minute-sources", help="List registered minute sources without reading bars")
    minute = commands.add_parser("minute", help="Read a registered minute source; default remains TDX")
    minute.add_argument("--source", default="tdx")
    minute.add_argument("--security", required=True)
    minute.add_argument("--start", required=True)
    minute.add_argument("--end", required=True)
    catalogue = commands.add_parser("catalogue")
    catalogue.add_argument("--query", default="")
    catalogue.add_argument("--limit", type=int, default=100)
    catalogue.add_argument("--offset", type=int, default=0)
    for name in ("prices", "coverage", "publish"):
        sub = commands.add_parser(name)
        sub.add_argument("--security", action="append", required=True)
        sub.add_argument("--start", required=True)
        sub.add_argument("--end", required=True)
        sub.add_argument("--source", choices=SOURCES, default="auto")
        if name in {"coverage", "publish"}:
            sub.add_argument(
                "--reference-set", help="Explicit configured reference set; no fallback"
            )
        if name == "publish":
            sub.add_argument("--store", help="Output Store root; otherwise use the manifest asset")
            sub.add_argument(
                "--mode",
                choices=("covered_research", "price_only_no_limits"),
                default="covered_research",
            )
    args = parser.parse_args(argv)
    try:
        if args.operation in {"minute", "minute-sources"}:
            if args.root:
                raise DataError("PRODUCT_MINUTE_CONFIGURATION", "Use a deployment for minute sources")
            owner = LocalMinuteProduct(args.deployment, profile=args.profile)
            result = owner.sources() if args.operation == "minute-sources" else owner.prices(
                args.security, args.start, args.end, source=args.source
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
            return 0
        configuration = (
            None
            if args.root and not args.deployment
            else resolve(
                args.deployment,
                profile=args.profile,
                reference_set=getattr(args, "reference_set", None),
            )
        )
        product = LocalDailyProduct(
            args.root or configuration["daily_root"],
            references=configuration["references"] if configuration else None,
        )
        if args.operation == "catalogue":
            result = product.catalogue(args.query, args.limit, args.offset)
        elif args.operation == "prices":
            result = product.prices(args.security, args.start, args.end, args.source)
        elif args.operation == "coverage":
            result = product.coverage(args.security, args.start, args.end, args.source)
        else:
            if not args.store and configuration is None:
                configuration = resolve(args.deployment, profile=args.profile)
            result = product.publish(
                args.store or configuration["release_store_root"],
                args.security,
                args.start,
                args.end,
                args.source,
                mode=args.mode,
                references=configuration["references"] if configuration else None,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except DataError as exc:
        print(
            json.dumps({"error": exc.as_dict()}, ensure_ascii=False, allow_nan=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
