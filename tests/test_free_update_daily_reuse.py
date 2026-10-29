from decimal import Decimal

import pytest

from ashare_data.providers import daily_sources
from ashare_data.providers.free_update_equities import collect, validate


class Capture:
    def __init__(self):
        self.receipts = []

    def get(self, url, headers=None):
        self.receipts.append({"url": url})
        return b"synthetic encoded source body"


def test_legacy_calendar_decoder_reused_with_explicit_window(monkeypatch):
    monkeypatch.setattr(
        daily_sources,
        "decode_sina",
        lambda raw, variable: ["2009-12-31", "2026-10-28", "2026-10-29", "2026-10-30"],
    )
    capture = Capture()
    params = {"end_date": "20261029"}
    records = collect(capture, "sina_trade_calendar", params)
    assert records == [{"trade_date": "2026-10-28"}, {"trade_date": "2026-10-29"}]
    assert capture.dataset_metadata["source_count"] == 2
    assert (
        validate(
            records,
            {"function": "sina_trade_calendar", "parameters": params},
            "2026-10-29",
            Decimal,
        )["gaps"]
        == []
    )
    with pytest.raises(ValueError, match="uniqueness"):
        validate(
            records + records,
            {"function": "sina_trade_calendar", "parameters": params},
            "2026-10-29",
            Decimal,
        )


def test_legacy_daily_reuse_keeps_native_quantity_and_missing_fields(monkeypatch):
    native = {
        "date": "2026-10-28",
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "2",
        "volume": "123.25",
        "amount": None,
        "preclose": None,
        "tradestatus": None,
        "is_st": None,
    }

    def old(http, symbol, start, end):
        assert (symbol, start, end) == ("sh600000", "2026-10-01", "2026-10-29")
        return [native], []

    monkeypatch.setattr(daily_sources, "sina_daily", old)
    params = {"symbol": "sh600000", "start_date": "20261001", "end_date": "20261029"}
    capture = Capture()
    rows = collect(capture, "sina_raw_daily", params)
    assert rows[0]["volume"] == "123.25" and rows[0]["amount"] is None
    assert (
        validate(rows, {"function": "sina_raw_daily", "parameters": params}, "2026-10-29", Decimal)[
            "gaps"
        ]
        == []
    )
    rows[0]["security"] = "sz000001"
    with pytest.raises(ValueError, match="security"):
        validate(rows, {"function": "sina_raw_daily", "parameters": params}, "2026-10-29", Decimal)


def test_period_bars_keep_partial_bucket_and_null_units(monkeypatch):
    native = [
        dict(
            date=day,
            open="1",
            high="3",
            low="1",
            close=close,
            volume="10",
            amount=None,
            preclose="1",
        )
        for day, close in [("2026-09-01", "2"), ("2026-09-28", "3"), ("2026-10-28", "2")]
    ]

    def old(http, symbol, start, end):
        assert start == "2026-09-01"
        return native, []

    monkeypatch.setattr(daily_sources, "sina_daily", old)
    params = {
        "symbol": "sh600000",
        "period": "monthly",
        "start_date": "20260928",
        "end_date": "20261029",
    }
    rows = collect(Capture(), "sina_period_bars", params)
    assert rows[0]["daily_rows"] == 2 and rows[0]["volume"] == "20" and rows[0]["amount"] is None
    assert rows[1]["period_still_open"] and not rows[0]["period_still_open"]
    job = {"function": "sina_period_bars", "parameters": params}
    assert validate(rows, job, "2026-10-29", Decimal)["business_key"] == [
        "security",
        "period_start",
    ]
    rows[0]["volume"] = "30"
    with pytest.raises(ValueError, match="aggregation"):
        validate(rows, job, "2026-10-29", Decimal)
