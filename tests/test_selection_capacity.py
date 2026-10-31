"""Full selections use actual row budgets, not a sample's security-count gate."""

import pytest

from ashare_data import DataError
from ashare_data.factors.evaluation import EvaluationConfig
from ashare_data.product import LocalDailyProduct
from ashare_data.product.daily import _selection
from tests import test_local_daily_product as daily_fixtures

library = daily_fixtures.library


@pytest.mark.parametrize("count", [101, 300, 5000])
def test_complete_selection_returns_every_registered_security(library, count):
    import sqlite3

    root, days = library
    ids = [f"{600000 + i}.XSHG" for i in range(count)]
    with sqlite3.connect(root / "daily.sqlite3") as db:
        for sid in ids:
            native = "sh" + sid[:6]
            db.execute("INSERT INTO listing_facts(id,symbol,ipo,body,observed_at) "
                       "VALUES(?,?,'2000-01-01','{}','2026-01-01')", (sid, native))
            db.execute("INSERT OR REPLACE INTO bars SELECT ?,date,source,open,high,low,close,volume,amount,"
                       "preclose,tradestatus,is_st,row_hash,response_id,observed_at FROM bars "
                       "WHERE symbol='sz000858' AND date=? AND source='baostock'", (native, days[0]))
    before = (root / "daily.sqlite3").stat().st_mtime_ns
    response = LocalDailyProduct(root).prices(ids, days[0], days[0], source="baostock")
    assert {row["security"] for row in response["rows"]} == set(ids)
    assert len(response["rows"]) == count and not response["gaps"]
    assert (root / "daily.sqlite3").stat().st_mtime_ns == before


def test_removed_count_gate_keeps_selection_and_explicit_missing_coverage(library):
    for ids in ([], ["600000.XSHG", "600000.XSHG"], ["invalid"]):
        with pytest.raises(DataError):
            _selection(ids, "2010-01-01", "2025-01-01", "auto")
    ids = [f"{600000+i}.XSHG" for i in range(5000)]
    result = LocalDailyProduct(library[0]).prices(ids, library[1][0], library[1][-1])
    assert result["securities"] == ids
    assert {row["security"] for row in result["coverage"]} == set(ids)
    assert len(result["coverage"]) == len(ids)
    assert sum(row["returned_days"] for row in result["coverage"]) == len(result["rows"])
    assert result["rows"] and result["gaps"]
    assert {row["security"] for row in result["rows"]} <= set(ids)


def test_evaluation_minimum_has_no_invented_upper_count():
    assert EvaluationConfig(input_id="test", universe_label="explicit", price_basis="raw",
                            min_assets=100001).min_assets == 100001
    for value in (0, 1, True, 2.5):
        with pytest.raises(ValueError):
            EvaluationConfig(input_id="test", universe_label="explicit", price_basis="raw", min_assets=value)
