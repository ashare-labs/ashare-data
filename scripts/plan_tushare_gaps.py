"""Refresh a compact existing-source inventory and preview gap-only requests offline."""

import argparse
import json
from pathlib import Path

from ashare_data.financials import tushare_gaps as gaps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--factor-inventory", type=Path)
    args = parser.parse_args()
    result = gaps.preview(args.baseline, args.root, args.factor_inventory)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
