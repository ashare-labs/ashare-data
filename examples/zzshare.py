"""Offline example: python examples/zzshare.py CAPTURE_DIRECTORY NEW_STORE."""

import sys
from ashare_data import Store


def main():
    store = Store.init(sys.argv[2])
    sid = store.import_zzshare_capture(sys.argv[1], enable_research=True)
    result = Store(store.root).zzshare(sid, enable_research=True).get_daily()
    for row in result.rows:
        print(
            row.security,
            row.trade_date,
            row.close.raw_token,
            row.close.value,
            row.source_claimed_limits.high.value,
            row.rule_derived_limits,
        )
    print(result.report)


if __name__ == "__main__":
    main()
