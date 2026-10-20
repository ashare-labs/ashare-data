"""Synthetic equivalence checks for date-query projections of one-code responses."""
from __future__ import annotations

import gzip
import fcntl
import json
import sqlite3

import pytest

from ashare_data.financials import expansion as e
from ashare_data.financials import tushare_dividends as d
from ashare_data.financials import tushare_gaps as g
from ashare_data.financials import tushare_reference as ts


@pytest.fixture
def source(tmp_path):
    db = tmp_path / "daily.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE instruments(symbol,name,ipo,out,board)")
        con.execute("INSERT INTO instruments VALUES('sh600000','synthetic','1999-01-01',NULL,'main')")
        con.execute("CREATE TABLE code_events(id,old_symbol,new_symbol,effective_date)")
        con.execute("CREATE TABLE metadata(key,value)")
    baseline = tmp_path / "baseline"
    e.prepare(baseline, db, "2010-01-01", "2015-12-31")
    _, manifest, _ = e.load_batch(baseline)
    tasks = [g.make_task("dividend", {"symbol": "sh600000"}, {"ex_date": date}, ["pay_date", "base_share"], manifest,
        reason="missing_existing_distribution_fields", gaps=["pay_date", "base_share"], matching_events=[dict(
            date=e.day(date), source_revision_id="event-" + date, cash_basis="gross_pre_tax_per_old_eligible_share",
            anchors={"cash_div_tax": "1", "stk_bo_rate": "0", "stk_co_rate": "0", "record_date": e.day(date[:4] + "0630")})])
        for date in ("20100701", "20110701", "20120701")]
    root = tmp_path / "source"
    ts._prepare(root, baseline, tasks, {})
    return root, baseline, manifest, tasks


def row(date, **extra):
    return dict(ts_code="600000.SH", end_date=str(int(date[:4]) - 1) + "1231", ann_date=date[:4] + "0420",
                div_proc="实施", ex_date=date, record_date=date[:4] + "0630", pay_date=date[:4] + "0702",
                cash_div_tax="1", stk_bo_rate="0", stk_co_rate="0", base_share="100", **extra)


def body(url, rows, **extra):
    fields = ts.request_fields(url).split(",")
    return e.encoded(dict(code=0, data=dict(fields=fields, items=[[r.get(k) for k in fields] for r in rows], **extra)))


def run(root, rows, *, max_jobs=0, extra=None):
    return e.run(root, max_jobs=max_jobs, interval=.0001, minimum_interval=.0001,
                 fetcher=lambda url: (200, body(url, rows(url) if callable(rows) else rows, **(extra or {})), {}),
                 normalizer=ts.normalize, retain_temporaries=True)


def receipt(root, task):
    return g.read(root / "receipts" / (task["id"] + ".json"), {})


