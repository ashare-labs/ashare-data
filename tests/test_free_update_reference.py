"""Synthetic source contracts: named fields, units, identities and replay scope."""
import gzip
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data.providers import free_update_reference as ref
from ashare_data.providers.free_update_semantics import numeric
from ashare_data.providers.free_update_acceptance import acceptance_dimensions
from ashare_data.providers.free_update_dates import require_dates, DateContractError
from ashare_data.providers import free_update_oil as oil
from ashare_data.providers import free_update_research as research
from test_free_update_native_reports import forecast


def profile():
    return dict.fromkeys(ref.PROFILE) | {"ASECCODE": "000001", "ORGNAME": "Synthetic", "ASECNAME": "Synthetic",
        "MARKET": "深交所", "F010D": "2000-01-01", "F006D": "2010-01-01", "F007N": 100}


def payload(rows):
    return json.dumps({"resultcode": 200, "count": len(rows), "total": len(rows), "records": rows}).encode()


def test_cninfo_named_fields_survive_reordering_with_all_native_values_retained():
    native = dict(reversed(list(profile().items()))) | {"SOURCE_EXTRA": "unknown native field"}
    rows, meta = ref.parse_cninfo(payload([native]), "stock_profile_cninfo", {"symbol": "000001"})
    assert rows[0]["注册资金"] == 100 and rows[0]["source_native"] == native
    assert rows[0]["security"] == "000001.SZ" and not meta["complete_universe"]
    result = ref.validate(rows, {"function": "stock_profile_cninfo", "parameters": {"symbol": "000001"}}, "2026-10-29", numeric)
    assert result["units"]["注册资金"] == "10000 CNY" and result["latest_period"] is None and not result["gaps"]
    with pytest.raises(ValueError, match="market"):
        ref.validate([rows[0] | {"所属市场": "上交所"}], {"function": "stock_profile_cninfo", "parameters": {"symbol": "000001"}}, "2026-10-29", numeric)


@pytest.mark.parametrize("changes", [{"resultcode": 403}, {"total": 2}, {"count": 0}, {"records": []}])
def test_cninfo_rejection_or_partial_window_cannot_pass(changes):
    body = json.loads(payload([profile()])) | changes
    with pytest.raises(ValueError):
        ref.parse_cninfo(json.dumps(body).encode(), "stock_profile_cninfo", {"symbol": "000001"})


def test_cninfo_captured_request_method_scope_body_and_hash_are_bound(tmp_path):
    raw = payload([profile()])
    sha = hashlib.sha256(raw).hexdigest()
    (tmp_path/(sha+".gz")).write_bytes(gzip.compress(raw))
    receipt = {"url": "https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1133?scode=000001",
        "sha256": sha, "protocol": "http", "status": 200, "request_method": "POST", "request_body_sha256": hashlib.sha256(b"").hexdigest()}
    capture = SimpleNamespace(root=tmp_path, receipts=[receipt])
    assert ref.captured_cninfo(capture, "stock_profile_cninfo", {"symbol": "000001"})[0]["security"] == "000001.SZ"
    for change in ({"request_method": "GET"}, {"request_body_sha256": "other"}, {"url": receipt["url"].replace("000001", "000002")}):
        with pytest.raises(ValueError):
            ref.captured_cninfo(SimpleNamespace(root=tmp_path, receipts=[receipt | change]), "stock_profile_cninfo", {"symbol": "000001"})


def test_ipo_schedule_known_fields_work_while_unknown_upper_limit_unit_stays_labeled():
    native = dict.fromkeys(ref.IPO) | {"SECCODE": "920001", "SECNAME": "Synthetic", "F002D": "2026-11-01", "F003N": 100, "F043N": 60, "F042N": 2}
    rows, meta = ref.parse_cninfo(payload([native]), "stock_new_ipo_cninfo", {})
    result = ref.validate(rows, {"function": "stock_new_ipo_cninfo", "parameters": {}}, "2026-10-29", numeric)
    assert meta["source_selection"] == {"timetype": "36", "market": "ALL"}
    assert not result["gaps"] and "not verified" in result["units"]["网上申购上限"]
    assert rows[0]["网上申购上限"] == 2 and rows[0]["source_native"]["F042N"] == 2
    assert require_dates(rows, {"function": "stock_new_ipo_cninfo"}, "2026-10-29")["allowed_future_count"] == 1


