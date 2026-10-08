"""Synthetic observation-boundary regressions for review L1/L2/L3; no live requests."""
import json
from datetime import datetime as RealDatetime

import pandas as pd
import pytest

from ashare_data import Client, DataError, capabilities
from ashare_data import live, transport


def payload(labels, close="10.25"):
    return json.dumps([{"day": t, "open": "10", "high": "11", "low": "9", "close": close,
                        "volume": "1000", "amount": "10250"} for t in labels]).encode()


@pytest.fixture
def clock(monkeypatch):
    class Clock(RealDatetime):
        instant = RealDatetime(2026, 9, 30, 14, tzinfo=live.TZ)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz) if tz is not None else cls.instant.replace(tzinfo=None)

    monkeypatch.setattr(live, "datetime", Clock)
    monkeypatch.setattr(transport, "datetime", Clock)
    return Clock


def cache_entry(root):
    path = next((root / "requests").glob("*.json"))
    return path, json.loads(path.read_text())


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("mode", ["only", "prefer"])
def test_daily_partial_stays_partial_even_on_next_day(clock, monkeypatch, tmp_path, legacy, mode):
    calls = []
    def read(*args):
        calls.append(1)
        return payload(["2026-09-29", "2026-09-30"])
    monkeypatch.setattr(transport, "public_read", read)
    first = Client(cache=tmp_path, cache_ttl=86400).get_price(
        "600000.XSHG", end_date="2026-09-30", count=1)
    if legacy:
        path, entry = cache_entry(tmp_path)
        del entry["request_started_at"]
        path.write_text(json.dumps(entry))
    clock.instant = RealDatetime(2026, 10, 1, 10, tzinfo=live.TZ)
    second = Client(cache=tmp_path, cache_mode=mode, cache_ttl=86400).get_price(
        "600000.XSHG", end_date="2026-09-30", count=1)
    pd.testing.assert_frame_equal(first, second)
    assert second.index[0] == pd.Timestamp("2026-09-29") and calls == [1]
    provenance = second.attrs["provenance"][0]
    assert provenance["completion_cutoff"] == "2026-09-30T14:00:00+08:00"
    assert provenance["completion_basis"] == ("legacy_observed_at" if legacy else "request_started_at")


@pytest.mark.parametrize("frequency,labels", [("1m", ["2026-09-30 14:54:00", "2026-09-30 14:55:00"]),
                                            ("5m", ["2026-09-30 14:50:00", "2026-09-30 14:55:00"])])
@pytest.mark.parametrize("mode", ["only", "prefer"])
def test_minute_partial_not_promoted_by_later_query(clock, monkeypatch, tmp_path, frequency, labels, mode):
    clock.instant = RealDatetime(2026, 9, 30, 14, 54, 59, tzinfo=live.TZ)
    calls = []
    def read(*args):
        calls.append(1)
        return payload(labels)
    monkeypatch.setattr(transport, "public_read", read)
    first = Client(cache=tmp_path, cache_ttl=86400).get_price(
        "600000.XSHG", end_date="2026-09-30", frequency=frequency, count=1)
    clock.instant = RealDatetime(2026, 9, 30, 16, tzinfo=live.TZ)
    second = Client(cache=tmp_path, cache_mode=mode, cache_ttl=86400).get_price(
        "600000.XSHG", end_date="2026-09-30", frequency=frequency, count=1)
    assert second.index[0] == pd.Timestamp(labels[0]) and calls == [1]
    pd.testing.assert_frame_equal(first, second)


@pytest.mark.parametrize("frequency,labels,before,after", [
    ("daily", ["2026-09-29", "2026-09-30"], (15, 4, 59), (15, 5, 0)),
    ("1m", ["2026-09-30 14:54:00", "2026-09-30 14:55:00"], (14, 54, 59), (14, 55, 0)),
    ("5m", ["2026-09-30 14:50:00", "2026-09-30 14:55:00"], (14, 54, 59), (14, 55, 0)),
])
def test_request_crossing_boundary_does_not_upgrade_cached_row(clock, monkeypatch, tmp_path, frequency, labels, before, after):
    clock.instant = RealDatetime(2026, 9, 30, *before, tzinfo=live.TZ)
    def read(*args):
        clock.instant = RealDatetime(2026, 9, 30, *after, tzinfo=live.TZ)
        return payload(labels)
    monkeypatch.setattr(transport, "public_read", read)
    Client(cache=tmp_path).get_price("600000.XSHG", end_date="2026-09-30", frequency=frequency, count=1)
    clock.instant = RealDatetime(2026, 9, 30, 16, tzinfo=live.TZ)
    result = Client(cache=tmp_path, cache_mode="only").get_price(
        "600000.XSHG", end_date="2026-09-30", frequency=frequency, count=1)
    assert result.index[0] == pd.Timestamp(labels[0])
    _, entry = cache_entry(tmp_path)
    assert entry["request_started_at"] < entry["observed_at"]


