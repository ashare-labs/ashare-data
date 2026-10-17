"""Synthetic full-session evidence; tests never acquire market data."""

from datetime import timedelta
from decimal import localcontext
from types import SimpleNamespace

import pytest

from ashare_data import DataError, TDXMinuteSessionView, TDXSessionSource
from ashare_data.market.minute_session import MODEL, _labels
from ashare_data.core.model import timestamp
from ashare_data.providers.tdx_bars import _seal
from ashare_data.providers.tdx_collection import collect_pages, encoded, request
from test_tdx_bars import Bar

HISTORY = "2026-10-08"
DAY = "2026-10-09"


def make_view(tmp_path, *, omit=None, frequency="1m", sid="600000.XSHG", bad_sid=False):
    labels = _labels(HISTORY) + _labels(DAY)
    if omit:
        labels.remove(omit)
    bars = [Bar(timestamp(t)) for t in labels]
    for bar in bars:
        if bar.time.strftime("%H:%M") == "14:59":
            bar.volume_wire_value = bar.volume_lots = bar.amount = 0.0
    config = request(sid, "116.205.183.150:7709", frequency, 0, len(bars), 800, 1, 6, 1)
    (tmp_path / "request.json").write_bytes(encoded(config))
    page = SimpleNamespace(
        bars=bars,
        raw_payload=len(bars).to_bytes(2, "little")
        + b"".join(bytes.fromhex(b.record_hex) for b in bars),
        full_code=("sh" if sid.endswith("XSHG") else "sz") + sid[:6],
        period_name=frequency,
        start=0,
        request_count=len(bars),
        adjust_mode_raw=0,
    )
    collect_pages(tmp_path, config, lambda *_: page, {"kind": "synthetic-test"})
    pin = _seal(tmp_path)
    source = TDXSessionSource("000001.XSHE" if bad_sid else sid, str(tmp_path), pin)
    return TDXMinuteSessionView([source], execution_days=[DAY], history_days=[HISTORY], model=MODEL)


def test_full_grid_partition_clocks_and_source_lineage(tmp_path):
    view = make_view(tmp_path)
    doc = view.export()
    obs = doc["session_observations"]
    assert len(doc["minute"]) == len(obs["history_minutes"]) == 240
    assert all(r["execution_day"] for r in doc["minute"])
    assert all(not r["execution_day"] for r in obs["history_minutes"])
    assert set(obs["history_sessions"]) == {HISTORY}
    assert len(doc["closing_auction_inputs"]) == 4
    assert [r["phase"] for r in doc["closing_auction_inputs"]] == ["closing_boundary"] + [
        "closing_auction_candidate"
    ] * 3
    assert doc["minute"][-2]["volume"] == "0.0"
    assert doc["minute"][-2]["amount"] == "0.0"
    assert doc["minute"][-2]["zero_volume"] is True
    for row in doc["minute"] + obs["history_minutes"]:
        assert all(
            row[k] is None
            for k in ("bar_start", "bar_end", "available_at", "historical_available_at")
        )
        assert timestamp(row["replay_bar_end"]) - timestamp(row["replay_bar_start"]) == timedelta(
            minutes=1
        )
        assert row["evidence"]["row_sha256"] and row["evidence"]["payload_sha256"]
        assert row["interval_verified"] is row["historical_pit"] is False
    doc["minute"][0]["open"] = "999"
    assert view.export()["minute"][0]["open"] == "10"


def test_open_proxy_is_only_a_single_price_and_no_future_bar(tmp_path):
    view = make_view(tmp_path)
    row = view.read(
        "600000.XSHG", trade_date=DAY, purpose="opening_price", as_of=DAY + "T09:30:00+08:00"
    )
    assert row["price"] == "10"
    assert row["source_label"] == DAY + "T09:31:00+08:00"
    assert row["replay_available_at"] == DAY + "T09:30:00+08:00"
    assert not {"high", "low", "close", "volume", "amount", "raw_fields"}.intersection(row)
    assert row["matching_allowed"] is False
    for purpose in ("minute_history", "valuation_only", "closed_daily", "closing_auction_matching"):
        with pytest.raises(DataError) as exc:
            view.read("600000.XSHG", trade_date=DAY, purpose=purpose, as_of=DAY + "T09:30:00+08:00")
        assert exc.value.code == "MINUTE_SESSION_NOT_YET_AVAILABLE"
    with pytest.raises(DataError):
        view.read(
            "600000.XSHG", trade_date=DAY, purpose="opening_price", as_of=DAY + "T09:29:59+08:00"
        )


