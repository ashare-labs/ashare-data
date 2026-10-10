"""Real externally supplied captures plus small, explicitly synthetic failure controls."""

import json
from contextlib import contextmanager
import os
from pathlib import Path
from dataclasses import FrozenInstanceError

import pytest

from ashare_data import Store, DataError, BaoStockSource, DailyStatusResult
from ashare_data import baostock as api, daily_status
from ashare_data.cli import main
from test_baostock import bundle


@contextmanager
def raises_code(code):
    with pytest.raises(DataError) as exc:
        yield
    assert exc.value.code == code


def captured(tmp_path, monkeypatch, rows=None, *, start="2020-01-02", end="2020-01-02", clock=None):
    original = api.request
    with monkeypatch.context() as patch:
        patch.setattr(api, "FIELDS", daily_status.FIELDS)
        patch.setattr(
            api,
            "request",
            lambda kind, security, ignored_start, stop: original(
                "daily_status", security, start, end
            ),
        )
        blobs, _ = bundle(
            tmp_path,
            "daily",
            rows=([["2020-01-02", "sh.600000", "1", "0", "3"]] if rows is None else rows),
            clock=clock,
        )
    return persist(tmp_path / "export", blobs)


def persist(path, blobs):
    path.mkdir()
    for name, value in blobs.items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(value)
    return path


@pytest.mark.parametrize("code", ["sh.600000", "sz.000001", "sz.300750"])
def test_real_captures_offline_roundtrip(tmp_path, monkeypatch, code):
    location = os.environ.get("ASHARE_DAILY_STATUS_EVIDENCE")
    if not location:
        pytest.skip("real external daily-status capture not supplied")

    def denied(*args, **kwargs):
        raise AssertionError("offline read must not fetch")

    monkeypatch.setattr(BaoStockSource, "fetch", denied)
    source = Path(location) / code
    raw = json.loads((source / "capture/sdk-rows.json").read_text())
    store = Store.init(tmp_path / "store")
    sid = store.import_baostock_capture(source)
    assert store.import_baostock_capture(source) == sid
    view = store.baostock(sid)
    result = view.get_status(require_known=True)
    assert isinstance(result, DailyStatusResult) and len(result.rows) == 3
    assert [r.source_fields for r in result.rows] == raw
    assert all(r.suspended.raw_value == "1" and r.suspended.value is False for r in result.rows)
    assert all(r.is_st.raw_value == "0" and r.is_st.value is False for r in result.rows)
    for r in result.rows:
        assert r.raw_record_sha256 == api.sh(r.source_fields)
        assert r.raw_response_sha256 and r.response_received_at.startswith("2026-10-10T")
        assert r.historical_available_at is r.historical_eligible is None
        assert r.evidence_kind == "live_local_capture" and not r.execution_permission
    assert result.report["coverage"]["requested_values_known"]
    assert not result.report["quality"]["historical_pit"]
    assert not result.report["quality"]["execution_permission"]
    assert not result.report["network_used"]
    assert view.descriptor()["version"] == daily_status.VERSION
    with raises_code("RECEIPT_NOT_VISIBLE"):
        view.at("2026-10-11T00:00:00Z")
    with raises_code("PIT_UNAVAILABLE"):
        view.at("2026-10-11T00:00:00Z", visibility="verified")


def test_request_is_generic_bounded_and_legacy_unchanged():
    old = api.request("daily", "600519.XSHG", "2020-01-02", "2020-01-03")
    new = api.request("daily_status", "600519.XSHG", "2020-01-02", "2020-01-03")
    assert old["params"]["fields"] == api.FIELDS
    assert new["params"]["fields"] == "date,code,tradestatus,isST,adjustflag"
    assert new["params"]["code"] == "sh.600519"
    with raises_code("BOUNDED_REQUEST"):
        api.request("daily_status", "600519.XSHG", "2020-01-01", "2020-02-01")
    with raises_code("INVALID_ARGUMENT"):
        api.request("daily_status", "600519.XSHG", "2020-01-01", "2020-01-02", "1m")


def test_old_daily_projects_missing_st_without_changing_price(tmp_path):
    blobs, _ = bundle(tmp_path, "daily")
    store = Store.init(tmp_path / "store")
    sid = store.import_baostock_capture(persist(tmp_path / "export", blobs))
    view = store.baostock(sid)
    before = view.get_price().to_dict()
    result = view.get_status()
    assert result.rows[0].suspended.value is False
    assert result.rows[0].is_st.value is None
    assert result.rows[0].is_st.state == "field_not_requested"
    assert result.rows[0].evidence_kind == "offline_test_only"
    assert result.report["quality"]["synthetic"] is True
    assert before == view.get_price().to_dict()
    assert view.descriptor()["version"] == api.VERSION
    with raises_code("DAILY_STATUS_UNKNOWN"):
        view.get_status(require_known=True)


