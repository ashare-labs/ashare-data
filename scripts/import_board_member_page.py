"""Import an already received Eastmoney member page; does not access the network."""

import argparse
import json
from pathlib import Path
from ashare_data.providers.free_update_board_members import FUNCTIONS, persist_received_page

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--function", choices=sorted(FUNCTIONS), required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    value = persist_received_page(args.root, args.function, args.code, args.page,
                                  args.body.read_bytes(), json.loads(args.receipt.read_text()))
    print(json.dumps(value, ensure_ascii=False))
