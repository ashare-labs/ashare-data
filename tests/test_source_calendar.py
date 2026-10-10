"""Bounded calendar contracts. Synthetic dates never count as actual source coverage."""
import json
import os
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from ashare_data import Store, BaoStockSource, SourceCalendarResult
from ashare_data import baostock as api
from ashare_data.cli import main
from test_baostock import bundle
from test_daily_status import persist, raises_code, resign_receipt

LEGACY_ID = "bc5099a071aae74b8b84750224113600bac6c3aed9598561478554578c22b3b7"


def captured(tmp_path, monkeypatch, flags=("1", "1", "0", "1", "0", "1", "0"), *,
             start="2024-01-05", rows=None, empty=False, clock=None):
    first = date.fromisoformat(start)
    end = (first + timedelta(days=len(flags) - 1)).isoformat()
    data = ([[ (first + timedelta(days=i)).isoformat(), flag] for i, flag in enumerate(flags)]
            if rows is None else rows)
    original = api.request
    with monkeypatch.context() as patch:
        patch.setattr(api, "request", lambda *args, **kwargs: original("calendar", None, start, end))
        blobs, _ = bundle(tmp_path, "calendar", rows=[] if empty else data, clock=clock)
    path = persist(tmp_path / "export", blobs)
    store = Store.init(tmp_path / "store")
    sid = store.import_baostock_capture(path)
    return store, sid, path


def test_generic_rows_links_and_old_trade_days_unchanged(tmp_path, monkeypatch):
    store, sid, path = captured(tmp_path, monkeypatch)
    assert store.import_baostock_capture(path) == sid
    view = store.baostock(sid)
    result = view.get_calendar(require_known=True)
    assert isinstance(result, SourceCalendarResult)
    assert [r.is_open.value for r in result.rows] == [True, True, False, True, False, True, False]
    assert result.report["coverage"]["requested_values_known"] is True
    assert result.report["coverage"]["weekday_inference"] is False
    assert result.report["quality"]["execution_permission"] is False
    assert result.report["quality"]["historical_pit"] is False
    links = view.get_calendar_links(trading_dates=["2024-01-08", date(2024, 1, 6)])
    assert [(r.previous_open, r.trading_date, r.next_open) for r in links.rows] == [
        (date(2024, 1, 6), date(2024, 1, 8), date(2024, 1, 10)),
        (date(2024, 1, 5), date(2024, 1, 6), date(2024, 1, 8)),
    ]
    assert [r.calendar_date for r in links.rows[0].evidence_rows] == [date(2024, 1, i) for i in range(6, 11)]
    assert [r.is_open.value for r in links.rows[0].evidence_rows] == [True, False, True, False, True]
    assert links.rows[0].execution_permission is False
    expected = [r.calendar_date.isoformat() for r in result.rows if r.is_open.value]
    assert list(view.get_trade_days().data["calendar_date"]) == expected
    assert Store(store.root).baostock(sid).get_calendar_links(trading_dates=["2024-01-08"]).rows[0] == links.rows[0]


def test_synthetic_long_closure_no_weekday_inference(tmp_path, monkeypatch):
    flags = ("1", "1", *("0",) * 8, "1")
    store, sid, _ = captured(tmp_path, monkeypatch, flags=flags, start="2026-09-29")
    link = store.baostock(sid).get_calendar_links(trading_dates=["2026-09-30"]).rows[0]
    assert link.previous_open == date(2026, 9, 29)
    assert link.next_open == date(2026, 10, 9)
    assert len(link.evidence_rows) == 11
    assert all(r.evidence_kind == "offline_test_only" for r in link.evidence_rows)


@pytest.mark.parametrize("anchor,code", [
    ("2024-01-05", "SOURCE_CALENDAR_BOUNDARY_UNKNOWN"),
    ("2024-01-10", "SOURCE_CALENDAR_BOUNDARY_UNKNOWN"),
    ("2024-01-07", "SOURCE_CALENDAR_NOT_OPEN"),
    ("2024-01-04", "SOURCE_CALENDAR_OUT_OF_SCOPE"),
    ("2024-01-12", "SOURCE_CALENDAR_OUT_OF_SCOPE"),
])
def test_boundaries_and_closed_anchors(tmp_path, monkeypatch, anchor, code):
    store, sid, _ = captured(tmp_path, monkeypatch)
    with raises_code(code):
        store.baostock(sid).get_calendar_links(trading_dates=[anchor])
    with raises_code(code):
        store.baostock(sid).get_calendar_links(trading_dates=["2024-01-08", anchor])


@pytest.mark.parametrize("days", [None, "2024-01-08", [], (), ["2024-01-08"] * 2,
                                ["2024-01-08"] * 32, [True], [1], ["20240108"],
                                ["2024-1-8"], ["2024-02-30"], [datetime(2024, 1, 8)],
                                ["2024-01-08T00:00:00+08:00"], {"2024-01-08"}])