def bond():
    return dict.fromkeys(ref.BOND_UNITS) | {"SECUCODE": "113001.SH", "SECURITY_CODE": "113001", "SECURITY_NAME_ABBR": "Synthetic",
        "TRADE_MARKET": "CNSESH", "CONVERT_STOCK_CODE": "600001", "ACTUAL_ISSUE_SCALE": 1,
        "PAR_VALUE": 100, "ISSUE_PRICE": 100, "ONLINE_GENERAL_AAU": 1000, "VALUE_DATE": "2026-01-01", "EXPIRE_DATE": "2032-01-01"}


def test_bond_native_null_quote_not_filled_and_subscription_limit_is_thousand_yuan():
    row = bond()
    class Capture:
        def get(self, url):
            q = parse_qs(urlsplit(url).query)
            assert q["sortColumns"] == ["SECUCODE"] and q["filter"] == ['(SECURITY_CODE="113001")']
            return json.dumps({"success": True, "result": {"pages": 1, "count": 1, "data": [row]}}).encode()
    capture = Capture()
    rows = ref.collect_bonds(capture, "bond_zh_cov_info", {"symbol": "113001", "indicator": "基本信息"})
    result = ref.validate(rows, {"function": "bond_zh_cov_info", "parameters": {"symbol": "113001"}}, "2026-10-29", numeric)
    assert result["units"]["ONLINE_GENERAL_AAU"] == "1000 CNY"
    assert rows[0]["CURRENT_BOND_PRICE"] is None and result["latest_period"] is None
    assert require_dates(rows, {"function": "bond_zh_cov_info"}, "2026-10-29")["allowed_future_count"] == 1
    with pytest.raises(DateContractError):
        require_dates([row | {"NOTICE_DATE": "2032-01-01"}], {"function": "bond_zh_cov_info"}, "2026-10-29")


@pytest.mark.parametrize("changes", [{"SECUCODE": "404001.NQ", "SECURITY_CODE": "404001"}, {"TRADE_MARKET": "CNSESZ"}, {"CONVERT_STOCK_CODE": "000001", "SECUCODE": "000001.SH", "SECURITY_CODE": "000001"}])
def test_bond_identity_market_mismatch_is_rejected(changes):
    with pytest.raises(ValueError):
        ref.validate([bond() | changes], {"function": "bond_zh_cov", "parameters": {}}, "2026-10-29", numeric)


@pytest.mark.parametrize("changes", [
    {"SECUCODE": "404001.NQ", "SECURITY_CODE": "404001", "TRADE_MARKET": "STAS00", "CONVERT_STOCK_CODE": "400194"},
    {"SECUCODE": "126001.SH", "SECURITY_CODE": "126001"},
    {"SECUCODE": "125001.SZ", "SECURITY_CODE": "125001", "TRADE_MARKET": "CNSESZ", "CONVERT_STOCK_CODE": "000001"},
])
def test_native_legacy_bond_terms_remain_usable_without_inferred_instrument_type(changes):
    row = bond() | changes
    result = ref.validate([row], {"function": "bond_zh_cov", "parameters": {}}, "2026-10-29", numeric)
    assert not result["gaps"] and result["source_rows"] == 1
    note = result["field_observations"][0]
    assert note["security"] == row["SECUCODE"] and not note["ordinary_convertible_equivalence_claimed"]
    assert row == bond() | changes


def test_bond_page_count_and_duplicate_identity_rejected():
    class Capture:
        def get(self, url):
            return json.dumps({"success": True, "result": {"pages": 1, "count": 2, "data": [bond(), bond()]}}).encode()
    with pytest.raises(ValueError, match="identities/count"):
        ref.collect_bonds(Capture(), "bond_zh_cov", {})


