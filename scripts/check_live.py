"""Opt-in small live check; outputs summaries, never bundles responses into source control."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from ashare_data import Client, DataError
from ashare_data.model import TZ


def run(cache):
    online = Client(cache=cache, cache_mode="refresh")
    day = datetime.now(TZ).date().isoformat()
    summary = {}
    for frequency in ["daily", "1m", "5m"]:
        data = online.get_price("600000.XSHG", end_date=day, frequency=frequency, count=2)
        cached = Client(cache=cache, cache_mode="only").get_price(
            "600000.XSHG", end_date=day, frequency=frequency, count=2)
        pd.testing.assert_frame_equal(data, cached)
        assert all(p["network_used"] for p in data.attrs["provenance"])
        assert all(not p["network_used"] for p in cached.attrs["provenance"])
        summary[frequency] = {"rows": len(data), "first": str(data.index[0]), "last": str(data.index[-1]),
                              "cache_replay_equal": True,
                              "provenance": data.attrs["provenance"][0]}
    multiple = online.get_price(["600000.XSHG", "000001.XSHE"], count=2, frequency="5m")
    assert len(multiple) == 4 and multiple.code.nunique() == 2
    summary["multiple"] = {"rows": len(multiple), "securities": multiple.code.nunique()}
    info = online.get_security_info("600000.XSHG")
    summary["security"] = {"code": info["code"], "has_name": bool(info["display_name"])}
    days = online.get_trade_days(end_date=day, count=2)
    summary["calendar"] = {"days": [d.isoformat() for d in days]}
    try:
        Client(cache=cache, cache_mode="only").get_price("300001.XSHE", count=2)
    except DataError as exc:
        assert exc.code == "CACHE_MISS"
        summary["offline_miss"] = exc.code
    else:
        raise AssertionError("Expected a missing cache entry")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.cache), ensure_ascii=False, indent=2))
