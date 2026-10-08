"""One explicit minute acquisition, or a completely offline fixed-capture read."""
import argparse
import json

from ashare_data import BaoStockSource, Store
from ashare_data.cli import json_value

p = argparse.ArgumentParser()
p.add_argument('--store', required=True)
p.add_argument('--capture')
p.add_argument('--fetch', action='store_true')
p.add_argument('--sdk-path')
p.add_argument('--frequency', choices=['5m', '15m', '30m', '60m'], default='5m')
a = p.parse_args()
if bool(a.capture) == a.fetch:
    p.error('choose --capture for offline read OR --fetch for one remote query')
store = Store(a.store)
if a.fetch:
    result = BaoStockSource(sdk_path=a.sdk_path).get_price(
        '600000.XSHG', store=store, start_date='2026-09-30', end_date='2026-09-30',
        frequency=a.frequency)
else:
    result = store.baostock(a.capture).get_price()
print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=json_value))
