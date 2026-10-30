"""Independent sources overlap; a shared source/store remains single-writer."""
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import threading

import pytest

import test_daily_update_registry as registry
from test_daily_update_registry import empty_usage


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return registry.setup.__wrapped__(tmp_path, monkeypatch)


def parallel(setup):
    module, path, config, now = setup
    config.update(schema="ad.daily-update-registry.v2", workers=8,
                  budget_scope="observation_store", chunks_per_run=5000,
                  seconds_per_run=21600, between_chunks_seconds=0, child_jobs=4)
    config["items"][1]["sources"] = ["stats.gov.cn"]
    path.write_text(json.dumps(config))
    return module, path, config, now


def test_independent_sources_overlap_and_completed_work_does_not_repeat(setup):
    m, p, c, now = parallel(setup)
    barrier = threading.Barrier(2)
    calls = []

    def runner(item, day, jobs, output):
        calls.append(item["id"])
        assert jobs == 4
        barrier.wait(timeout=3)
        return dict(complete=True, settled=True)

    result = m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)
    assert set(calls) == {"quotes", "news"} and result["peak_active_children"] == 2
    assert result["budget_scope"] == "observation_store" and result["budget_count"] == 2
    assert not m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)["work"]
    assert m.run(p)["workers"] == 8


@pytest.mark.parametrize("same", ["upstream", "store"])
def test_shared_upstream_alias_or_store_cannot_overlap(setup, same):
    m, p, c, now = parallel(setup)
    if same == "upstream":
        c["items"][1]["sources"] = ["quotes.sina.cn"]
    else:
        item = c["items"][1]
        child = m.read(item["profile"])
        child["root"] = c["items"][0]["observation_root"]
        Path(item["profile"]).write_text(json.dumps(child))
        item.update(observation_root=child["root"], profile_sha256=m.sha(item["profile"]))
    p.write_text(json.dumps(c))
    gate = threading.Lock()

    def runner(*args):
        assert gate.acquire(blocking=False), "two children share one upstream or writer"
        threading.Event().wait(0.02)
        gate.release()
        return dict(complete=True, settled=True)

    r = m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)
    assert r["peak_active_children"] == 1 and r["launched_chunks"] == 2


def test_one_store_budget_does_not_stop_other_sources_or_cached_work(setup):
    m, p, c, now = parallel(setup)
    calls = []

    def usage(config, day):
        return dict(requests=20 if config["items"][0]["id"] == "quotes" else 0, response_bytes=0)

    def runner(item, *args):
        calls.append(item["id"])
        return dict(complete=True, settled=True)

    r = m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=usage)
    assert calls == ["news"] and r["reason"] == "daily_resource_budget"
    assert "quotes" not in r["items"] and r["usage"]["requests"] == 20


def test_usage_history_scanned_once_not_every_chunk(setup):
    m, p, c, now = parallel(setup)
    scans, calls = [], {}

    def usage(config, day):
        scans.append((config["items"][0]["id"], day))
        return empty_usage()

    def runner(item, *args):
        n = calls.get(item["id"], 0)+1
        calls[item["id"]] = n
        return dict(complete=n == 4, settled=n == 4)

    r = m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=usage)
    assert r["launched_chunks"] == 8 and len(scans) == 2


def test_real_refusal_stops_same_family_without_stopping_other_sources(setup):
    m, p, c, now = parallel(setup)
    child = dict(c["items"][0], id="sina-next")
    c["items"].append(child)
    p.write_text(json.dumps(c))
    calls = []

    def runner(item, *args):
        calls.append(item["id"])
        if item["id"] == "quotes":
            root = Path(item["observation_root"])
            root.mkdir()
            with sqlite3.connect(root / "observations.sqlite") as db:
                db.execute("CREATE TABLE cooldowns(host TEXT,until_epoch REAL,reason TEXT)")
                db.execute("INSERT INTO cooldowns VALUES(?,?,?)", ("quotes.sina.cn", now.timestamp()+7200, "HTTP 429 Retry-After"))
        return dict(complete=True, settled=True)

    r = m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)
    assert set(calls) == {"quotes", "news"}
    assert any(x["id"] == "sina-next" and x.get("state") == "cooldown_skipped" for x in r["work"])


