"""Synthetic native protocol and date-boundary regressions, never live proofs."""
import json
from types import SimpleNamespace

import pytest

from ashare_data.providers.free_updates import free_update_futures as futures
from ashare_data.providers.free_updates.free_update_semantics import numeric, validate_semantics
from ashare_data.providers.free_updates import plan


def native(day, **changes):
    return {"d": day, "o": "100", "h": "110", "l": "90", "c": "105", "v": "10", "p": "11", "s": "0.000"} | changes


def body(rows, symbol="V0"):
    return ("/*<script>location.href='//sina.com';</script>*/\nvar _"+symbol+"2021_08_17=("+json.dumps(rows)+");").encode()


def capture(rows):
    return SimpleNamespace(get=lambda url: body(rows))


def params(**changes):
    return {"symbol": "V0", "start_date": "20261014", "end_date": "20261028"} | changes


def test_native_named_fields_order_units_zero_settlement_and_identity():
    rows = futures.daily_rows(body([dict(reversed(list(native("2026-10-14").items())))]), "V0")
    result = futures.validate(rows, {"function": "futures_main_sina", "parameters": params()}, "2026-10-28", numeric)
    assert rows[0]["settlement_zero_placeholder"] and rows[0]["动态结算价"] == "0.000"
    assert result["units"]["开盘价"] == "CNY/tonne" and result["product_contract"]["tonnes_per_contract"] == 5
    with pytest.raises(ValueError, match="identity"):
        futures.validate([rows[0] | {"exchange": "SHFE"}], {"function": "futures_main_sina", "parameters": params()}, "2026-10-28", numeric)


@pytest.mark.parametrize("damage", ["extra", "duplicate", "date", "callback", "script"])
def test_native_schema_malformed_dates_duplicates_and_code_not_executed(damage):
    rows = [native("2026-10-14")]
    if damage == "extra":
        rows[0]["unknown"] = 1
    if damage == "duplicate":
        rows *= 2
    if damage == "date":
        rows[0]["d"] = "0026-10-14"
    content = body(rows)
    if damage == "callback":
        content = content.replace(b"_V0", b"_M0")
    if damage == "script":
        content += b" arbitrary_code();"
    with pytest.raises(ValueError):
        futures.daily_rows(content, "V0")


@pytest.mark.parametrize("change", [{"开盘价": "89"}, {"成交量": "0.5"}, {"持仓量": "-1"}, {"settlement_zero_placeholder": False}, {"日期": "2026-11-01"}])
def test_futures_ohlc_integer_contracts_settlement_and_future_guard(change):
    rows = futures.daily_rows(body([native("2026-10-14")]), "V0")
    with pytest.raises(ValueError):
        futures.validate([rows[0] | change], {"function": "futures_main_sina", "parameters": params()}, "2026-10-28", numeric)


def test_week_resample_expands_left_boundary_and_sums_whole_source_bucket():
    c = capture([native("2026-10-09"), native("2026-10-12", v="30"), native("2026-10-14", v="40"), native("2026-10-16", v="50")])
    p = params(period="W-FRI", end_date="20261016")
    rows = futures.collect(c, "futures_resample", p)
    assert c.dataset_metadata["effective_start_date"] == "2026-10-10"
    assert rows[0]["成交量"] == "120" and rows[0]["source_daily_dates"] == ["2026-10-12", "2026-10-14", "2026-10-16"]
    assert rows[0]["周期未结束"] and not rows[0]["source_left_boundary_truncated"]
    assert not futures.validate(rows, {"function": "futures_resample", "parameters": p}, "2026-10-16", numeric)["gaps"]
    with pytest.raises(ValueError, match="boundary"):
        futures.validate([rows[0] | {"周期未结束": False}], {"function": "futures_resample", "parameters": p}, "2026-10-16", numeric)


def test_month_uses_actual_last_date_and_flags_source_inception_partial_bucket():
    c = capture([native("2026-10-14"), native("2026-10-28")])
    p = params(period="ME")
    rows = futures.collect(c, "futures_resample", p)
    assert rows[0]["日期"] == "2026-10-28" and rows[0]["周期结束"] == "2026-10-31" and rows[0]["周期未结束"]
    assert futures.validate(rows, {"function": "futures_resample", "parameters": p}, "2026-10-28", numeric)["gaps"]
    assert not futures.validate(rows, {"function": "futures_resample", "parameters": p | {"symbol": "M0"}}, "2026-10-28", numeric)["contract_reviewed"]


def test_worker_rejects_native_future_actual_even_outside_requested_historical_window(tmp_path, monkeypatch):
    from ashare_data.providers.free_updates import free_update_worker as worker
    monkeypatch.setattr(worker.Capture, "get", lambda self, url: body([native("2026-10-14"), native("2027-01-01")]))
    job = plan(rows=[253], context={"future_main": "V0"}, as_of="2026-10-28")["jobs"][0]
    result = worker.run({"job": job, "raw_root": str(tmp_path/"raw"), "as_of": "2026-10-28"})
    assert result["status"] == "failed" and "future futures actual" in result["error"]


