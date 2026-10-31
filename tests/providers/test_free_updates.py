"""Synthetic offline contract tests; none count as live menu acceptance."""
import gzip
import hashlib
import json

import pytest

from ashare_data.providers.free_updates import catalog, plan, assess_records, source_date
from ashare_data.providers.free_updates.free_update_worker import Capture, SourceBlocked
from ashare_data.storage.free_updates import connect, ingest, query, report, writer


def sample(db, root, run_id="one", value=1, at="2026-10-28T11:00:00+00:00"):
    job = plan(rows=[137],as_of="2026-10-28")["jobs"][0]
    raw = json.dumps({"value":value}).encode()
    sha = hashlib.sha256(raw).hexdigest()
    (root/"raw").mkdir(exist_ok=True)
    (root/"raw"/(sha+".gz")).write_bytes(gzip.compress(raw))
    db.execute("INSERT INTO runs(run_id,job_id,as_of,started_at,status,job_json,result_json) VALUES(?,?,?,?,?,?,?)",
               (run_id,job["job_id"],"2026-10-28",at,"running",json.dumps(job),"{}"))
    db.commit()
    result={"status":"received","finished_at":at,"records":[{"TRADE_DATE":"2026-10-20","LPR1Y":value}],
            "receipts":[{"sha256":sha,"host":"example.test","status":200}],"fixture":True}
    return job,result


def test_exact_scope_and_shared_packaging():
    c=catalog()
    assert len(c["items"])==198
    assert c["workbook_sha256"]=="9f5df53320550da2e4b3497d62382abe7a0b3adab1bca5e7d5310b3919da6a58"
    assert c["counts"]=={"可尝试":198,"部分可尝试":53,"本轮尚未找到":18}
    p=plan(rows=[60,61],context={"stock_prefixed":"sh600000"},as_of="2026-10-28")
    assert len(p["items"])==2 and len(p["jobs"])==1
    assert p["jobs"][0]["menu_rows"]==[60,61]
    with pytest.raises(ValueError,match="outside"):
        plan(rows=[29])


def test_explicit_context_and_multiple_symbols():
    p=plan(rows=[12],as_of="2026-10-28")
    assert p["items"][0]["missing_context"]==["stock_symbol"] and not p["jobs"]
    p=plan(rows=[12],context=[{"stock_symbol":"sh600000"},{"stock_symbol":"sz000001"}],as_of="2026-10-28")
    assert len(p["jobs"])==2
    assert {j["parameters"]["symbol"] for j in p["jobs"]}=={"sh600000", "sz000001"}
    assert all(j["parameters"]["period"]=="1" for j in p["jobs"])


def test_shared_macro_checkpoint_adds_new_menu_query_mapping_without_download(tmp_path,monkeypatch):
    from ashare_data.storage.free_updates import collect
    one=plan(rows=[147],as_of='2026-10-29')
    job=one['jobs'][0]
    with connect(tmp_path) as db:
        db.execute('INSERT INTO runs(run_id,job_id,as_of,started_at,finished_at,status,job_json,result_json) VALUES(?,?,?,?,?,?,?,?)',
                   ('one',job['job_id'],'2026-10-29','2026-10-29T04:00:00+00:00','2026-10-29T04:00:01+00:00','received',json.dumps(job),'{}'))
        db.execute('INSERT INTO menu_runs VALUES(?,?)',(147,'one'))
        db.commit()
    def no_worker(*args,**kwargs):raise AssertionError('checkpoint reuse must not request source')
    monkeypatch.setattr('ashare_data.storage.free_updates._worker_result',no_worker)
    shared=plan(rows=[147,148,149,163],as_of='2026-10-29')
    assert len(shared['jobs'])==1
    result=collect(tmp_path,shared,max_jobs=1)
    assert result[0]['status']=='already_received'
    with connect(tmp_path,readonly=True) as db:
        assert {r[0] for r in db.execute('SELECT excel_row FROM menu_runs')}=={147,148,149,163}
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==1


def test_no_production_roots(tmp_path):
    (tmp_path/"CURRENT").write_text("production")
    with pytest.raises(ValueError,match="existing"):
        connect(tmp_path)
    assert (tmp_path/"CURRENT").read_text()=="production"