@pytest.mark.parametrize(
    "trade,st,trade_state,st_state",
    [
        ("0", "1", "source_observed", "source_observed"),
        ("", "0", "source_empty", "source_observed"),
        ("1", "", "source_observed", "source_empty"),
        ("2", "0", "unsupported_enum", "source_observed"),
        ("1", "false", "source_observed", "unsupported_enum"),
    ],
)
def test_flags_are_independent_and_unknown_is_not_false(
    tmp_path,
    monkeypatch,
    trade,
    st,
    trade_state,
    st_state,
):
    path = captured(tmp_path, monkeypatch, [["2020-01-02", "sh.600000", trade, st, "3"]])
    store = Store.init(tmp_path / "store")
    view = store.baostock(store.import_baostock_capture(path))
    row = view.get_status().rows[0]
    assert (row.suspended.state, row.is_st.state) == (trade_state, st_state)
    assert row.suspended.value == ({"0": True, "1": False}.get(trade))
    assert row.is_st.value == ({"0": False, "1": True}.get(st))
    if "source_observed" != trade_state or "source_observed" != st_state:
        with raises_code("DAILY_STATUS_UNKNOWN"):
            view.get_status(require_known=True)


@pytest.mark.parametrize("empty", [False, True])
def test_natural_day_gaps_are_unknown_not_calendar_or_halt(tmp_path, monkeypatch, empty):
    path = captured(tmp_path, monkeypatch, [] if empty else None, end="2020-01-05")
    store = Store.init(tmp_path / "store")
    view = store.baostock(store.import_baostock_capture(path))
    result = view.get_status()
    assert len(result.rows) == 4
    for row in result.rows[0 if empty else 1 :]:
        assert row.suspended.state == row.is_st.state == "row_missing_unknown"
        assert row.suspended.value is row.is_st.value is None
        assert row.raw_record_sha256 is row.raw_response_sha256 is row.response_received_at is None
    assert not result.report["coverage"]["calendar_inferred"]
    with raises_code("DAILY_STATUS_UNKNOWN"):
        view.get_status(require_known=True)


def test_types_cli_and_wrong_kind_refusal(tmp_path, monkeypatch, capsys):
    path = captured(tmp_path, monkeypatch)
    store = Store.init(tmp_path / "store")
    assert main(["--store", str(store.root), "baostock-import", str(path)]) == 0
    sid = json.loads(capsys.readouterr().out)["capture_id"]
    result = store.baostock(sid).get_status()
    row = result.rows[0]
    with pytest.raises(FrozenInstanceError):
        row.suspended.value = True
    row.source_fields["tradestatus"] = "0"
    result.report["quality"]["historical_pit"] = True
    assert row.source_fields["tradestatus"] == "1"
    assert result.report["quality"]["historical_pit"] is False
    args = ["--store", str(store.root), "baostock-query", "statuses", "--capture", sid]
    assert main(args + ["--require-known"]) == 0
    assert json.loads(capsys.readouterr().out) == result.to_dict()
    with raises_code("QUERY_KIND_MISMATCH"):
        store.baostock(sid).get_price()
    assert main(args + ["--end", "2020-01-02T15:00:00+08:00"]) == 2
    assert "UNSUPPORTED_FREQUENCY" in capsys.readouterr().err
    assert (
        main(
            [
                "--store",
                str(store.root),
                "baostock-query",
                "quality",
                "--capture",
                sid,
                "--require-known",
            ]
        )
        == 2
    )
    assert "INVALID_ARGUMENT" in capsys.readouterr().err
    basic = tmp_path / "basic"
    basic.mkdir()
    blobs, _ = bundle(basic)
    basic_id = store.import_baostock_capture(persist(basic / "export", blobs))
    with raises_code("QUERY_KIND_MISMATCH"):
        store.baostock(basic_id).get_status()


