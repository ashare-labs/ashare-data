"""Explicit acquisition or offline reopening; no implicit fetch on a cache miss."""
import argparse
import json

from ashare_data import BaoStockSource, Store
from ashare_data.cli import json_value

p = argparse.ArgumentParser()
p.add_argument('--store', required=True)
p.add_argument('--capture', help='read an existing capture without SDK or network')
p.add_argument('--fetch', action='store_true', help='explicitly allow one bounded public query')
p.add_argument('--sdk-path', help='existing official BaoStock 0.9.4 root')
a = p.parse_args()
if bool(a.capture) == a.fetch:
    p.error('choose exactly one of --capture or --fetch')
store = Store(a.store)
if a.fetch:
    source = BaoStockSource(sdk_path=a.sdk_path)
    sid = source.fetch(store, kind='daily', security='600000.XSHG',
                       start_date='2026-09-28', end_date='2026-09-30')
else:
    sid = a.capture
view = store.baostock(sid)
print(json.dumps(view.get_price().to_dict(), ensure_ascii=False, indent=2, default=json_value))