def test_dedupe_revisions_received_visibility_and_readback(tmp_path):
    root=tmp_path/"sample"
    with writer(root) as db:
        job,result=sample(db,root)
        assert ingest(db,root,"one",job,result,as_of="2026-10-28")["inserted"]==1
        job,result=sample(db,root,"two",at="2026-10-28T12:00:00+00:00")
        assert ingest(db,root,"two",job,result,as_of="2026-10-28")["inserted"]==0
        job,result=sample(db,root,"three",value=2,at="2026-10-28T13:00:00+00:00")
        assert ingest(db,root,"three",job,result,as_of="2026-10-28")["inserted"]==1
    assert len(query(root,137,as_of="2026-10-28T14:00:00+00:00"))==2
    assert len(query(root,137,as_of="2026-10-28T12:30:00+00:00"))==1
    assert query(root,137,as_of="2026-10-28T12:30:00+00:00")[0]["last_seen"]=="2026-10-28T12:00:00+00:00"
    assert query(root,137,as_of="2026-10-20T00:00:00+00:00")==[]
    assert report(root)["completed"]==0


def test_tampered_raw_never_commits(tmp_path):
    root=tmp_path/"sample"
    with writer(root) as db:
        job,result=sample(db,root)
        path=root/"raw"/(result["receipts"][0]["sha256"]+".gz")
        path.write_bytes(gzip.compress(b"tampered"))
        with pytest.raises(ValueError,match="hash"):
            ingest(db,root,"one",job,result,as_of="2026-10-28")
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]==0


def test_query_duplicate_payload_uses_latest_deterministic_provenance(tmp_path):
    root=tmp_path/'sample'
    with writer(root) as db:
        for run_id,at in [('first','2026-10-28T11:00:00+00:00'),
                          ('second','2026-10-28T12:00:00+00:00'),
                          ('reparse','2026-10-28T12:00:00+00:00')]:
            job,result=sample(db,root,run_id,at=at)
            job['implementation_sha256']=run_id
            db.execute('UPDATE runs SET job_json=? WHERE run_id=?',(json.dumps(job),run_id))
            ingest(db,root,run_id,job,result,as_of='2026-10-28')
    before=query(root,137,as_of='2026-10-28T11:30:00+00:00')
    after=query(root,137,as_of='2026-10-28T12:30:00+00:00')
    assert len(before)==len(after)==1
    assert before[0]['source_context']['implementation_sha256']=='first'
    assert after[0]['source_context']['implementation_sha256']=='reparse'
    assert after[0]['first_seen']=='2026-10-28T11:00:00+00:00'
    assert after[0]['last_seen']=='2026-10-28T12:00:00+00:00'


def test_empty_stale_and_invalid_ohlc():
    with pytest.raises(ValueError,match="empty"):
        assess_records([],mode="series",as_of="2026-10-28")
    assert assess_records([{"日期":"2020-01-01","值":1}],mode="series",as_of="2026-10-28")["freshness"]=="stale"
    assert assess_records([{"值":1}],mode="series",as_of="2026-10-28")["freshness"]=="unverified_date"
    with pytest.raises(ValueError,match="OHLC"):
        assess_records([{"date":"2026-10-28","open":5,"high":3,"low":1,"close":2}],mode="bars",as_of="2026-10-28")
    assert source_date("2026年09月份")=="2026-09-01"
    assert source_date("20261028")=="2026-10-28"


def test_source_challenge_stops_and_cooldown_persists(tmp_path):
    c=Capture(tmp_path/"raw",interval=0)
    with pytest.raises(SourceBlocked,match="review"):
        c.before("http://127.0.0.1/private")
    assert c.before("http://114.115.232.154:8080/handler/download.ashx") == "114.115.232.154"
    with pytest.raises(SourceBlocked, match="review"):
        c.before("http://114.115.232.154:8080/private")
    with pytest.raises(SourceBlocked,match="HTTP 429"):
        c.record("https://data.eastmoney.com/a","data.eastmoney.com",429,b"limited",{"Retry-After":"7200"})
    with pytest.raises(SourceBlocked,match="cooling"):
        c.before("https://data.eastmoney.com/b")
    root=tmp_path/"sample"
    with writer(root) as db:
        job,result=sample(db,root)
        result.update(status="blocked",records=[],receipts=c.receipts)
        ingest(db,root,"one",job,result,as_of="2026-10-28")
        assert db.execute("SELECT host FROM cooldowns").fetchone()[0]=="data.eastmoney.com"
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]==0


def test_writer_lock(tmp_path):
    with writer(tmp_path/"store"):
        with pytest.raises(RuntimeError,match="already running"):
            with writer(tmp_path/"store"):
                pass


