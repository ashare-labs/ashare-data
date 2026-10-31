"""Source pagination, identity, date echoes and optional-field preservation."""
from copy import deepcopy
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data.providers.free_updates.free_update_billboard import collect
from ashare_data.providers.free_updates.free_update_fx import COLUMNS, collect as collect_fx, parse
from ashare_data.providers.free_updates.free_update_manual import prepare
from ashare_data.providers.free_updates.free_update_semantics import validate_semantics


def institution(code="000001"):
    return {"SECUCODE": code+".SZ", "SECURITY_CODE": code, "TRADE_DATE": "2026-10-28", "EXPLANATION": "测试披露",
        "BUY_AMT": 100, "SELL_AMT": 40, "NET_BUY_AMT": 60, "ACCUM_AMOUNT": 1000, "CLOSE_PRICE": 10,
        "CHANGE_RATE": 3, "RATIO": 6, "TURNOVERRATE": 2, "FREECAP": 84.26, "D10_CLOSE_ADJCHRATE": 8}


def test_institution_native_pagination_does_not_repeat_first_request():
    pages = [[institution()], [institution("000002")]]
    calls = []

    class Capture:
        def get(self, url):
            page = int(parse_qs(urlsplit(url).query)["pageNumber"][0])
            calls.append(page)
            return json.dumps({"result": {"pages": 2, "count": 2, "data": pages[page-1]}})

    cap = Capture()
    job = {"function": "stock_lhb_jgmmtj_em", "parameters": {"start_date": "20261014", "end_date": "20261028"}}
    rows = collect(cap, job["function"], job["parameters"])
    semantic = validate_semantics(rows, job, "2026-10-29")
    assert calls == [1, 2] and rows == pages[0]+pages[1]
    assert semantic["units"]["FREECAP"] == "source_native_scale_unconfirmed" and not semantic["gaps"]
    assert rows[0]["D10_CLOSE_ADJCHRATE"] == 8
    wrong = deepcopy(rows)
    wrong[0]["SECUCODE"] = "000999.SZ"
    with pytest.raises(ValueError, match="identity"):
        validate_semantics(wrong, job, "2026-10-29")
    with pytest.raises(ValueError, match="duplicate"):
        validate_semantics([rows[0], rows[0]], job, "2026-10-29")


def test_broker_side_is_explicit_and_native_ratios_are_not_percent_scaled():
    row = {"SECUCODE": "000530.SZ", "SECURITY_CODE": "000530", "TRADE_DATE": "2026-10-28",
        "OPERATEDEPT_CODE": "1000", "OPERATEDEPT_NAME": "Test", "EXPLANATION": "榜单原因", "BUY": 100,
        "SELL": 40, "NET": 60, "ACCUM_AMOUNT": 1000, "ACCUM_VOLUME": 100, "CLOSE_PRICE": 10,
        "CHANGE_RATE": 3, "TOTAL_BUYRIO": .1, "TOTAL_SELLRIO": .04, "source_side": "BUY", "source_row_number": 1}
    job = {"function": "stock_lhb_stock_detail_em", "parameters": {"symbol": "000530", "date": "20261028", "flag": "买入"}}
    check = validate_semantics([row], job, "2026-10-29")
    assert check["units"]["TOTAL_BUYRIO"] == "fraction" and "source_side" in check["business_key"]
    with pytest.raises(ValueError, match="side"):
        validate_semantics([row | {"source_side": "SELL"}], job, "2026-10-29")


def test_anonymous_institution_seats_are_distinct_disclosed_rows_across_pages():
    original = [{"SECUCODE": "000559.SZ", "SECURITY_CODE": "000559", "TRADE_DATE": "2026-10-28",
        "OPERATEDEPT_CODE": "0", "OPERATEDEPT_NAME": "机构专用", "TRADE_ID": 100433884,
        "EXPLANATION": "同一上榜原因", "BUY": buy, "SELL": sell, "NET": buy-sell,
        "ACCUM_AMOUNT": 1000, "ACCUM_VOLUME": 100, "CLOSE_PRICE": 10,
        "CHANGE_RATE": 3, "TOTAL_BUYRIO": buy/1000, "TOTAL_SELLRIO": sell/1000}
        for buy, sell in [(90, 100), (25, 80), (7, 60)]]
    calls = []

    class Capture:
        def get(self, url):
            page = int(parse_qs(urlsplit(url).query)["pageNumber"][0])
            calls.append(page)
            return json.dumps({"result": {"pages": 2, "count": 3,
                                          "data": original[:2] if page == 1 else original[2:]}})

    job = {"function": "stock_lhb_stock_detail_em", "parameters": {"symbol": "000559", "date": "20261028", "flag": "卖出"}}
    rows = collect(Capture(), job["function"], job["parameters"])
    assert calls == [1, 2] and [r["source_row_number"] for r in rows] == [1, 2, 3]
    assert [{k: v for k, v in r.items() if k not in {"source_side", "source_row_number"}} for r in rows] == original
    semantics = validate_semantics(rows, job, "2026-10-29")
    assert semantics["unique_keys"] == 3 and semantics["units"]["TOTAL_SELLRIO"] == "fraction"
    assert semantics["units"]["SELL"] == "CNY" and semantics["units"]["ACCUM_VOLUME"] == "shares"
    with pytest.raises(ValueError, match="positions"):
        validate_semantics([rows[0], rows[0], rows[2]], job, "2026-10-29")


