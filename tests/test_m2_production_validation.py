"""PM2-R1/R2 boundary branches through the public producer, with unchanged raw bytes."""

import json
import os
from pathlib import Path

import pytest

from ashare_data import DataError
from ashare_data.m2_source import event_identities, source_limits, source_volume


def recipe(kind):
    location = os.environ.get("ASHARE_M2_COMPONENTS")
    if not location:
        pytest.skip("private M2 components absent; set ASHARE_M2_COMPONENTS to run")
    root = Path(location) / kind
    body = json.loads((root / "component.json").read_text())
    for doc in body["documents"]:
        for key, artifact in list(doc["artifacts"].items()):
            doc["artifacts"][key] = {"path": str(root / artifact["path"])}
    return {key: body[key] for key in ("kind", "classification", "documents", "claims")}








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