def test_tdx_protocol_records_and_truncation():
    import struct
    from ashare_data.providers.tdx_blocks import parse_blocks,parse_members,packet,exchange_code
    data=bytearray(164)
    struct.pack_into("<H",data,0,2)
    struct.pack_into("<H",data,4,1)
    data[6:12]=b"880583"
    name="充电桩".encode("gbk")
    data[28:28+len(name)]=name
    row=parse_blocks(data,3)[0]
    assert row["block_code"]=="880583" and row["block_name"]=="充电桩"
    assert exchange_code(row["block_code"])==20583
    with pytest.raises(ValueError,match="truncated"):
        parse_blocks(data[:-1],3)
    members=bytearray(94)
    struct.pack_into("<H",members,24,1)
    struct.pack_into("<H",members,26,1)
    members[28:34]=b"600000"
    assert parse_members(members,"880583")[0]["security"]=="sh600000"
    assert packet(0x1231,b"12")==bytes.fromhex("0100000000010400040031123132")


def test_treasury_date_field_variants():
    from ashare_data.providers.free_updates.free_update_worker import direct_records
    class FakeCapture:
        def get(self,url):
            return b'<feed><properties><QUOTE_DATE>2026-10-27T00:00:00</QUOTE_DATE><RATE>4</RATE></properties></feed>'
    rows=direct_records(FakeCapture(),"treasury_rates",{"year":"2026","kind":"daily_treasury_long_term_rate"})
    assert rows==[{"QUOTE_DATE":"2026-10-27T00:00:00","RATE":"4","date":"2026-10-27"}]


def test_urllib_excel_uses_same_review_and_receipts(tmp_path):
    pytest.importorskip("requests")
    pytest.importorskip("curl_cffi")
    pytest.importorskip("openpyxl")
    from io import BytesIO
    from email.message import Message
    from urllib.response import addinfourl
    from unittest.mock import patch
    import pandas as pd
    import urllib.request

    stream = BytesIO()
    pd.DataFrame({"value": [1]}).to_excel(stream, index=False)
    capture = Capture(tmp_path / "raw", interval=0)
    with patch("requests.Session.send"), patch("curl_cffi.requests.Session.request"), patch("urllib.request.urlopen"), patch("ashare_data.providers.free_updates.free_update_worker.build_opener") as opener:
        capture.install()
        with pytest.raises(SourceBlocked, match="review"):
            pd.read_excel("http://127.0.0.1/private.xlsx")
        opener.assert_not_called()
        url = "https://www.sse.com.cn/example.xlsx"
        opener.return_value.open.return_value = addinfourl(BytesIO(stream.getvalue()), Message(), url, 200)
        frame = pd.read_excel(url, engine="openpyxl")
        assert frame["value"].tolist() == [1]
        assert len(capture.receipts) == 1
        assert capture.receipts[0]["sha256"] == hashlib.sha256(stream.getvalue()).hexdigest()
        with pytest.raises(SourceBlocked, match="TLS"):
            urllib.request.urlopen(url, context=object())


def test_sse_publication_window_and_real_statistical_date():
    pytest.importorskip("bs4")
    from ashare_data.providers.free_updates.free_update_public_pages import collect
    class FakeCapture:
        urls = []
        def get(self, url, headers=None):
            self.urls.append(url)
            rows = [] if "2026-10-28" in url else [{"SEC_CODE": "510300", "SEC_NAME": "沪深300ETF", "ETF_TYPE": "股票", "STAT_DATE": "2026-10-27", "TOT_VOL": "100.25"}]
            return json.dumps({"result": rows, "pageHelp": {"total": len(rows)}}).encode()
    capture = FakeCapture()
    rows = collect(capture, "fund_etf_scale_sse", {"date": "20261028"})
    assert rows[0]["统计日期"] == "2026-10-27" and rows[0]["基金份额"] == 1002500
    assert capture.dataset_metadata["fallback_calendar_days"] == 1 and len(capture.urls) == 2
    with pytest.raises(ValueError):
        collect(capture, "fund_etf_scale_sse", {"date": ""})


