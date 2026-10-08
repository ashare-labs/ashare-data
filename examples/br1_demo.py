"""Seal and inspect conditional projections in an existing strict D1 Store. No engine."""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from ashare_data import DataError, Store
from ashare_data.br1 import STRICT_DATASET, PROFILE_SHA256


def run(root):
    store = Store(root)
    original = store.d1(STRICT_DATASET).owner_receipt().to_dict()
    old_files = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ("d1-objects", "d1-manifests", "research-objects", "research-manifests")
        for p in (store.root / folder).iterdir()
        if p.is_file()
    }
    sid = store.compose_br1(
        STRICT_DATASET, mode="conditional_research", assumption_ids=("A-EQ", "A-NORMAL", "A-AD08")
    )
    assert sid == store.compose_br1(
        STRICT_DATASET, mode="conditional_research", assumption_ids=("A-EQ", "A-NORMAL", "A-AD08")
    )
    view = Store(root).br1(sid, mode="conditional_research")
    assert view.descriptor().profile_sha256 == PROFILE_SHA256
    assert not view.descriptor().complete and not view.descriptor().verified_absent
    assert (
        view.admission().model_projection_permission and not view.admission().execution_permission
    )
    assert view.instrument().model_eligible and view.instrument().fact.historical_eligible is None
    assert view.modeled_events().modeled_events == ()
    assert view.modeled_events().evidence_status == "assumed_no_relevant_events"
    assert not view.modeled_events().verified_absent
    assert len(view.modeled_events().known_event_ids) == 5
    calls = view.profile().allowed_calls
    values = [view.adjusted_prev_close(**c.to_dict()) for c in calls]
    assert [p.value for p in values] == list(
        map(Decimal, ["12.3700", "12.4700", "12.6000", "12.4600"])
    )
    assert all(
        p.assumption_ids == ("A-EQ", "A-AD08") and p.absolute_adjustment_factor is None
        for p in values
    )
    limits = [view.price_limits(c.trade_date.isoformat()) for c in calls]
    assert [(p.lower, p.upper) for p in limits] == [
        (Decimal(a), Decimal(b))
        for a, b in [("11.13", "13.61"), ("11.22", "13.72"), ("11.34", "13.86"), ("11.21", "13.71")]
    ]
    refusals = []
    for name, fn, code in [
        ("strict_events", store.d1(STRICT_DATASET).corporate_actions, "D1_FACT_UNAVAILABLE"),
        ("BR1_execution", view.require_execution, "BR1_REVIEW_REQUIRED"),
        ("missing_mode", lambda: store.br1(sid, mode="strict"), "BR1_EXPLICIT_MODE_REQUIRED"),
        (
            "unreviewed_call",
            lambda: view.adjusted_prev_close(
                **{**calls[0].to_dict(), "adjustment_requested": "none"}
            ),
            "BR1_UNREVIEWED_CALL",
        ),
    ]:
        try:
            fn()
        except DataError as e:
            assert e.code == code
            refusals.append({"method": name, "code": code})
        else:
            raise AssertionError("missing refusal: " + name)
    assert store.d1(STRICT_DATASET).owner_receipt().to_dict() == original
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in old_files.items())
    assert store.recover_br1() == []
    return {
        "owner_receipt": view.owner_receipt().to_dict(),
        "modeled_events": view.modeled_events().to_dict(),
        "limits": [p.to_dict() for p in limits],
        "prev_close": [p.to_dict() for p in values],
        "strict_refusals": refusals,
        "old_object_files_unchanged": len(old_files),
        "network_calls": 0,
        "execution": "NOT_RUN",
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    result = run(args.store)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["owner_receipt"]["descriptor"]))
