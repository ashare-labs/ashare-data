import json
from copy import deepcopy
import pytest
from ashare_data.providers.free_updates import free_update_board_bars as boards
from ashare_data.providers.free_updates.free_update_semantics import numeric

NAME = "stock_board_concept_hist_em"
PARAMS = {"symbol": "BK1234", "start_date": "20261027", "end_date": "20261030"}
BODY = {
    "rc": 0,
    "data": {
        "code": "BK1234",
        "market": 90,
        "name": "Synthetic board",
        "dktotal": 100,
        "klines": ["2026-10-30,10,11,12,9,100,200,30,10,1,2"],
    },
}


class Capture:
    def __init__(self, body):
        self.body = body
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return json.dumps(self.body).encode()


def test_resolved_code_goes_straight_to_original_quote_endpoint():
    c = Capture(BODY)
    rows = boards.collect(c, NAME, PARAMS)
    assert len(c.urls) == 1 and "91.push2his.eastmoney.com" in c.urls[0]
    assert "secid=90.BK1234" in c.urls[0] and "fqt=0" in c.urls[0]
    assert "/clist/" not in c.urls[0]
    assert (
        c.dataset_metadata["complete_returned_window"]
        and not c.dataset_metadata["complete_history"]
    )
    result = boards.validate(rows, {"function": NAME, "parameters": PARAMS}, "2026-10-30", numeric)
    assert result["latest_period"] == "2026-10-30" and not result["gaps"]
    assert rows[0]["成交量"] == "100"  # No inferred volume scale.


@pytest.mark.parametrize("damage", ["code", "market", "date", "width", "duplicate", "ohlc"])
def test_identity_date_and_numeric_corruption_are_not_accepted(damage):
    b = deepcopy(BODY)
    if damage == "code":
        b["data"]["code"] = "BK9999"
    elif damage == "market":
        b["data"]["market"] = 1
    elif damage == "date":
        b["data"]["klines"][0] = b["data"]["klines"][0].replace("2026-10-30", "2027-10-30")
    elif damage == "width":
        b["data"]["klines"][0] += ",999"
    elif damage == "duplicate":
        b["data"]["klines"] *= 2
    elif damage == "ohlc":
        b["data"]["klines"][0] = b["data"]["klines"][0].replace(",12,9,", ",8,9,")
    with pytest.raises(ValueError):
        rows = boards.collect(Capture(b), NAME, PARAMS)
        boards.validate(rows, {"function": NAME, "parameters": PARAMS}, "2026-10-30", numeric)


def test_name_cannot_reintroduce_a_directory_dependency():
    with pytest.raises(ValueError, match="BK board code"):
        boards.request_url(NAME, PARAMS | {"symbol": "Synthetic board"})
    assert "https://7.push2his.eastmoney.com" in boards.request_url(
        "stock_board_industry_hist_em", PARAMS
    )