def test_bounded_public_pages_keep_units_products_and_cursors():
    pytest.importorskip("bs4")
    from ashare_data.providers.free_updates.free_update_public_pages import parse_carbon, parse_fund_flow, amount
    html = '<table class="jyhq_tab"><tr><th>日期</th><th>成交量(吨)</th><th>成交均价(元/吨)</th><th>成交额(元)</th></tr><script>currentpage="2";totalpage="151";</script><tr><td>2026-10-20</td><td>10</td><td>100.01</td><td>1,000.10(BEA)</td></tr></table>'
    rows, coverage = parse_carbon(html.encode(), 2)
    assert coverage["next_page"] == 3 and not coverage["complete_history"]
    assert rows[0]["产品"] == "BEA" and rows[0]["成交额"] == 1000.1
    with pytest.raises(ValueError, match="page identity"):
        parse_carbon(html.encode(), 1)
    with pytest.raises(ValueError, match="units"):
        parse_carbon(html.replace("成交额(元)", "成交额(万元)").encode(), 2)
    assert amount("-3.25亿") == -325000000 and amount("--") is None
    with pytest.raises(ValueError, match="unrecognized"):
        amount("123美元")
    headers = ["序号", "股票代码", "股票简称", "最新价", "涨跌幅", "换手率", "流入资金(元)", "流出资金(元)", "净额(元)", "成交额(元)"]
    values = ["1", "000001", "平安银行", "12", "-0.5%", "2%", "1亿", "9000万", "1000万", "1.9亿"]
    html = '<span class="page_info">1/10</span><table class="J-ajax-table"><thead><tr>' + ''.join(f'<th>{h}</th>' for h in headers) + '</tr></thead><tbody><tr>' + ''.join(f'<td>{v}</td>' for v in values) + '</tr></tbody></table>'
    rows, coverage = parse_fund_flow(html.encode("gb18030"), 1)
    assert rows[0]["股票代码"] == "000001" and rows[0]["涨跌幅"] == -0.5
    assert rows[0]["净额(元)"] == 10000000 and rows[0]["source_text"]["净额(元)"] == "1000万"
    assert coverage["next_page"] == 2 and not coverage["atomic_snapshot"]


def test_macro_period_units_and_stopped_columns():
    from ashare_data.providers.free_updates.free_update_semantics import validate_semantics
    job = {"function": "macro_china_ppi", "parameters": {}}
    rows = [{"REPORT_DATE": "2026-09-01", "TIME": "2026年09月份", "BASE": 105, "BASE_SAME": 5, "BASE_ACCUMULATE": 102.3}]
    result = validate_semantics(rows, job, "2026-10-28")
    assert result["units"]["BASE_SAME"] == "percent" and result["latest_announcement"] is None
    with pytest.raises(ValueError, match="duplicate"):
        validate_semantics(rows*2, job, "2026-10-28")
    with pytest.raises(ValueError, match="contradicts"):
        validate_semantics([{**rows[0], "TIME": "2026年08月份"}], job, "2026-10-28")
    result = validate_semantics([{**rows[0], "BASE_SAME": None}], job, "2026-10-28")
    assert result["gaps"] and result["missing_value_counts"]["BASE_SAME"] == 1
    job = {"function": "macro_indicator_em", "parameters": {"indicator": "industrial_yoy"}}
    rows = [{"日期": "2026-09-01", "statistical_month": "2026-09", "indicator": "industrial_yoy", "source_field": "BASE_SAME", "publication_date": None, "value": 5.5}]
    assert not validate_semantics(rows, job, "2026-10-28")["gaps"]
    with pytest.raises(ValueError, match="mapping"):
        validate_semantics([{**rows[0], "source_field": "BASE_ACCUMULATE"}], job, "2026-10-28")


def test_macro_pagination_is_not_silently_truncated():
    from ashare_data.providers.free_updates.free_update_worker import direct_records
    class FakeCapture:
        def get(self,url):
            page=2 if "pageNumber=2" in url else 1
            return json.dumps({"success":True,"result":{"pages":2,"count":2,"data":[{"TRADE_DATE":f"2026-10-{page:02d}"}]}}).encode()
    rows=direct_records(FakeCapture(),"macro_china_lpr",{})
    assert len(rows)==2
    assert rows[1]["TRADE_DATE"]=="2026-10-02"