def test_phone_unit_identity_missing_months_and_previous_available_change():
    from ashare_data.providers.free_updates.free_update_industry_contracts import RATE_FIELDS
    base = {"INDICATOR_ID": "EMI00225823", "INDICATOR_NAME": "手机出货量:当月值", **dict.fromkeys(RATE_FIELDS, 1)}
    rows = [base | {"REPORT_DATE": "2026-06-01", "INDICATOR_VALUE": 1900}, base | {"REPORT_DATE": "2026-09-01", "INDICATOR_VALUE": 2000}]
    result = validate_semantics(rows, {"function": "macro_china_mobile_number", "parameters": {}}, "2026-10-28")
    assert result["unavailable_statistical_months"] == ["2026-07", "2026-08"]
    assert result["units"]["INDICATOR_VALUE"].startswith("10000 handsets")
    assert "not guaranteed" in result["rate_definitions"]["CHANGE_RATE"] and not result["gaps"]
    with pytest.raises(ValueError, match="identity"):
        validate_semantics([rows[0] | {"INDICATOR_NAME": "手机零售销量"}], {"function": "macro_china_mobile_number", "parameters": {}}, "2026-10-28")


def test_lpr_coverage_starts_at_each_nonnull_tenor_not_auxiliary_rate_start():
    rows = [{"TRADE_DATE": "1991-01-01", "RATE_1": 5}, {"TRADE_DATE": "2013-10-25", "LPR1Y": 4},
        {"TRADE_DATE": "2019-08-20", "LPR1Y": 3, "LPR5Y": 4}]
    job = {"function": "macro_china_lpr", "parameters": {}}
    result = validate_semantics(rows, job, "2026-10-28")
    assert result["field_value_coverage"]["LPR1Y"]["first_period"] == "2013-10-25"
    assert result["field_value_coverage"]["LPR5Y"]["first_period"] == "2019-08-20"
    assert result["field_value_coverage"]["RATE_1"]["role"].startswith("auxiliary")
    assert validate_semantics(rows[:2], job, "2026-10-28")["gaps"]


@pytest.mark.parametrize("first,second", [({"h": "95"}, {}), ({"v": "-5"}, {"v": "10"}), ({"v": "0.5"}, {"v": "0.5"})])
def test_aggregation_cannot_hide_invalid_individual_daily_prices_or_quantities(first, second):
    c = capture([native("2026-10-12", **first), native("2026-10-14", **second)])
    with pytest.raises(ValueError):
        futures.collect(c, "futures_resample", params(period="W-FRI"))


@pytest.mark.parametrize('symbol,exchange,unit', [('AU0','SHFE','CNY/gram'), ('IF0','CFFEX','index points'), ('TF0','CFFEX','CNY clean price per CNY 100 face value'), ('SC0','INE','CNY/barrel excluding tax'), ('SI0','GFEX','CNY/tonne')])
def test_product_specific_price_and_settlement_units_never_default_to_tonnes(symbol, exchange, unit):
    rows = futures.daily_rows(body([native('2026-10-14')], symbol), symbol)
    result = futures.validate(rows, {'function':'futures_main_sina','parameters':params(symbol=symbol)}, '2026-10-29', numeric)
    assert rows[0]['exchange'] == exchange and result['units']['开盘价'] == unit
    assert result['units']['动态结算价'].startswith(unit+';')
    assert 'source contract counts' in result['units']['成交量']


@pytest.mark.parametrize('symbol,key,multiplier,unit', [
    ('AL0','tonnes_per_contract',5,'CNY/tonne'), ('ZN0','tonnes_per_contract',5,'CNY/tonne'),
    ('PB0','tonnes_per_contract',5,'CNY/tonne'), ('NI0','tonnes_per_contract',1,'CNY/tonne'),
    ('SN0','tonnes_per_contract',1,'CNY/tonne'), ('AG0','kilograms_per_contract',15,'CNY/kilogram'),
    ('RB0','tonnes_per_contract',10,'CNY/tonne'), ('HC0','tonnes_per_contract',10,'CNY/tonne'),
])
def test_shfe_contract_size_does_not_scale_source_prices_or_contract_volume(symbol,key,multiplier,unit):
    rows = futures.daily_rows(body([native('2026-10-14')], symbol), symbol)
    result = futures.validate(rows, {'function':'futures_main_sina','parameters':params(symbol=symbol)}, '2026-10-29', numeric)
    assert result['product_contract'][key] == multiplier and result['units']['开盘价'] == unit
    assert rows[0]['开盘价'] == '100' and rows[0]['成交量'] == '10'
    assert 'CMES unverified' in rows[0]['continuous_definition']
