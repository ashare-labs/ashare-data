"""Calendar append uses frozen source agreement and rejects unrelated metadata changes."""
from copy import deepcopy
from datetime import date
import gzip
import hashlib
import shutil
import sqlite3

import pytest

from ashare_data.core.model import DataError
from ashare_data.providers import daily_sources
from ashare_data.storage import calendar_release as calendar
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, canonical
from test_financial_release import market
from test_unit_publication import published_base, publish


def calendar_parent(tmp_path, monkeypatch, parent=None):
    parent = parent or market(tmp_path / "market")
    source_days = [f"2026-10-{d:02}" for d in range(8, 32) if date(2026, 10, d).weekday() < 5]
    monkeypatch.setattr(daily_sources, "decode_sina", lambda raw, kind: source_days)
    raw = b"frozen fixture calendar"
    sina = tmp_path / "sina.gz"
    sina.write_bytes(gzip.compress(raw, mtime=0))
    root = tmp_path / "official"
    (root / "raw").mkdir(parents=True)
    (root / "receipts").mkdir()
    body = canonical({"data": [{"jyrq": f"2026-10-{d:02}",
             "jybz": "1" if f"2026-10-{d:02}" in source_days else "0"} for d in range(1, 32)]})
    official = root / "raw/month.json.gz"
    official.write_bytes(gzip.compress(body, mtime=0))
    ref = calendar.reference(official)
    ref["path"] = "raw/month.json.gz"
    receipt = {"raw": ref, "observed_at": "2026-10-19T10:32:38+00:00",
               "attempts": [{"response_sha256": hashlib.sha256(body).hexdigest(), "http_status": 200,
                 "url": "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=2026-10"}]}
    (root / "receipts/szse_calendar-2026-10.json").write_bytes(canonical(receipt))
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        metadata = parent.root / "work/calendar-base.sqlite3"
        shutil.copyfile(parent.metadata_path, metadata)
        with sqlite3.connect(metadata) as db:
            db.executescript("""CREATE TABLE calendar(date TEXT PRIMARY KEY,source TEXT,receipt TEXT);
                CREATE TABLE responses(id TEXT PRIMARY KEY,url TEXT,started_at TEXT,received_at TEXT,
                    sha256 TEXT,status INTEGER,error TEXT,bytes INTEGER);
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO calendar VALUES('2026-10-14','sina_trading_dates_sh','fixture-sina');""")
            db.execute("INSERT INTO responses VALUES(?,?,?,?,?,?,?,?)", (
                "fixture-sina", "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt", None,
                "2026-10-14T11:20:53+00:00", hashlib.sha256(raw).hexdigest(), 200, None, len(raw)))
        value = deepcopy(parent.manifest)
        value["metadata"] = writer.object(metadata, "sqlite3")
        writer.publish(value, expected_parent=parent.release_id, verify_unchanged=False)
    return MarketRelease(parent.root), dict(end="2026-10-20", sina_raw=sina,
                                           sina_response_id="fixture-sina", szse_root=root)


def test_calendar_and_units_publish_together_without_rewriting_facts(tmp_path, monkeypatch):
    parent, units, _ = published_base(tmp_path)
    parent, args = calendar_parent(tmp_path, monkeypatch, parent)
    output = tmp_path / "prepared-calendar"
    plan = calendar.prepare(parent, output, **args)
    assert [x["date"] for x in plan["added_sessions"]] == [
        "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]
    assert calendar.prepare(parent, output, **args) == plan
    assert plan["new_network_requests"] == 0
    assert plan["historical_pit_certified"] is False
    assert publish(parent, units, calendar_plan=output / "plan.json")["calendar_sessions_added"] == 4
    current = MarketRelease(parent.root)
    assert current.manifest["files"] == parent.manifest["files"]
    with current.metadata() as db:
        assert tuple(db.execute("SELECT max(date),count(*) FROM calendar").fetchone()) == ("2026-10-20", 5)
    with parent.metadata() as db:
        assert tuple(db.execute("SELECT max(date),count(*) FROM calendar").fetchone()) == ("2026-10-14", 1)
    assert publish(parent, units, calendar_plan=output / "plan.json")["already_applied"]


def test_calendar_rejects_forged_metadata_even_with_updated_checksum(tmp_path, monkeypatch):
    parent, args = calendar_parent(tmp_path, monkeypatch)
    output = tmp_path / "prepared-calendar"
    plan = calendar.prepare(parent, output, **args)
    with sqlite3.connect(plan["metadata"]["path"]) as db:
        db.execute("INSERT INTO metadata VALUES('unrelated','bad')")
    plan["metadata"] = calendar.reference(plan["metadata"]["path"])
    (output / "plan.json").write_bytes(canonical(plan))
    with pytest.raises(DataError, match="Unexpected new metadata rows"):
        calendar.verify_plan(parent, output / "plan.json")
    plan["expected_insertions"]["metadata"].append(["unrelated", "bad"])
    (output / "plan.json").write_bytes(canonical(plan))
    with pytest.raises(DataError, match="Declared insertions"):
        calendar.verify_plan(parent, output / "plan.json")
    assert MarketRelease(parent.root).sha256 == parent.sha256


def test_disagreement_blocks_calendar_before_creating_output(tmp_path, monkeypatch):
    parent, args = calendar_parent(tmp_path, monkeypatch)
    monkeypatch.setattr(daily_sources, "decode_sina", lambda raw, kind: ["2026-10-14", "2026-10-30"])
    output = tmp_path / "rejected-calendar"
    with pytest.raises(DataError, match="flags differ"):
        calendar.prepare(parent, output, **args)
    assert not output.exists()
    assert MarketRelease(parent.root).sha256 == parent.sha256
