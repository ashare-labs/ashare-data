"""Synthetic protocol-record fixtures, not licensed market-data redistribution."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import json

import pytest

from ashare_data import DataError, TDXBarSource, TDXBarView
from ashare_data.tdx_bars import _seal
from ashare_data.tdx_bars_core import collect_pages, encoded, request


@dataclass
class Bar:
    time: datetime
    open: float = 10.0
    high: float = 10.2
    low: float = 9.9
    close: float = 10.1
    open_price_milli: int = 10000
    high_price_milli: int = 10200
    low_price_milli: int = 9900
    close_price_milli: int = 10100
    volume_raw: int = 1
    amount_raw: int = 2
    volume_wire_value: float = 123400.0
    volume_lots: float = 1234.0
    amount: float = 1246340.0
    record_hex: str = "00000000000000000000000000000000"


BASE = datetime(2020, 1, 2, 7, 0, tzinfo=timezone.utc)


def page(offset, count, size=None, newest=None):
    size = count if size is None else size
    newest = BASE - timedelta(minutes=offset) if newest is None else newest
    bars = tuple(Bar(newest - timedelta(minutes=i)) for i in reversed(range(size)))
    return SimpleNamespace(bars=bars, raw_payload=size.to_bytes(2, "little") +
                           b"".join(bytes.fromhex(b.record_hex) for b in bars),
                           full_code="sh600000", start=offset, request_count=count,
                           period_name="1m", adjust_mode_raw=0)


def capture(tmp_path, fetch=page, **changes):
    config = request("600000.XSHG", "116.205.183.150:7709", "1m", 0, 4, 2, 3, 6, 1)
    config.update(changes)
    (tmp_path / "request.json").write_bytes(encoded(config))
    journal = collect_pages(tmp_path, config, fetch, {"name": "synthetic-test"}, sleep=lambda _: None)
    view = TDXBarView(tmp_path, expected_sha256=_seal(tmp_path))
    return view, journal


def test_native_paging_decimal_units_and_offline_selection(tmp_path):
    calls = []
    def fetch(offset, count):
        calls.append((offset, count))
        return page(offset, count)
    view, journal = capture(tmp_path, fetch)
    assert calls == [(0, 2), (2, 2)]
    assert journal["stop_reason"] == "requested_count_reached"
    result = view.get_price(mode="retrospective", labels=[BASE.isoformat(), "2019-01-01T15:00:00+08:00"])
    assert len(result.data) == 1
    assert result.data.iloc[0]["volume"] == Decimal(123400)
    assert result.data.iloc[0]["close"] == Decimal("10.1")
    assert result.report["missing_requested_labels"] == ["2019-01-01T15:00:00+08:00"]
    for key in ("complete_history", "historical_pit", "execution_admitted", "network_used"):
        assert result.report[key] is False
    assert result.report["historical_available_at"] is None
    result.report["sdk"]["name"] = "changed"
    assert view.describe()["sdk"]["name"] == "synthetic-test"


def test_short_page_advances_actual_rows_and_continues(tmp_path):
    calls = []
    def fetch(offset, count):
        calls.append((offset, count))
        return page(offset, count, size=1 if offset == 0 else count)
    view, journal = capture(tmp_path, fetch, count=3)
    assert calls == [(0, 2), (1, 2)]
    assert journal["pages"][0]["status"] == "short_page_continue"
    assert len(view.get_price(mode="retrospective").data) == 3


@pytest.mark.parametrize("kind,reason", [("empty", "remote_empty_page"),
    ("repeat", "repeated_or_overlapping_page"), ("forward", "non_monotonic_page"),
    ("timeout", "timeout"), ("rate", "rate_limited"), ("auth", "authorization_failure")])
def test_stops_preserves_partial_and_requires_explicit_opt_in(tmp_path, kind, reason):
    def fetch(offset, count):
        if offset == 0:
            return page(offset, count)
        if kind == "empty":
            return page(offset, count, size=0)
        if kind == "repeat":
            return page(offset, count, newest=BASE)
        if kind == "forward":
            return page(offset, count, newest=BASE+timedelta(minutes=10))
        if kind == "timeout":
            raise TimeoutError("test timeout")
        raise RuntimeError("429 rate limit" if kind == "rate" else "403 forbidden")
    view, journal = capture(tmp_path, fetch)
    assert journal["stop_reason"] == reason
    assert len(journal["pages"]) == 2
    with pytest.raises(DataError, match="before requested count"):
        view.get_price(mode="retrospective")
    assert len(view.get_price(mode="retrospective", allow_partial=True).data) == 2
    if kind in {"repeat", "forward", "empty"}:
        assert (tmp_path / "page-001.bin").exists()


def test_local_page_cap_is_not_remote_exhaustion(tmp_path):
    view, _ = capture(tmp_path, max_pages=1)
    report = view.describe()
    assert report["stop_reason"] == "local_page_limit"
    assert report["next_offset"] == 2
    assert report["complete_history"] is False


def test_protocol_limit_never_wraps_offset(tmp_path):
    calls = []
    def fetch(offset, count):
        calls.append(offset)
        return page(offset, count)
    view, _ = capture(tmp_path, fetch, start_offset=65535)
    assert calls == [65535]
    assert view.describe()["stop_reason"] == "protocol_offset_limit"


def test_invalid_ohlc_retains_raw_but_never_returns_row(tmp_path):
    def fetch(offset, count):
        p = page(offset, count)
        p.bars[0].high = 9.0
        p.bars[0].high_price_milli = 9000
        return p
    view, _ = capture(tmp_path, fetch)
    assert (tmp_path / "page-000.bin").exists()
    assert view.describe()["stop_reason"] == "source_error"
    assert view.get_price(mode="retrospective", allow_partial=True).data.empty


def test_reopen_tamper_is_rejected(tmp_path):
    view, _ = capture(tmp_path)
    path = tmp_path / "page-000.bin"
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(DataError) as exc:
        view.get_price(mode="retrospective")
    assert exc.value.code == "TDX_INTEGRITY"


def test_missing_sdk_retains_failure_without_installation(tmp_path):
    view = TDXBarSource(host="116.205.183.150:7709", python_executable="/nonexistent/python").fetch(
        "600000.XSHG", directory=tmp_path/"capture")
    assert view.describe()["stop_reason"] == "sdk_python_unavailable"
    assert view.describe()["accepted_rows"] == 0


def test_existing_capture_and_invalid_arguments_do_not_connect(tmp_path):
    source = TDXBarSource(host="116.205.183.150:7709", python_executable="/nonexistent/python")
    with pytest.raises(DataError) as exc:
        source.fetch("600000.XSHG", directory=tmp_path)
    assert exc.value.code == "TDX_CAPTURE_EXISTS"
    for kwargs in ({"start_offset": 65536}, {"page_size": 801}, {"frequency": "15m"}, {"count": True}):
        with pytest.raises(DataError):
            source.fetch("600000.XSHG", directory=tmp_path/"unused", **kwargs)
    for mode in ("verified", "assumed", None):
        with pytest.raises(DataError):
            source.get_price("600000.XSHG", directory=tmp_path/"unused", mode=mode)
    with pytest.raises(DataError):
        source.get_price("600000.XSHG", directory=tmp_path/"unused", mode="retrospective",
                         start="2024-03-04 09:58")
    assert not (tmp_path/"unused").exists()


def test_forged_manifest_cannot_change_pinned_view(tmp_path):
    view, _ = capture(tmp_path)
    path = tmp_path / "manifest.json"
    value = json.loads(path.read_bytes())
    value["files"]["../escape"] = "0" * 64
    path.write_bytes(encoded(value))
    with pytest.raises(DataError):
        TDXBarView(tmp_path, expected_sha256=view.manifest_sha256)
