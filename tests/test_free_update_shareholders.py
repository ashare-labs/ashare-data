"""Synthetic protocol, identity and revision tests; never live-source evidence."""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data.providers.free_update_shareholders import collect, validate
from ashare_data.providers.free_updates import encoded, plan
from ashare_data.storage.free_updates import collect as persist, connect, query


def holding(**changes):
    return dict({"SECUCODE": "600000.SH", "SECURITY_CODE": "600000", "END_DATE": "2026-06-30 00:00:00",
        "NOTICE_DATE": "2026-08-28 00:00:00", "UPDATE_DATE": "2026-09-01 00:00:00",
        "HOLDER_CODE": "H001", "HOLDER_NAME": "Synthetic holder", "SHARES_TYPE": "A股",
        "HOLD_NUM": 100, "HOLD_RATIO": 25, "FREE_HOLDNUM_RATIO": 30, "RANK": 1, "HOLDER_RANK": 1,
        "HOLD_NUM_CHANGE": "不变", "报告期": "2026-06-30", "公告日期": "2026-08-28"}, **changes)


def job(name="stock_gdfx_top_10_em"):
    return {"function": name, "parameters": {"symbol": "sh600000", "date": "20260630"}}


@pytest.mark.parametrize("name,ratio", [("stock_gdfx_top_10_em", "HOLD_RATIO"),
                                       ("stock_gdfx_free_top_10_em", "FREE_HOLDNUM_RATIO")])
def test_announcement_is_not_source_update_or_report_period(name, ratio):
    checked = validate([holding()], job(name), "2026-10-29")
    assert checked["units"] == {"HOLD_NUM": "shares", ratio: "percent"}
    assert checked["latest_period"] == "2026-06-30"
    assert checked["latest_announcement"] == "2026-08-28"
    assert checked["latest_source_update"] == "2026-09-01"
    assert not checked["intraday_publication_verified"] and not checked["gaps"]
    missing = validate([holding(NOTICE_DATE=None, 公告日期=None)], job(name), "2026-10-29")
    assert missing["gaps"] and missing["latest_announcement"] is None


@pytest.mark.parametrize("changes", [
    {"SECUCODE": "600000.SZ"}, {"SECURITY_CODE": "600001"}, {"END_DATE": "2026-03-31"},
    {"HOLD_NUM": 1.1}, {"HOLD_RATIO": 101}, {"RANK": 11},
    {"NOTICE_DATE": "2026-06-01", "公告日期": "2026-06-01"},
    {"NOTICE_DATE": "2026-11-01", "公告日期": "2026-11-01"},
    {"NOTICE_DATE": "invalid"}, {"公告日期": "2026-09-01"}, {"HOLDER_NAME": None},
])
def test_identity_units_and_dates_fail_closed(changes):
    with pytest.raises(ValueError):
        validate([holding(**changes)], job(), "2026-10-29")


def test_all_pages_and_source_totals_are_checked():
    class Capture:
        repeated = False
        changed = False
        def get(self, url):
            params = parse_qs(urlsplit(url).query)
            assert params["reportName"] == ["RPT_DMSK_HOLDERS"]
            assert params["filter"] == ['(SECURITY_CODE="600000")(END_DATE=\'2026-06-30\')']
            page = int(params["pageNumber"][0])
            row = holding(HOLDER_NAME="Same holder" if self.repeated else f"Synthetic holder {page}")
            return encoded({"success": True, "result": {"count": 3 if self.changed and page == 2 else 2,
                "pages": 2, "data": [row]}})
    capture = Capture()
    rows = collect(capture, job()["function"], job()["parameters"])
    assert len(rows) == capture.dataset_metadata["source_count"] == 2
    assert capture.dataset_metadata["complete_requested_issuer_period"]
    assert not capture.dataset_metadata["complete_universe"]
    capture.repeated = True
    with pytest.raises(ValueError, match="duplicate"):
        collect(capture, job()["function"], job()["parameters"])
    capture.repeated, capture.changed = False, True
    with pytest.raises(ValueError, match="totals changed"):
        collect(capture, job()["function"], job()["parameters"])


def test_missing_person_code_is_preserved_and_name_collision_rejected():
    row = holding(HOLDER_CODE=None)
    assert not validate([row],job(),"2026-10-29")["gaps"]
    with pytest.raises(ValueError,match="duplicate"):
        validate([row,holding(HOLDER_CODE="different")],job(),"2026-10-29")


def test_revision_and_reversion_keep_received_by_history(tmp_path, monkeypatch):
    prepared = plan(rows=[23], context={"stock_prefixed": "sh600000", "report_date": "20260630"}, as_of="2026-10-29")
    connect(tmp_path).close()
    (tmp_path/"raw").mkdir()
    records = [holding()]
    result = {}
    def response(day, shares):
        records[0]["HOLD_NUM"] = shares
        body = encoded(records)
        sha = hashlib.sha256(body).hexdigest()
        (tmp_path/"raw"/(sha+".gz")).write_bytes(gzip.compress(body, mtime=0))
        result.update(status="received", records=deepcopy(records), finished_at=day,
            observation_as_of="2026-10-29", receipts=[{"sha256": sha, "status": 200}],
            implementation_sha256=prepared["jobs"][0]["implementation_sha256"])
    def replay(args, **kwargs):
        Path(args[-1]).write_bytes(encoded(result))
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr("ashare_data.storage.free_updates.subprocess.run", replay)
    for day, shares, expected in [("2026-10-29T00:00:00+00:00",100,1),
                                  ("2026-10-29T01:00:00+00:00",120,1),
                                  ("2026-10-29T02:00:00+00:00",100,0)]:
        response(day, shares)
        run = persist(tmp_path, prepared, force=True)[0]
        assert run["status"] == "received" and run["inserted"] == expected
    assert len(query(tmp_path,23,as_of="2026-10-29T00:30:00+00:00")) == 1
    assert len(query(tmp_path,23,as_of="2026-10-29T01:30:00+00:00")) == 2
    with connect(tmp_path,readonly=True) as db:
        assert db.execute("SELECT count(*) FROM run_records").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
        assert all(json.loads(r[0])["公告日期"] == "2026-08-28" for r in db.execute("SELECT payload_json FROM observations"))
