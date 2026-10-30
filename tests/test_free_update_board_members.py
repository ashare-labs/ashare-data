import json
from copy import deepcopy

import pytest

from ashare_data.providers import free_update_board_members as boards
from ashare_data.providers.free_update_semantics import validate_semantics

NAME = "stock_board_concept_cons_em"
PARAMS = {"symbol": "BK1024"}


class Capture:
    def __init__(self, damage=None):
        self.urls, self.damage = [], damage

    def get_curl(self, url):
        self.urls.append(url)
        page = len(self.urls)
        values = [{"f12": str(600000+i), "f13": 1, "f14": "测试证券"} for i in range((page-1)*100, min(page*100, 102))]
        if page == 2 and self.damage == "duplicate":
            values[0]["f12"] = "600000"
        if self.damage == "unknown_market":
            values[0]["f13"] = 99
        if self.damage == "missing_name":
            values[0]["f14"] = ""
        if page == 1 and self.damage == "short_page":
            values.pop()
        return json.dumps({"rc": 0, "data": {"total": 103 if page == 2 and self.damage == "changing_total" else 102, "diff": values}}).encode()


def test_all_pages_are_joined_and_capture_time_is_not_an_entry_date():
    c = Capture()
    rows = boards.collect(c, NAME, PARAMS)
    check = validate_semantics(rows, {"function": NAME, "parameters": PARAMS}, "2026-10-30")
    assert len(rows) == 102 and len(c.urls) == 2
    assert "pn=2" in c.urls[1] and "fs=b%3ABK1024" in c.urls[1]
    assert c.dataset_metadata["source_total"] == 102 and c.dataset_metadata["effective_date"] is None
    assert check["business_key"][-1] == "_snapshot_date" and check["latest_period"] is None
    assert "入选日期" not in rows[0]


@pytest.mark.parametrize("damage", ["duplicate", "changing_total", "unknown_market", "missing_name", "short_page"])
def test_broken_pages_cannot_look_like_a_complete_member_list(damage):
    with pytest.raises(ValueError):
        boards.collect(Capture(damage), NAME, PARAMS)


def test_native_route_zero_does_not_become_a_guessed_shenzhen_identity():
    rows = boards.collect(Capture(), NAME, PARAMS)
    row = deepcopy(rows[0])
    row.update(证券代码="920770", 原生市场代码=0, 原生证券身份="eastmoney:0.920770")
    check = validate_semantics([row], {"function": NAME, "parameters": PARAMS}, "2026-10-30")
    assert check["contract_reviewed"] and not any(k in row for k in ["exchange", "交易所"])


def test_native_board_code_is_required():
    with pytest.raises(ValueError):
        boards.parameters(NAME, {"symbol": "绿色电力"})