def test_financial_semantics_identity_dates_and_duplicate_keys():
    from ashare_data.providers.free_updates.free_update_semantics import validate_semantics
    job={"function":"stock_balance_sheet_by_report_em","parameters":{"symbol":"SH600000"}}
    row={"SECUCODE":"600000.SH","SECURITY_CODE":"600000","CURRENCY":"CNY",
         "REPORT_DATE":"2026-06-30","NOTICE_DATE":"2026-08-28","REPORT_TYPE":"中报",
         "TOTAL_ASSETS":100,"TOTAL_LIABILITIES":80,"TOTAL_EQUITY":20,"TOTAL_LIAB_EQUITY":100}
    result=validate_semantics([row],job,"2026-10-28")
    assert result["latest_period"]=="2026-06-30" and result["latest_announcement"]=="2026-08-28"
    assert result["units"]["TOTAL_ASSETS"]=="CNY"
    for replacement, match in [({"SECURITY_CODE":"000001"},"identity"),
                                ({"NOTICE_DATE":"2026-06-01"},"date"),
                                ({"TOTAL_ASSETS":102},"balance sheet")]:
        with pytest.raises(ValueError,match=match):
            validate_semantics([dict(row,**replacement)],job,"2026-10-28")
    with pytest.raises(ValueError,match="duplicate"):
        validate_semantics([row,row],job,"2026-10-28")
    unknown=validate_semantics([dict(row,NOTICE_DATE=None)],job,"2026-10-28")
    assert unknown["missing_announcement_rows"]==1 and unknown["latest_announcement"] is None


def test_policy_release_is_not_a_daily_staleness_rule():
    value=assess_records([{"TRADE_DATE":"2025-05-15","rate":1}],mode="policy_event",as_of="2026-10-28")
    assert value["freshness"]=="release_verification_pending"
    assert value["latest_nonfuture_date"]=="2025-05-15"


def test_failure_attempts_and_timeouts_are_bounded(tmp_path):
    from ashare_data.providers.free_updates.free_update_worker import bounded_timeout
    assert bounded_timeout((3,None))==(3,12)
    assert bounded_timeout(100)==12
    c=Capture(tmp_path,interval=0,request_limit=2)
    c.before("https://data.eastmoney.com/a")
    c.before("https://data.eastmoney.com/a")
    with pytest.raises(SourceBlocked,match="budget"):
        c.before("https://data.eastmoney.com/a")


def test_transient_error_persists_cooldown_without_rows(tmp_path):
    c=Capture(tmp_path/"raw",interval=0)
    c.transport_failure("https://data.eastmoney.com/a","data.eastmoney.com",TimeoutError())
    root=tmp_path/"sample"
    with writer(root) as db:
        job,result=sample(db,root)
        result.update(status="failed",records=[],receipts=c.receipts)
        ingest(db,root,"one",job,result,as_of="2026-10-28")
        assert db.execute("SELECT host FROM cooldowns").fetchone()[0]=="data.eastmoney.com"
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]==0


def test_factor_parser_never_executes_source_javascript():
    from ashare_data.providers.free_updates.free_update_equities import collect
    class FakeCapture:
        def get(self,url,headers):
            return b'var sh600000hfq={"total":1,"data":[{"d":"2026-08-28","f":"1.23"}]};'
    params={"symbol":"sh600000","adjust":"hfq-factor"}
    assert collect(FakeCapture(),"stock_zh_a_daily",params)[0]["hfq_factor"]=="1.23"
    class ExecutablePayload:
        def get(self,url,headers):
            return b'var sh600000hfq=__import__("os").system("false");'
    with pytest.raises(ValueError,match="payload"):
        collect(ExecutablePayload(),"stock_zh_a_daily",params)


def test_futures_month_label_is_last_actual_trade_date():
    import pandas as pd
    from ashare_data.providers.free_updates.free_update_worker import special
    class FakeAK:
        def futures_main_sina(self,**kwargs):
            return pd.DataFrame({"日期":["2026-10-01","2026-10-13"],"开盘价":[1,2],
                                 "最高价":[3,4],"最低价":[1,2],"收盘价":[2,3],"成交量":[2,3]})
    result=special(FakeAK(),"futures_resample",{"symbol":"X0","period":"ME","end_date":"20261028"})
    assert result.iloc[0]["日期"]==pd.Timestamp("2026-10-13")
    assert bool(result.iloc[0]["周期未结束"])
    assert result.iloc[0]["成交量"]==5


def test_null_actual_forecast_cannot_refresh_a_series():
    rows=[{"日期":"2025-01-01","今值":2}, {"日期":"2026-10-28","今值":None,"预测值":3}]
    result=assess_records(rows,mode="series",as_of="2026-10-28")
    assert result["freshness"]=="stale" and result["latest_nonfuture_date"]=="2025-01-01"


