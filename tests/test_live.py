"""Synthetic protocol tests. Live acceptance is separate and never counted here."""
import json
import socket
import urllib.error
from datetime import date, datetime

import pandas as pd
import pytest

from ashare_data import Client, DataError, capabilities, get_all_securities, get_price
from ashare_data import transport
from ashare_data import live
from ashare_data.live import _bars, _calendar


def payload(days=None, *, amount=True):
    rows = [{"day": d, "open": "10.000", "high": "10.200", "low": "9.800",
             "close": "10.100", "volume": "1200", **({"amount": "12060.1234"} if amount else {})}
            for d in (days or ["2026-09-30 14:56:00", "2026-09-30 14:57:00", "2026-09-30 15:00:00"])]
    return json.dumps(rows).encode()


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    def read(url, timeout):
        calls.append(url)
        return payload(["2026-09-29", "2026-09-30"] if "scale=240" in url else None,
                       amount="scale=240" not in url)
    monkeypatch.setattr(transport, "public_read", read)
    return calls


def test_direct_single_dataframe_without_store(upstream, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = get_price("600000.XSHG", frequency="1m", count=2)
    assert isinstance(data, pd.DataFrame)
    assert list(data.columns) == ["open", "close", "high", "low", "volume"]
    assert data.index.name == "time" and data.index.tz is None
    assert data.iloc[-1].close == 10.1 and data.volume.dtype.kind == "i"
    assert data.attrs["volume_unit"] == "share"
    assert data.attrs["available_at"] is None and not data.attrs["point_in_time_verified"]
    assert list(tmp_path.iterdir()) == []
    assert len(upstream) == 1


def test_default_daily_works(upstream):
    data = get_price("600000.XSHG", count=2)
    assert len(data) == 2 and data.attrs["frequency"] == "daily"
    assert "scale=240" in upstream[0]


def test_multiple_long_table_and_no_partial_success(upstream, monkeypatch):
    data = get_price(["600000.XSHG", "000001.XSHE"], count=2, frequency="5m")
    assert list(data.columns[:2]) == ["time", "code"] and len(data) == 4
    assert set(data.code) == {"600000.XSHG", "000001.XSHE"}
    def fail_second(url, timeout):
        if "sz" in url:
            raise DataError("NETWORK_ERROR", "synthetic failure")
        return payload()
    monkeypatch.setattr(transport, "public_read", fail_second)
    with pytest.raises(DataError, match="synthetic failure"):
        get_price(["600000.XSHG", "000001.XSHE"], count=1, frequency="1m")


def test_date_count_and_inclusive_boundaries(upstream):
    data = get_price("600000.XSHG", frequency="1m", start_date="2026-09-30 14:56",
                     end_date="2026-09-30 14:57", fields=["close", "money"])
    assert len(data) == 2 and data.iloc[0].money == 12060.1234
    data = get_price("600000.XSHG", frequency="daily", end_date="2026-09-30", count=1)
    assert data.index[0] == pd.Timestamp("2026-09-30")


def test_auction_not_synthesized_or_shifted(upstream):
    data = get_price("600000.XSHG", frequency="1m", count=2)
    assert data.index.strftime("%H:%M").tolist() == ["14:57", "15:00"]
    assert data.attrs["provenance"][0]["irregular_intervals"][0]["classification"] == "closing_auction_source_interval"
    with pytest.raises(DataError) as exc:
        get_price("600000.XSHG", frequency="1m", count=2, strict=True)
    assert exc.value.code == "IRREGULAR_SOURCE_GRID"


@pytest.mark.parametrize("kwargs,code", [
    ({"fq": "pre"}, "UNSUPPORTED_ADJUSTMENT"), ({"fq": "post"}, "UNSUPPORTED_ADJUSTMENT"),
    ({"skip_paused": True}, "UNSUPPORTED_PAUSED"), ({"fill_paused": True}, "UNSUPPORTED_PAUSED"),
    ({"panel": True}, "UNSUPPORTED_PANEL"), ({"round": True}, "UNSUPPORTED_ROUND"),
    ({"frequency": "15m"}, "UNSUPPORTED_FREQUENCY"), ({"fields": ["paused"]}, "UNSUPPORTED_FIELD"),
    ({"fields": ["money"]}, "UNSUPPORTED_FIELD"), ({"count": 0}, "INVALID_COUNT"),
    ({"count": True}, "INVALID_COUNT"), ({"count": 1001}, "INVALID_COUNT"),
    ({"start_date": "2026-09-30", "count": 1}, "INVALID_REQUEST"),
    ({"start_date": "2026-10-01", "end_date": "2026-09-30"}, "INVALID_RANGE"),
    ({"fields": []}, "INVALID_REQUEST"), ({"fields": ["close", "close"]}, "INVALID_REQUEST"),
    ({"start_date": "not a date"}, "INVALID_DATE"),
])
def test_invalid_options_fail_before_network(upstream, kwargs, code):
    with pytest.raises(DataError) as exc:
        get_price("600000.XSHG", **kwargs)
    assert exc.value.code == code and not upstream


@pytest.mark.parametrize("security", ["sh600000", "000001.XSHG", "600000.XSHE", "../../foo", "430047.XBSE", [], ["600000.XSHG"] * 11])
def test_unsupported_security(upstream, security):
    with pytest.raises(DataError):
        get_price(security, count=1)
    assert not upstream


def test_missing_history_and_count_are_errors(upstream):
    for kwargs in [{"start_date": "2026-04-09", "end_date": "2026-09-30"}, {"count": 3}]:
        with pytest.raises(DataError) as exc:
            get_price("600000.XSHG", **kwargs)
        assert exc.value.code == "COVERAGE_INCOMPLETE"
        assert exc.value.details["source_first"] == "2026-09-29"


def test_cache_same_api_offline_and_corruption(upstream, tmp_path, monkeypatch):
    online = Client(cache=tmp_path).get_price("600000.XSHG", count=2, frequency="1m")
    def no_network(*args):
        pytest.fail("offline cache attempted network")
    monkeypatch.setattr(transport, "public_read", no_network)
    offline = Client(cache=tmp_path, cache_mode="only").get_price("600000.XSHG", count=2, frequency="1m")
    pd.testing.assert_frame_equal(online, offline)
    assert offline.attrs["provenance"][0]["cache_hit"]
    assert not offline.attrs["provenance"][0]["network_used"]
    with pytest.raises(DataError) as exc:
        Client(cache=tmp_path, cache_mode="only").get_price("000001.XSHE", count=2)
    assert exc.value.code == "CACHE_MISS"
    next((tmp_path / "objects").iterdir()).write_bytes(b"corrupt")
    with pytest.raises(DataError) as exc:
        Client(cache=tmp_path, cache_mode="only").get_price("600000.XSHG", count=2, frequency="1m")
    assert exc.value.code == "CACHE_CORRUPT"


def test_refresh_preserves_old_response_objects(upstream, tmp_path, monkeypatch):
    client = Client(cache=tmp_path)
    client.get_price("600000.XSHG", count=2, frequency="1m")
    old = {p.name: p.read_bytes() for p in (tmp_path / "objects").iterdir()}
    monkeypatch.setattr(transport, "public_read", lambda *a: payload().replace(b'10.100', b'10.150'))
    Client(cache=tmp_path, cache_mode="refresh").get_price("600000.XSHG", count=2, frequency="1m")
    assert len(list((tmp_path / "objects").iterdir())) == 2
    assert all((tmp_path / "objects" / k).read_bytes() == v for k, v in old.items())


@pytest.mark.parametrize("body", [b"null", b"[]", b"<html>refused</html>", b'{"error":"denied"}', payload().replace(b'10.200', b'9.000'), payload().replace(b'1200', b'NaN'), payload().replace(b'10.000', b'-1')])
def test_source_failure_not_cached(body, tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "public_read", lambda *a: body)
    with pytest.raises(DataError):
        Client(cache=tmp_path).get_price("600000.XSHG", count=1, frequency="1m")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status,expected", [(403, "SOURCE_ACCESS_DENIED"), (451, "SOURCE_ACCESS_DENIED"), (302, "SOURCE_ACCESS_DENIED"), (429, "RATE_LIMITED"), (502, "SOURCE_HTTP_ERROR")])
def test_http_failure_classified_and_not_retried(monkeypatch, status, expected):
    calls = []
    class Opener:
        def open(self, *a, **kw):
            calls.append(1)
            raise urllib.error.HTTPError("https://example.org/", status, "error", {}, None)
    monkeypatch.setattr(transport.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(transport.time, "sleep", lambda *a: None)
    with pytest.raises(DataError) as exc:
        transport.public_read("https://example.org/", 1)
    assert exc.value.code == expected and calls == [1]


def test_network_failure_separate_from_source_capability(monkeypatch):
    class Opener:
        def open(self, *a, **kw):
            raise urllib.error.URLError(socket.timeout("timed out"))
    monkeypatch.setattr(transport.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(transport.time, "sleep", lambda *a: None)
    with pytest.raises(DataError) as exc:
        transport.public_read("https://example.org/", 1)
    assert exc.value.code == "NETWORK_ERROR"


def calendar_payload():
    # Synthetic parser sample, not a live calendar proof.
    return ("2026年休市安排" + " ".join(f"{m}月{a}日（星期一）至{m}月{b}日（星期二）休市"
            for m, a, b in [(1,1,3), (2,15,23), (4,4,6), (5,1,5), (6,19,21), (9,25,27), (10,1,7)])).encode()


def test_calendar_does_not_treat_makeup_weekends_as_trading_days(monkeypatch):
    monkeypatch.setattr(transport, "public_read", lambda *a: calendar_payload())
    c = Client()
    assert c.get_trade_days("2026-09-30", "2026-10-10") == [date(2026,9,30), date(2026,10,8), date(2026,10,9)]
    assert c.get_trade_days(end_date="2026-10-08", count=2) == [date(2026,9,30), date(2026,10,8)]
    with pytest.raises(DataError) as exc:
        c.get_trade_days("2025-12-30", "2026-01-06")
    assert exc.value.code == "CALENDAR_COVERAGE"
    with pytest.raises(DataError):
        _calendar(b"layout changed")


def test_security_info_is_current_not_historical(monkeypatch):
    fields = ["测试证券"] + ["0"] * 29 + ["2026-09-30", "15:00:00", "00"]
    body = ('var hq_str_sh600000="' + ",".join(fields) + '";\n').encode("gb18030")
    monkeypatch.setattr(transport, "public_read", lambda *a: body)
    info = Client().get_security_info("600000.XSHG")
    assert info["display_name"] == "测试证券" and info["start_date"] is None
    assert info["historical_membership_supported"] is False
    with pytest.raises(DataError) as exc:
        get_all_securities(date="2026-04-09")
    assert exc.value.code == "UNSUPPORTED_UNIVERSE"


def test_capabilities_do_not_call_network(upstream):
    assert capabilities()["automatic_source_fallback"] is False
    assert capabilities()["joinquant_equivalent"] is False
    assert not upstream


def test_duplicate_labels_rejected():
    with pytest.raises(DataError) as exc:
        _bars(payload(["2026-09-30 14:55:00"] * 2), 1)
    assert exc.value.code == "SOURCE_SCHEMA_ERROR"


def test_dated_query_cache_key_survives_next_day(upstream, tmp_path, monkeypatch):
    class Clock(datetime):
        day = 30
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, cls.day, 16, tzinfo=tz)
    monkeypatch.setattr(live, "datetime", Clock)
    client = Client(cache=tmp_path)
    first = client.get_price("600000.XSHG", end_date="2026-09-30", count=1)
    class NextClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 1, 16, tzinfo=tz)
    monkeypatch.setattr(live, "datetime", NextClock)
    replay = Client(cache=tmp_path, cache_mode="only").get_price("600000.XSHG", end_date="2026-09-30", count=1)
    pd.testing.assert_frame_equal(first, replay)
    assert len(upstream) == 1


def test_incomplete_current_daily_bar_excluded(upstream, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 30, 14, tzinfo=tz)
    monkeypatch.setattr(live, "datetime", Clock)
    result = get_price("600000.XSHG", count=1)
    assert result.index[0] == pd.Timestamp("2026-09-29")
