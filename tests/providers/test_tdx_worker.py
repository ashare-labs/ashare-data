"""Run the real worker process with a synthetic SDK and disabled sockets."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from ashare_data.providers.tdx_bars import TDXBarSource


@pytest.mark.parametrize(
    "version,reason", [("3.0.3", "remote_empty_page"), ("0.0.0", "source_error")]
)
def test_real_sdk_worker_entrypoint_imports_renamed_sibling_without_ad_or_network(
    tmp_path, monkeypatch, version, reason
):
    sdk_root = tmp_path / "sdk"
    package = sdk_root / "eltdx"
    (package / "api").mkdir(parents=True)
    (package / "models").mkdir()
    (package / "api/bars.py").write_text("# synthetic SDK identity\n")
    (package / "models/kline.py").write_text("# synthetic SDK identity\n")
    (package / "__init__.py").write_text("""
import sys
import socket
import urllib.request
import http.client
from types import SimpleNamespace
assert 'ashare_data' not in sys.modules, 'worker must not import AD in SDK environment'
assert hasattr(http.client, 'HTTPConnection'), 'SDK must use the standard-library http package'
def blocked(*args, **kwargs):
    raise AssertionError('NETWORK_DISABLED_SYNTHETIC_WORKER_TEST')
socket.socket = socket.create_connection = blocked
class TdxClient:
    def __init__(self, **kwargs):
        assert kwargs['probe_hosts'] is False
        self.bars = self
    def connect(self):
        pass
    def close(self):
        pass
    def get(self, code, **kwargs):
        return SimpleNamespace(bars=[], raw_payload=bytes([0,0]), full_code=code,
                               start=kwargs['start'], request_count=kwargs['count'],
                               period_name=kwargs['period'], adjust_mode_raw=0)
""")
    dist = sdk_root / ("eltdx-" + version + ".dist-info")
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: eltdx\nVersion: " + version + "\n")
    monkeypatch.setenv("PYTHONPATH", str(sdk_root))
    calls = []
    original = subprocess.run

    def launched(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", launched)
    view = TDXBarSource(host="synthetic.invalid:7709", python_executable=sys.executable).fetch(
        "600519.XSHG",
        directory=tmp_path / "capture",
        count=1,
        max_pages=1,
    )
    assert len(calls) == 1 and Path(calls[0][2]).name == "tdx_bars_worker.py"
    assert view.describe()["stop_reason"] == reason
    journal = json.loads((tmp_path / "capture/journal.json").read_bytes())
    if version == "3.0.3":
        assert journal["sdk"]["version"] == version
        assert (tmp_path / "capture/page-000.bin").read_bytes() == bytes([0, 0])
    else:
        assert "eltdx 3.0.3" in journal["error"]