def test_failed_worker_with_rows_never_commits(tmp_path):
    with writer(tmp_path/"store") as db:
        job,result=sample(db,tmp_path/"store")
        result["status"]="failed"
        with pytest.raises(ValueError,match="must not commit"):
            ingest(db,tmp_path/"store","one",job,result,as_of="2026-10-28")
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]==0


def test_resumed_budget_advances_and_source_groups_do_not_overlap(tmp_path, monkeypatch):
    import threading
    import time
    from ashare_data.storage.free_updates import collect
    p=plan(rows=[137,197,198],as_of="2026-10-28")
    held=set()
    lock=threading.Lock()
    seen=[]
    def fake(root,request_path,output_path,journal_path,timeout):
        request=json.loads(request_path.read_text())
        job=request["job"]
        groups=set(job["source_families"])
        with lock:
            assert not groups & held
            held.update(groups)
            seen.append(job["job_id"])
        time.sleep(.02)
        with lock:
            held.difference_update(groups)
        raw=b"synthetic queue fixture"
        sha=hashlib.sha256(raw).hexdigest()
        (root/"raw"/(sha+".gz")).write_bytes(gzip.compress(raw))
        records=[{"TRADE_DATE":"2026-10-20","LPR1Y":3}] if job["function"]=="macro_china_lpr" else [{"date":"2026-10-27","BC_1YEAR":"4"}]
        return {"status":"received","records":records,"receipts":[{"sha256":sha,"host":"example.test","status":200}]}
    monkeypatch.setattr("ashare_data.storage.free_updates._worker_result",fake)
    root=tmp_path/"store"
    first=collect(root,p,max_jobs=1,workers=2)
    assert first[0]["status"]=="received"
    resumed=collect(root,p,max_jobs=2,workers=2)
    assert sum(r["status"]=="already_received" for r in resumed)==1
    assert sum(r["status"]=="received" for r in resumed)==2
    assert len(seen)==3 and len(set(seen))==3


def test_timeout_retains_http_receipts_and_attempt_budget(tmp_path,monkeypatch):
    import subprocess
    from ashare_data.storage.free_updates import collect
    def timeout(args,**kwargs):
        request=json.loads(__import__("pathlib").Path(args[-2]).read_text())
        journal=__import__("pathlib").Path(request["journal_path"])
        journal.write_text(json.dumps({"host":"example.test","status":429,"sha256":"unused"})+"\n{truncated")
        raise subprocess.TimeoutExpired("synthetic",1)
    monkeypatch.setattr("ashare_data.storage.free_updates.subprocess.run",timeout)
    p=plan(rows=[137],as_of="2026-10-28")
    root=tmp_path/"store"
    result=collect(root,p,timeout=1,attempts_per_day=1)
    assert result[0]["status"]=="timeout" and len(result[0]["receipts"])==1
    assert collect(root,p,timeout=1,attempts_per_day=1)[0]["status"]=="daily_attempt_limit"
    with connect(root,readonly=True) as db:
        assert db.execute("SELECT host FROM cooldowns").fetchone()[0]=="example.test"


def test_scheduled_preview_is_readonly_and_rejects_production(tmp_path):
    from ashare_data.providers.free_updates.free_update_schedule import run_scheduled
    config=tmp_path/"schedule.json"
    config.write_text(json.dumps({"schema":"ad.free-update-schedule.v1","root":"sample","rows":[137]}))
    result=run_scheduled(config,as_of="2026-10-28")
    assert result["dry_run"] and result["jobs"]==1 and not (tmp_path/"sample").exists()
    (tmp_path/"sample").mkdir()
    (tmp_path/"sample"/"CURRENT").write_text("untouched")
    with pytest.raises(ValueError,match="existing"):
        run_scheduled(config,as_of="2026-10-28")


def test_month_and_annual_period_dates_keep_their_frequency():
    assert source_date("2026.9")=="2026-09-01"
    assert source_date("202609")=="2026-09-01"
    result=assess_records([{"统计年度":"2025","数量":2}],mode="series",as_of="2026-10-28")
    assert result["period_precision"]=="year" and result["freshness"]=="recent"
    assert result["latest_nonfuture_date"]=="2025-01-01"
    assert source_date("202613") is None


def test_snapshot_date_is_observation_time_not_backdated_request(tmp_path):
    with writer(tmp_path/"store") as db:
        job,result=sample(db,tmp_path/"store")
        job=dict(job,mode="snapshot",function="synthetic_snapshot")
        ingest(db,tmp_path/"store","one",job,result,as_of="2020-01-01")
        payload=json.loads(db.execute("SELECT payload_json FROM observations").fetchone()[0])
        assert payload["_snapshot_date"]=="2026-10-28"


