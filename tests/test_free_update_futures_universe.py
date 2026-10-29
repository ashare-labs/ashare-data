"""Inert directory parsing, resume integrity and admission of reviewed units."""
import json

import pytest

from ashare_data.providers import free_update_futures_universe as universe


def navigation():
    return b"ARRFUTURESNODES = {czce:['ZZ', ['cotton','mh_qh',3]], dce:['DCE'], shfe:['SHFE'], cffex:['CFFEX'], gfex:['GFEX']}; trailing_js_not_executed();"


def test_literal_navigation_does_not_execute_trailing_script_and_does_not_infer_exchange():
    assert universe.nodes(navigation()) == [{"vendor_group": "czce", "vendor_group_name": "ZZ", "product_name": "cotton", "node": "mh_qh"}]
    for bad in [navigation().replace(b"['cotton'", b"[__import__('os').getcwd()"), navigation().replace(b"'mh_qh'", b"'../../bad'"), navigation().replace(b"['DCE']", b"[]")]:
        with pytest.raises((ValueError, SyntaxError)):
            universe.nodes(bad)


def test_continuous_identity_must_be_unique_explicit_not_a_delivery_or_index():
    rows = [{"symbol": "CF0", "name": "棉花连续"}, {"symbol": "CF2701", "name": "棉花2701"}]
    assert universe.continuous_candidates(json.dumps(rows))[0]["symbol"] == "CF0"
    assert universe.continuous_candidates("[]") == []
    for bad in [[1], rows*2, [rows[0] | {"name": "棉花指数"}], [{"symbol": "CF0", "name": "连续"}, {"symbol": "V0", "name": "连续"}], rows*50]:
        with pytest.raises(ValueError):
            universe.continuous_candidates(json.dumps(bad))


def test_resume_checks_saved_candidates_against_raw_and_does_not_refetch(tmp_path, monkeypatch):
    from ashare_data.providers.free_update_worker import Capture
    calls = []
    def get(self, url):
        calls.append(url)
        body = navigation() if url == universe.INDEX_URL else json.dumps([{"symbol": "CF0", "name": "棉花连续"}]).encode()
        self.record(url, "vip.stock.finance.sina.com.cn", 200, body, {})
        return body
    monkeypatch.setattr(Capture, "get", get)
    first = universe.discover(tmp_path, max_nodes=1)
    assert first["contexts"] == [{"future_main": "CF0"}] and len(calls) == 2
    assert universe.discover(tmp_path, max_nodes=1)["attempted_this_run"] == 0 and len(calls) == 2
    path = tmp_path/"nodes/mh_qh.json"
    saved = json.loads(path.read_text())
    saved["candidates"][0]["symbol"] = "V0"
    path.unlink()  # tamper only this synthetic test fixture
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="native receipt"):
        universe.discover(tmp_path, max_nodes=1)