def test_preserves_successes_versions_event_fields_and_physical_source(source, tmp_path):
    root, _, _, tasks = source
    run(root, [row("20100701")], max_jobs=1)
    first = (root / "receipts" / (tasks[0]["id"] + ".json")).read_bytes()
    capture = tmp_path / "capture"
    planned = d.prepare(capture, root)
    assert planned["planned_tasks"] == 1 and planned["original_pending_dates"] == 2
    original = row("20110701")
    revision = original | {"ann_date": "20110520", "base_share": "120"}
    rows = [original, revision, row("20120701"), row("20130701")]
    run(capture, rows)
    summary = d.apply(capture)
    assert summary["original_network_requests_avoided"] == 2
    assert summary["net_network_requests_saved_so_far"] == 1
    assert (root / "receipts" / (tasks[0]["id"] + ".json")).read_bytes() == first
    for task in tasks[1:]:
        reused = receipt(root, task)
        assert reused["attempts"] == [] and reused["coverage"]["original_query_executed"] is False
        e.verify_receipt(root, reused)
        projected = g.normalized(root, reused)
        date = ts.request_identity(task["url"])[1]["ex_date"]
        direct, _ = g.f.observations(body(task["url"], [r for r in rows if r["ex_date"] == date]), task)
        assert [r["fields"] for r in projected] == [r["fields"] for r in direct]
        assert all(r["source_batch"] == str(capture) and r["local_projection"] for r in projected)
        assert all(r["source_ordinal"] == i for r, i in zip(projected, [0, 1] if date == "20110701" else [2]))
        before = reused["normalized"]["sha256"]
        ts.normalize(root, {}, task, reused)
        assert reused["normalized"]["sha256"] == before  # Replay follows the physical response, not the date URL.
    _, _, captures = e.load_batch(capture)
    assert len(g.normalized(capture, receipt(capture, captures[0]))) == 4  # Unplanned events also retained.
    assert (root / receipt(root, tasks[1])["raw"]["path"]).stat().st_ino == (capture / receipt(capture, captures[0])["raw"]["path"]).stat().st_ino
    assert d.prepare(capture, root) == planned
    assert d.apply(capture)["net_network_requests_saved_so_far"] == 1
    run(root, lambda _: pytest.fail("Reused date was requested on resume"))
    ledger = list(g.reconciliation_rows({"existing_fields": []}, [root]))
    observed = [o for item in ledger for o in item["observations"] if o["source_batch"] == str(capture)]
    assert observed and all(o["source_task_id"] == captures[0]["id"] for o in observed)


@pytest.mark.parametrize("reason", ["missing_date", "wrong_anchor", "has_more", "row_cap", "missing_fields"])
def test_uncertain_coverage_leaves_date_fallback(source, tmp_path, reason):
    root, _, _, tasks = source
    capture = tmp_path / "capture"
    d.prepare(capture, root)
    rows = [row("20100701"), row("20110701"), row("20120701")]
    if reason == "missing_date":
        rows = rows[1:]
    elif reason == "wrong_anchor":
        rows[0]["cash_div_tax"] = "2"
    elif reason == "row_cap":
        rows = [rows[0]] * 2000
    if reason == "missing_fields":
        def fetch(url):
            parsed = json.loads(body(url, rows))
            index = parsed["data"]["fields"].index("base_share")
            parsed["data"]["fields"].pop(index)
            for values in parsed["data"]["items"]:
                values.pop(index)
            return 200, e.encoded(parsed), {}
        e.run(capture, fetcher=fetch, normalizer=ts.normalize)
    else:
        run(capture, rows, extra={"has_more": True} if reason == "has_more" else None)
    summary = d.apply(capture)
    expected = 0 if reason in {"has_more", "row_cap", "missing_fields"} else 2
    assert summary["original_network_requests_avoided"] == expected
    assert not receipt(root, tasks[0]).get("source_reuse")
    calls = []
    def fallback(url):
        params = ts.request_identity(url)[1]
        assert "ex_date" in params
        calls.append(params["ex_date"])
        return [row(params["ex_date"])]
    run(root, fallback)
    assert len(calls) == 3 - expected
    assert all(receipt(root, task)["state"] == "complete" for task in tasks)


def test_successful_whole_code_response_is_reused_without_network(source, tmp_path):
    old, baseline, manifest, dates = source
    broad = g.make_task("dividend", {"symbol": "sh600000"}, {}, ["pay_date", "base_share"], manifest,
                       reason="unresolved_existing_source", gaps=[], matching_events=[])
    root = tmp_path / "with-broad"
    ts._prepare(root, baseline, [broad, *dates], {})
    run(root, [row("20100701"), row("20110701"), row("20120701")], max_jobs=1)
    capture = tmp_path / "capture"
    assert d.prepare(capture, root)["retained_existing_broad_responses"] == 1
    run(capture, lambda _: pytest.fail("Existing whole-code response fetched again"))
    assert d.apply(capture)["net_network_requests_saved_so_far"] == 3
    run(root, lambda _: pytest.fail("Derived date response fetched again"))


