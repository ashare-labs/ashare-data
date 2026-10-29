"""Synthetic issuer-report fixtures with source-labelled monetary/ratio units."""
import json
from math import ceil
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data.providers import free_update_financial_indicators as financial
from ashare_data.providers.free_update_semantics import numeric

PARAMS = {"symbol": "600000.SH", "indicator": "按报告期"}
JOB = {"function": financial.FUNCTION, "parameters": PARAMS}


def report(sector="银行", day="2026-06-30"):
    return dict.fromkeys(financial.UNITS) | dict.fromkeys(financial.COMMON | financial.SECTORS[sector], 10) | {
        "SECUCODE": "600000.SH", "SECURITY_CODE": "600000", "CURRENCY": "CNY", "ORG_TYPE": sector,
        "REPORT_DATE": day, "REPORT_TYPE": "中报", "REPORT_YEAR": day[:4], "NOTICE_DATE": "2026-08-28", "UPDATE_DATE": "2026-08-28",
        "LTDRR": 1.1, "ROEJQ": 5.5, "PARENTNETPROFIT": -100, "SOURCE_EXTRA": "uninterpreted"} | ({"JZB": 60, "JZC": 100, "JZBJZC": 60} if sector == "证券" else {})


@pytest.mark.parametrize("sector", financial.SECTORS)
def test_sector_requirements_keep_native_ratios_percent_and_cny_distinct(sector):
    result = financial.validate([report(sector)], JOB, "2026-10-28", numeric)
    assert not result["gaps"] and result["units"]["LTDRR"] == "dimensionless_ratio"
    assert result["units"]["EPSJB"] == "CNY/share" and result["units"]["ROEJQ"] == "percent"
    assert result["units"]["PARENTNETPROFIT"] == "CNY" and result["uninterpreted_native_extra_fields"] == ["SOURCE_EXTRA"]
    assert result["latest_period"] == "2026-06-30" and result["latest_announcement"] == "2026-08-28"


@pytest.mark.parametrize("change", [{"SECUCODE": "000001.SZ"}, {"CURRENCY": "USD"}, {"REPORT_DATE": "2026-06-15"},
    {"REPORT_YEAR": "2025"}, {"REPORT_TYPE": "年报"}, {"NOTICE_DATE": "2026-05-30"}, {"NOTICE_DATE": "2026-11-01"}, {"NOTICE_DATE": "2026-08-28 junk"}])
def test_report_announcement_identity_and_currency_are_not_inferred(change):
    with pytest.raises(ValueError):
        financial.validate([report() | change], JOB, "2026-10-28", numeric)


def test_missing_latest_notice_or_sector_measure_remains_gap_and_nulls_not_zero():
    for field in ["NOTICE_DATE", "GROSSLOANS", "TOTALOPERATEREVE"]:
        assert financial.validate([report() | {field: None}], JOB, "2026-10-28", numeric)["gaps"]
    assert financial.validate([report() | {"ORG_TYPE": "unreviewed"}], JOB, "2026-10-28", numeric)["gaps"]
    old = report(day="2025-06-30") | {"NOTICE_DATE": None}
    result = financial.validate([old, report()], JOB, "2026-10-28", numeric)
    assert result["missing_announcement_rows"] == 1 and not result["gaps"]
    assert old["NOTICE_DATE"] is None


def test_financial_pages_use_issuer_filter_reconcile_counts_and_retain_native_extras():
    source = [report(day=f"{year}-06-30") for year in range(1900, 2027)]
    class Capture:
        def get(self, url):
            q = parse_qs(urlsplit(url).query)
            assert q["filter"] == ['(SECUCODE="600000.SH")'] and q["type"] == [financial.CONTRACT["report"]]
            page, size = int(q["p"][0]), int(q["ps"][0])
            return json.dumps({"success": True, "result": {"pages": ceil(len(source)/size), "count": len(source), "data": source[(page-1)*size:page*size]}}).encode()
    c = Capture()
    assert financial.collect(c, PARAMS) == source and c.dataset_metadata["pages_received"] == 2
    assert c.dataset_metadata["native_extra_fields_retained_without_unit_claim"] == ["SOURCE_EXTRA"]
    assert not c.dataset_metadata["complete_universe"]