@pytest.mark.parametrize("corrupt", [False, True])
def test_interrupted_status_publish_recovers_or_aborts(tmp_path, monkeypatch, corrupt):
    path = captured(tmp_path, monkeypatch)
    store = Store.init(tmp_path / "store")
    original = api.immutable_write

    def interrupt(path, data):
        if path.parent.name == "baostock-manifests":
            raise RuntimeError("simulated interruption")
        return original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(api, "immutable_write", interrupt)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            store.import_baostock_capture(path)
    sid = store.baostock_snapshots()[0]["capture_id"]
    with raises_code("BAOSTOCK_NOT_PUBLISHED"):
        store.baostock(sid)
    if corrupt:
        digest = api.bh((path / "capture/page-01.response.bin").read_bytes())
        target = store.root / "baostock-objects" / digest
        target.chmod(0o644)
        target.write_bytes(b"broken")
    assert store.recover_baostock() == [
        {"capture_id": sid, "status": "aborted" if corrupt else "published"}
    ]
    if not corrupt:
        assert store.baostock(sid).get_status(require_known=True).rows[0].is_st.value is False


def test_import_rejects_symlink_and_corruption(tmp_path, monkeypatch):
    path = captured(tmp_path, monkeypatch)
    store = Store.init(tmp_path / "store")
    link = path / "outside.json"
    link.symlink_to(path / "attempt.json")
    with raises_code("BOUNDED_IMPORT"):
        store.import_baostock_capture(path)
    link.unlink()
    (path / "capture/page-01.response.bin").write_bytes(b"broken")
    with raises_code("INTEGRITY"):
        store.import_baostock_capture(path)


def test_invalid_strict_input_does_not_fetch(monkeypatch):
    source = object.__new__(BaoStockSource)
    monkeypatch.setattr(source, "fetch", lambda *a, **k: pytest.fail("unexpected fetch"))
    with raises_code("INVALID_ARGUMENT"):
        source.get_status(
            "600000.XSHG",
            store=None,
            start_date="2020-01-02",
            end_date="2020-01-02",
            require_known=1,
        )


def resign_receipt(directory, mutate):
    """Re-sign an explicit synthetic inconsistency, not a real capture."""
    receipt_path = directory / "capture/receipt.json"
    receipt = json.loads(receipt_path.read_text())
    mutate(receipt)
    receipt.pop("receipt_id")
    receipt["receipt_id"] = api.sh(receipt)
    receipt_path.write_bytes(api.canonical(receipt))
    research_path = directory / "capture/research-rows.json"
    research = json.loads(research_path.read_text())
    for row in research:
        row["receipt_id"] = receipt["receipt_id"]
    research_path.write_bytes(api.canonical(research))
    storage_path = directory / "capture/storage-record.json"
    storage = json.loads(storage_path.read_text())
    storage["receipt_id"] = receipt["receipt_id"]
    for entry in storage["artifacts"]:
        value = (directory / "capture" / entry["path"]).read_bytes()
        entry.update(bytes=len(value), sha256=api.bh(value))
    storage.pop("storage_id")
    storage["storage_id"] = api.sh(storage)
    storage_path.write_bytes(api.canonical(storage))


@pytest.mark.parametrize(
    "field,value",
    [
        ("last_byte_received_at", "not-a-timestamp"),
        ("last_byte_received_at", "2020-01-02T00:00:00+00:00"),
        ("last_byte_received_at", None),
        ("first_byte_received_at", "2020-01-02T00:00:00+00:00"),
        ("request_started_at", "2020-01-02T00:00:00+00:00"),
    ],
)
def test_page_time_must_match_events(tmp_path, monkeypatch, field, value):
    path = captured(tmp_path, monkeypatch)

    def mutate(receipt):
        if value is None:
            receipt["pages"][0].pop(field)
        else:
            receipt["pages"][0][field] = value

    resign_receipt(path, mutate)
    with raises_code("DAILY_STATUS_CLOCK_INVALID"):
        Store.init(tmp_path / "store").import_baostock_capture(path)


def test_recorded_missing_clock_stays_unknown(tmp_path, monkeypatch):
    path = captured(tmp_path, monkeypatch, clock=lambda: None)
    store = Store.init(tmp_path / "store")
    result = store.baostock(store.import_baostock_capture(path)).get_status(require_known=True)
    assert result.rows[0].response_received_at is None
    assert result.report["quality"]["capture_clock_state"] == "unknown"


def test_max_date_empty_capture_returns_unknown(tmp_path, monkeypatch):
    path = captured(tmp_path, monkeypatch, rows=[], start="9999-12-31", end="9999-12-31")
    store = Store.init(tmp_path / "store")
    result = store.baostock(store.import_baostock_capture(path)).get_status()
    assert len(result.rows) == 1
    assert result.rows[0].trade_date.isoformat() == "9999-12-31"
    assert result.rows[0].is_st.state == "row_missing_unknown"
