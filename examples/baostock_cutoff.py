"""Only reads an existing capture. No source SDK or acquisition is invoked."""

import argparse
import json
from ashare_data import Store
from ashare_data.cli import json_value


def main():
    p = argparse.ArgumentParser(description="离线分钟源标签截止示例；不证明闭合/PIT")
    p.add_argument("--store", required=True)
    p.add_argument("--capture", required=True)
    p.add_argument("--end", required=True)
    args = p.parse_args()
    view = Store(args.store).baostock(args.capture).at(args.end, visibility="source_label")
    print(json.dumps(view.get_price().to_dict(), ensure_ascii=False, default=json_value, indent=2))


if __name__ == "__main__":
    main()