def test_macro_page_preserves_sections_and_explicit_remaining_offset():
    from ashare_data.providers.free_updates.free_update_macro_pages import parse_page
    body={"count":3,"config":{"all":[[0,"统计时间"],[1,"值","亿元"]]},
          "data":{"非累计":[["2026.9","2"],["2026.8","1"]],"累计":[["2026.9","7"],["2026.8","5"]]}}
    rows,coverage=parse_page(body,offset=0,requested=2)
    assert len(rows)==4 and {r["source_section"] for r in rows}=={"累计","非累计"}
    assert not coverage["complete_history"] and coverage["next_offset"]==2
    assert coverage["units"]=={"值":"亿元"}
    body["data"]["累计"][0].append("unexpected")
    with pytest.raises(ValueError,match="width"):
        parse_page(body,offset=0,requested=2)


def test_macro_group_boundary_overlap_uses_source_offsets_and_shared_series(tmp_path):
    from ashare_data.providers.free_updates.free_update_macro_pages import parse_page
    body={"count":6,"config":{"all":[[0,"统计时间"]]},"data":[["2026.9"],["2026.8"],["2026.7"]]}
    rows,coverage=parse_page(body,offset=0,requested=2)
    assert len(rows)==3 and coverage["next_offset"]==2 and coverage["boundary_overlap_possible"]
    root=tmp_path/"store"
    with writer(root) as db:
        job,result=sample(db,root)
        job=dict(job,function="sina_macro_page",parameters={"dataset":"example","offset":0,"limit":2})
        assert ingest(db,root,"one",job,result,as_of="2026-10-28")["inserted"]==1
        _,result=sample(db,root,"two")
        job=dict(job,parameters=dict(job["parameters"],offset=2))
        assert ingest(db,root,"two",job,result,as_of="2026-10-28")["inserted"]==0


def test_cpi_alternative_has_explicit_period_units_and_missing_publication():
    from ashare_data.providers.free_updates.free_update_worker import direct_records
    class FakeCapture:
        def get(self,url):
            return json.dumps({"result":{"pages":1,"count":1,"data":[{"REPORT_DATE":"2026-09-01","NATIONAL_SEQUENTIAL":.2,"NATIONAL_SAME":.4}]}}).encode()
    capture=FakeCapture()
    result=direct_records(capture,"cpi_rate_em",{"indicator":"mom"})
    assert result[0]["value"]==.2 and result[0]["statistical_month"]=="2026-09"
    assert result[0]["publication_date"] is None
    assert capture.dataset_metadata["unit"]=="percent"


def test_gdp_official_quarter_not_cumulative_or_qoq_and_annual_title():
    pytest.importorskip("bs4")
    from ashare_data.providers.free_updates.free_update_official import parse_gdp
    body = '''<meta name="ArticleTitle" content="2025年四季度和全年国内生产总值初步核算结果">
    <meta name="PubDate" content="2026/01/20 09:30">
    <table><tr><td>GDP环比</td><td>1.2</td></tr></table>
    <table><tr><td>单位：%</td></tr><tr><td>年份</td><td>1季度</td><td>2季度</td><td>3季度</td><td>4季度</td></tr>
    <tr><td>2025</td><td>5.4</td><td>5.2</td><td>4.8</td><td>4.5</td></tr>
    <tr><td>注：同比增长速度为与上年同期对比的增长速度。</td></tr></table>'''
    rows = parse_gdp(body, "https://www.stats.gov.cn/example")
    assert rows[-1]["value"] == 4.5 and rows[-1]["日期"] == "2025-12-31"
    assert rows[-1]["publication_at"] == "2026-01-20T09:30:00+08:00"
    with pytest.raises(ValueError, match="unit"):
        parse_gdp(body.replace("单位：%", "单位：亿元"), "https://www.stats.gov.cn/example")
    with pytest.raises(ValueError, match="future quarter"):
        parse_gdp(body.replace("四季度和全年", "三季度"), "https://www.stats.gov.cn/example")
    from ashare_data.providers.free_updates.free_update_official import collect_gdp, NBS_RELEASES
    class Capture:
        def get(self, url):
            if url == NBS_RELEASES:
                return '<a href="202601/release.html">2025年四季度和全年国内生产总值初步核算结果</a>'.encode()
            return body.encode()
    capture = Capture()
    assert collect_gdp(capture) == parse_gdp(body, NBS_RELEASES+'202601/release.html')
    assert capture.dataset_metadata['complete_returned_window']
    assert capture.dataset_metadata['source_count'] == 4
    assert not capture.dataset_metadata['complete_history']


