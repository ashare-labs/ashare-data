"""Static THS files must preserve native identity, daily keys and raw units."""
import json
from copy import deepcopy

import pytest

from ashare_data.providers.free_updates import free_update_ths_bars as ths
from ashare_data.providers.free_updates.free_update_semantics import validate_semantics

NAME = "stock_board_concept_index_ths"
PARAMS = {"symbol": "886078", "start_date": "20261029", "end_date": "20261030"}
ANNUAL = {"data": "20261028,10,12,9,11,100,200,,,,0;20261029,11,13,10,12,110,220,,,,0"}
TODAY = {"bk_886078": {"1": "20261030", "7": "12", "8": "14", "9": "11", "11": "13", "13": 120, "19": "240", "name": "测试概念", "dt": "2104"}}


class Capture:
    def __init__(self, annual=None, today=None):
        self.annual, self.today = deepcopy(annual or ANNUAL), deepcopy(today or TODAY)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        period = "today" if url.endswith("today.js") else "2026"
        data = self.today if period == "today" else self.annual
        return (f"quotebridge_v4_line_bk_886078_01_{period}(" + json.dumps(data) + ");").encode()


def checked(capture):
    rows = ths.collect(capture, NAME, PARAMS)
    return rows, validate_semantics(rows, {"function": NAME, "parameters": PARAMS}, "2026-10-30")


def test_static_current_quote_extends_annual_window_without_directory_or_tokens():
    c = Capture()
    rows, result = checked(c)
    assert [r["日期"] for r in rows] == ["2026-10-29", "2026-10-30"]
    assert all(u.startswith("https://d.10jqka.com.cn/v4/line/bk_886078/01/") for u in c.urls)
    assert rows[-1]["成交量"] == 120 and rows[-1]["成交额"] == "240"
    assert rows[-1]["native_extra_fields"]["dt"] == "2104"
    assert c.dataset_metadata["annual_rows_received"] == 2
    assert result["business_key"] == ["板块类型", "板块代码", "日期"]
    assert result["latest_period"] == "2026-10-30"


def test_current_file_revises_annual_same_date_without_duplicate_daily_key():
    c = Capture()
    c.annual["data"] += ";20261030,12,13,11,12,115,230,,,,0"
    rows, result = checked(c)
    assert len(rows) == 2 and result["unique_keys"] == 2
    assert rows[-1]["收盘"] == "13" and rows[-1]["source_file"] == "today"


def test_new_year_holiday_uses_latest_published_year_without_inventing_a_new_bar():
    c = Capture()
    c.annual["data"] = "20261230,10,12,9,11,100,200,,,,0"
    c.today["bk_886078"]["1"] = "20261231"
    params = PARAMS | {"start_date": "20261228", "end_date": "20270101"}
    rows = ths.collect(c, NAME, params)
    result = validate_semantics(rows, {"function": NAME, "parameters": params}, "2027-01-01")
    assert result["latest_period"] == "2026-12-31"
    assert [r["日期"] for r in rows] == ["2026-12-30", "2026-12-31"]
    assert not any(u.endswith("2027.js") for u in c.urls)


@pytest.mark.parametrize("damage", ["code", "future", "year", "duplicate", "width", "ohlc", "nonfinite", "negative", "missing"])
def test_incorrect_identity_dates_or_values_are_rejected(damage):
    c = Capture()
    q = c.today["bk_886078"]
    if damage == "code":
        c.today["bk_999999"] = c.today.pop("bk_886078")
    elif damage == "future":
        q["1"] = "20261031"
    elif damage == "year":
        c.annual["data"] = c.annual["data"].replace("20261028", "20271028")
    elif damage == "duplicate":
        c.annual["data"] += ";" + c.annual["data"].split(";")[0]
    elif damage == "width":
        c.annual["data"] += ",extra"
    elif damage == "ohlc":
        q["8"] = "10"
    elif damage == "nonfinite":
        q["13"] = "Infinity"
    elif damage == "negative":
        q["19"] = "-1"
    else:
        del q["11"]
    with pytest.raises(ValueError):
        checked(c)


def test_jsonp_is_not_executed_and_callback_must_match():
    for body in (b"evil({})", b"quotebridge_v4_line_bk_999999_01_today({})", b"quotebridge_v4_line_bk_886078_01_today({});alert(1)"):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            ths.decode(body, "886078", "today")


def test_unresolved_name_and_historical_asof_cannot_mislabel_current_file():
    with pytest.raises(ValueError, match="native six-digit"):
        ths.parameters(NAME, PARAMS | {"symbol": "测试概念"})
    rows, _ = checked(Capture())
    with pytest.raises(ValueError, match="current as-of"):
        validate_semantics(rows, {"function": NAME, "parameters": PARAMS}, "2026-10-31")


def test_next_day_window_reuses_index_checkpoint_and_recovers_interrupted_write(tmp_path, monkeypatch):
    from ashare_data.providers.free_updates import free_update_manual as manual
    rows, semantics = checked(Capture())
    old = {"function": NAME, "parameters": PARAMS | {"start_date": "20261028", "end_date": "20261029"}, "job_id": "seed"}
    new = {"function": NAME, "parameters": PARAMS, "job_id": "delta"}
    proof = {"run_id": "seed", "function": NAME, "records": 1, "semantics": semantics,
             "coverage": {"complete_returned_window": True}, "observed_at": "2026-10-30T00:00:00+00:00"}
    first = manual.write_snapshot(tmp_path, 73, old, rows[:1], proof)
    pointer = next((tmp_path / "manual-snapshots").glob("latest-*.json"))
    saved = pointer.read_bytes()
    replace = manual.os.replace
    def interrupted(source, target):
        if target == pointer:
            raise OSError("injected interruption")
        return replace(source, target)
    monkeypatch.setattr(manual.os, "replace", interrupted)
    with pytest.raises(OSError, match="injected interruption"):
        manual.write_snapshot(tmp_path, 73, new, rows, proof | {"run_id": "delta", "records": 2})
    assert pointer.read_bytes() == saved
    assert manual.query_snapshot(first["snapshot"])["total"] == 1
    monkeypatch.setattr(manual.os, "replace", replace)
    second = manual.write_snapshot(tmp_path, 73, new, rows, proof | {"run_id": "delta", "records": 2})
    assert second["new_records"] == 1 and second["revised_records"] == 0
    assert manual.read_snapshot(second["snapshot"])[0]["previous_snapshot"] == first["snapshot_id"]
    repeat = manual.write_snapshot(tmp_path, 73, new, rows, proof | {"run_id": "delta", "records": 2})
    assert repeat["new_records"] == 0 and repeat["checkpoint_reused"]
