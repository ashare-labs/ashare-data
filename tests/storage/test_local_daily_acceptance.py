"""Acceptance must distinguish bulletin republications from issuer originals."""

import gzip
import hashlib
import json
from pathlib import Path
import runpy

import pytest

from ashare_data.providers.baostock_receipt.baostock_collection import import_captures
from ashare_data.storage.local_daily import LocalDailyStore


ACCEPT = runpy.run_path(str(Path(__file__).parents[2] / "scripts/accept_local_daily.py"))


def setup_evidence(root):
    body = "<p>Synthetic exchange bulletin 2012-04-05</p><p>600001 full-day suspension</p>".encode()
    sha = hashlib.sha256(body).hexdigest()
    (root / "raw").mkdir(exist_ok=True)
    (root / "reports").mkdir(exist_ok=True)
    (root / "raw" / (sha + ".gz")).write_bytes(gzip.compress(body))
    item = dict(
        symbol="sh600001",
        start="2012-04-05",
        end_exclusive="2012-04-06",
        original_author="Synthetic exchange",
        publisher="Synthetic bulletin archive",
        receipts=[
            dict(
                sha256=sha,
                encoding="utf-8",
                reviewed_text_fragments=[
                    "Synthetic exchange bulletin 2012-04-05",
                    "600001 full-day suspension",
                ],
            )
        ],
    )
    path = root / "reports/exchange-republished-suspension-evidence.json"
    path.write_text(json.dumps(dict(intervals=[item])))
    return path, item


def test_republished_halt_explains_gap_without_inventing_bar_or_issuer_notice(tmp_path):
    store = LocalDailyStore(tmp_path)
    store.save_universe(
        [
            dict(
                symbol="sh600001",
                name="synthetic",
                ipo="2012-04-05",
                out=None,
                board="main",
                source="fixture",
                receipt="x",
            )
        ],
        [],
        "2012-04-05",
        "2012-04-06",
    )
    store.save_calendar(["2012-04-05", "2012-04-06"], [dict(url="fixture", sha256="f")])
    store.plan("2012-04-05", "2012-04-06")
    setup_evidence(tmp_path)
    import_captures(store)
    store.close()
    result = ACCEPT["audit"](tmp_path)
    assert result["counts"]["main_expected"] == result["counts"]["main_missing"] == 2
    assert result["counts"]["main_observed"] == result["physical_source_rows"] == 0
    assert result["gap_classes"] == {
        "exchange_notice_republication_nontrading_without_bar": 1,
        "awaiting_collection": 1,
    }
    proof = next(g for g in result["gap_dispositions"] if g["start"] == "2012-04-05")
    assert len(proof["evidence_sha256"]) == 64
    assert proof["issuer_notice_sha256"] == ""
    assert not result["exchange_republication_evidence_errors"]


@pytest.mark.parametrize("damage", ["raw_bytes", "reviewed_text", "empty_attribution"])
def test_invalid_republication_evidence_cannot_reclassify_a_gap(tmp_path, damage):
    path, item = setup_evidence(tmp_path)
    if damage == "raw_bytes":
        (tmp_path / "raw" / (item["receipts"][0]["sha256"] + ".gz")).write_bytes(
            gzip.compress(b"different bulletin")
        )
    elif damage == "reviewed_text":
        item["receipts"][0]["reviewed_text_fragments"] = ["600001 traded all day"]
    else:
        item["original_author"] = ""
    path.write_text(json.dumps(dict(intervals=[item])))
    intervals, errors = ACCEPT["exchange_republication_evidence"](tmp_path)
    assert intervals == []
    assert len(errors) == 1


def test_missing_amount_separates_explicit_halts_activity_and_conflicting_sources(tmp_path):
    store = LocalDailyStore(tmp_path)
    dates = ["2012-04-05", "2012-04-06", "2012-04-09", "2012-04-10", "2012-04-11"]
    store.save_universe(
        [
            dict(
                symbol="sh600001",
                name="synthetic",
                ipo=dates[0],
                out=None,
                board="main",
                source="fixture",
                receipt="x",
            )
        ],
        [],
        dates[0],
        dates[-1],
    )
    store.save_calendar(dates, [dict(url="fixture", sha256="f")])
    store.plan(dates[0], dates[-1])
    setup_evidence(tmp_path)
    import_captures(store)
    job = dict(store.db.execute("SELECT * FROM jobs WHERE kind='bars' LIMIT 1").fetchone())
    rows = [
        dict(
            date=d,
            open="10",
            high="10",
            low="10",
            close="10",
            volume=v,
            amount=None,
            tradestatus=status,
            is_st=None,
            preclose=None,
        )
        for d, v, status in zip(dates, [None, "100", "0", None, None], ["0", None, None, "0", "1"])
    ]
    store.ingest(job, dict(source="baostock", rows=rows, receipts=[]))
    conflict = dict(rows[3], volume="100", tradestatus="1")
    store.ingest(dict(job, id="other"), dict(source="sina", rows=[conflict], receipts=[]))
    store.close()
    result = ACCEPT["audit"](tmp_path)
    assert result["counts"]["main_missing"] == 0
    assert result["counts"]["main_amount_missing"] == 5
    assert result["amount_gap_classes"] == {
        "explicit_source_halt_without_amount": 1,
        "reported_trading_activity_without_amount": 2,
        "source_halt_activity_conflict_without_amount": 1,
        "no_explicit_status_or_activity_without_amount": 1,
    }
    assert sum(g["security_days"] for g in result["amount_gap_dispositions"]) == 5
    store = LocalDailyStore(tmp_path, readonly=True)
    try:
        assert (
            store.db.execute("SELECT count(*) FROM bars WHERE amount IS NOT NULL").fetchone()[0]
            == 0
        )
        assert store.db.execute("SELECT count(*) FROM bars").fetchone()[0] == 6
    finally:
        store.close()
