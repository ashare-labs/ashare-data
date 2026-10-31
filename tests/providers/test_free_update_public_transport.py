from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_data.providers.free_updates import free_update_public_transport as transport
from ashare_data.providers.free_updates.free_update_worker import Capture, SourceBlocked

URL = "https://push2.eastmoney.com/api/qt/clist/get?fs=b%3ABK1024"


def response(monkeypatch, status=200, code=0, verify="0"):
    def run(command, **kwargs):
        assert command[:2] == ["/usr/bin/curl", "--disable"]
        assert "--insecure" not in command and "-k" not in command and "--location" not in command
        assert command[command.index("--retry")+1] == "0"
        assert command[command.index("--proxy")+1] == "http://127.0.0.1:12345"
        assert command[command.index("--user-agent")+1] == "ashare-data public-source research"
        Path(command[command.index("--dump-header")+1]).write_bytes(f"HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 {status} Result\r\nRetry-After: 120\r\n\r\n".encode())
        Path(command[command.index("--output")+1]).write_bytes(b'{"rc":0}')
        return SimpleNamespace(stdout=f"{status}\n{verify}\n", returncode=code)
    monkeypatch.setattr(transport, "getproxies", lambda: {"https": "http://127.0.0.1:12345"})
    monkeypatch.setattr(transport.subprocess, "run", run)


def test_transport_preserves_normal_identity_admission_and_receipts(tmp_path, monkeypatch):
    response(monkeypatch)
    c = Capture(tmp_path, request_limit=1)
    assert c.get_curl(URL) == b'{"rc":0}'
    assert c.attempts == 1 and c.receipts[0]["status"] == 200
    assert c.receipts[0]["request_method"] == "GET" and c.receipts[0]["retry_after"] == "120"
    with pytest.raises(SourceBlocked, match="budget"):
        c.get_curl(URL)


@pytest.mark.parametrize("status", [401, 403, 429, 508])
def test_refusal_is_recorded_and_stops_host(tmp_path, monkeypatch, status):
    response(monkeypatch, status=status)
    c = Capture(tmp_path)
    with pytest.raises(SourceBlocked, match=f"HTTP {status}"):
        c.get_curl(URL)
    assert "push2.eastmoney.com" in c.blocked and c.receipts[0]["status"] == status


def test_tls_error_does_not_retry_or_relax_verification(tmp_path, monkeypatch):
    response(monkeypatch, status=0, code=60, verify="20")
    c = Capture(tmp_path)
    with pytest.raises(OSError, match="TLS=20"):
        c.get_curl(URL)
    assert c.attempts == 1 and c.receipts[0]["status"] == "transport_error"


def test_other_host_or_credentialed_proxy_is_not_a_fallback(tmp_path, monkeypatch):
    c = Capture(tmp_path)
    with pytest.raises(ValueError, match="only reviewed"):
        c.get_curl("https://q.10jqka.com.cn/")
    monkeypatch.setattr(transport, "getproxies", lambda: {"https": "http://name:secret@localhost:12345"})
    with pytest.raises(ValueError, match="credentialed"):
        c.get_curl(URL)
    assert c.attempts == 0
