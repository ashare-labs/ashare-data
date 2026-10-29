"""Replace retired derived customs units without modifying original evidence."""
import argparse
import json

from ashare_data.providers.free_update_macro_contracts import repair_customs_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="exact immutable snapshot directory")
    parser.add_argument("destination", help="isolated replacement store, never formal data root")
    args = parser.parse_args()
    print(json.dumps(repair_customs_snapshot(args.source, args.destination), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