def test_calendar_exception_does_not_allow_future_update_metadata():
    job = {"function": "tool_trade_date_hist_sina"}
    assert require_dates([{"trade_date": "2027-01-01"}], job, "2026-10-29")["allowed_future_count"] == 1
    with pytest.raises(DateContractError):
        require_dates([{"trade_date": "2027-01-01", "UPDATE_DATE": "2199-01-01"}], job, "2026-10-29")


@pytest.mark.parametrize("name,base,fields", [
    ("energy_oil_hist", {"DIM_DATE": "2026-09-25", "调整日期": "2026-09-25"}, oil.HISTORY_UNITS),
    ("energy_oil_detail_latest", {"DIM_DATE": "2026-09-25", "调整日期": "2026-09-25", "DIM_ID": "synthetic", "CITYNAME": "Synthetic"}, oil.REGION_UNITS)])
def test_oil_date_only_record_cannot_pass_price_contract(name, base, fields):
    with pytest.raises(ValueError, match="required current prices"):
        oil.validate([base | dict.fromkeys(fields)], {"function": name}, "2026-10-29", numeric)


@pytest.mark.parametrize("change", [{"SECUCODE": "000001.SH"}, {"DEC_AIMPRICEMIN": -1}, {"DEC_AIMPRICEMAX": 0}])
def test_forecast_index_identity_or_single_sided_nonpositive_target_rejected(change):
    with pytest.raises(ValueError):
        research.validate([forecast() | change], {"function": "stock_profit_forecast_em", "parameters": {}}, "2026-10-29", numeric)


def test_forecast_source_membership_does_not_imply_estimated_eps_available():
    row = forecast() | {"EPS2": None, "EPS3": None, "EPS4": None}
    result = research.validate([row], {"function": "stock_profit_forecast_em", "parameters": {}}, "2026-10-29", numeric)
    assert result["forecast_eps_available_securities"] == 0 and result["forecast_eps_unavailable_securities"] == 1


def test_technical_and_current_window_are_not_publication_freshness():
    item = {"implementation_verified": True, "coverage_verified": True, "calls": [{"function": "energy_oil_hist"}], "sample_runs": []}
    result = acceptance_dimensions(item)
    assert result["technical_sample_verified"] and result["source_window_verified"]
    assert result["freshness_label"] == "实现测试通过但新鲜度未验"
    assert not result["freshness_verified"] and not result["daily_data_ready"]


def test_stock_directory_keeps_migration_aliases_and_excludes_b_and_cdr(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("universe_export_test", Path(__file__).resolve().parent / "support/prepare_free_update_universe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from ashare_data.providers.free_update_disclosures import DIRECTORY
    rows = [{"code": "830001", "category": "A股", "orgId": "one-issuer", "zwjc": "Synthetic old"},
            {"code": "920001", "category": "A股", "orgId": "one-issuer", "zwjc": "Synthetic new"},
            {"code": "200001", "category": "B股", "orgId": "b", "zwjc": "B"},
            {"code": "689001", "category": "CDR", "orgId": "cdr", "zwjc": "CDR"}]
    body = json.dumps({"stockList": rows}).encode()
    sha = hashlib.sha256(body).hexdigest()
    (tmp_path/"raw").mkdir()
    (tmp_path/"runs").mkdir()
    (tmp_path/"raw"/(sha+".gz")).write_bytes(gzip.compress(body))
    (tmp_path/"runs/example.json").write_text(json.dumps({"receipts": [{"url": DIRECTORY, "sha256": sha, "received_at": "2026-10-28T00:00:00+00:00"}]}))
    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): return SimpleNamespace(fetchall=lambda: [{"run_id": "example"}])
    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: DB())
    result = module.export_stock_directory(tmp_path)
    assert result["selected_count"] == 2 and result["excluded_count"] == 2
    assert result["multi_identity_issuers"] == {"one-issuer": ["830001.BJ", "920001.BJ"]}
    assert all(not r["active_listing_verified"] for r in result["members"])


