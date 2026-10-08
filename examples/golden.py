"""Synthetic golden data: one explicit three-minute window, NEVER real market history."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from ashare_data import Store
from ashare_data.compat import JQStyle

SYMBOL = "000001.XSHE"
START = "2026-04-09T14:53:00+08:00"
END = "2026-04-09T14:56:00+08:00"
REQUIREMENTS = [{"symbols": [SYMBOL], "start": START, "end": END}]


def bundle():
    source = {"id": "synthetic-golden-v1", "kind": "synthetic", "location": "generated:golden-v1",
              "observed_at": "2026-04-09T15:10:00+08:00", "label": "end",
              "time_semantics": "verified", "time_evidence": "synthetic construction, not vendor verification",
              "volume_unit": "lots", "lot_size": 100, "amount_unit": "CNY_10K",
              "price_unit": "CNY", "price_basis": "unadjusted",
              "unit_evidence": "synthetic fixture: 2 lots = 200 shares; 0.2 CNY_10K = 2000 CNY"}
    rows = []
    for minute in (54, 55, 56):
        t = f"2026-04-09T14:{minute}:00+08:00"
        rows.append({"symbol": SYMBOL, "timestamp": t, "open": "10", "high": "10.2",
                     "low": "9.8", "close": "10.1", "volume": "2", "amount": "0.2",
                     "quality": "synthetic", "quality_reason": "golden fixture only",
                     "available_at": t, "visibility_evidence": "synthetic scheduled visibility",
                     "provider_extra": "preserved"})
    return {"schema_version": 1, "source": source, "bars": rows,
            "calendar": [{"date": "2026-04-09", "is_open": True,
                          "sessions": [["2026-04-09T09:30:00+08:00", "2026-04-09T11:30:00+08:00"],
                                       ["2026-04-09T13:00:00+08:00", "2026-04-09T15:00:00+08:00"]],
                          "available_at": "2026-04-08T00:00:00+08:00", "evidence": "synthetic calendar fixture only"}],
            "instrument_sets": [{"effective_date": "2026-04-09", "available_at": "2026-04-08T12:00:00+08:00",
                                 "observed_at": "2026-04-09T15:10:00+08:00", "scope": "sample",
                                 "evidence": "synthetic dated universe; not today's real stock pool",
                                 "instruments": [{"symbol": SYMBOL, "name": "合成平安样本", "type": "stock",
                                                  "source_fields": {"fixture": True}}]}]}


def write_fixtures(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (("golden.json", bundle()), ("requirements.json", REQUIREMENTS)):
        (directory / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n")


def run(root):
    store = Store.init(root)
    original = bundle()
    batch = store.import_bundle(original)
    assert store.import_bundle(original) == batch
    report = store.validate([batch], requirements=REQUIREMENTS, policy="synthetic")
    assert report["passed"]
    first = store.publish(report["id"])
    revised = copy.deepcopy(original)
    revised["bars"][0]["close"] = "10.15"
    second_batch = store.import_bundle(revised)
    second_report = store.validate([second_batch], requirements=REQUIREMENTS, policy="synthetic")
    second = store.publish(second_report["id"])
    with store.snapshot(first) as old, store.snapshot(second) as new:
        original_close = old.bars([SYMBOL], START, END, quality="synthetic")["rows"][0]["close"]
        revised_close = new.bars([SYMBOL], START, END, quality="synthetic")["rows"][0]["close"]
        closed = JQStyle(old, quality="synthetic").get_price(
            SYMBOL, "2026-04-09T14:55:00+08:00", "2026-04-09T14:55:00+08:00")
        assert closed["rows"][0]["bar_start"] == "2026-04-09T14:54:00+08:00"
        assert original_close != revised_close
        return {"batch_id": batch, "snapshot_id": first, "revised_snapshot_id": second,
                "old_close": str(original_close), "revised_close": str(revised_close),
                "closed_at_1455": closed["rows"][0]["bar_start"], "network": "none"}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--store", default=".data/golden")
    p.add_argument("--write-fixtures")
    args = p.parse_args()
    if args.write_fixtures:
        write_fixtures(args.write_fixtures)
    print(json.dumps(run(args.store), ensure_ascii=False, indent=2))