@pytest.mark.parametrize("damage", ["rejected", "count", "repeat", "keys"])
def test_source_failure_count_changes_duplicate_pages_and_keys_rejected(damage):
    class Capture:
        def get(self, url):
            page = int(parse_qs(urlsplit(url).query)["p"][0])
            row = report(day="2026-06-30" if page == 1 or damage in {"repeat", "keys"} else "2025-06-30")
            if page == 2 and damage == "keys":
                row["PARENTNETPROFIT"] = 9
            return json.dumps({"success": damage != "rejected", "result": {"pages": 2, "count": 3 if damage == "count" and page == 2 else 2, "data": [row]}}).encode()
    with pytest.raises(ValueError):
        financial.collect(Capture(), PARAMS)


def test_quarter_only_and_index_identity_do_not_silently_enter_report_contract():
    for p in [PARAMS | {"indicator": "按单季度"}, PARAMS | {"symbol": "000001.SH"}, PARAMS | {"token": "x"}]:
        with pytest.raises(ValueError):
            financial.requested_identity(p)


def test_securities_net_capital_ratio_is_percent_and_disagreement_is_retained():
    good = report("证券")
    result = financial.validate([good], JOB, "2026-10-28", numeric)
    assert result["units"]["JZBJZC"] == "percent" and result["ratio_scale_checks"] == 1 and not result["gaps"]
    bad = good | {"JZBJZC": 0.6}
    result = financial.validate([bad], JOB, "2026-10-28", numeric)
    assert result["gaps"] and result["ratio_scale_disagreements"][0]["source_percent"] == "0.6" and bad["JZBJZC"] == 0.6


@pytest.mark.parametrize("field", ["ZYGPGMJZC", "ZYGDSYLZQJZB"])
def test_unproved_securities_panel_scale_is_not_inferred_from_missing_percent_label(field):
    result = financial.validate([report("证券") | {field: 12}], JOB, "2026-10-28", numeric)
    assert "unverified" in result["units"][field] and any(field in g for g in result["gaps"])


def test_insurer_accounting_applicability_is_explicit_issuer_period_and_not_forward_fill():
    job = {'function':financial.FUNCTION,'parameters':{'symbol':'601318.SH','indicator':'按报告期'}}
    identity = {'SECUCODE':'601318.SH','SECURITY_CODE':'601318'}
    half = report('保险') | identity
    quarter = report('保险', '2026-09-30') | identity | {'REPORT_TYPE':'三季报','NOTICE_DATE':'2026-10-27','SOLVENCY_AR':None,'COMPENSATE_EXPENSE':None}
    result = financial.validate([half, quarter], job, '2026-10-29', numeric)
    assert len(result['field_applicability']) == 2 and result['field_applicability'][1]['value_available']
    assert not any('必要指标缺失' in g for g in result['gaps']) and any('EARNED_PREMIUM' in g for g in result['gaps'])
    assert quarter['SOLVENCY_AR'] is None and quarter['COMPENSATE_EXPENSE'] is None
    assert any('半年/年度' in g for g in financial.validate([quarter], job, '2026-10-29', numeric)['gaps'])
    other = quarter | {'SECUCODE':'601628.SH','SECURITY_CODE':'601628'}
    result = financial.validate([other], job | {'parameters':job['parameters'] | {'symbol':'601628.SH'}}, '2026-10-29', numeric)
    assert not result['field_applicability'] and any('COMPENSATE_EXPENSE' in g for g in result['gaps'])


def test_citic_official_denominator_conflict_is_not_a_global_unit_mapping():
    row = report('证券') | {'SECUCODE':'600030.SH','SECURITY_CODE':'600030','ZYGDSYLZQJZB':327.98}
    job = JOB | {'parameters':PARAMS | {'symbol':'600030.SH'}}
    result = financial.validate([row], job, '2026-10-29', numeric)
    assert result['source_label_conflicts'][0]['matched_report_period'] == '2026-09-30'
    assert 'unverified' in result['units']['ZYGDSYLZQJZB'] and result['gaps']
