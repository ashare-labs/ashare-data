"""Portable source volume, decimal and event identity boundaries."""

import pytest

from ashare_data import DataError
from ashare_data.research.portable_source import event_identities, source_limits, source_volume


@pytest.mark.parametrize("value,expected", [("0", 0), ("0" * 24, 0), ("9" * 24, 10**24 - 1)])
def test_volume_bound_accepts_convertible_lexemes(value, expected):
    assert source_volume(value) == expected


def test_derived_upper_limit_must_fit_consumer_decimal_bound():
    # Raw decimal fits, but the actual 110% consumer result does not.
    with pytest.raises(DataError) as exc:
        source_limits("999999999999999999999999")
    assert exc.value.code == "PORTABLE_DECIMAL"


def test_event_collection_and_missing_fields_are_defensively_checked():
    for events in (None, {}, [None], [{}], [{"id": "x", "event_type": "cash_dividend"}]):
        with pytest.raises(DataError) as exc:
            event_identities(events)
        assert exc.value.code == "PORTABLE_COMPONENT_SCHEMA"