def test_worker_uses_valid_native_response_even_if_sdk_projection_breaks(tmp_path, monkeypatch):
    from ashare_data.providers import free_update_worker as worker
    from ashare_data.providers.free_updates import plan
    capture = []
    monkeypatch.setattr(worker.Capture, "install", lambda self: capture.append(self))
    monkeypatch.setattr("ashare_data.providers.akshare_loader.ReviewedAKShare", lambda: SimpleNamespace(version="1.20.2"))
    def broken_sdk(*args):
        raw = payload([profile() | {"SOURCE_EXTRA": "new field"}])
        capture[0].record("https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1133?scode=000001", "webapi.cninfo.com.cn", 200, raw, {},
            request_method="POST", request_body_sha256=hashlib.sha256(b"").hexdigest())
        raise ValueError("Length mismatch")
    monkeypatch.setattr(worker, "special", broken_sdk)
    job = plan(rows=[20], context=[{"stock":"000001"}], as_of="2026-10-29")["jobs"][0]
    job.update(function="stock_profile_cninfo", parameters={"symbol":"000001"})
    monkeypatch.setattr(worker, "catalog", lambda: {"items": [{"calls": [{"function": "stock_profile_cninfo"}]}]})
    request = {"job": job, "raw_root": str(tmp_path/"raw"), "as_of": "2026-10-29"}
    result = worker.run(request)
    assert result["status"] == "received" and result["records"][0]["source_native"]["SOURCE_EXTRA"] == "new field"
    def denied(*args):
        raise worker.SourceBlocked("HTTP 403")
    monkeypatch.setattr(worker, "special", denied)
    assert worker.run(request)["status"] == "blocked"
    def missing(*args):
        raise ValueError("No original response")
    monkeypatch.setattr(worker, "special", missing)
    assert worker.run(request)["status"] == "failed"


@pytest.mark.parametrize("first,last", [("VALUE_DATE", "EXPIRE_DATE"), ("TRANSFER_START_DATE", "TRANSFER_END_DATE"), ("IB_START_DATE", "IB_END_DATE")])
def test_bond_term_order_is_checked_even_when_future_schedule_is_legal(first, last):
    row = bond() | {first: "2035-01-01", last: "2032-01-01"}
    assert require_dates([row], {"function": "bond_zh_cov"}, "2026-10-29")["violation_count"] == 0
    with pytest.raises(ValueError, match="starts after"):
        ref.validate([row], {"function": "bond_zh_cov", "parameters": {}}, "2026-10-29", numeric)
    missing = bond() | {"VALUE_DATE": None, "EXPIRE_DATE": None}
    result = ref.validate([missing], {"function": "bond_zh_cov", "parameters": {}}, "2026-10-29", numeric)
    assert not result["gaps"] and result["field_observations"][0]["kind"] == "incomplete_contract_dates"


@pytest.mark.parametrize("day", ["1990-01-01 trailing", "0023-01-01"])
def test_profile_founded_date_uses_full_format_and_reviewed_year_range(day):
    rows, _ = ref.parse_cninfo(payload([profile() | {"F010D": day}]), "stock_profile_cninfo", {"symbol": "000001"})
    job = {"function": "stock_profile_cninfo", "parameters": {"symbol": "000001"}}
    with pytest.raises(DateContractError):
        require_dates(rows, job, "2026-10-29")
    with pytest.raises(ValueError):
        ref.validate(rows, job, "2026-10-29", numeric)


def test_failed_run_does_not_refresh_last_live_received_time():
    runs = [{"run_id": "one", "status": "received", "finished_at": "2026-10-28T00:00:00+00:00", "evidence": {"receipts": [{}]}},
            {"run_id": "two", "status": "failed", "finished_at": "2026-10-29T00:00:00+00:00", "evidence": {}}]
    assert acceptance_dimensions({"sample_runs": runs})["last_live_received_at"] == runs[0]["finished_at"]


