"""Offline public import and daily-state reads; no data acquisition."""
import argparse
import json
from pathlib import Path

from ashare_data import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--capture-directory", action="append", required=True)
    args = parser.parse_args()
    store = Store(args.store) if Path(args.store).exists() else Store.init(args.store)
    results = []
    for directory in args.capture_directory:
        sid = store.import_baostock_capture(directory)
        results.append(store.baostock(sid).get_status().to_dict())
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
