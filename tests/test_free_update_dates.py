"""Synthetic date-role regressions; no provider observations in test fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from ashare_data.providers.free_update_dates import DateContractError, require_dates
from ashare_data.providers.free_updates import plan
from ashare_data.storage.free_updates import collect, connect
from test_free_updates import sample


def job(name):
    return {"function": name, "parameters": {}}


@pytest.mark.parametrize("value", ["2026-11", "202611", "20261101", "2026年11月份", "2026-10-30T01:00:00+08:00"])
def test_future_actual_rejects_whole_batch_without_filtering(value):
    rows = [{"月份": "2026-09", "value": 1}, {"月份": value, "value": 2}]
    before = deepcopy(rows)
    with pytest.raises(DateContractError) as error:
        require_dates(rows, job("macro_stock_finance"), "2026-10-29")
    assert rows == before and error.value.date_audit["violations"][0]["row_index"] == 1


@pytest.mark.parametrize("value", ["2026-02-30", "2026-13-01", "202613", "20261099", "2026-10-01 arbitrary", "2026-10-01T29:00:00"])
def test_invalid_dates_never_pass_by_prefix(value):
    with pytest.raises(DateContractError):
        require_dates([{"日期": value}], job("anything"), "2026-10-29")


def test_native_forecast_year_marker_is_per_row_not_mode_of_all_rows():
    rows = [{"YEAR1": 2025, "YEAR_MARK1": "A", "YEAR2": 2026, "YEAR_MARK2": "E"},
            {"YEAR1": 2024, "YEAR_MARK1": "A", "YEAR2": 2027, "YEAR_MARK2": "E"}]
    result = require_dates(rows, job("stock_profit_forecast_em"), "2026-10-29")
    assert result["allowed_future_count"] == 2
    for mark in ("A", None, "forecast"):
        with pytest.raises(DateContractError):
            require_dates([{"YEAR1": 2027, "YEAR_MARK1": mark}], job("stock_profit_forecast_em"), "2026-10-29")


def test_future_schedule_is_not_actual_execution_or_generic_forecast_flag():
    result = require_dates([{"申购日期": "2026-10-30T00:00:00.000"}], job("bond_zh_cov"), "2026-10-29")
    assert result["allowed_future_count"] == 1
    assert result["allowed_future_dates"][0]["actual_observation"] is False
    with pytest.raises(DateContractError):
        require_dates([{"日期": "2027-01-01", "forecast": True}], job("bond_zh_cov"), "2026-10-29")
    with pytest.raises(DateContractError):
        require_dates([{"申购日期": "2026-10-30"}], job("unknown"), "2026-10-29")


def test_macro_scheduled_release_requires_native_no_actual_schema():
    name = "macro_china_gdp_yearly"
    row = {"日期": "2026-11-01", "indicator": name, "今值": None, "预测值": 1}
    assert require_dates([row], job(name), "2026-10-29")["allowed_future_count"] == 1
    for changes in ({"今值": 1}, {"indicator": "wrong"}):
        with pytest.raises(DateContractError):
            require_dates([row | changes], job(name), "2026-10-29")


def test_failed_collection_retains_original_result_and_raw_receipt(tmp_path, monkeypatch):
    root = tmp_path / "isolated"
    with connect(root) as db:
        _, result = sample(db, root)
        db.execute("DELETE FROM runs")
    result["records"] = [{"TRADE_DATE": "2026-10-20", "LPR1Y": 1}, {"TRADE_DATE": "2027-10-20", "LPR1Y": 2}]
    result["observation_as_of"] = "2026-10-29"
    def replay(args, **kwargs):
        Path(args[-1]).write_text(json.dumps(result))
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr("ashare_data.storage.free_updates.subprocess.run", replay)
    receipt = collect(root, plan(rows=[137], as_of="2026-10-29"), force=True)[0]
    assert receipt["status"] == "validation_failed"
    assert receipt["returned"] == receipt["inserted"] == 0
    assert receipt["original_record_count"] == 2
    assert json.loads((root / "runs" / receipt["original_result_file"]).read_text())["records"] == result["records"]
    assert (root / "raw" / (result["receipts"][0]["sha256"]+".gz")).is_file()
    with connect(root, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
