"""Synthetic safe-boundary and source-handoff regressions; no real credentials."""

import fcntl
import gzip
import sqlite3

import pytest

from ashare_data.financials import batch_followup as follow
from ashare_data.financials import expansion as e
from ashare_data.financials import limit_expansion as limits
from ashare_data.financials import tushare_reference as ts


@pytest.fixture
def prepared(tmp_path):
    db = tmp_path / "daily.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE instruments(symbol,name,ipo,out,board)")
        con.executemany("INSERT INTO instruments VALUES(?,?,?,?,?)", [
            ("sh600000", "synthetic", "1999-01-01", None, "main"),
            ("sz000001", "synthetic", "1999-01-01", None, "main"),
        ])
        con.execute("CREATE TABLE code_events(id,old_symbol,new_symbol,effective_date)")
        con.execute("CREATE TABLE metadata(key,value)")
    base, legacy, root = [tmp_path / n for n in ("base", "legacy", "tushare")]
    e.prepare(base, db, "2010-01-01", "2010-01-05")
    limits.prepare(legacy, base, "2010-01-01", "2010-01-05")
    ts.prepare_calendar(root / "calendar", base)
    e.atomic(root / "calendar/supervisor.json", e.encoded({"state": follow.FINISHED}))
    tasks = [dict(id="tushare_stk_limit-" + d, kind="tushare_stk_limit", symbol="", start=d, end=d,
                  url=ts.request_url("stk_limit", {"trade_date": d.replace("-", "")}))
             for d in ("2010-01-04", "2010-01-05")]
    ts._prepare(root / "limits", base, tasks, {})
    return base, legacy, root


def capture(root, days=("20100104", "20100105"), codes=("600000.SH", "000001.SZ")):
    def fetch(url):
        _, params = ts.request_identity(url)
        rows = [[params["trade_date"], code, 12, 10] for code in codes] if params["trade_date"] in days else []
        return 200, e.encoded(dict(code=0, data=dict(fields=["trade_date", "ts_code", "up_limit", "down_limit"], items=rows))), {}
    ts.run(root / "limits", fetch)


def completed(root, state=follow.FINISHED):
    e.atomic(root / "progress.json", e.encoded(dict(pid=123, updated_at="2010-01-01T00:00:00+00:00")))
    e.atomic(root / "worker-exit.json", e.encoded(dict(pid=123, at="2010-01-01T01:00:00+00:00", stop_requested=False)))
    e.atomic(root / "supervisor.json", e.encoded(dict(pid=123, at="2010-01-01T02:00:00+00:00", state=state)))


def test_handoff_uses_ordinary_historical_and_recent_results_and_waits_for_writer(prepared, tmp_path):
    _, legacy, root = prepared
    capture(root)
    record = tmp_path / "handoff.json"
    assert follow.request_handoff(root, legacy, record)["state"] == "waiting_for_ordinary_tushare_results"
    with (root / "worker.lock").open("a") as new_lock, (legacy / "writer.lock").open("a") as old_lock:
        fcntl.flock(new_lock, fcntl.LOCK_EX)
        fcntl.flock(old_lock, fcntl.LOCK_EX)
        result = follow.request_handoff(root, legacy, record)
        assert result["state"] == "stop_requested" and follow.owned_handoff(legacy, record)
        assert not result["evidence"]["complete_history_asserted"]
        assert follow.follow_batch(legacy, "limits", handoff_record=record)["state"] == "writer_active"
    completed(legacy, "stopped")
    result = follow.follow_batch(legacy, "limits", handoff_record=record)
    assert result["state"] == "verified_with_explicit_gaps"
    assert result["legacy_pending_preserved"] and result["network_requests"] == 0
    assert (legacy / "STOP").exists()


