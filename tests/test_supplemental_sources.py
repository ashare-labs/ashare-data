from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_data.providers.supplemental_sources import parse_cni, parse_quotes, parse_topics


def wire(symbol="sh600000", day="2026-10-30", volume="12345", no_trade=False):
    fields = ["浦发银行", "10", "9.9", "10.2", "10.3", "9.8", "10.1", "10.2", volume, "125000"]
    fields += ["0"] * 20 + [day, "15:34:59", "00", "D|100|1020"]
    if no_trade:
        for n in [1, 3, 4, 5, 8, 9]:
            fields[n] = "0"
    return f'var hq_str_{symbol}="{",".join(fields)}";\n'.encode("gb18030")


def test_bulk_quotes_keep_identity_date_units_and_native_tail():
    records, coverage = parse_quotes(wire() + b'var hq_str_sz000001="";\n',
        ["sh600000", "sz000001"], "2026-10-30T12:00:00+00:00")
    assert records[0]["volume"] == "12345" and records[0]["volume_unit"] == "share"
    assert records[0]["amount_unit"] == "CNY" and records[0]["native_fields"][-1] == "D|100|1020"
    assert records[0]["quote_time"] == "2026-10-30T15:34:59+08:00"
    assert coverage["empty_securities"] == ["sz000001"]
    assert len(records) == 1


@pytest.mark.parametrize("body,symbols,reason", [
    (wire(), ["sz000001"], "security set"),
    (wire()+wire(), ["sh600000"], "security set"),
    (wire(day="2026-10-31"), ["sh600000"], "future"),
    (wire(volume="1.5"), ["sh600000"], "fractional"),
    (wire()+b"<script>bad</script>", ["sh600000"], "content"),
])
def test_invalid_bulk_quote_is_rejected(body, symbols, reason):
    with pytest.raises(ValueError, match=reason):
        parse_quotes(body, symbols, "2026-10-30T12:00:00+00:00")


def test_zero_trade_quote_is_retained_without_fabricating_price_or_suspension():
    records, _ = parse_quotes(wire(no_trade=True), ["sh600000"], "2026-10-30T12:00:00+00:00")
    assert records[0]["zero_trade_fields"] and records[0]["close"] == "0"
    assert "suspended" not in records[0]


def test_cni_current_file_header_and_actual_date_not_monthly_weight_file():
    import pandas as pd
    data = pd.DataFrame([["2026-10-29", "300750", "宁德时代", "工业", "13.64"]],
                        columns=["日期", "样本代码", "样本简称", "所属行业", "权重（%）"])
    def xls(frame):
        stream = BytesIO()
        frame.to_excel(stream, index=False)
        return stream.getvalue()
    rows, coverage = parse_cni(xls(data), "2026-10-30")
    assert rows[0]["交易所"] == "深圳证券交易所" and rows[0]["日期"] == "2026-10-29"
    assert rows[0]["权重百分比"] == "13.64" and not coverage["monthly_weight_file_used"]
    partial = data.copy()
    partial.loc[0, "权重（%）"] = "--"
    unweighted, meta = parse_cni(xls(partial), "2026-10-30")
    assert unweighted[0]["权重百分比"] is None and meta["available_weights"] == 0
    assert unweighted[0]["成分券代码"] == "300750"
    with pytest.raises(ValueError, match="columns"):
        parse_cni(xls(data.assign(**{"总市值(亿元)": "1"})), "2026-10-30")
    with pytest.raises(ValueError, match="future"):
        parse_cni(xls(data), "2026-10-28")


def test_topics_keep_native_date_and_score_without_inventing_role():
    raw = dict(bflag=1, ztrq=20220228, ztmc="CIPS", gld=3, rxsj=20261030,
               ztnr="公开题材说明", arec=4, id=2817, sslb=2)
    rows, _ = parse_topics([dict(rows=[raw])], "sh600000")
    assert rows[0]["source_native"] == raw and rows[0]["first_publication_at"] is None
    assert "date" not in rows[0] and "heat_score" not in rows[0]


