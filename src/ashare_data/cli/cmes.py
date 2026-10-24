"""Offline CMES candidate import and standard release queries."""

import argparse
import csv
import json
from zipfile import BadZipFile

from ashare_data.core.model import DataError
from ashare_data.providers.cmes import BAR_PROFILES, normalize_security
from ashare_data.storage.cmes_release import import_cmes
from ashare_data.storage.market_release import MarketRelease


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("import", help="Build an isolated CMES candidate; no deployment changes")
    build.add_argument("--root", required=True)
    for name in ("daily", "minute", "calendar", "st"):
        build.add_argument("--" + name, nargs="+", default=[])
    build.add_argument("--minute-profile", choices=[p for p in BAR_PROFILES if p.startswith("minute-")])
    build.add_argument("--daily-profile", choices=[p for p in BAR_PROFILES if p.startswith("daily-")],
                       default="daily-lots-kcny")
    build.add_argument("--security", nargs="+")
    build.add_argument("--encoding", choices=["utf-8-sig", "gb18030"], default="utf-8-sig")
    build.add_argument("--free-reserve-gib", type=float, default=1)
    query = sub.add_parser("query", help="Read the exact standard MarketRelease, without fallback")
    query.add_argument("--root", required=True)
    query.add_argument("--release-id", type=int)
    query.add_argument("--manifest-sha256")
    query.add_argument("--security", required=True)
    query.add_argument("--table", choices=["bars_1d", "bars_1m"], default="bars_1d")
    query.add_argument("--start", required=True)
    query.add_argument("--end", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            result = import_cmes(
                args.root, daily=args.daily, daily_profile=args.daily_profile,
                minute=args.minute, minute_profile=args.minute_profile,
                calendars=args.calendar, st=args.st, securities=args.security, encoding=args.encoding,
                free_reserve_bytes=int(args.free_reserve_gib * 1024**3),
            )
        else:
            release = MarketRelease(args.root, args.release_id,
                                    expected_manifest_sha256=args.manifest_sha256)
            result = dict(release=release.receipt(), rows=release.read(
                args.table, normalize_security(args.security), args.start, args.end,
            ))
        print(json.dumps(result, ensure_ascii=False, default=str, allow_nan=False))
        return 0
    except (DataError, OSError, ValueError, csv.Error, BadZipFile) as error:
        result = error.as_dict() if isinstance(error, DataError) else {"code": "CMES_INPUT", "message": str(error)}
        print(json.dumps({"error": result}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
