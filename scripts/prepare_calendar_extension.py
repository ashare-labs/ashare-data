"""Prepare a bounded calendar correction from retained evidence; no network requests."""

import argparse
import json
from pathlib import Path

from ashare_data.storage.calendar_release import prepare
from ashare_data.storage.market_release import MarketRelease


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-root", required=True, type=Path)
    parser.add_argument("--release-id", required=True, type=int)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--end", required=True)
    parser.add_argument("--sina-raw", required=True, type=Path)
    parser.add_argument("--sina-response-id", required=True)
    parser.add_argument("--szse-root", required=True, type=Path)
    args = parser.parse_args()
    parent = MarketRelease(args.market_root, args.release_id,
                           expected_manifest_sha256=args.manifest_sha256)
    value = prepare(parent, args.output, end=args.end, sina_raw=args.sina_raw,
                    sina_response_id=args.sina_response_id, szse_root=args.szse_root)
    print(json.dumps({"plan": str(args.output / "plan.json"),
                      "parent_release_id": parent.release_id,
                      "added_sessions": value["added_sessions"],
                      "mapping_sha256": value["mapping_sha256"],
                      "new_network_requests": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