def test_argument_contract(tmp_path, monkeypatch, days):
    store, sid, _ = captured(tmp_path, monkeypatch)
    with raises_code("INVALID_ARGUMENT"):
        store.baostock(sid).get_calendar_links(trading_dates=days)


@pytest.mark.parametrize("strict", [1, None, "true"])
def test_strict_bool(tmp_path, monkeypatch, strict):
    store, sid, _ = captured(tmp_path, monkeypatch)
    with raises_code("INVALID_ARGUMENT"):
        store.baostock(sid).get_calendar(require_known=strict)


@pytest.mark.parametrize("start,flags", [("0001-01-01", ("1", "1", "1")),
                                        ("9999-12-29", ("1", "1", "1"))])
def test_date_extremes(tmp_path, monkeypatch, start, flags):
    store, sid, _ = captured(tmp_path, monkeypatch, flags, start=start)
    rows = store.baostock(sid).get_calendar(require_known=True).rows
    assert len(rows) == 3
    link = store.baostock(sid).get_calendar_links(trading_dates=[rows[1].calendar_date]).rows[0]
    assert (link.previous_open, link.next_open) == (rows[0].calendar_date, rows[2].calendar_date)
    for anchor in (rows[0].calendar_date, rows[-1].calendar_date):
        with raises_code("SOURCE_CALENDAR_BOUNDARY_UNKNOWN"):
            store.baostock(sid).get_calendar_links(trading_dates=[anchor])


def test_empty_success_remains_unknown(tmp_path, monkeypatch):
    store, sid, _ = captured(tmp_path, monkeypatch, empty=True)
    view = store.baostock(sid)
    rows = view.get_calendar().rows
    assert len(rows) == 7
    for row in rows:
        assert row.is_open.value is None and row.is_open.state == "row_missing_unknown"
        assert row.raw_record_sha256 is row.raw_response_sha256 is row.response_received_at is None
        assert row.source_fields == {}
    with raises_code("SOURCE_CALENDAR_UNKNOWN"):
        view.get_calendar(require_known=True)
    with raises_code("SOURCE_CALENDAR_UNKNOWN"):
        view.get_calendar_links(trading_dates=["2024-01-08"])


@pytest.mark.parametrize("rows,code", [
    ([["2024-01-05", "1"]], "SOURCE_SCHEMA_ERROR"),
    # Protocol identity validation rejects duplicates/out-of-range before row projection.
    ([["2024-01-05", "1"], ["2024-01-05", "0"]], "SOURCE_REQUEST_FAILED"),
    ([["2024-01-04", "1"]], "SOURCE_REQUEST_FAILED"),
    ([["2024-01-05", ""]], "SOURCE_SCHEMA_ERROR"),
    ([["2024-01-05", "2"]], "SOURCE_SCHEMA_ERROR"),
    ([["2024-01-05", "true"]], "SOURCE_SCHEMA_ERROR"),
])
def test_invalid_calendar_is_not_repaired(tmp_path, monkeypatch, rows, code):
    store, sid, _ = captured(tmp_path, monkeypatch, rows=rows)
    with raises_code(code):
        store.baostock(sid).get_calendar()


def test_immutable_nested_results_and_metadata(tmp_path, monkeypatch):
    store, sid, _ = captured(tmp_path, monkeypatch)
    view = store.baostock(sid)
    result = view.get_calendar()
    before = result.to_dict()
    result.report["coverage"]["missing_dates"].append("2024-01-08")
    result.rows[0].source_fields["is_trading_day"] = "0"
    result.to_dict()["rows"][0]["is_open"]["value"] = False
    with pytest.raises(FrozenInstanceError):
        result.rows[0].is_open.value = False
    assert view.get_calendar().to_dict() == before
    assert Store(store.root).baostock(sid).get_calendar().to_dict() == before
    capabilities = BaoStockSource.capabilities()
    capabilities["source_calendar_policy"] = "changed"
    assert BaoStockSource.capabilities()["source_calendar_policy"] == "source-calendar-1"


@pytest.mark.parametrize("kind", ["basic", "daily"])
def test_wrong_kind(tmp_path, kind):
    blobs, _ = bundle(tmp_path, kind)
    store = Store.init(tmp_path / "store")
    sid = store.import_baostock_capture(persist(tmp_path / "export", blobs))
    with raises_code("QUERY_KIND_MISMATCH"):
        store.baostock(sid).get_calendar()


@pytest.mark.parametrize("field,value", [("last_byte_received_at", "bad"),
                                        ("last_byte_received_at", "2024-01-01T00:00:00"),
                                        ("last_byte_received_at", None),
                                        ("first_byte_received_at", "2024-01-01T00:00:00Z")])
def test_clock_binding(tmp_path, monkeypatch, field, value):
    store, _, path = captured(tmp_path, monkeypatch)
    resign_receipt(path, lambda r: r["pages"][0].__setitem__(field, value))
    sid = store.import_baostock_capture(path)
    with raises_code("SOURCE_CALENDAR_CLOCK_INVALID"):
        store.baostock(sid).get_calendar()


