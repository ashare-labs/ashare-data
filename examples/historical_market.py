"""Offline daily-state evidence for a minute consumer; no broker or account ledger."""

import argparse
import json

from ashare_data import HistoricalMarketView

parser = argparse.ArgumentParser()
parser.add_argument("binding")
parser.add_argument("--sha256", required=True)
args = parser.parse_args()
view = HistoricalMarketView(args.binding, expected_binding_sha256=args.sha256)
result = {
    "coverage": view.coverage(),
    "one_order_daily_state": view.order_state(
        "300750.XSHE",
        as_of="2020-08-24T09:31:00+08:00",
        side="BUY",
        accept_daily_replay=True,
        accept_limit_model=True,
    ),
    "actions": view.corporate_actions(
        ["000651.XSHE", "300750.XSHE"], start="2022-01-01", end="2023-12-31"
    ),
}
print(json.dumps(result, ensure_ascii=False, indent=2))
