"""Read an existing fixed calendar capture offline; never collect or infer days."""
import argparse
import json

from ashare_data import Store

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--store', required=True)
p.add_argument('--capture', required=True)
p.add_argument('--dates', required=True, nargs='+')
args = p.parse_args()
view = Store(args.store).baostock(args.capture)
print(json.dumps(view.get_calendar(require_known=True).to_dict(), ensure_ascii=False))
print(json.dumps(view.get_calendar_links(trading_dates=args.dates).to_dict(), ensure_ascii=False))
