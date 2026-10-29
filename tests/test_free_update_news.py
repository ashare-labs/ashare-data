"""Synthetic feed cases; no live coverage is claimed by these tests."""
from datetime import datetime, timezone

import pytest

from ashare_data.providers.free_update_news import parse, publication, validate


def test_epoch_news_is_independent_of_the_host_timezone():
    stamp=int(datetime(2026,10,28,12,30,tzinfo=timezone.utc).timestamp())
    assert publication(stamp,epoch=True)=="2026-10-28T20:30:00+08:00"
    rows=parse("stock_info_global_ths",{"code":"200","data":{"list":[{
        "id":"a","title":"title","digest":"body","rtime":stamp,"url":"https://news.10jqka.com.cn/a"}]}})
    result=validate(rows,{"function":"stock_info_global_ths"},"2026-10-28")
    assert result["latest_announcement"]=="2026-10-28T20:30:00+08:00" and not result["gaps"]
    with pytest.raises(ValueError,match="duplicate"):
        validate(rows*2,{"function":"stock_info_global_ths"},"2026-10-28")
    with pytest.raises(ValueError,match="date"):
        validate(rows,{"function":"stock_info_global_ths"},"2026-10-27")
    with pytest.raises(ValueError,match="identity"):
        validate(rows,{"function":"stock_info_global_sina"},"2026-10-28")


@pytest.mark.parametrize("name,body",[
    ("stock_info_global_sina",{"result":{"status":{"code":0},"data":{"feed":{"list":[{"id":1,"rich_text":"news","create_time":"2026-10-28 21:00:00"}]}}}}),
    ("stock_info_global_em",{"code":"1","data":{"fastNewsList":[{"code":"20261028123","title":"title","summary":"body","showTime":"2026-10-28 21:00:00"}]}}),
    ("stock_info_global_cls",{"errno":0,"data":{"roll_data":[{"id":2,"title":"","content":"body","ctime":1793192400}]}})])
def test_feed_native_identity_and_source_content(name,body):
    rows=parse(name,body)
    assert validate(rows,{"function":name},"2026-10-28")["contract_reviewed"]
    with pytest.raises(ValueError):
        validate([dict(rows[0],title="",content="")],{"function":name},"2026-10-28")


def test_air_transport_source_unit_period_and_percent_checks():
    from ashare_data.providers.free_update_semantics import validate_semantics
    job={"function":"sina_macro_page","parameters":{"dataset":"macro_china_passenger_load_factor"}}
    row={"统计时间":"2026.9","客座率":86.5,"载运率":76,"source_units":{"客座率":"%","载运率":"%"}}
    assert not validate_semantics([row],job,"2026-10-28")["gaps"]
    for bad in [dict(row,客座率=101),dict(row,统计时间="2027.1"),dict(row,source_units={})]:
        with pytest.raises(ValueError):
            validate_semantics([bad],job,"2026-10-28")


def test_scheduler_artifact_does_not_load_and_production_root_is_refused(tmp_path):
    import json
    import plistlib
    import sys
    from ashare_data.providers.free_update_handoff import render_launch_agent
    from ashare_data.storage.free_updates import connect
    config=tmp_path/"schedule.json"
    config.write_text('{}')
    output=tmp_path/"schedule.plist"
    result=render_launch_agent(python=sys.executable,config=config,output=output,hour=21,minute=30)
    assert not result["timer_installed"]
    assert plistlib.loads(output.read_bytes())["ProgramArguments"][-1]=="--execute"
    with pytest.raises(ValueError,match="replace"):
        render_launch_agent(python=sys.executable,config=config,output=output)
    root=tmp_path/"marked"
    connect(root).close()
    (root/"CURRENT.json").write_text(json.dumps({"release_id":10}))
    with pytest.raises(ValueError,match="production"):
        connect(root)


def test_cls_offline_replay_uses_original_request_cursor_not_current_clock(tmp_path, monkeypatch):
    import gzip
    import hashlib
    import json
    from datetime import datetime, timezone, timedelta
    from ashare_data.providers import free_update_news as news
    from ashare_data.providers.free_update_coverage import CapturedResponses
    at = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
    epoch = int(at.timestamp())
    body = json.dumps({'errno': 0, 'data': {'roll_data': [
        {'id': 1, 'title': 'Synthetic', 'content': 'Fixture', 'ctime': epoch-30}]}}).encode()
    sha = hashlib.sha256(body).hexdigest()
    (tmp_path / 'raw').mkdir()
    (tmp_path / 'raw' / (sha+'.gz')).write_bytes(gzip.compress(body))
    class Live:
        def get(self, url):
            self.receipt = {'url': url, 'protocol': 'http', 'status': 200, 'sha256': sha,
                'received_at': (at+timedelta(seconds=1)).isoformat()}
            return body
    live = Live()
    monkeypatch.setattr(news.time, 'time', lambda: epoch)
    rows = news.collect(live, 'stock_info_global_cls', {})
    monkeypatch.setattr(news.time, 'time', lambda: epoch+86400)
    captured = CapturedResponses(tmp_path, {'receipts': [live.receipt]}, at, at+timedelta(seconds=2))
    assert news.collect(captured, 'stock_info_global_cls', {}) == rows
    assert captured.dataset_metadata == live.dataset_metadata
    assert captured.dataset_metadata['complete_returned_window']
    assert not captured.dataset_metadata['complete_history']
    receipt = dict(live.receipt, url=live.receipt['url'].replace(str(epoch), str(epoch+100)))
    with pytest.raises(ValueError, match='cursor outside'):
        news.collect(CapturedResponses(tmp_path, {'receipts': [receipt]}, at, at+timedelta(seconds=2)), 'stock_info_global_cls', {})


def test_treasury_feed_window_requires_requested_year_and_no_unread_next_page():
    from ashare_data.providers.free_update_worker import direct_records
    class Feed:
        raw = b'<feed><properties><NEW_DATE>2026-10-28</NEW_DATE><BC_1YEAR>4.1</BC_1YEAR></properties></feed>'
        def get(self, url):
            return self.raw
    f = Feed()
    params = {'kind': 'daily_treasury_yield_curve', 'year': '2026'}
    assert direct_records(f, 'treasury_rates', params)[0]['date'] == '2026-10-28'
    assert f.dataset_metadata['source_count'] == 1 and not f.dataset_metadata['complete_history']
    with pytest.raises(ValueError, match='requested year'):
        direct_records(f, 'treasury_rates', dict(params, year='2025'))
    f.raw = f.raw.replace(b'<feed>', b'<feed><link rel="next" href="page2"/>')
    with pytest.raises(ValueError, match='paginated'):
        direct_records(f, 'treasury_rates', params)