def test_captured_date_is_not_replaced_and_temporaries_are_retained(source, tmp_path):
    root, _, _, tasks = source
    run(root, [row("20100701")], max_jobs=1)
    captured = receipt(root, tasks[0]) | {"state": "captured"}
    e.atomic(root / "receipts" / (tasks[0]["id"] + ".json"), e.encoded(captured))
    orphan = root / "retained-evidence.tmp"
    orphan.write_text("retain")
    capture = tmp_path / "capture"
    d.prepare(capture, root)
    _, _, planned = e.load_batch(capture)
    assert tasks[0]["id"] not in planned[0]["coalesces"]
    run(capture, [row("20110701"), row("20120701")])
    d.apply(capture)
    run(root, lambda _: pytest.fail("Captured response fetched again"))
    assert orphan.read_text() == "retain"
    assert not receipt(root, tasks[0]).get("source_reuse")


def test_failed_response_snapshot_preserves_attempt_history(source, tmp_path):
    root, _, _, tasks = source
    prior = dict(task_id=tasks[0]["id"], state="failed_retryable", attempts=[{"error": "synthetic timeout"}])
    e.atomic(root / "receipts" / (tasks[0]["id"] + ".json"), e.encoded(prior))
    capture = tmp_path / "capture"
    d.prepare(capture, root)
    snapshot = json.loads(gzip.decompress((capture / "original-receipts.json.gz").read_bytes()))
    assert snapshot[tasks[0]["id"]]["receipt"] == prior
    run(capture, [row("20100701"), row("20110701"), row("20120701")])
    d.apply(capture)
    assert receipt(root, tasks[0])["attempts"] == prior["attempts"]


def test_boundary_lock_and_failed_atomic_write_preserve_evidence(source, tmp_path, monkeypatch):
    root, _, _, _ = source
    with (root / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            d.prepare(tmp_path / "capture", root)
    assert not (tmp_path / "capture").exists()
    def failed_replace(*_):
        raise OSError("synthetic failed atomic replace")
    monkeypatch.setattr(e.os, "replace", failed_replace)
    target = tmp_path / "checkpoint.json"
    with e.preserve_temporaries(), pytest.raises(OSError):
        e.atomic(target, b"preserve partial output")
    saved = list(tmp_path.glob("checkpoint.json.*.tmp"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"preserve partial output"


def test_null_stock_anchors_reuse_date_query_but_do_not_certify_event_binding(source, tmp_path):
    root, _, _, tasks = source
    capture = tmp_path / "capture"
    d.prepare(capture, root)
    rows = [row(date) | {"stk_bo_rate": None, "stk_co_rate": None} for date in ("20100701", "20110701", "20120701")]
    run(capture, rows)
    assert d.apply(capture)["net_network_requests_saved_so_far"] == 2
    for task in tasks:
        reused = receipt(root, task)
        assert reused["coverage"]["matched_event_count"] == 0
        assert reused["coverage"]["unresolved_event_bindings"] == 1
        assert reused["coverage"]["missing_anchors_imputed"] is False
        actual = g.normalized(root, reused)
        date = ts.request_identity(task["url"])[1]["ex_date"]
        expected, _ = g.f.observations(body(task["url"], [r for r in rows if r["ex_date"] == date]), task)
        assert [r["fields"] for r in actual] == [r["fields"] for r in expected]
        assert actual[0]["fields"]["stk_bo_rate"]["value"] is None
    ledger = list(g.reconciliation_rows({"existing_fields": []}, [root]))
    assert all(item["state"] == "unresolved_ambiguous_event_match" for item in ledger)


@pytest.mark.parametrize("anchor", ["record_date", "cash_div_tax"])
def test_null_required_event_anchor_still_falls_back(source, tmp_path, anchor):
    root, _, _, tasks = source
    capture = tmp_path / "capture"
    d.prepare(capture, root)
    run(capture, [row(date) | {anchor: None} for date in ("20100701", "20110701", "20120701")])
    result = d.apply(capture)
    assert result["original_network_requests_avoided"] == 0
    assert result["states"]["fallback_event_not_matched"] == len(tasks)