def test_close_and_auction_available_only_at_declared_end(tmp_path):
    view = make_view(tmp_path)
    with pytest.raises(DataError):
        view.read(
            "600000.XSHG", trade_date=DAY, purpose="valuation_only", as_of=DAY + "T14:59:59+08:00"
        )
    close = view.read(
        "600000.XSHG", trade_date=DAY, purpose="valuation_only", as_of=DAY + "T15:00:00+08:00"
    )
    assert close["source_label"] == DAY + "T15:00:00+08:00"
    assert close["matching_allowed"] is False
    before = view.read(
        "600000.XSHG",
        trade_date=DAY,
        purpose="closing_auction_matching",
        as_of=DAY + "T14:59:59+08:00",
    )
    after = view.read(
        "600000.XSHG",
        trade_date=DAY,
        purpose="closing_auction_matching",
        as_of=DAY + "T15:00:00+08:00",
    )
    assert len(before) == 3 and len(after) == 4
    assert before[-1]["volume"] == "0.0"
    assert after[-1]["exchange_matching_verified"] is False


def test_daily_volume_posthoc_access_has_no_future_close(tmp_path):
    view = make_view(tmp_path)
    daily = view.read(
        "600000.XSHG", trade_date=DAY, purpose="closed_daily", as_of=DAY + "T15:00:00+08:00"
    )
    assert daily["volume"] == "29492600.0"  # 239 x 123400, with one real fixture zero retained.
    assert len(daily["evidence"]["rows"]) == len(daily["coverage"]["expected_labels"]) == 240
    capacity = view.read(
        "600000.XSHG",
        trade_date=DAY,
        purpose="jq_daily_initial_capacity",
        as_of=DAY + "T09:35:00+08:00",
    )
    assert capacity["volume"] == daily["volume"]
    assert capacity["non_pit"] is True and capacity["strategy_visible"] is False
    assert "close" not in capacity and "price" not in capacity
    assert capacity["historical_available_at"] is None
    assert capacity["replay_available_at"] == daily["replay_available_at"]


@pytest.mark.parametrize("label", ["09:31", "11:00", "14:59", "15:00"])
def test_missing_source_label_rejected_not_filled(tmp_path, label):
    view = make_view(tmp_path, omit=DAY + f"T{label}:00+08:00")
    with pytest.raises(DataError) as exc:
        view.export()
    assert exc.value.code == "MINUTE_SESSION_MISSING_LABELS"
    assert exc.value.details["missing_labels"] == [DAY + f"T{label}:00+08:00"]


@pytest.mark.parametrize("kwargs", [{"frequency": "5m"}, {"bad_sid": True}])
def test_wrong_capture_scope_rejected(tmp_path, kwargs):
    with pytest.raises(DataError) as exc:
        make_view(tmp_path, **kwargs).export()
    assert exc.value.code == "MINUTE_SESSION_SOURCE_SCOPE"


def test_hash_tamper_after_construction_rejected(tmp_path):
    view = make_view(tmp_path)
    view.export()
    p = tmp_path / "page-000.json"
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(DataError) as exc:
        view.export()
    assert exc.value.code == "TDX_INTEGRITY"


def test_scope_model_and_future_requests_rejected(tmp_path):
    view = make_view(tmp_path)
    with pytest.raises(DataError):
        TDXMinuteSessionView(view._sources, execution_days=[DAY], model="verified")
    with pytest.raises(DataError):
        TDXMinuteSessionView(view._sources, execution_days=[DAY], history_days=[DAY], model=MODEL)
    with pytest.raises(DataError):
        view.read(
            "000001.XSHE", trade_date=DAY, purpose="opening_price", as_of=DAY + "T09:30:00+08:00"
        )
    with pytest.raises(DataError):
        view.read(
            "600000.XSHG",
            trade_date="2026-10-12",
            purpose="closed_daily",
            as_of=DAY + "T15:00:00+08:00",
        )


def test_export_decimal_precision_and_determinism(tmp_path):
    view = make_view(tmp_path)
    before = view.export_bytes()
    with localcontext() as context:
        context.prec = 3
        assert view.export_bytes() == before
