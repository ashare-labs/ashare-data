"""Policy boundaries use synthetic fixtures and traps; never contact legacy sources."""

import json
from pathlib import Path
import socket
import subprocess

import pytest

from ashare_data import BaoStockSource, DataError, Store, TDXBarSource, TDXBarView, ZzshareSource
from ashare_data.providers import baostock, zzshare


@pytest.fixture(autouse=True)
def no_external_acquisition(monkeypatch):
    for key in (
        "ASHARE_CONSUMER_DEPLOYMENT", "ASHARE_DEPLOYMENT", "ASHARE_DATA_ROOT",
        "ASHARE_QUANT_FACTOR_SOURCES", "ASHARE_QUANT_PYTHON_HISTORY_FACTORS",
        "ASHARE_QUANT_MARKET_STORES", "ASHARE_QUANT_DAILY_STORE", "ASHARE_QUANT_DIAGNOSTICS",
        "AQ_API_URL", "QUANT_SERVICE_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("network and subprocess execution are forbidden in this test")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def bind_policy(tmp_path, monkeypatch):
    path = tmp_path / "consumer.json"
    path.write_text(json.dumps({"profiles": {"product": {
        "consumer_policy": "committed_market_only",
        "market_release": {"asset": "synthetic-unopened-market", "mode": "current"},
    }}}))
    return lambda: monkeypatch.setenv("ASHARE_CONSUMER_DEPLOYMENT", str(path))


def block_acquisition_io(monkeypatch, capture_dir):
    touched = []

    def trap(label):
        def forbidden(*args, **kwargs):
            touched.append(label)
            raise AssertionError("policy must precede " + label)
        return forbidden

    find_spec = baostock.importlib.util.find_spec

    def sdk_spec(name, *args, **kwargs):
        if name == "baostock":
            return trap("SDK discovery")(name)
        return find_spec(name, *args, **kwargs)

    mkdir = Path.mkdir

    def guarded_mkdir(path, *args, **kwargs):
        if path == capture_dir:
            return trap("capture directory creation")()
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(baostock.importlib.util, "find_spec", sdk_spec)
    monkeypatch.setattr(baostock, "sdk_identity", trap("SDK files"))
    monkeypatch.setattr(baostock.tempfile, "gettempdir", trap("BaoStock lock/staging"))
    monkeypatch.setattr(zzshare.urllib.request, "build_opener", trap("HTTP opener"))
    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    return touched


@pytest.mark.parametrize("source,kwargs", [
    (BaoStockSource, {}),
    (BaoStockSource, {"sdk_path": "/unopened-test-sdk"}),
    (BaoStockSource, {"sdk_path": "/unopened-test-sdk", "timeout": 0}),
    (TDXBarSource, {"host": "203.0.113.1:7709"}),
    (TDXBarSource, {"host": "invalid"}),
    (ZzshareSource, {"enabled": True}),
    (ZzshareSource, {"enabled": "invalid"}),
])
def test_bound_constructor_rejects_before_sdk_io_or_argument_checks(
    tmp_path, monkeypatch, bind_policy, source, kwargs
):
    bind_policy()
    capture_dir = tmp_path / "unused-capture"
    touched = block_acquisition_io(monkeypatch, capture_dir)
    with pytest.raises(DataError) as caught:
        source(**kwargs)
    assert caught.value.code == "LEGACY_SOURCE_DISABLED"
    assert caught.value.details == {"operation": source.__name__, "fallback": False}
    assert touched == []
    assert not capture_dir.exists()


def make_source(name, tmp_path, monkeypatch):
    if name == "baostock":
        # Only the constructor's SDK identity is stubbed. Never discover/read an installed SDK.
        with monkeypatch.context() as setup:
            setup.setattr(baostock, "sdk_identity", lambda path: {})
            return BaoStockSource(sdk_path=tmp_path / "unopened-sdk")
    if name == "tdx":
        return TDXBarSource(host="203.0.113.1:7709")
    return ZzshareSource(enabled=True)


@pytest.mark.parametrize("name", ["baostock", "tdx", "zzshare"])
@pytest.mark.parametrize("valid_arguments", [True, False])
def test_existing_instance_fetch_rechecks_later_policy_before_any_side_effect(
    tmp_path, monkeypatch, bind_policy, name, valid_arguments
):
    source = make_source(name, tmp_path, monkeypatch)
    bind_policy()
    capture_dir = tmp_path / "unused-capture"
    touched = block_acquisition_io(monkeypatch, capture_dir)
    security = "600000.XSHG" if valid_arguments else "invalid"

    class UnopenedStore:
        @property
        def root(self):
            raise AssertionError("blocked fetch must not inspect or write its store")

    with pytest.raises(DataError) as caught:
        if name == "baostock":
            source.fetch(UnopenedStore(), kind="basic", security=security)
        elif name == "tdx":
            source.fetch(security, directory=capture_dir)
        else:
            source.fetch(UnopenedStore(), security=security,
                         start_date="2026-09-28", end_date="2026-09-28")
    assert caught.value.code == "LEGACY_SOURCE_DISABLED"
    assert caught.value.details == {"operation": type(source).__name__ + ".fetch", "fallback": False}
    assert touched == []
    assert not capture_dir.exists()


def test_unbound_constructors_and_existing_argument_errors_are_unchanged(tmp_path, monkeypatch):
    for name in ("baostock", "tdx", "zzshare"):
        assert make_source(name, tmp_path, monkeypatch) is not None
    with pytest.raises(DataError) as caught:
        BaoStockSource(sdk_path="/unopened-test-sdk", timeout=0)
    assert caught.value.code == "INVALID_ARGUMENT"
    with pytest.raises(DataError) as caught:
        TDXBarSource(host="invalid")
    assert caught.value.code == "INVALID_ARGUMENT"
    with pytest.raises(DataError) as caught:
        ZzshareSource().fetch(object(), security="invalid", start_date="bad", end_date="bad")
    assert caught.value.code == "ZZSHARE_DISABLED"


@pytest.mark.parametrize("name", ["baostock", "tdx", "zzshare"])
def test_bound_policy_keeps_frozen_offline_views_readable(
    name, tmp_path, monkeypatch, bind_policy
):
    # Reuse protocol fixtures that explicitly identify themselves as synthetic, offline controls.
    if name == "baostock":
        from test_baostock import bundle

        store = Store.init(tmp_path / "archive")
        blobs, _ = bundle(tmp_path)
        capture_id = baostock._publish(store, blobs)

        def reopen():
            return Store(store.root).baostock(capture_id).get_security_info()
    elif name == "tdx":
        from test_tdx_bars import capture

        view, _ = capture(tmp_path)

        def reopen():
            return TDXBarView(tmp_path, expected_sha256=view.manifest_sha256).get_price(
                mode="retrospective"
            )
    else:
        from test_zzshare import capture

        store = Store.init(tmp_path / "archive")
        capture_id = store.import_zzshare_capture(capture(tmp_path), enable_research=True)

        def reopen():
            return Store(store.root).zzshare(capture_id, enable_research=True).get_daily()
    bind_policy()
    touched = block_acquisition_io(monkeypatch, tmp_path / "unused-capture")
    result = reopen()
    assert result.report["network_used"] is False
    assert touched == []
