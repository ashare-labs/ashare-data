"""PM2-R1/R2 boundary branches through the public producer, with unchanged raw bytes."""

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from ashare_data import DataError, Store
from ashare_data.m2_source import event_identities, source_limits, source_volume


def recipe(kind):
    root = Path(os.environ["ASHARE_M2_COMPONENTS"]) / kind
    body = json.loads((root / "component.json").read_text())
    for doc in body["documents"]:
        for key, artifact in list(doc["artifacts"].items()):
            doc["artifacts"][key] = {"path": str(root / artifact["path"])}
    return {key: body[key] for key in ("kind", "classification", "documents", "claims")}


@pytest.mark.parametrize(
    "field,value",
    [
        (field, value)
        for field in ("preclose", "open", "high", "low", "close", "amount")
        for value in ("NaN", "Infinity", "1e1", " 10 ", "1000000000000000000000000", "0.000000001")
    ]
    + [("preclose", "0"), ("preclose", "-12.47"), ("amount", "-1")]
    + [("volume", value) for value in ("", "-1", "+100", "1.0", "1e2", "１００", "0" * 25)],
)
def test_numeric_rejected_before_publication(tmp_path, field, value):
    market, facts = recipe("market"), recipe("facts")
    doc = next(d for d in market["documents"] if d["name"] == "legacy-daily")
    original = Path(doc["artifacts"]["raw"]["path"])
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    body = json.loads(original.read_text())
    next(row for row in body["rows"] if row["date"] == "2020-01-03")[field] = value
    changed = tmp_path / "input.json"
    changed.write_text(json.dumps(body))
    before = changed.read_bytes()
    doc["artifacts"]["raw"] = {"path": str(changed)}
    store = Store.init(tmp_path / "store")
    market_id = store.import_m2_sources(**market)
    facts_id = store.import_m2_sources(**facts)
    request = dict(
        price_dataset_id=market_id,
        facts_component_id=facts_id,
        window_ids=["m2a"],
        mode="conditional_research",
    )
    report = store.validate_m2(**request).to_dict()
    assert report["status"] == "BLOCKED" and report["gaps"]
    assert store.m2_report(report["report_id"]).to_dict() == report
    with pytest.raises(DataError) as exc:
        store.compose_m2(**request)
    assert exc.value.code == "M2_COMPOSITION_BLOCKED"
    assert store.m2_snapshots() == []
    assert changed.read_bytes() == before
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize(
    "field,value",
    [
        (field, value)
        for field in ("id", "event_type")
        for value in (None, "", " ", True, [], "x" * 129)
    ]
    + [
        ("entitled_security", value)
        for value in (None, "", "unknown", "600000", " 360003.XSHG", "600000.xshg", True, [])
    ],
)
def test_all_event_identities_precede_relevance_filter(tmp_path, field, value):
    facts = recipe("facts")
    # An otherwise unrelated preferred-share event must still have valid identity/structure.
    event = next(e for e in facts["claims"]["events"] if e["entitled_security"] == "360003.XSHG")
    event[field] = value
    store = Store.init(tmp_path / "store")
    with pytest.raises(DataError) as exc:
        store.import_m2_sources(**facts)
    assert exc.value.code == "M2_COMPONENT_SCHEMA"
    assert store.m2_snapshots() == []


def test_duplicate_event_id_blocks_public_component(tmp_path):
    facts = recipe("facts")
    facts["claims"]["events"].append(copy.deepcopy(facts["claims"]["events"][0]))
    store = Store.init(tmp_path / "store")
    with pytest.raises(DataError) as exc:
        store.import_m2_sources(**facts)
    assert exc.value.code == "M2_SOURCE_CONFLICT"
    assert store.m2_snapshots() == []


@pytest.mark.parametrize("value,expected", [("0", 0), ("0" * 24, 0), ("9" * 24, 10**24 - 1)])
def test_volume_bound_accepts_convertible_lexemes(value, expected):
    assert source_volume(value) == expected


def test_derived_upper_limit_must_fit_consumer_decimal_bound():
    # Raw decimal fits, but the actual 110% consumer result does not.
    with pytest.raises(DataError) as exc:
        source_limits("999999999999999999999999")
    assert exc.value.code == "M2_DECIMAL"


def test_event_collection_and_missing_fields_are_defensively_checked():
    for events in (None, {}, [None], [{}], [{"id": "x", "event_type": "cash_dividend"}]):
        with pytest.raises(DataError) as exc:
            event_identities(events)
        assert exc.value.code == "M2_COMPONENT_SCHEMA"


def test_explicit_preferred_security_is_valid_even_with_null_record_date():
    events = recipe("facts")["claims"]["events"]
    assert any(e["entitled_security"] == "360003.XSHG" and e["record_date"] is None for e in events)
    event_identities(events)
