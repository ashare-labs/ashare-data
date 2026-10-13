"""Query a verified official report archive; dates mean Shanghai start-of-day."""

import argparse
import json

from ashare_data import FinancialPIT
from ashare_data.f1_financial import RESEARCH_MODE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--pack-sha256")
    parser.add_argument("--security", action="append", required=True)
    parser.add_argument("--field", action="append", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--period")
    parser.add_argument("--basis", choices=["YTD", "quarter", "TTM"], default="YTD")
    parser.add_argument("--mode", choices=["strict", RESEARCH_MODE], required=True)
    args = parser.parse_args()
    result = FinancialPIT.from_file(
        args.pack, evidence_root=args.evidence_root, expected_sha256=args.pack_sha256
    ).query(
        args.security,
        args.field,
        as_of=args.as_of,
        period=args.period,
        basis=args.basis,
        mode=args.mode,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(row["status"] == "ok" for row in result["rows"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
