"""Synthetic native schemas: identity, unit, timestamp and pagination regressions."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from ashare_data.providers.free_updates import free_update_disclosures as disclosures
from ashare_data.providers.free_updates import free_update_research as research
from ashare_data.providers.free_updates import free_update_rates as rates
from ashare_data.providers.free_updates import free_update_oil as oil
from ashare_data.providers.free_updates.free_update_coverage import CapturedResponses
from ashare_data.providers.free_updates.free_update_semantics import numeric, validate_semantics
from ashare_data.providers.free_updates import plan
from ashare_data.storage.free_updates import collect as persist, connect, report


def forecast():
    return dict.fromkeys(research.UNITS, None) | {"SECUCODE": "000001.SZ", "SECURITY_CODE": "000001", "SECURITY_NAME_ABBR": "Synthetic",
        "RATING_ORG_NUM": 2, "RATING_BUY_NUM": 1, "YEAR1": 2025, "YEAR_MARK1": "A", "EPS1": -1,
        "YEAR2": 2026, "YEAR_MARK2": "E", "EPS2": 1, "YEAR3": 2027, "YEAR_MARK3": "E", "EPS3": 2,
        "YEAR4": 2028, "YEAR_MARK4": "E", "EPS4": None}


def test_forecast_native_a_e_null_and_negative_eps_preserved():
    row = forecast()
    value = research.validate([row], {"function": "stock_profit_forecast_em", "parameters": {}}, "2026-10-29", numeric)
    assert value["native_fiscal_year_roles"] == {"2025A": 1, "2026E": 1, "2027E": 1, "2028E": 1}
    assert value["missing_field_observations"]["EPS4"] == 1 and row["EPS1"] == -1
    assert row["RATING_ADD_NUM"] is None and not value["gaps"]


@pytest.mark.parametrize("change", [{"YEAR_MARK2": "A"}, {"YEAR3": 2026}, {"SECUCODE": "200001.SZ", "SECURITY_CODE": "200001"}, {"RATING_BUY_NUM": .1}, {"EPS2": "NaN"}, {"DEC_AIMPRICEMAX": 1, "DEC_AIMPRICEMIN": 2}])
def test_forecast_semantic_failures(change):
    with pytest.raises(ValueError):
        research.validate([forecast() | change], {"function": "stock_profit_forecast_em", "parameters": {}}, "2026-10-29", numeric)


def test_forecast_count_pages_and_security_uniqueness():
    class Capture:
        def get(self, url):
            assert parse_qs(urlsplit(url).query)["pageNumber"] == ["1"]
            assert parse_qs(urlsplit(url).query)["sortColumns"] == ["SECUCODE"]
            return json.dumps({"success": True, "result": {"pages": 1, "count": 2, "data": [forecast(), forecast()]}}).encode()
    with pytest.raises(ValueError, match="uniqueness"):
        research.collect(Capture(), {})


PARAMS = {"symbol": "000001", "market": "沪深京", "start_date": "20261015", "end_date": "20261029"}
STAMP = int(datetime.fromisoformat("2026-10-21T00:00:00+08:00").timestamp()*1000)


def announcement():
    return {"secCode": "000001", "secName": "Synthetic", "orgId": "synthetic-org", "announcementId": "123456",
        "announcementTitle": "Synthetic announcement", "announcementTime": STAMP, "adjunctUrl": "finalpage/2026-10-21/123456.PDF", "adjunctSize": 1}


class DisclosureCapture:
    def __init__(self, count=1, changes=None):
        self.count, self.changes, self.posts = count, changes or {}, []

    def get(self, url):
        assert url == disclosures.DIRECTORY
        return json.dumps({"stockList": [{"code": "000001", "category": "A股", "orgId": "synthetic-org", "zwjc": "Synthetic"}]}).encode()

    def post(self, url, parameters):
        self.posts.append(dict(parameters))
        assert url == disclosures.QUERY and parameters["stock"] == "000001,synthetic-org"
        page = int(parameters["pageNum"])
        count = min(30, max(0, self.count-(page-1)*30))
        rows = [announcement() | {"announcementId": str(123456+(page-1)*30+i)} | self.changes for i in range(count)]
        return json.dumps({"totalAnnouncement": self.count, "hasMore": page*30 < self.count, "totalpages": 0, "announcements": rows}).encode()


def test_announcements_native_epoch_org_count_and_real_page_boundaries():
    capture = DisclosureCapture(count=31)
    rows = disclosures.collect(capture, PARAMS)
    assert [p["pageNum"] for p in capture.posts] == ["1", "2"]
    result = disclosures.validate(rows, {"function": "stock_zh_a_disclosure_report_cninfo", "parameters": PARAMS}, "2026-10-29")
    assert result["unique_keys"] == 31 and not result["gaps"]
    assert rows[0]["announcement_date"] == "2026-10-21"
    assert rows[0]["source_time_precision"] == "source_calendar_date" and rows[0]["first_publication_at"] is None


@pytest.mark.parametrize("changes", [{"orgId": "wrong"}, {"secCode": "000002"}, {"announcementTime": STAMP/1000}, {"announcementTime": STAMP+100*86400000}])
def test_announcement_identity_epoch_units_and_window_rejected(changes):
    with pytest.raises(ValueError):
        disclosures.collect(DisclosureCapture(changes=changes), PARAMS)


def test_post_replay_binds_query_body_not_just_url(tmp_path):
    raw = b'{}'
    sha = hashlib.sha256(raw).hexdigest()
    (tmp_path/"raw").mkdir()
    (tmp_path/"raw"/(sha+".gz")).write_bytes(gzip.compress(raw))
    at = datetime(2026, 10, 29, tzinfo=timezone.utc)
    parameters = {"pageNum": "1"}
    receipt = {"url": disclosures.QUERY, "protocol": "http", "status": 200, "sha256": sha,
        "received_at": at.isoformat(), "request_method": "POST", "request_body_sha256": hashlib.sha256(urlencode(parameters).encode()).hexdigest()}
    replay = CapturedResponses(tmp_path, {"receipts": [receipt]}, at, at)
    with pytest.raises(ValueError, match="POST method/body"):
        replay.post(disclosures.QUERY, {"pageNum": "2"})


def test_verified_empty_announcements_checkpoint_without_sample_pass(tmp_path, monkeypatch):
    root = tmp_path / "isolated"
    connect(root).close()
    (root/"raw").mkdir()
    real = DisclosureCapture(count=0)
    receipts = []
    at = datetime.now(timezone.utc).isoformat()
    def captured(url, body, method, parameters=None):
        sha = hashlib.sha256(body).hexdigest()
        (root/"raw"/(sha+".gz")).write_bytes(gzip.compress(body))
        receipts.append({"url": url, "protocol": "http", "status": 200, "sha256": sha, "host": "www.cninfo.com.cn",
            "received_at": at, "request_method": method, "request_body_sha256": hashlib.sha256(urlencode(parameters).encode() if parameters else b"").hexdigest()})
        return body
    class Capture:
        def get(self, url):
            return captured(url, real.get(url), "GET")
        def post(self, url, parameters):
            return captured(url, real.post(url, parameters), "POST", parameters)
    capture = Capture()
    assert disclosures.collect(capture, PARAMS) == []
    result = {"status": "empty_source_window", "started_at": at, "finished_at": at, "records": [], "receipts": receipts, "coverage": capture.dataset_metadata}
    def replay(args, **kwargs):
        Path(args[-1]).write_text(json.dumps(result))
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr("ashare_data.storage.free_updates.subprocess.run", replay)
    planned = plan(rows=[111], context={"stock": "000001"}, as_of="2026-10-29")
    first = persist(root, planned)[0]
    assert first["status"] == "empty_source_window" and first["inserted"] == 0
    assert persist(root, planned)[0]["status"] == "already_checked_empty"
    status = next(i for i in report(root)["items"] if i["excel_row"] == 111)
    assert not status["implementation_verified"] and status["status"].startswith("无新公告")


def test_shibor_all_terms_bp_scale_and_missing_not_filled():
    rows = [{"日期": "2026-10-27", **{k: "1.01" if "定价" in k else "0" for k in rates.UNITS}},
            {"日期": "2026-10-28", **{k: "1.02" if "定价" in k else "1" for k in rates.UNITS}}]
    result = rates.validate(rows, {"function": "macro_china_shibor_all"}, "2026-10-29", numeric)
    assert not result["gaps"] and result["consecutive_day_scale_checks"] == 8
    rows[1]["O/N-涨跌幅"] = ".01"
    checked = rates.validate(rows, {"function": "macro_china_shibor_all"}, "2026-10-29", numeric)
    assert checked["scale_disagreements"] and not checked["gaps"]
    assert rows[1]["O/N-涨跌幅"] == ".01"


def test_oil_native_named_prices_not_column_position():
    row = {"DIM_ID": "synthetic-region", "CITYNAME": "Synthetic", "DIM_DATE": "2026-10-21 00:00:00", "调整日期": "2026-10-21"}
    for grade, price in [("0", 7), ("89", 8), ("92", 9), ("95", 10)]:
        row.update({"V"+grade: price, "QE"+grade: price-1, "ZDE"+grade: 1})
    # Intentionally reverse insertion order, including 92 and 95.
    row = dict(reversed(list(row.items())))
    result = oil.validate([row], {"function": "energy_oil_detail_latest"}, "2026-10-29", numeric)
    assert result["regional_price_change_checks"] == 4 and result["units"]["V95"] == "CNY/litre"
    row["V95"], row["V92"] = row["V92"], row["V95"]
    with pytest.raises(ValueError, match="reconcile"):
        oil.validate([row], {"function": "energy_oil_detail_latest"}, "2026-10-29", numeric)


def test_customs_monthly_vs_ytd_dollar_amounts_and_percent():
    from ashare_data.providers.free_updates.free_update_macro_contracts import CONTRACTS, customs_actuals
    fields = CONTRACTS["macro_china_hgjck"]
    row = customs_actuals([dict.fromkeys(fields, 1) | {"REPORT_DATE": "2026-09-01", "TIME": "2026年09月份"}])[0]
    result = validate_semantics([row], {"function": "macro_china_hgjck", "parameters": {}}, "2026-10-29")
    assert not result["gaps"] and fields["EXIT_BASE"] == "1000 USD, current month"
    assert fields["IMPORT_ACCUMULATE"] == "1000 USD, year-to-date" and fields["EXIT_BASE_SAME"] == "percent"
    assert row['TRADE_BALANCE_100M_USD'] == '0'


def test_customs_display_scaling_and_monthly_balance_do_not_use_cumulative_fields():
    from ashare_data.providers.free_updates.free_update_macro_contracts import CONTRACTS, customs_actuals
    native=dict.fromkeys(CONTRACTS['macro_china_hgjck'],1) | {
        'REPORT_DATE':'2026-09-01','TIME':'2026年09月份',
        'EXIT_BASE':'400000000.125','IMPORT_BASE':'300000000.025',
        'EXIT_ACCUMULATE':'9000000000','IMPORT_ACCUMULATE':'8000000000'}
    row=customs_actuals([native])[0]
    assert row['TRADE_BALANCE_100M_USD']=='1000.000001'
    assert row['EXIT_BASE']==native['EXIT_BASE']
    assert row['customs_raw_amount_unit']=='1000 USD'
    validate_semantics([row],{'function':'macro_china_hgjck','parameters':{}},'2026-10-29')
    for bad in [row|{'TRADE_BALANCE_100M_USD':'10000'},row|{'customs_raw_amount_unit':'100000000 USD'}]:
        with pytest.raises(ValueError,match='balance formula or native unit'):
            validate_semantics([bad],{'function':'macro_china_hgjck','parameters':{}},'2026-10-29')
    assert customs_actuals([native|{'IMPORT_BASE':None}])[0]['TRADE_BALANCE_100M_USD'] is None
    assert customs_actuals([native|{'EXIT_BASE':'0','IMPORT_BASE':'100000'}])[0]['TRADE_BALANCE_100M_USD']=='-1'


def test_retired_customs_snapshot_units_are_not_returned_as_current(monkeypatch):
    from ashare_data.providers.free_updates.free_update_macro_contracts import CONTRACTS, require_customs_units
    from ashare_data.providers.free_updates.free_update_manual import query_snapshot
    units = dict(CONTRACTS['macro_china_hgjck'])
    require_customs_units('macro_china_hgjck', units)
    for field in ['EXIT_BASE', 'IMPORT_BASE', 'EXIT_ACCUMULATE', 'IMPORT_ACCUMULATE']:
        wrong = units | {field: '100000000 USD'}
        monkeypatch.setattr('ashare_data.providers.free_updates.free_update_manual.read_snapshot',
                            lambda path: ({'function': 'macro_china_hgjck', 'semantics': {'units': wrong}}, []))
        with pytest.raises(ValueError, match='retired customs amount-unit'):
            query_snapshot('unused synthetic path')
    # The fix is deliberately scoped: FX reserves use hundred-million USD.
    require_customs_units('macro_china_fx_gold', {'FOREX': '100000000 USD'})


def test_spot_native_bad_year_never_disappears_as_sdk_dropna():
    from ashare_data.providers.free_updates.free_update_spot_contracts import collect_goods
    from ashare_data.providers.free_updates.free_update_dates import require_dates
    rows = [{"symbol": "PB", "opendate": "0026-02-04", "price": "1", "zde": "0", "zdf": "0"},
            {"symbol": "PB", "opendate": "2026-10-28", "price": "2", "zde": "1", "zdf": "100"}]
    class Capture:
        def get(self, url):
            return json.dumps({"result": {"status": {"code": 0}, "data": {"data": rows}}}).encode()
    capture = Capture()
    result = collect_goods(capture, {"symbol": "澳大利亚粉矿价格"})
    assert len(result) == 1 and result[0]["日期"] == "2026-10-28"
    assert capture.dataset_metadata["quarantined_rows"][0]["native"] == rows[0]
    assert capture.dataset_metadata["native_source_count"] == 2
    assert capture.dataset_metadata["quarantined_count"] == 1
    require_dates(result, {"function": "spot_goods"}, "2026-10-29")