def html(day="2026-10-28", echo="2026-10-29"):
    header = "".join("<td>"+k+"</td>" for k in COLUMNS)
    values = [day, *(["100"]*25)]
    body = "".join("<td>"+k+"</td>" for k in values)
    return (f'<input name="startDate" value="2026-10-14"><input name="endDate" value="{echo}">'
            f'<input name="queryYN" value="true"><table><tr>{header}</tr><tr>{body}</tr></table>').encode()


@pytest.mark.parametrize("side,flag,missing", [("BUY", "买入", "SELL"), ("SELL", "卖出", "BUY")])
def test_single_side_disclosure_preserves_absent_opposite_amount(side, flag, missing):
    row = {"SECUCODE": "600785.SH", "SECURITY_CODE": "600785", "TRADE_DATE": "2026-10-28",
        "OPERATEDEPT_CODE": "0", "OPERATEDEPT_NAME": "机构专用", "EXPLANATION": "榜单原因",
        "BUY": 100, "SELL": 40, "NET": 100, "ACCUM_AMOUNT": 1000, "ACCUM_VOLUME": 100,
        "CLOSE_PRICE": 10, "CHANGE_RATE": 3, "TOTAL_BUYRIO": .1, "TOTAL_SELLRIO": .04,
        "source_side": side, "source_row_number": 1}
    row[missing] = None
    row["TOTAL_"+missing+"RIO"] = None
    original = deepcopy(row)
    job = {"function": "stock_lhb_stock_detail_em", "parameters": {"symbol": "600785", "date": "20261028", "flag": flag}}
    check = validate_semantics([row], job, "2026-10-29")
    assert row == original and check["quality_issues"][0]["missing_fields"] == [missing]
    assert check["quality_issues"][0]["net_identity_checked"] is False
    with pytest.raises(ValueError, match="amount missing"):
        validate_semantics([row | {side: None}], job, "2026-10-29")


def test_safe_bounded_post_checks_echo_and_quotation_directions():
    pytest.importorskip("bs4")
    params = {"start_date": "20261014", "end_date": "20261029"}

    class Capture:
        def post(self, url, body):
            assert body == {"startDate": "2026-10-14", "endDate": "2026-10-29", "queryYN": "true"}
            return html()

    cap = Capture()
    rows = collect_fx(cap, params)
    semantic = validate_semantics(rows, {"function": "safe_rmb_window", "parameters": params}, "2026-10-29")
    assert semantic["units"]["美元"] == "CNY per 100 foreign currency"
    assert semantic["units"]["韩元"] == "foreign currency per 100 CNY"
    assert cap.dataset_metadata["historical_excel_downloaded"] is False
    with pytest.raises(ValueError, match="echo"):
        parse(html(echo="2026-10-28"), params)
    with pytest.raises(ValueError, match="outside"):
        parse(html(day="2026-10-30"), params)
    with pytest.raises(ValueError, match="empty"):
        parse(html().split(b"<tr><td>2026-10-28")[0]+b"</table>", params)


def test_manual_requires_all_three_billboard_jobs_and_recent_fx_window(tmp_path):
    path = tmp_path / "profile.json"
    profile = {"schema": "ad.free-update-manual.v1", "root": str(tmp_path / "data"), "rows": [66, 67, 192],
               "context": {"start_date": "20261014", "date": "20261028", "stock": "000530"}}
    path.write_text(json.dumps(profile))
    _, planned, _ = prepare(path, as_of="2026-10-29")
    assert {i["excel_row"]: len(i["job_ids"]) for i in planned["items"]} == {66: 1, 67: 3, 192: 1}
    profile["lookback_days"] = 60
    path.write_text(json.dumps(profile))
    with pytest.raises(ValueError, match="31|window"):
        prepare(path, as_of="2026-10-29")
