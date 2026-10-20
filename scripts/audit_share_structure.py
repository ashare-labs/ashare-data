"""Audit negative share cells and existing same-provider float evidence offline."""

import argparse
import json
from pathlib import Path

from ashare_data.financials.share_structure_audit import audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--float-batch", required=True)
    parser.add_argument("--captured-before")
    parser.add_argument("--output", required=True, help="New audit JSON; never overwrite old evidence")
    args = parser.parse_args()
    result = audit(args.batch, args.float_batch, captured_before=args.captured_before)
    with Path(args.output).open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: result[k] for k in ("tasks", "negative_cells", "groups", "counts")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
