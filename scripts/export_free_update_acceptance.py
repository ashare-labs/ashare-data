"""Refresh the one local acceptance entry from a frozen, verified 198-row ledger."""
import argparse
import json
from pathlib import Path

from ashare_data.providers.free_update_acceptance import export_local_ledger

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workbook', type=Path)
    args = parser.parse_args()
    print(json.dumps(export_local_ledger(args.ledger, args.output, workbook=args.workbook), ensure_ascii=False))