def test_recorded_clock_failure_does_not_fabricate_time(tmp_path, monkeypatch):
    store, sid, _ = captured(tmp_path, monkeypatch, clock=lambda: None)
    rows = store.baostock(sid).get_calendar().rows
    assert all(row.response_received_at is None for row in rows)
    assert all(row.historical_available_at is None for row in rows)
    with raises_code("PIT_UNAVAILABLE"):
        store.baostock(sid).at("2026-10-10T00:00:00Z", visibility="verified")


def test_cli_success_and_parameter_rejection(tmp_path, monkeypatch, capsys):
    store, sid, _ = captured(tmp_path, monkeypatch)
    args = ["--store", str(store.root), "baostock-query"]
    assert main(args + ["calendar", "--capture", sid, "--require-known"]) == 0
    assert json.loads(capsys.readouterr().out) == store.baostock(sid).get_calendar().to_dict()
    assert main(args + ["calendar-links", "--capture", sid, "--dates", "2024-01-08"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"][0]["previous_open"] == "2024-01-06"
    for more in (["calendar-links"], ["calendar-links", "--dates", "2024-01-05"],
                 ["calendar", "--dates", "2024-01-08"],
                 ["calendar-links", "--dates", "2024-01-08", "--require-known"],
                 ["calendar", "--end", "2024-01-08T15:00:00+08:00"]):
        assert main(args + list(more) + ["--capture", sid]) == 2
        assert json.loads(capsys.readouterr().err)["error"]["code"]


@pytest.mark.parametrize("corrupt", [False, True])
def test_existing_publication_recovery_with_new_calendar_view(tmp_path, monkeypatch, corrupt):
    _, _, path = captured(tmp_path, monkeypatch)
    store = Store.init(tmp_path / "second-store")
    original = api.immutable_write

    def interrupt(target, data):
        if target.parent.name == "baostock-manifests":
            raise RuntimeError("synthetic interruption")
        return original(target, data)

    with monkeypatch.context() as patch:
        patch.setattr(api, "immutable_write", interrupt)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            store.import_baostock_capture(path)
    sid = store.baostock_snapshots()[0]["capture_id"]
    if corrupt:
        h = api.bh((path / "capture/page-01.response.bin").read_bytes())
        p = store.root / "baostock-objects" / h
        p.chmod(0o644)
        p.write_bytes(b"broken")
    assert store.recover_baostock() == [{"capture_id": sid, "status": "aborted" if corrupt else "published"}]
    if not corrupt:
        assert len(store.baostock(sid).get_calendar(require_known=True).rows) == 7


def test_real_existing_calendar_is_not_new_window_coverage(monkeypatch):
    root = os.environ.get("ASHARE_CALENDAR_STORE")
    if not root:
        pytest.skip("existing real calendar store not supplied")
    monkeypatch.setattr(BaoStockSource, "fetch", lambda *a, **k: pytest.fail("must stay offline"))
    view = Store(Path(root)).baostock(LEGACY_ID)
    result = view.get_calendar(require_known=True)
    assert [r.calendar_date.isoformat() for r in result.rows] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    assert all(r.is_open.raw_value == "1" and r.is_open.value for r in result.rows)
    assert all(r.evidence_kind == "live_local_capture" for r in result.rows)
    assert all(r.raw_response_sha256 and r.raw_record_sha256 for r in result.rows)
    link = view.get_calendar_links(trading_dates=["2026-09-29"]).rows[0]
    assert (link.previous_open, link.next_open) == (date(2026, 9, 28), date(2026, 9, 30))
    assert result.report["network_used"] is False
    with raises_code("SOURCE_CALENDAR_OUT_OF_SCOPE"):
        view.get_calendar_links(trading_dates=["2026-10-09"])
    with raises_code("PIT_UNAVAILABLE"):
        view.at("2026-10-11T00:00:00Z", visibility="verified")


def test_real_new_window_failure_not_empty_success(tmp_path):
    root = os.environ.get("ASHARE_CALENDAR_FAILURE")
    if not root:
        pytest.skip("external failed calendar acquisition not supplied")
    attempt = json.loads((Path(root) / "attempt.json").read_text())
    assert attempt["status"] == "login_failed"
    assert attempt["login"]["error_code"] == "10002007"
    store = Store.init(tmp_path / "failed-store")
    sid = store.import_baostock_capture(root)
    assert store.import_baostock_capture(root) == sid
    view = Store(store.root).baostock(sid)
    assert view.descriptor()["rows"] == 0
    with raises_code("SOURCE_REQUEST_FAILED"):
        view.get_calendar()
    with raises_code("SOURCE_REQUEST_FAILED"):
        view.get_calendar_links(trading_dates=["2026-09-30"])