def test_cninfo_chinext_board_label_is_bound_to_sz_growth_board_code():
    native = profile() | {"ASECCODE": "300001", "MARKET": "深交所创业板"}
    rows, _ = ref.parse_cninfo(payload([native]), "stock_profile_cninfo", {"symbol": "300001"})
    job = {"function": "stock_profile_cninfo", "parameters": {"symbol": "300001"}}
    assert not ref.validate(rows, job, "2026-10-29", numeric)["gaps"]
    native["ASECCODE"] = "000001"
    rows, _ = ref.parse_cninfo(payload([native]), "stock_profile_cninfo", {"symbol": "000001"})
    with pytest.raises(ValueError, match="market"):
        ref.validate(rows, {"function": "stock_profile_cninfo", "parameters": {"symbol": "000001"}}, "2026-10-29", numeric)


def test_unreceived_item_freshness_label_never_claims_snapshot_or_dates_received():
    for mode in ["snapshot", "calendar", "series"]:
        result = acceptance_dimensions({"mode": mode, "sample_runs": [{"run_id": "failed", "status": "blocked", "returned": 0}]})
        assert "已取" not in result["freshness_label"] and "已记录" not in result["freshness_label"]
        assert result["last_live_received_at"] is None and not result["technical_sample_verified"]


@pytest.mark.parametrize("first,last", [("VALUE_DATE", "EXPIRE_DATE"), ("TRANSFER_START_DATE", "TRANSFER_END_DATE"), ("IB_START_DATE", "IB_END_DATE")])
@pytest.mark.parametrize("missing_end", [False, True])
def test_bond_partial_term_is_annotated_without_blocking_known_issuance_fields(first, last, missing_end):
    row = bond() | {first: "2026-01-01", last: "2032-01-01"}
    row[last if missing_end else first] = None
    result = ref.validate([row], {"function": "bond_zh_cov", "parameters": {}}, "2026-10-29", numeric)
    assert not result["gaps"] and result["field_observations"][0]["fields"] == [first, last]


def test_valid_bond_identity_with_other_exchange_underlying_is_rejected():
    with pytest.raises(ValueError, match="underlying stock exchange"):
        ref.validate([bond() | {"CONVERT_STOCK_CODE": "000001"}], {"function": "bond_zh_cov", "parameters": {}}, "2026-10-29", numeric)


def test_st_list_all_pages_are_native_identity_snapshots_not_quote_or_effective_dates():
    from urllib.parse import parse_qs, urlsplit
    from types import SimpleNamespace
    from ashare_data.providers.free_update_reference import collect_st, validate_st
    source = [{'f12':f'600{i:03d}','f13':1,'f14':f'*ST样本{i}'} for i in range(101)]
    def get(url):
        query = parse_qs(urlsplit(url).query)
        assert query['fields'] == ['f12,f13,f14'] and query['fid'] == ['f12']
        page = int(query['pn'][0])
        return json.dumps({'rc':0,'data':{'total':101,'diff':source[(page-1)*100:page*100]}}).encode()
    c = SimpleNamespace(get=get)
    rows = collect_st(c, {})
    assert len(rows) == 101 and c.dataset_metadata['pages_received'] == 2
    assert c.dataset_metadata['source_effective_time'] is None
    assert validate_st(rows, {})['latest_period'] is None
    for bad in [rows+[rows[0]], [rows[0] | {'名称':'普通股'}], [rows[0] | {'security':'600000.SZ'}]]:
        with pytest.raises(ValueError):
            validate_st(bad,{})


def test_st_board_b_shares_are_preserved_without_expanding_rmb_financial_identity():
    from ashare_data.providers.free_update_reference import st_identity, stock_market, validate_st
    for code, market in [('900915','SH'), ('200012','SZ')]:
        assert st_identity(code) == (market, 'B')
        with pytest.raises(ValueError):
            stock_market(code)
        row = {'代码':code,'名称':'ST样本B','security':code+'.'+market,'share_class':'B',
            'source_native':{'f12':code,'f13':{'SH':1,'SZ':0}[market],'f14':'ST样本B'},'source_market_filter':'m:0 f:4,m:1 f:4'}
        assert validate_st([row],{})['share_classes'] == ['B']
        with pytest.raises(ValueError):
            validate_st([row | {'share_class':'A'}],{})


