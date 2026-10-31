"""Night bars and holiday-adjacent labels cannot inherit daily date semantics."""

import json

import pytest

from ashare_data.providers.free_updates import free_update_futures_minutes as minutes
from ashare_data.providers.free_updates.free_update_semantics import validate_semantics


def native(day="2026-10-23 21:38:00"):
    return {
        "d": day,
        "o": "5001.0",
        "h": "5002.0",
        "l": "4999.0",
        "c": "5000.0",
        "v": "20",
        "p": "100",
    }


def rows(raw, symbol="V0", period="1"):
    return minutes.parse(
        ("/*<script>location.href='//sina.com';</script>*/\n=(" + json.dumps(raw) + ");").encode(),
        {"symbol": symbol, "period": period},
    )


def check(values, symbol="V0", period="1"):
    return validate_semantics(
        values,
        {"function": minutes.FUNCTION, "parameters": {"symbol": symbol, "period": period}},
        "2026-10-29",
    )


@pytest.mark.parametrize("period", ["1", "5", "15", "30", "60"])
@pytest.mark.parametrize("symbol", ["V0", "V2701"])
def test_all_ten_contexts_preserve_friday_and_after_midnight_labels_without_inference(
    period, symbol
):
    values = rows(
        [native(), native("2026-10-24 00:01:00"), native("2026-10-26 09:01:00")], symbol, period
    )
    result = check(values, symbol, period)
    assert all(
        r["timestamp"] is None and r["trading_date"] is None and r["bar_label_position"] is None
        for r in values
    )
    assert (
        values[0]["source_label_calendar_date"] == "2026-10-23"
        and values[1]["source_label_calendar_date"] == "2026-10-24"
    )
    assert (
        result["latest_period"] is None
        and result["contract_reviewed"]
        and result["night_label_count"] == 2
    )
    assert not result["gaps"] and not result["ready_for_trade_timestamp_backtest"]
    assert len(result["unresolved_field_semantics"]) == (2 if symbol == "V0" else 3)
    assert result["units"]["open"] == "CNY/tonne"
    assert "unverified" in result["units"]["volume"]


@pytest.mark.parametrize(
    "change",
    [
        {"trading_date": "2026-10-26"},
        {"timestamp": "2026-10-23T21:38:00+08:00"},
        {"bar_label_position": "end"},
        {"source_symbol": "V2701"},
    ],
)
def test_unsupported_night_date_or_timestamp_mapping_is_rejected(change):
    value = rows([native()])[0] | change
    with pytest.raises(ValueError, match="inference"):
        check([value])


@pytest.mark.parametrize(
    "raw",
    [
        [native(), native()],
        [native("2026-10-26 09:01:00"), native()],
        [native("2026-02-30 09:01:00")],
        [native() | {"new": 1}],
    ],
)
def test_duplicate_order_invalid_calendar_and_schema_fail(raw):
    with pytest.raises(ValueError):
        rows(raw)


@pytest.mark.parametrize(
    "change", [{"h": "4998"}, {"v": "1.5"}, {"p": "-1"}, {"d": "2027-10-23 21:38:00"}]
)
def test_invalid_bar_quantity_and_future_labels_fail(change):
    with pytest.raises(ValueError):
        check(rows([native() | change]))


def test_executable_suffix_and_injected_callback_are_not_executed():
    with pytest.raises(ValueError):
        minutes.parse(b"=([]);dangerous()", {"symbol": "V0", "period": "1"})
    with pytest.raises(ValueError):
        rows([native()], "V0;code")