def test_sge_local_midnight_and_outer_date_join():
    pytest.importorskip("bs4")
    from ashare_data.providers.free_updates.free_update_official import parse_sge_benchmark
    data = {"zp": [[1460995200000,256.92], [1461081600000,261.15]], "wp": [[1461081600000,259.97]]}
    rows = parse_sge_benchmark(data, "gold")
    assert rows[0]["交易时间"] == "2016-04-19" and rows[0]["午盘价"] is None
    assert rows[1]["早盘价"] == 261.15 and rows[1]["午盘价"] == 259.97
    data["wp"].append([1461081600000,259.97])
    with pytest.raises(ValueError, match="duplicate"):
        parse_sge_benchmark(data, "gold")
    with pytest.raises(ValueError, match="midnight"):
        parse_sge_benchmark({"zp": [[1460995200001,256.92]], "wp": []}, "gold")


def test_known_bad_projection_excluded_without_deleting_audit(tmp_path):
    with writer(tmp_path/"store") as db:
        job, result = sample(db, tmp_path/"store")
        job = dict(job, function="spot_golden_benchmark_sge", menu_rows=[215])
        db.execute("UPDATE runs SET job_json=? WHERE run_id='one'", (json.dumps(job),))
        ingest(db,tmp_path/"store","one",job,result,as_of="2026-10-28")
    assert query(tmp_path/"store", 215) == []
    audit = query(tmp_path/"store", 215, include_superseded=True)
    assert len(audit) == 1 and audit[0]["superseded_projection"]
    assert audit[0]["source_context"]["function"] == "spot_golden_benchmark_sge"


def test_safe_direction_and_unknown_currency_fail_closed():
    from ashare_data.providers.free_updates.free_update_semantics import validate_semantics
    columns = ["美元","欧元","日元","港元","英镑","澳元","新西兰元","新加坡元","瑞士法郎","加元",
               "澳门元","林吉特","卢布","兰特","韩元","迪拉姆","里亚尔","福林","兹罗提","丹麦克朗","瑞典克朗","挪威克朗","里拉","比索","泰铢"]
    row = dict.fromkeys(columns)
    row.update(日期="2026-10-28", 美元=700, 林吉特=60)
    job = {"function":"currency_boc_safe", "parameters":{}}
    result = validate_semantics([row], job, "2026-10-28")
    assert result["units"]["美元"] == "CNY per 100 foreign currency"
    assert result["units"]["林吉特"] == "foreign currency per 100 CNY"
    with pytest.raises(ValueError, match="columns"):
        validate_semantics([dict(row, unknown=1)], job, "2026-10-28")


def test_government_curve_filter_and_contract():
    pd = pytest.importorskip("pandas")
    from ashare_data.providers.free_updates.free_update_worker import special
    from ashare_data.providers.free_updates.free_update_semantics import validate_semantics
    class Source:
        def bond_china_yield(self, **params):
            return pd.DataFrame([{"曲线名称": name, "日期":"2026-10-28", "10年":1.63}
                                 for name in ["中债国债收益率曲线", "中债商业银行普通债收益率曲线(AAA)"]])
    params = {"start_date":"20261014", "end_date":"20261028"}
    rows = special(Source(), "china_government_yield", params).to_dict("records")
    assert len(rows) == 1
    job = {"function":"china_government_yield", "parameters":params}
    assert validate_semantics(rows,job,"2026-10-28")["business_key"] == ["曲线名称","日期"]
    with pytest.raises(ValueError, match="identity"):
        validate_semantics([dict(rows[0], 曲线名称="中债商业银行普通债收益率曲线(AAA)")],job,"2026-10-28")


def test_discontinued_official_series_stays_in_menu_but_not_daily_jobs(tmp_path):
    from ashare_data.providers.free_updates.free_update_schedule import scheduled_plan
    config=tmp_path/"schedule.json"
    config.write_text(json.dumps({"schema":"ad.free-update-schedule.v1","root":"sample","rows":[225,137]}))
    prepared, summary=scheduled_plan(config,as_of="2026-10-28")
    assert len(prepared["items"]) == 2 and summary["discontinued_history_only_rows"] == [225]
    assert all(225 not in j["menu_rows"] for j in prepared["jobs"])
