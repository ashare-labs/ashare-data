"""Explicit local-package demonstration; no external source path is auto-discovered.

Run with the real fixed official package and three frozen price/calendar inputs.
The separate test suite uses synthetic pins only for failure injection.
"""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from ashare_data import DataError, Store
from ashare_data.d1 import PLAN_SHA256, PRICE_DATASET, SOURCE_MANIFEST


def run(package, prices, calendar, root):
    package = Path(package)
    manifest = (package / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == SOURCE_MANIFEST
    originals = {
        Path(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in [*prices, calendar]
    }
    for f in json.loads(manifest)["files"]:
        originals[package / f["path"]] = f["sha256"]
    store = Store.init(root)
    pid = store.import_research(prices, calendar_path=calendar)
    assert pid == PRICE_DATASET
    fid = store.import_d1_facts(package)
    sid = store.compose_d1(pid, fid)
    original = store.research(pid).get_price("600000.XSHG", count=6).to_dict()
    assert store.import_d1_facts(package) == fid
    assert store.compose_d1(pid, fid) == sid
    view = Store(root).d1(sid)
    assert not view.descriptor().complete
    assert view.descriptor().price_dataset_id == pid
    assert view.descriptor().facts_component_id == fid
    assert len(store.d1_facts(fid).records()) == 16
    assert view.instrument().initial_listing_date.isoformat() == "1999-11-10"
    assert view.instrument().historical_eligible is None
    assert view.rules().price_tick == Decimal("0.01")
    assert view.rules().buy_round_lot == 100
    assert view.rules().odd_lot_remaining_balance_sell_once
    assert not view.rules().general_sell_round_lot_required
    assert [b.close for b in view.bars()] == list(
        map(Decimal, ["12.4700", "12.6000", "12.4600", "12.5000"])
    )
    assert [b.volume for b in view.bars()] == [51629079, 38018810, 41001193, 28421482]
    assert [d.is_trading_day for d in view.calendar()] == [True, True, False, False, True, True]
    assert all(s.suspended is False and s.is_st is False for s in view.statuses())
    limits = [(x.lower, x.upper) for x in view.limit_candidates()]
    assert limits == [
        (Decimal(a), Decimal(b))
        for a, b in [("11.13", "13.61"), ("11.22", "13.72"), ("11.34", "13.86"), ("11.21", "13.71")]
    ]
    prev = view.prev_close_candidates()
    assert [x.candidate_value for x in prev] == list(
        map(Decimal, ["12.3700", "12.4700", "12.6000", "12.4600"])
    )
    assert [x.call.history_dt.isoformat() for x in prev] == [
        "2019-12-31",
        "2020-01-02",
        "2020-01-03",
        "2020-01-06",
    ]
    assert all(
        x.observed_equality and x.engine_value is None and not x.eligible_for_engine for x in prev
    )
    events = view.known_events()
    assert len(events) == 5
    assert events[0].record_date.isoformat() == "2019-06-10" and events[
        0
    ].cash_per_share_before_tax == Decimal(".35")
    assert events[1].record_date.isoformat() == "2020-07-22" and events[
        1
    ].cash_per_share_before_tax == Decimal(".60")
    assert (
        events[2].resulting_security == "110059.XSHG"
        and events[2].conversion_start.isoformat() == "2020-05-06"
    )
    assert events[3].entitled_security == events[4].entitled_security == "360003.XSHG"
    assert not view.event_coverage().verified_absent and view.event_coverage().metadata_rows == 248
    assert view.admission().blockers == (
        "instrument",
        "price_limits",
        "event_absence",
        "adjusted_prev_close",
    )
    refusals = []
    for method, call, code in [
        ("price_limits", lambda: view.price_limits("2020-01-03"), "D1_FACT_UNAVAILABLE"),
        ("corporate_actions", view.corporate_actions, "D1_FACT_UNAVAILABLE"),
        (
            "require_execution",
            lambda: view.require_execution(plan_sha256=PLAN_SHA256),
            "D1_ADMISSION_BLOCKED",
        ),
        *[
            (
                "AD08:" + p.call.trade_date.isoformat(),
                lambda p=p: view.adjusted_prev_close(**p.call.to_dict()),
                "D1_FACT_UNAVAILABLE",
            )
            for p in prev
        ],
    ]:
        try:
            call()
        except DataError as e:
            assert e.code == code
            refusals.append({"method": method, "code": code})
        else:
            raise AssertionError("missing strict refusal: " + method)
    assert store.research(pid).get_price("600000.XSHG", count=6).to_dict() == original
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in originals.items())
    assert store.recover_d1() == []
    return {
        "descriptor": view.descriptor().to_dict(),
        "owner_receipt": view.owner_receipt().to_dict(),
        "admission": view.admission().to_dict(),
        "coverage": view.coverage().to_dict(),
        "quality": view.quality().to_dict(),
        "strict_refusals": refusals,
        "bars": [x.to_dict() for x in view.bars()],
        "events": [x.to_dict() for x in events],
        "limits": [x.to_dict() for x in view.limit_candidates()],
        "prev_close": [x.to_dict() for x in prev],
        "original_files_unchanged": len(originals),
        "network_calls": 0,
        "execution": "NOT_RUN",
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--package", required=True)
    p.add_argument("--prices", nargs=2, required=True)
    p.add_argument("--calendar", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    result = run(a.package, a.prices, a.calendar, a.store)
    Path(a.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "dataset_id": result["descriptor"]["dataset_id"],
                "status": "BLOCKED",
                "network_calls": 0,
            }
        )
    )