def test_st_native_zero_route_is_not_assumed_to_be_shenzhen_or_all_beijing():
    for code,market,cls in [('920001','BJ','A'),('873001','NATIVE_0','unresolved_BJ_or_NEEQ')]:
        assert ref.st_identity(code)==(market,cls)
        row={'代码':code,'名称':'ST样本','security':code+'.'+market,'share_class':cls,
            'source_native':{'f12':code,'f13':0,'f14':'ST样本'},'source_market_filter':'m:0 f:4,m:1 f:4'}
        assert not ref.validate_st([row],{})['gaps']
        with pytest.raises(ValueError):
            ref.validate_st([row|{'security':code+'.SZ'}],{})


def test_csi_current_snapshot_preserves_leading_codes_and_native_date(monkeypatch):
    pd = pytest.importorskip("pandas")
    from ashare_data.providers.free_update_semantics import validate_semantics
    headings = ["日期Date", "指数代码 Index Code", "指数名称 Index Name", "指数英文名称Index Name(Eng)",
        "成份券代码Constituent Code", "成份券名称Constituent Name", "成份券英文名称Constituent Name(Eng)",
        "交易所Exchange", "交易所英文名称Exchange(Eng)"]
    frame = pd.DataFrame([[20261028, 300, "样本指数", "Example", 1, "样本股", "Stock", "深圳证券交易所", "Shenzhen"]], columns=headings)
    monkeypatch.setattr(pd, "read_excel", lambda value: frame.copy())
    class Capture:
        def get(self, url):
            assert url.endswith("/000300cons.xls")
            return b"fixture workbook"
    capture = Capture()
    rows = ref.collect_index_components(capture, {"symbol": "000300"})
    assert rows[0]["指数代码"] == "000300" and rows[0]["成分券代码"] == "000001"
    assert rows[0]["日期"] == "2026-10-28T00:00:00.000"
    assert capture.dataset_metadata["complete_returned_window"]
    assert not capture.dataset_metadata["historical_api_required"]
    assert not validate_semantics(rows, {"function":"index_stock_cons_csindex", "parameters":{"symbol":"000300"}}, "2026-10-29")["gaps"]
    frame.columns = list(reversed(headings))
    with pytest.raises(ValueError, match="columns changed"):
        ref.collect_index_components(capture, {"symbol": "000300"})
    with pytest.raises(ValueError, match="index code"):
        ref.collect_index_components(capture, {"symbol": "../unreviewed"})


def test_bond_full_issuance_query_uses_source_order_and_retains_native_fields():
    row = bond() | {"UNKNOWN_TYPE": "native value"}
    class Capture:
        def get(self, url):
            q = parse_qs(urlsplit(url).query)
            assert q["sortColumns"] == ["PUBLIC_START_DATE"] and q["sortTypes"] == ["-1"]
            assert q["columns"] == ["ALL"] and "filter" not in q
            return json.dumps({"success": True, "result": {"pages": 1, "count": 1, "data": [row]}}).encode()
    assert ref.collect_bonds(Capture(), "bond_zh_cov", {}) == [row]


def test_per_bond_query_serialization_stays_separate_from_full_list_order():
    urls = []
    class Capture:
        def get(self, url):
            urls.append(url)
            return json.dumps({"success": True, "result": {"pages": 1, "count": 1, "data": [bond()]}}).encode()
    ref.collect_bonds(Capture(), "bond_zh_cov_info", {"symbol": "113001", "indicator": "基本信息"})
    assert urls[0].startswith("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_BOND_CB_LIST&columns=ALL&sortColumns=SECUCODE&sortTypes=1&pageNumber=1&pageSize=500&quoteColumns=")
    assert urls[0].endswith("&source=WEB&client=WEB&filter=%28SECURITY_CODE%3D%22113001%22%29")


def test_native_delisting_reference_is_not_completed_or_announced_delisting():
    row = bond() | {"DELIST_DATE": "2026-12-01"}
    result = require_dates([row], {"function": "bond_zh_cov"}, "2026-10-29")
    assert result["allowed_future_count"] == 2
    assert row["DELIST_DATE"] == "2026-12-01"
