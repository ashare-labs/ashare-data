"""External real evidence and explicitly synthetic controls; no business network."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
import json
import os
from pathlib import Path

import pytest

from ashare_data import BaoStockSource, SourcePrecloseResult, Store
from ashare_data import baostock as api, preclose
from ashare_data.cli import main
from test_baostock import bundle
from test_daily_status import persist, raises_code, resign_receipt


def captured(tmp_path, monkeypatch, rows=None, *, start="2020-01-02", end="2020-01-02", clock=None):
    original = api.request
    with monkeypatch.context() as patch:
        patch.setattr(api, "FIELDS", preclose.FIELDS)
        patch.setattr(
            api,
            "request",
            lambda kind, security, a, b: original("daily_preclose", security, start, end),
        )
        blobs, _ = bundle(
            tmp_path,
            "daily",
            rows=([[start, "sh.600000", "9.1234", "3"]] if rows is None else rows),
            clock=clock,
        )
    return persist(tmp_path / "export", blobs)


def view_for(path, tmp_path):
    store = Store.init(tmp_path / "store")
    return store.baostock(store.import_baostock_capture(path))


@pytest.mark.parametrize("code", ["sh.600000", "sz.000001", "sz.300750"])
def test_real_captures_roundtrip(tmp_path, monkeypatch, code):
    location = os.environ.get("ASHARE_PRECLOSE_EVIDENCE")
    if not location:
        pytest.skip("real external preclose capture not supplied")
    monkeypatch.setattr(BaoStockSource, "fetch", lambda *a, **k: pytest.fail("unexpected fetch"))
    source = Path(location) / code
    raw = json.loads((source / "capture/sdk-rows.json").read_text())
    receipt = json.loads((source / "capture/receipt.json").read_text())
    store = Store.init(tmp_path / "store")
    sid = store.import_baostock_capture(source)
    assert store.import_baostock_capture(source) == sid
    view = Store(store.root).baostock(sid)
    result = view.get_preclose(require_known=True)
    assert isinstance(result, SourcePrecloseResult) and len(result.rows) == 3
    assert [r.source_fields for r in result.rows] == raw
    for row, original in zip(result.rows, raw, strict=True):
        assert row.preclose.raw_value == original["preclose"]
        assert row.preclose.value == Decimal(original["preclose"])
        assert str(row.preclose.value) == original["preclose"]
        assert row.preclose.state == "source_observed"
        assert row.source_adjustflag == row.request_adjustflag == "3"
        assert row.raw_record_sha256 == api.sh(original)
        assert row.raw_response_sha256 == receipt["pages"][0]["raw_response_sha256"]
        assert row.response_received_at == receipt["pages"][0]["last_byte_received_at"]
        assert row.price_basis == preclose.BASIS and row.unit == "CNY/share"
        assert row.reference_price_eligible is row.historical_available_at is None
        assert not row.events_complete and not row.execution_permission
    assert (
        view.quality()["price_basis"] == result.report["quality"]["price_basis"] == preclose.BASIS
    )
    assert result.report["definition"]["document_sha256"] == preclose.DEFINITION_SHA256
    assert result.report["coverage"]["requested_values_known"]
    assert not result.report["network_used"] and not result.report["quality"]["historical_pit"]
    assert not result.report["coverage"]["query_complete"]
    assert view.descriptor()["version"] == preclose.VERSION
    with raises_code("RECEIPT_NOT_VISIBLE"):
        view.at("2026-10-11T00:00:00Z")
    with raises_code("PIT_UNAVAILABLE"):
        view.at("2026-10-11T00:00:00Z", visibility="verified")


def test_generic_profile_and_old_fields():
    req = api.request("daily_preclose", "600519.XSHG", "2020-01-01", "2020-01-31")
    assert req["params"] == dict(
        code="sh.600519",
        start_date="2020-01-01",
        end_date="2020-01-31",
        fields=preclose.FIELDS,
        frequency="d",
        adjustflag="3",
    )
    assert (
        api.request("daily", "600519.XSHG", "2020-01-01", "2020-01-01")["params"]["fields"]
        == api.FIELDS
    )
    with raises_code("BOUNDED_REQUEST"):
        api.request("daily_preclose", "600519.XSHG", "2020-01-01", "2020-02-01")
    with raises_code("INVALID_ARGUMENT"):
        api.request("daily_preclose", "600519.XSHG", "2020-01-01", "2020-01-01", "1m")
    assert "daily_preclose" in BaoStockSource.capabilities()["kinds"]


def test_old_daily_not_requested_never_substitutes_close(tmp_path):
    blobs, _ = bundle(tmp_path, "daily")
    view = view_for(persist(tmp_path / "export", blobs), tmp_path)
    old_price, old_status, old_quality = (
        view.get_price().to_dict(),
        view.get_status().to_dict(),
        view.quality(),
    )
    result = view.get_preclose()
    assert result.rows[0].preclose.state == "field_not_requested"
    assert result.rows[0].preclose.raw_value is result.rows[0].preclose.value is None
    assert view.get_price().to_dict() == old_price
    assert view.get_status().to_dict() == old_status
    assert view.quality() == old_quality
    assert view.descriptor()["version"] == api.VERSION
    with raises_code("SOURCE_PRECLOSE_UNKNOWN"):
        view.get_preclose(require_known=True)


@pytest.mark.parametrize(
    "raw,state,value",
    [
        ("9.1234", "source_observed", "9.1234"),
        ("12.1234567", "source_observed", "12.1234567"),
        ("0.0000", "source_zero", "0.0000"),
        ("", "source_empty", None),
        ("-1", "invalid_numeric", None),
        ("NaN", "invalid_numeric", None),
        ("Infinity", "invalid_numeric", None),
        ("1e4", "invalid_numeric", None),
        (" 1.0", "invalid_numeric", None),
        ("1.0 ", "invalid_numeric", None),
        ("1_000", "invalid_numeric", None),
        ("abc", "invalid_numeric", None),
        ("1" * 65, "invalid_numeric", None),
    ],
)
def test_missing_invalid_zero_and_precision(tmp_path, monkeypatch, raw, state, value):
    view = view_for(
        captured(tmp_path, monkeypatch, [["2020-01-02", "sh.600000", raw, "3"]]), tmp_path
    )
    result = view.get_preclose()
    field = result.rows[0].preclose
    assert field.raw_value == raw and field.state == state
    assert field.value == (Decimal(value) if value is not None else None)
    if value is None:
        with raises_code("SOURCE_PRECLOSE_UNKNOWN"):
            view.get_preclose(require_known=True)
    else:
        assert view.get_preclose(require_known=True).rows[0].preclose.to_dict()["value"] == value
    assert result.rows[0].reference_price_eligible is None


@pytest.mark.parametrize("empty", [True, False])
def test_natural_day_missing_is_unknown(tmp_path, monkeypatch, empty):
    rows = (
        []
        if empty
        else [
            ["2020-02-28", "sh.600000", "9.0000", "3"],
            ["2020-03-01", "sh.600000", "8.0000", "3"],
        ]
    )
    view = view_for(
        captured(tmp_path, monkeypatch, rows, start="2020-02-28", end="2020-03-01"), tmp_path
    )
    result = view.get_preclose()
    assert len(result.rows) == 3
    assert result.rows[1].trade_date.isoformat() == "2020-02-29"
    assert result.rows[1].preclose.state == "row_missing_unknown"
    assert result.rows[1].preclose.value is result.rows[1].raw_record_sha256 is None
    assert not result.report["coverage"]["calendar_inferred"]
    with raises_code("SOURCE_PRECLOSE_UNKNOWN"):
        view.get_preclose(require_known=True)


@pytest.mark.parametrize(
    "fault", ["duplicate", "reverse", "outside", "wrong-code", "wrong-adjustflag", "missing-column"]
)
def test_bad_source_schema_never_yields_normal_rows(tmp_path, monkeypatch, fault):
    rows = [["2020-01-02", "sh.600000", "9.0000", "3"], ["2020-01-03", "sh.600000", "8.0000", "3"]]
    if fault == "duplicate":
        rows[1] = rows[0][:]
    elif fault == "reverse":
        rows.reverse()
    elif fault == "outside":
        rows[1][0] = "2020-01-04"
    elif fault == "wrong-code":
        rows[1][1] = "sz.000001"
    elif fault == "wrong-adjustflag":
        rows[1][3] = "2"
    else:
        rows[1].pop()
    view = view_for(captured(tmp_path, monkeypatch, rows, end="2020-01-03"), tmp_path)
    with raises_code("SOURCE_REQUEST_FAILED"):
        view.get_preclose()


def test_clock_binding_and_recorded_failure(tmp_path, monkeypatch):
    bad = tmp_path / "bad"
    bad.mkdir()
    path = captured(bad, monkeypatch)
    resign_receipt(path, lambda r: r["pages"][0].update(last_byte_received_at="bad"))
    with raises_code("SOURCE_PRECLOSE_CLOCK_INVALID"):
        Store.init(bad / "store").import_baostock_capture(path)
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    view = view_for(captured(unknown, monkeypatch, clock=lambda: None), unknown)
    result = view.get_preclose(require_known=True)
    assert result.rows[0].response_received_at is None
    assert result.report["quality"]["capture_clock_state"] == "unknown"


def test_max_date_empty(tmp_path, monkeypatch):
    view = view_for(
        captured(tmp_path, monkeypatch, [], start="9999-12-31", end="9999-12-31"), tmp_path
    )
    assert view.get_preclose().rows[0].preclose.state == "row_missing_unknown"


def test_types_cli_and_wrong_kind(tmp_path, monkeypatch, capsys):
    view = view_for(captured(tmp_path, monkeypatch), tmp_path)
    result = view.get_preclose()
    with pytest.raises(FrozenInstanceError):
        result.rows[0].preclose.value = Decimal("1")
    result.rows[0].source_fields["preclose"] = "changed"
    result.report["quality"]["historical_pit"] = True
    assert result.rows[0].preclose.raw_value == "9.1234"
    assert not result.report["quality"]["historical_pit"]
    args = [
        "--store",
        str(tmp_path / "store"),
        "baostock-query",
        "preclose",
        "--capture",
        result.capture_id,
    ]
    assert main(args + ["--require-known"]) == 0
    assert json.loads(capsys.readouterr().out) == result.to_dict()
    assert main(args + ["--end", "2020-01-02T15:00:00+08:00"]) == 2
    assert "UNSUPPORTED_FREQUENCY" in capsys.readouterr().err
    with raises_code("QUERY_KIND_MISMATCH"):
        view.get_price()
    basic = tmp_path / "basic"
    basic.mkdir()
    blobs, _ = bundle(basic)
    with raises_code("QUERY_KIND_MISMATCH"):
        view_for(persist(basic / "export", blobs), basic).get_preclose()


def test_explicit_acquisition_and_invalid_strict_input(tmp_path, monkeypatch):
    path = captured(tmp_path, monkeypatch)
    store = Store.init(tmp_path / "store")
    sid = store.import_baostock_capture(path)
    source = object.__new__(BaoStockSource)
    calls = []

    def fetch(actual_store, **kw):
        assert actual_store is store
        calls.append(kw)
        return sid

    monkeypatch.setattr(source, "fetch", fetch)
    result = source.get_preclose(
        "600000.XSHG", store=store, start_date="2020-01-02", end_date="2020-01-02"
    )
    assert calls[0]["kind"] == "daily_preclose" and result.report["network_used"]
    assert not store.baostock(sid).get_preclose().report["network_used"]
    with raises_code("INVALID_ARGUMENT"):
        source.get_preclose(
            "600000.XSHG",
            store=store,
            start_date="2020-01-02",
            end_date="2020-01-02",
            require_known=1,
        )
    assert len(calls) == 1


@pytest.mark.parametrize("corrupt", [False, True])
def test_interrupted_new_profile_publication(tmp_path, monkeypatch, corrupt):
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
        obj = (
            store.root
            / "baostock-objects"
            / api.bh((path / "capture/page-01.response.bin").read_bytes())
        )
        obj.chmod(0o644)
        obj.write_bytes(b"broken")
    assert store.recover_baostock() == [
        {"capture_id": sid, "status": "aborted" if corrupt else "published"}
    ]
    if not corrupt:
        assert store.baostock(sid).get_preclose(require_known=True).rows[
            0
        ].preclose.value == Decimal("9.1234")