@pytest.mark.parametrize("case", ["missing_early", "one_exchange", "cooldown"])
def test_handoff_does_not_pause_legacy_without_bounded_usable_source(prepared, tmp_path, case):
    _, legacy, root = prepared
    capture(root, days=("20100105",) if case == "missing_early" else ("20100104", "20100105"),
            codes=("600000.SH",) if case == "one_exchange" else ("600000.SH", "000001.SZ"))
    if case == "cooldown":
        e.atomic(root / "limits/source-state.json", e.encoded({"api.tushare.pro": {"cooldown_until": 1e20}}))
    with (root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert follow.request_handoff(root, legacy, tmp_path / "handoff.json")["state"] == "waiting_for_ordinary_tushare_results"
    assert not (legacy / "STOP").exists()


def test_user_stop_and_stale_finished_status_never_allow_replay(prepared, tmp_path):
    base, _, root = prepared
    completed(base)
    progress = follow.read(base / "progress.json") | {"pid": 999}
    e.atomic(base / "progress.json", e.encoded(progress))
    assert follow.follow_batch(base, "public")["state"] == "waiting_for_natural_source_boundary"
    (base / "STOP").write_text("user requested stop")
    assert follow.follow_batch(base, "public")["state"] == "user_stop_preserved"
    assert (base / "STOP").read_text() == "user requested stop"
    assert follow.request_handoff(root, base, tmp_path / "handoff.json")["state"] == "existing_stop_preserved"


def test_monitor_can_recover_handoff_after_tushare_finishes(prepared, tmp_path):
    _, legacy, root = prepared
    capture(root)
    for name in ("calendar", "limits", "capped-targets"):
        e.atomic(root / name / "supervisor.json", e.encoded(dict(state=follow.FINISHED)))
    assert follow.request_handoff(root, legacy, tmp_path / "handoff.json")["state"] == "stop_requested"


def test_captured_failure_replayed_only_after_normal_source_end(tmp_path):
    root = tmp_path / "batch"
    root.mkdir()
    task = dict(id="empty", kind=limits.KIND, symbol="sh600000", start="2010-01-01", end="2010-01-05",
                url=limits.url_for("sh600000", "2010-01-01", "2010-01-05"))
    universe = e.encoded(dict(instruments=[]))
    e.atomic(root / "universe.json", universe)
    e.atomic(root / "tasks.json", e.encoded([task]))
    e.atomic(root / "manifest.json", e.encoded(dict(schema=e.SCHEMA, baseline_codes=1, target_codes=1,
             source_families=[limits.KIND], universe_sha256=e.digest(universe), tasks_sha256=e.digest(e.encoded([task])))))
    raw = e.encoded(dict(code=200, data=dict(count=0, list=[])))
    receipt = dict(state="parse_failed", raw=e.artifact(root, "raw/empty.json.gz", gzip.compress(raw)),
                   task_id="empty", attempts=[dict(response_sha256=e.digest(raw))], observed_at="2026-10-19T00:00:00+00:00")
    e.atomic(root / "receipts/empty.json", e.encoded(receipt))
    assert follow.follow_batch(root, "limits")["state"] == "waiting_for_natural_source_boundary"
    completed(root)
    result = follow.follow_batch(root, "limits")
    assert result["network_requests"] == 0
    assert result["result"]["task_states"] == {"empty_unknown": 1}
    assert follow.read(root / "receipts/empty.json")["raw"] == receipt["raw"]


def test_daily_validation_refuses_changed_review_file(tmp_path):
    review = tmp_path / "review.json"
    review.write_text("{}")
    with pytest.raises(ValueError, match="Field review changed"):
        follow.validate_daily(dict(official_root=str(tmp_path), review=str(review), review_sha256="changed"))


def test_finished_followup_is_not_repeated_each_poll(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(follow, "follow_batch", lambda *a, **k: calls.append(1) or {"state": "verified_with_explicit_gaps"})
    spec = dict(batches=[dict(id="synthetic", root=str(tmp_path), kind="public")])
    state = follow.tick(spec, {}, tmp_path)
    follow.tick(spec, state, tmp_path)
    assert calls == [1]
