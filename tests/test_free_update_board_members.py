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


def received_page(page, observed="2026-10-30T13:21:02+00:00"):
    import hashlib
    capture = Capture()
    capture.urls = ["previous"] * (page - 1)
    body = capture.get_curl(boards.request_url("BK1024", page))
    receipt = dict(url=boards.request_url("BK1024", page), status=200, protocol="http",
                   received_at=observed, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    return body, receipt


def test_independent_page_imports_keep_receipt_times_and_do_not_overwrite_each_other(tmp_path):
    from ashare_data.providers.free_update_manual import read_snapshot, query_snapshot
    args = received_page(1)
    first = boards.persist_received_page(tmp_path, NAME, "BK1024", 1, *args)
    tail = boards.persist_received_page(tmp_path, NAME, "BK1024", 2,
                                       *received_page(2, "2026-10-30T14:26:56+00:00"))
    repeat = boards.persist_received_page(tmp_path, NAME, "BK1024", 1, *args)
    assert repeat["checkpoint_reused"] and repeat["new_records"] == repeat["revised_records"] == 0
    assert first["records"] == 100 and tail["records"] == 2
    assert len(list((tmp_path/"manual-snapshots").glob("latest-*.json"))) == 2
    manifest, rows = read_snapshot(first["snapshot"])
    assert manifest["scope_id"] != read_snapshot(tail["snapshot"])[0]["scope_id"]
    assert manifest["coverage"]["source_total"] == 102
    assert manifest["coverage"]["complete_received_page"]
    assert not manifest["coverage"]["complete_named_board"]
    assert {r["采集时间"] for r in rows} == {args[1]["received_at"]}
    assert query_snapshot(first["snapshot"], limit=60)["next_offset"] == 60
    assert len(query_snapshot(first["snapshot"], offset=60, limit=60)["records"]) == 40
    assert "入选日期" not in rows[0]


@pytest.mark.parametrize("damage", ["hash", "page", "status", "naive_time", "future_time"])
def test_page_import_rejects_unproven_or_wrong_receipt(tmp_path, damage):
    body, receipt = received_page(1)
    if damage == "hash":
        body += b" "
    if damage == "page":
        receipt["url"] = boards.request_url("BK1024", 2)
    if damage == "status":
        receipt["status"] = 401
    if damage == "naive_time":
        receipt["received_at"] = "2026-10-30T13:21:02"
    if damage == "future_time":
        receipt["received_at"] = "2099-10-30T13:21:02+00:00"
    with pytest.raises(ValueError):
        boards.persist_received_page(tmp_path, NAME, "BK1024", 1, body, receipt)
    assert not (tmp_path/"manual-snapshots").exists()


def test_interrupted_page_import_can_resume_without_losing_other_page(tmp_path, monkeypatch):
    from ashare_data.providers import free_update_manual as manual
    first = boards.persist_received_page(tmp_path, NAME, "BK1024", 1, *received_page(1))
    write = manual.write_snapshot
    def fail(*args, **kwargs):
        raise OSError("simulated local write interruption")
    monkeypatch.setattr(manual, "write_snapshot", fail)
    with pytest.raises(OSError):
        boards.persist_received_page(tmp_path, NAME, "BK1024", 2, *received_page(2))
    assert len(manual.read_snapshot(first["snapshot"])[1]) == 100
    monkeypatch.setattr(manual, "write_snapshot", write)
    resumed = boards.persist_received_page(tmp_path, NAME, "BK1024", 2, *received_page(2))
    assert resumed["new_records"] == 2
    (tmp_path/"CURRENT").write_text("do not touch")
    with pytest.raises(ValueError, match="production root"):
        boards.persist_received_page(tmp_path, NAME, "BK1024", 2, *received_page(2))
