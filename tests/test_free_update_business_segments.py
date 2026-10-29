import json

import pytest

from ashare_data.providers import free_update_business_segments as segments
from ashare_data.providers.free_update_semantics import numeric


def native(**changes):
    return {
        "SECUCODE": "600001.SH",
        "SECURITY_CODE": "600001",
        "REPORT_DATE": "2026-06-30 00:00:00",
        "MAINOP_TYPE": "2",
        "ITEM_NAME": "产品甲",
        "MAIN_BUSINESS_INCOME": 100,
        "MBI_RATIO": 0.3,
        "MAIN_BUSINESS_COST": 60,
        "MBC_RATIO": 0.3,
        "MAIN_BUSINESS_RPOFIT": 40,
        "MBR_RATIO": 0.3,
        "GROSS_RPOFIT_RATIO": 0.4,
        "RANK": 1,
    } | changes


def check(source):
    rows = [segments.project(r, "600001.SH") for r in source]
    return segments.validate(
        rows,
        {"function": segments.FUNCTION, "parameters": {"symbol": "SH600001"}},
        "2026-10-29",
        numeric,
    )


def test_amount_unit_and_fraction_keep_original_scale_and_availability():
    class Capture:
        def get(self, url):
            assert url.endswith("?code=SH600001")
            return json.dumps({"zygcfx": [native()]}).encode()

    c = Capture()
    rows = segments.collect(c, {"symbol": "SH600001"})
    result = check([native()])
    assert rows[0]["收入比例"] == 0.3 and rows[0]["主营收入"] == 100
    assert (
        rows[0]["publication_at"] is None and c.dataset_metadata["source_publication_time"] is None
    )
    assert not result["gaps"] and result["units"]["主营收入"] == "CNY"


def test_missing_optional_industry_fields_stay_null_without_general_formula():
    row = native(MAIN_BUSINESS_COST=None, MAIN_BUSINESS_RPOFIT=None, GROSS_RPOFIT_RATIO=None)
    result = check([row])
    assert result["field_missing_counts"]["主营成本"] == 1 and not result["gaps"]
    assert segments.project(row, "600001.SH")["主营成本"] is None
    assert check([native(MAIN_BUSINESS_INCOME=None)])["gaps"]


@pytest.mark.parametrize("change", [{"MAIN_BUSINESS_RPOFIT": 50}, {"GROSS_RPOFIT_RATIO": 40}])
def test_disclosed_equation_or_percent_scale_conflict_is_a_gap(change):
    assert check([native(**change)])["gaps"]


@pytest.mark.parametrize(
    "change",
    [
        {"SECUCODE": "600002.SH"},
        {"MAINOP_TYPE": "9"},
        {"REPORT_DATE": "2027-06-30"},
        {"ITEM_NAME": ""},
    ],
)
def test_cross_issuer_classification_and_future_period_fail(change):
    with pytest.raises(ValueError):
        check([native(**change)])


def test_classifications_remain_separate_and_duplicate_keys_fail():
    assert check([native(), native(MAINOP_TYPE="3")])["unique_keys"] == 2
    with pytest.raises(ValueError):
        check([native(), native()])