def test_latest_membership_replaces_prior_list_and_full_quotes_are_shared():
    from ashare_data.providers.supplemental_updates import project
    from test_supplemental_updates import source
    prior = source("index_stock_cons_csindex", [dict(指数代码="000300", 日期="2026-10-29",
                   交易所="上海证券交易所", 成分券代码="600001")])
    current = source("index_stock_cons_csindex", [dict(指数代码="000300", 日期="2026-10-30",
                     交易所="上海证券交易所", 成分券代码="600000")])
    current[0].update(observed_at="2026-10-30T12:00:00+00:00", snapshot_id="new")
    rows, _ = parse_quotes(wire(), ["sh600000"], "2026-10-30T12:00:00+00:00")
    value = project(125, [prior, current, source("sina_current_quotes", rows)])
    assert value[4]["complete_current_member_quotes"]
    assert value[4]["missing_member_quotes"] == [] and value[4]["member_count"] == 1
    assert value[0][0]["volume_unit"] == "share"
    assert project(112, [prior, current])[0][0]["成分券代码"] == "600000"


def test_batch_day_reuse_and_failure_resume_preserve_successful_scopes(tmp_path, monkeypatch):
    import ashare_data.providers.supplemental_batch as batch
    import ashare_data.providers.free_update_worker as worker
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(dict(manual_snapshot_queries=[])))
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(dict(schema="ad.supplemental-19-batch.v1", root=str(tmp_path/"store"),
        source_index=str(seed), source_roots=[], csi_indices=[], topic_securities=[], max_jobs=4, attempts_per_day=2)))
    calls = []
    clock = datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz)
    monkeypatch.setattr(batch, "datetime", Clock)
    monkeypatch.setattr(worker, "now", lambda: clock.isoformat())
    day = clock.date().isoformat()
    failed = False
    def collect(cap, function, params, as_of):
        nonlocal failed
        calls.append((function, as_of))
        if function == "cni_current_members":
            rows = [dict(指数代码="399006", 日期=as_of, 交易所="深圳证券交易所", 成分券代码="300750")]
        else:
            if not failed:
                failed = True
                raise ValueError("temporary parser fixture failure")
            rows, _ = parse_quotes(wire(symbol="sz300750", day=as_of), ["sz300750"], as_of+"T23:59:59+08:00")
        cap.record("https://www.cnindex.com.cn/example", "www.cnindex.com.cn", 200,
                   json.dumps(rows).encode(), {})
        return rows, dict(source_count=len(rows), complete_returned_window=True)
    monkeypatch.setattr(batch, "collect", collect)
    first = batch.run(profile, execute=True, as_of=day)
    assert not first["all_requested_jobs_received"] and first["attempted"] == 2
    second = batch.run(profile, execute=True, as_of=day)
    assert second["all_requested_jobs_received"] and second["attempted"] == 1
    third = batch.run(profile, execute=True, as_of=day)
    assert third["attempted"] == 0 and third["network_requests"] == 0
    assert sum(name == "cni_current_members" for name, _ in calls) == 1
    assert all(i.get("new_records", 0) == i.get("revised_records", 0) == 0 for i in third["derived"]["items"])
    assert len(list((Path(first["state"]).parents[2]/"manual-snapshots").glob("latest-*.json"))) == 2
    clock += timedelta(days=1)
    next_day = batch.run(profile, execute=True)
    assert next_day["attempted"] == 2 and next_day["all_requested_jobs_received"]
    assert next_day["state"] != first["state"]
    assert Path(first["state"]).exists()
    assert next_day["derived"]["items"][7]["scope"]["source_dates"]["through"] == clock.date().isoformat()
    clock += timedelta(days=1)
    stop = tmp_path / "store/STOP"
    stop.write_text("pause")
    paused = batch.run(profile, execute=True)
    assert paused["deferred"] and paused["attempted"] == 0
    stop.unlink()
    resumed = batch.run(profile, execute=True)
    assert resumed["attempted"] == 2 and resumed["all_requested_jobs_received"]


def test_shared_refusal_covers_new_sina_host(tmp_path):
    import sqlite3
    from ashare_data.providers.supplemental_batch import cooling_hosts
    p = tmp_path/"source"
    p.mkdir()
    with sqlite3.connect(p/"observations.sqlite") as db:
        db.execute("CREATE TABLE cooldowns(host TEXT,until_epoch REAL,reason TEXT)")
        db.execute("INSERT INTO cooldowns VALUES(?,?,?)", ("finance.sina.com.cn", 9999999999, "HTTP403"))
    assert "hq.sinajs.cn" in cooling_hosts([p])