@pytest.mark.parametrize("frequency,label,boundary", [("daily", "2026-09-30", (15,5)),
                                                     ("1m", "2026-09-30 14:55:00", (14,55)),
                                                     ("5m", "2026-09-30 14:55:00", (14,55))])
def test_observation_at_boundary_remains_usable(clock, monkeypatch, frequency, label, boundary):
    clock.instant = RealDatetime(2026, 9, 30, *boundary, tzinfo=live.TZ)
    monkeypatch.setattr(transport, "public_read", lambda *a: payload([label]))
    result = Client().get_price("600000.XSHG", frequency=frequency, count=1)
    assert result.index[0] == pd.Timestamp(label)


@pytest.mark.parametrize("mode", ["refresh", "prefer"])
def test_new_after_close_request_can_return_updated_daily_values(clock, monkeypatch, tmp_path, mode):
    calls = []
    def read(*args):
        calls.append(1)
        return payload(["2026-09-29", "2026-09-30"], "10.25" if len(calls) == 1 else "10.50")
    monkeypatch.setattr(transport, "public_read", read)
    Client(cache=tmp_path).get_price("600000.XSHG", end_date="2026-09-30", count=1)
    clock.instant = RealDatetime(2026, 9, 30, 16, tzinfo=live.TZ)
    result = Client(cache=tmp_path, cache_mode=mode).get_price("600000.XSHG", end_date="2026-09-30", count=1)
    assert len(calls) == 2 and result.index[0] == pd.Timestamp("2026-09-30")
    assert result.iloc[0].close == 10.50


def test_only_partial_daily_observation_remains_unavailable(clock, monkeypatch, tmp_path):
    monkeypatch.setattr(transport, "public_read", lambda *a: payload(["2026-09-30"]))
    with pytest.raises(DataError) as first:
        Client(cache=tmp_path).get_price("600000.XSHG", end_date="2026-09-30", count=1)
    assert first.value.code == "NO_COMPLETED_BARS"
    clock.instant = RealDatetime(2026, 9, 30, 16, tzinfo=live.TZ)
    with pytest.raises(DataError) as second:
        Client(cache=tmp_path, cache_mode="only").get_price("600000.XSHG", end_date="2026-09-30", count=1)
    assert second.value.code == "NO_COMPLETED_BARS"


def test_future_daily_date_is_not_accepted_after_clock_cutoff(clock, monkeypatch):
    clock.instant = RealDatetime(2026, 9, 30, 16, tzinfo=live.TZ)
    monkeypatch.setattr(transport, "public_read", lambda *a: payload(["2026-10-01"]))
    with pytest.raises(DataError) as exc:
        Client().get_price("600000.XSHG", count=1)
    assert exc.value.code == "NO_COMPLETED_BARS"


def test_inconsistent_cache_observation_times_rejected(clock, monkeypatch, tmp_path):
    monkeypatch.setattr(transport, "public_read", lambda *a: payload(["2026-09-29"]))
    Client(cache=tmp_path).get_price("600000.XSHG", count=1)
    path, entry = cache_entry(tmp_path)
    entry["request_started_at"] = "2026-09-30T15:00:00+08:00"
    path.write_text(json.dumps(entry))
    clock.instant = RealDatetime(2026, 9, 30, 16, tzinfo=live.TZ)
    with pytest.raises(DataError) as exc:
        Client(cache=tmp_path, cache_mode="only").get_price("600000.XSHG", count=1)
    assert exc.value.code == "CACHE_CORRUPT"


@pytest.mark.parametrize("raw,code,kind", [(b"null", "SOURCE_NO_DATA", None), (b"[]", "SOURCE_NO_DATA", None),
    (b'{"error":"access denied"}', "SOURCE_SCHEMA_ERROR", "service_error_object"),
    (b'"service unavailable"', "SOURCE_SCHEMA_ERROR", "unexpected_json_type"),
    (b"42", "SOURCE_SCHEMA_ERROR", "unexpected_json_type"), (b"false", "SOURCE_SCHEMA_ERROR", "unexpected_json_type"),
    (b"{}", "SOURCE_SCHEMA_ERROR", "unexpected_json_type")])
def test_error_response_categories_and_no_cache(clock, monkeypatch, tmp_path, raw, code, kind):
    monkeypatch.setattr(transport, "public_read", lambda *a: raw)
    with pytest.raises(DataError) as exc:
        Client(cache=tmp_path).get_price("600000.XSHG", count=1)
    assert exc.value.code == code
    if kind:
        assert exc.value.details["response_kind"] == kind
    assert list(tmp_path.iterdir()) == []


def test_all_capability_lists_are_isolated(clock, monkeypatch):
    original = capabilities()
    caller_copy = capabilities()
    for value in caller_copy.values():
        if isinstance(value, list):
            value.append("caller value")
    assert capabilities() == original
    monkeypatch.setattr(transport, "public_read", lambda *a: payload(["2026-09-29"]))
    result = Client().get_price("600000.XSHG", count=1)
    assert list(result.columns) == original["default_fields"]
