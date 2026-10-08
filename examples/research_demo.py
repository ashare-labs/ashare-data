"""Bounded real-data round trip using public APIs; no bundled private samples.

Offline: python examples/research_demo.py --store ./research --paths raw1.json raw2.json --calendar calendar.json
Explicit network: python examples/research_demo.py --store ./research --fetch 600000.XSHG --count 2
"""

import argparse
import hashlib
import json
from pathlib import Path

from ashare_data import DataError, Store
from ashare_data.cli import json_value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--paths", nargs="+")
    inputs.add_argument("--fetch", nargs="+")
    parser.add_argument("--calendar")
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--start", default="2019-12-30")
    parser.add_argument("--end", default="2020-01-07")
    parser.add_argument("--security", nargs="+", default=["600000.XSHG", "000001.XSHE"])
    args = parser.parse_args()
    store = Store.init(args.store)
    sources = (args.paths or []) + ([args.calendar] if args.calendar else [])
    before = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources}
    if args.fetch:
        sid = store.fetch_price(args.fetch, count=args.count)
    else:
        sid = store.import_research(args.paths, calendar_path=args.calendar)
        assert sid == store.import_research(args.paths, calendar_path=args.calendar)
    # Release the original Store; all reads through the documented reopening API.
    view = Store(args.store).research(sid)
    result = (
        view.get_price(args.fetch, count=args.count)
        if args.fetch
        else view.get_price(
            args.security,
            start_date=args.start,
            end_date=args.end,
            fields=["open", "close", "high", "low", "volume", "money"],
        )
    )
    refusals = {}
    for flag in ("require_final", "require_complete", "require_fresh", "require_tradable"):
        try:
            view.get_price((args.fetch or args.security)[0], count=1, **{flag: True})
        except DataError as exc:
            refusals[flag] = exc.as_dict()
        else:
            raise AssertionError("unexpected strong admission: " + flag)
    assert before == {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources}
    print(
        json.dumps(
            {
                "descriptor": view.descriptor(),
                "result": result.to_dict(),
                "source_hashes_unchanged": before,
                "strict_refusals": refusals,
            },
            ensure_ascii=False,
            indent=2,
            default=json_value,
        )
    )


if __name__ == "__main__":
    main()
