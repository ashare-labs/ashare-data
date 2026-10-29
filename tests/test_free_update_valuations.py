from copy import deepcopy
import json

import pytest

from ashare_data.providers.free_update_valuations import UNITS, collect, validate
from ashare_data.providers.free_update_semantics import numeric, validate_semantics


def row():
    return dict.fromkeys(UNITS, None) | {"SECUCODE": "000001.SZ", "SECURITY_CODE": "000001",
        "SECURITY_NAME_ABBR": "Synthetic", "TRADE_DATE": "2026-10-28 00:00:00", "CLOSE_PRICE": 2,
        "TOTAL_SHARES": 1000, "FREE_SHARES_A": 800, "TOTAL_MARKET_CAP": 2000,
        "NOTLIMITED_MARKETCAP_A": 1600, "PE_TTM": -1.5}


JOB = {"function": "stock_value_em", "parameters": {"symbol": "000001"}}


def test_units_security_and_negative_ratio_preserved():
    result = validate([row()], JOB, "2026-10-29", numeric)
    assert result["contract_reviewed"] and not result["gaps"]
    assert result["units"]["FREE_SHARES_A"].startswith("shares")
    assert result["latest_announcement"] is None


@pytest.mark.parametrize("changes", [
    {"SECUCODE": "000002.SZ"}, {"SECURITY_CODE": "2"}, {"TRADE_DATE": "2026-13-01"},
    {"TRADE_DATE": "2027-01-01"}, {"FREE_SHARES_A": .8}, {"NOTLIMITED_MARKETCAP_A": .16},
    {"CLOSE_PRICE": "NaN"},
])
def test_security_date_and_scaled_unit_regressions(changes):
    with pytest.raises(ValueError):
        validate([row() | changes], JOB, "2026-10-29", numeric)


def test_counts_and_repeated_pages_are_checked():
    class Capture:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)
            return json.dumps({"success": True, "result": {"pages": 2, "count": 2, "data": [row()]}}).encode()
    with pytest.raises(ValueError, match="repeated page"):
        collect(Capture(), JOB["parameters"])
    with pytest.raises(ValueError, match="explicit six-digit"):
        collect(Capture(), {"symbol": '000001")'})
    with pytest.raises(ValueError, match="duplicate"):
        validate([row(), deepcopy(row())], JOB, "2026-10-29", numeric)


def test_gold_money_does_not_inherit_sina_ounce_unit():
    data = {"REPORT_DATE": "2026-09-01 00:00:00", "TIME": "2026年09月份",
            "GOLD_RESERVES": 1, "FOREX": 2, "GOLD_RESERVES_SAME": 3,
            "GOLD_RESERVES_SEQUENTIAL": 4, "FOREX_SAME": 5, "FOREX_SEQUENTIAL": 6}
    result = validate_semantics([data], {"function": "macro_china_fx_gold", "parameters": {}}, "2026-10-29")
    assert result["units"]["GOLD_RESERVES"] == result["units"]["FOREX"] == "100000000 USD"
    assert not result["gaps"]
