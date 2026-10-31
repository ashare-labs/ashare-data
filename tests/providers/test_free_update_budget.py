"""Synthetic transport admission tests; never source-availability evidence."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

from ashare_data.providers.free_updates.free_update_budget import (
    ENV, RequestBudget, ResourceBudgetReached, financial_fetcher, from_environment,
)


def configured(tmp_path, monkeypatch, requests=3, size=40 * 1024**2):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps(dict(schema="ad.public-request-budget.v1",
        requests_per_day=requests, response_bytes_per_day=size)))
    monkeypatch.setenv(ENV, str(path))
    monkeypatch.setenv(ENV + "_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    return RequestBudget(path)


def test_attempts_charged_before_transport_and_failure_keeps_reservation(tmp_path, monkeypatch):
    b = configured(tmp_path, monkeypatch, requests=2, size=100)
    first = b.reserve("https://example/1", 60, day="2026-10-29")
    with pytest.raises(ResourceBudgetReached, match="no request sent"):
        b.reserve("https://example/2", 60, day="2026-10-29")
    b.settle(first, 20)
    b.reserve("https://example/2", 60, day="2026-10-29")  # process died: stays charged
    total = b.reconcile("2026-10-29", dict(requests=0, response_bytes=0))
    assert total == dict(requests=2, response_bytes=80)
    with pytest.raises(ResourceBudgetReached):
        b.reserve("https://example/3", 0, day="2026-10-29")
    assert RequestBudget(b.path).reconcile("2026-10-29", total) == total
    with pytest.raises(ValueError, match="repeated"):
        b.settle(first, 0)


def test_atomic_admission_cannot_oversubscribe_and_day_rollover(tmp_path, monkeypatch):
    b = configured(tmp_path, monkeypatch, requests=3, size=100)
    def request(_):
        try:
            return b.reserve("https://example", 20, day="2026-10-29")
        except ResourceBudgetReached:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(request, range(16)))
    assert len([v for v in admitted if v]) == 3
    assert b.reserve("https://example", 20, day="2026-10-30")
    assert b.reconcile("2026-10-29", dict(requests=9, response_bytes=150)) == dict(requests=9,response_bytes=150)
    with pytest.raises(ResourceBudgetReached):
        b.reserve("https://example", 0, day="2026-10-29")


def test_invalid_env_or_changed_config_fails_closed(tmp_path, monkeypatch):
    b = configured(tmp_path, monkeypatch)
    assert from_environment().config == b.config
    b.path.write_text('{}')
    with pytest.raises(ValueError, match="changed"):
        from_environment()


def test_capture_stops_before_opener_when_shared_budget_is_exhausted(tmp_path, monkeypatch):
    from ashare_data.providers.free_updates.free_update_worker import Capture
    b = configured(tmp_path, monkeypatch, requests=1)
    token = b.reserve("https://example", 0)
    b.settle(token, 0)
    monkeypatch.setattr("ashare_data.providers.free_updates.free_update_worker.build_opener",
                        lambda *_: pytest.fail("network started after budget exhaustion"))
    with pytest.raises(ResourceBudgetReached):
        Capture(tmp_path / 'raw', interval=0).get("https://data.eastmoney.com/x")


def test_tdx_budget_blocks_before_connection(tmp_path, monkeypatch):
    from ashare_data.providers.free_updates.free_update_worker import Capture
    from ashare_data.providers.tdx_blocks import Client
    b = configured(tmp_path, monkeypatch, requests=1)
    b.reserve("https://example", 0)
    monkeypatch.setattr("ashare_data.providers.tdx_blocks.socket.create_connection",
                        lambda *_a, **_k: pytest.fail("TDX connected despite budget"))
    with pytest.raises(ResourceBudgetReached):
        Client(Capture(tmp_path / 'raw', interval=0))


def test_financial_guard_does_not_change_normalizer_or_existing_fetch(tmp_path, monkeypatch):
    from ashare_data.financials import expansion as source
    original = source.SafeRedirect
    source_bytes = Path(source.__file__).read_bytes()
    b = configured(tmp_path, monkeypatch, requests=1)
    calls = []
    monkeypatch.setattr(source, "fetch", lambda url: (calls.append(url) or 200, b'{}', {}))
    fetch = financial_fetcher(source)
    assert fetch('https://quotes.sina.cn/example') == (200, b'{}', {})
    assert source.SafeRedirect is original
    assert Path(source.__file__).read_bytes() == source_bytes
    with pytest.raises(ResourceBudgetReached):
        fetch('https://quotes.sina.cn/second')
    assert len(calls) == 1
    state = json.loads((b.root / 'request-budget-state.json').read_text())
    assert list(state['days'].values()) == [dict(requests=1,response_bytes=2)]


def test_requests_reads_bounded_body_and_preserves_source_refusal(tmp_path, monkeypatch):
    import io
    import requests
    from urllib3.response import HTTPResponse
    from unittest.mock import patch
    from ashare_data.providers.free_updates.free_update_worker import Capture, SourceBlocked
    b = configured(tmp_path, monkeypatch)
    response = requests.Response()
    response.status_code = 429
    response.url = 'https://data.eastmoney.com/x'
    response.raw = HTTPResponse(body=io.BytesIO(b'limited'), preload_content=False)
    response.headers = {'Retry-After':'7200'}
    with patch('requests.Session.send', return_value=response) as original, \
         patch('curl_cffi.requests.Session.request'), patch('urllib.request.urlopen'):
        c = Capture(tmp_path/'raw', interval=0)
        c.install()
        with pytest.raises(SourceBlocked, match='HTTP 429'):
            requests.get(response.url)
        assert original.call_args.kwargs['stream'] is True
        assert original.call_args.kwargs['verify'] is True
        assert original.call_args.kwargs['allow_redirects'] is False
        assert c.receipts[0]['retry_after'] == '7200'
    state = json.loads((b.root/'request-budget-state.json').read_text())
    assert list(state['days'].values())[0] == dict(requests=1, response_bytes=7)


def test_curl_callback_bounded_and_budgeted(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import patch
    from curl_cffi import requests as curl_requests
    from ashare_data.providers.free_updates.free_update_worker import Capture
    b = configured(tmp_path, monkeypatch)
    def network(session, method, url, **kwargs):
        assert kwargs['verify'] and kwargs['allow_redirects'] is False
        assert kwargs['content_callback'](b'part1') == 5
        assert kwargs['content_callback'](b'part2') == 5
        return SimpleNamespace(url=url,status_code=200,headers={},content=b'')
    with patch('requests.Session.send'), patch('urllib.request.urlopen'), \
         patch('curl_cffi.requests.Session.request', network):
        c = Capture(tmp_path/'raw', interval=0)
        c.install()
        response = curl_requests.Session().get('https://data.eastmoney.com/x')
        assert response.content == b'part1part2'
        assert c.receipts[0]['bytes'] == 10
    assert list(json.loads((b.root/'request-budget-state.json').read_text())['days'].values())[0]['response_bytes'] == 10
