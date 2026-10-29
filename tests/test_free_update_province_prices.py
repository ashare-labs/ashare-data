import copy
import json
from types import SimpleNamespace

import pytest

from ashare_data.providers import free_update_province_prices as prices
from ashare_data.providers.free_update_semantics import validate_semantics

PAGE = '''<input name="csrfmiddlewaretoken" value="synthetic-form-token">
<h2>各省均价实时排行榜</h2><script>
let title = '全国瘦肉型猪出栏均价';
function getmapdata() {draw('元/公斤');} function drawline1() {}
function provincepricerank() { act: "provincerank"; data.regionlist; $("#reptable"); }
function showdayinds() {}
</script>'''
DATA = {"res": "1", "regionlist": [{"name": "北京市", "value": 12, "diff": "+0.10"},
                                    {"name": "新疆", "value": 9, "diff": "-0.20"}]}


def parsed(body=None, page=PAGE):
    capture = SimpleNamespace()
    rows = prices.parse(capture, page, json.dumps(DATA if body is None else body))
    return rows, capture.dataset_metadata


def check(rows):
    return validate_semantics(rows, {"function": prices.FUNCTION, "parameters": {}}, "2026-10-29")


def test_actual_rank_preserves_source_order_native_changes_and_missing_provinces():
    rows, meta = parsed()
    assert rows[0]["源展示序号"] == 1 and rows[0]["source_change_display"] == "+0.10"
    assert rows[0]["source_publication_date"] is None and rows[0]["source_change_unit"] is None
    assert check(rows)["units"]["价格"] == "CNY/kg"
    assert "天津市" in meta["missing_provinces"] and not meta["complete_national_coverage"]
    assert meta["complete_returned_window"] and not meta["complete_history"]


@pytest.mark.parametrize("change", ["map", "duplicate", "order", "negative", "region", "percent", "date"])
def test_source_or_projection_changes_cannot_silently_pass(change):
    data = copy.deepcopy(DATA)
    if change == "map":
        data = {"res": "1", "vlist": []}
    if change == "duplicate":
        data["regionlist"][1]["name"] = "北京市"
    if change == "order":
        data["regionlist"].reverse()
    if change == "negative":
        data["regionlist"][1]["value"] = -1
    if change == "region":
        data["regionlist"][1]["name"] = "市辖区"
    if change == "percent":
        data["regionlist"][1]["diff"] = "-0.20%"
    with pytest.raises(ValueError):
        rows, _ = parsed(data)
        if change == "date":
            rows[0]["source_publication_date"] = "2026-10-29"
        check(rows)


def test_unit_from_another_chart_or_missing_form_is_not_accepted():
    for page in [PAGE.replace("元/公斤", "元/头"), PAGE.replace('name="csrfmiddlewaretoken"', 'name="other"')]:
        with pytest.raises(ValueError):
            parsed(page=page)


def test_new_shfe_contracts_do_not_erase_tax_or_multiplier_differences():
    from ashare_data.providers.free_update_futures import PRODUCTS
    assert PRODUCTS["FU0"]["price_unit"] == "CNY/tonne excluding tax"
    assert PRODUCTS["OP0"]["tonnes_per_contract"] == 40
    assert PRODUCTS["AO0"]["tonnes_per_contract"] == 20
    assert PRODUCTS["BR0"]["tonnes_per_contract"] == 5