def test_protected_macro_window_only_blocks_shared_source(setup):
    m, p, c, now = parallel(setup)
    c["items"][0]["sources"] = ["eastmoney.com"]
    c["protected_sources"] = ["eastmoney.com"]
    p.write_text(json.dumps(c))
    calls = []

    def runner(item, *args):
        calls.append(item["id"])
        return dict(complete=True, settled=True)

    r = m.run(p, execute=True, clock=lambda: now.replace(hour=21, minute=30), runner=runner, usage_reader=empty_usage)
    assert calls == ["news"] and r["reason"] == "protected_schedule_window"


def test_upgrade_preserves_old_checkpoint_and_uses_same_outer_lock(setup):
    m, p, c, now = parallel(setup)
    root = Path(c["root"])
    root.mkdir()
    old = {name: b"unchanged old controller evidence" for name in ["profile.json", "state.json", "request-budget-config.json"]}
    for name, body in old.items():
        (root / name).write_bytes(body)
    result = m.run(p, execute=True, clock=lambda: now,
                   runner=lambda *args: dict(complete=True, settled=True), usage_reader=empty_usage)
    assert Path(result["state_path"]).is_relative_to(root / "dispatches")
    assert all((root / name).read_bytes() == body for name, body in old.items())
    later = m.run(p, execute=True, clock=lambda: now+timedelta(days=1),
                  runner=lambda *args: dict(complete=True, settled=True), usage_reader=empty_usage)
    assert all(x["day"] == "2026-10-30" for x in later["items"].values())


def test_batch_chunk_clamps_to_the_existing_frozen_profile(setup, monkeypatch):
    m, p, c, now = parallel(setup)
    item = dict(c["items"][0], kind="securities")
    calls = []
    from ashare_data.providers import free_update_batch

    def run(profile, **kwargs):
        calls.append(kwargs["max_jobs"])
        return dict(all_requested_jobs_received=True, attempted=1)

    monkeypatch.setattr(free_update_batch, "run", run)
    assert m.child(item, "2026-10-30", 4)["complete"] and calls == [1]


def test_stop_drains_owned_children_without_launching_more_work(setup):
    m, p, c, now = parallel(setup)
    c["workers"] = 1
    p.write_text(json.dumps(c))
    calls = []

    def runner(item, *args):
        calls.append(item["id"])
        (Path(c["root"]) / "STOP").touch()
        return dict(complete=True, settled=True)

    r = m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)
    assert calls == ["quotes"] and r["reason"] == "stop_requested"
    assert r["items"]["quotes"]["complete"] and "news" not in r["items"]


def test_initializing_owned_database_is_read_only_after_its_lease_finishes(setup):
    m, p, c, now = parallel(setup)
    root = Path(c["items"][0]["observation_root"])
    root.mkdir()
    with sqlite3.connect(root / "observations.sqlite"):
        pass
    assert not m.blocked_sources(c, now, active_roots={str(root)})
    with sqlite3.connect(root / "observations.sqlite") as db:
        db.execute("CREATE TABLE cooldowns(host TEXT,until_epoch REAL,reason TEXT)")
        db.execute("INSERT INTO cooldowns VALUES(?,?,?)", ("quotes.sina.cn", now.timestamp()+3600, "HTTP 429"))
    assert m.source_block(c["items"][0], m.blocked_sources(c, now))


def test_existing_schedule_protects_shared_store_even_for_a_different_source(setup):
    m, p, c, now = parallel(setup)
    c.update(protected_sources=["eastmoney.com"],
             protected_observation_roots=[c["items"][0]["observation_root"]])
    p.write_text(json.dumps(c))
    calls = []

    def runner(item, *args):
        calls.append(item["id"])
        return dict(complete=True, settled=True)

    r = m.run(p, execute=True, clock=lambda: now.replace(hour=21, minute=30), runner=runner, usage_reader=empty_usage)
    assert calls == ["news"] and r["reason"] == "protected_schedule_window"
