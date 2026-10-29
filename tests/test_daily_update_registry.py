from datetime import datetime, timedelta
import fcntl
import importlib.util
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pytest


def module():
    path = Path(__file__).parents[1] / "scripts/run_daily_update_registry.py"
    spec = importlib.util.spec_from_file_location("daily_registry", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    m = module()
    monkeypatch.setattr(m.time, "sleep", lambda seconds: None)
    package = tmp_path / "frozen/site-packages/example/__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("immutable example")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            dict(
                package_origin=str(package),
                python=str(tmp_path / "frozen/bin/python"),
                frozen_package_files={"__init__.py": m.sha(package)},
            )
        )
    )
    children = []
    for name in ["quotes", "news"]:
        child = tmp_path / (name + ".json")
        child.write_text(
            json.dumps(dict(root=str(tmp_path / name), max_jobs=1, workers=1, attempts_per_day=1))
        )
        children.append(
            dict(
                id=name,
                kind="manual",
                frequency="daily",
                sources=["sina.com.cn"],
                rows=[12],
                profile=str(child),
                profile_sha256=m.sha(child),
                runtime_manifest=str(manifest),
                runtime_manifest_sha256=m.sha(manifest),
                observation_root=str(tmp_path / name),
                child_timeout_seconds=80,
            )
        )
    config = dict(
        schema="ad.daily-update-registry.v1",
        root=str(tmp_path / "registry"),
        isolated_workspace=str(tmp_path),
        dispatcher_sha256=m.sha(m.__file__),
        chunks_per_run=10,
        seconds_per_run=900,
        http_requests_per_day=20,
        response_bytes_per_day=10000000,
        minimum_free_bytes=1,
        between_chunks_seconds=1,
        child_jobs=1,
        items=children,
        quiet_local_windows=[["21:20", "22:00"]],
    )
    profile = tmp_path / "registry.json"
    profile.write_text(json.dumps(config))
    now = datetime(2026, 10, 29, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    return m, profile, config, now


def empty_usage(*args):
    return dict(requests=0, response_bytes=0)


def test_late_items_defer_without_settling_and_local_cache_ignores_network_budget(setup):
    m, p, c, now = setup
    item = c["items"][0]
    item.update(kind="cached_macro", not_before_local="22:10")
    profile = Path(item["profile"])
    child = m.read(profile)
    child["source_root"] = str(p.parent / "source")
    profile.write_text(json.dumps(child))
    item["profile_sha256"] = m.sha(profile)
    p.write_text(json.dumps(c))
    calls = []
    def runner(item, day, jobs, output):
        calls.append(item["id"])
        return dict(complete=True, settled=True)
    def spent(*args):
        return dict(requests=20, response_bytes=0)
    early = m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=spent)
    assert not calls and not early["items"]
    later = m.run(p, execute=True, clock=lambda: now.replace(hour=22, minute=10),
                  runner=runner, usage_reader=spent)
    assert calls == ["quotes"] and later["items"]["quotes"]["complete"]


def test_dependency_pending_does_not_spin_or_mark_finished(setup):
    m,p,c,now = setup
    calls = []
    def runner(item, day, jobs, output):
        calls.append(item["id"])
        return dict(complete=False, settled=False, deferred=True)
    result=m.run(p,execute=True,clock=lambda:now,runner=runner,usage_reader=empty_usage)
    assert calls == ["quotes", "news"]
    assert all(not v["settled"] for v in result["items"].values())


def test_daily_resume_finished_jobs_skip_and_next_date_opens(setup):
    m, p, c, now = setup
    calls = []
    counts = {}

    def runner(item, day, jobs, output):
        calls.append((item["id"], day))
        key = (item["id"], day)
        counts[key] = counts.get(key, 0) + 1
        return dict(complete=counts[key] >= 2, settled=counts[key] >= 2)

    assert m.run(p)["dry_run"] and not Path(c["root"]).exists()
    result = m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)
    assert [i[0] for i in calls] == ["quotes", "news", "quotes", "news"]
    assert all(i["complete"] for i in result["items"].values())
    assert not m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)[
        "work"
    ]
    later = m.run(
        p,
        execute=True,
        clock=lambda: now + timedelta(days=1),
        runner=runner,
        usage_reader=empty_usage,
    )
    assert all(i["day"] == "2026-10-30" and i["complete"] for i in later["items"].values())


def test_shared_cooldown_blocks_other_profile_same_source(setup):
    m, p, c, now = setup
    root = Path(c["items"][0]["observation_root"])
    root.mkdir()
    with sqlite3.connect(root / "observations.sqlite") as db:
        db.execute("CREATE TABLE cooldowns(host TEXT,until_epoch REAL,reason TEXT)")
        db.execute(
            "INSERT INTO cooldowns VALUES(?,?,?)",
            ("quotes.sina.com.cn", now.timestamp() + 3600, "HTTP 429"),
        )

    def never(*args):
        raise AssertionError("source restriction bypassed")

    r = m.run(p, execute=True, clock=lambda: now, runner=never, usage_reader=empty_usage)
    assert len(r["work"]) == 2 and all(i["state"] == "cooldown_skipped" for i in r["work"])


@pytest.mark.parametrize("tag,host", [
    ("sina", "quotes.sina.cn"), ("sina.cn", "finance.sina.com.cn"),
    ("sina.com.cn", "sina"), ("tdx-f10", "static.tdx.com.cn"),
])
def test_source_alias_and_receipt_host_share_refusal(tag, host):
    m = module()
    refusal = {host: {"permanent": True, "reason": "HTTP403"}}
    assert m.source_block({"sources": [tag]}, refusal) == refusal
    assert m.source_block({"sources": ["unrelated.example"]}, refusal) == {}


def test_old_unfinished_scope_resumes_before_today_without_claiming_success(setup):
    m, p, c, now = setup
    c["chunks_per_run"] = 1
    p.write_text(json.dumps(c))
    calls = []

    def runner(item, day, jobs, output):
        calls.append((item["id"], day))
        return dict(complete=False, settled=len(calls) > 2)

    m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)
    m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)
    r = m.run(
        p,
        execute=True,
        clock=lambda: now + timedelta(days=1),
        runner=runner,
        usage_reader=empty_usage,
    )
    assert calls[-1] == ("quotes", "2026-10-29")
    assert r["items"]["quotes"]["settled"] and not r["items"]["quotes"]["complete"]


def test_child_failure_defers_shared_source_and_retains_error(setup):
    m, p, c, now = setup

    def failed(*args):
        raise TimeoutError("synthetic timeout")

    r = m.run(p, execute=True, clock=lambda: now, runner=failed, usage_reader=empty_usage)
    assert len(r["work"]) == 2 and r["work"][1]["state"] == "cooldown_skipped"
    assert "TimeoutError" in r["items"]["quotes"]["error"]
    assert not r["items"]["quotes"]["complete"]


def test_stop_resource_budget_quiet_window_and_unique_instance(setup):
    m, p, c, now = setup

    def never(*args):
        raise AssertionError("should not run")

    r = m.run(
        p,
        execute=True,
        clock=lambda: now.replace(hour=21, minute=25),
        runner=never,
        usage_reader=empty_usage,
    )
    assert r["reason"] == "protected_schedule_window"
    root = Path(c["root"])
    (root / "STOP").touch()
    assert (
        m.run(p, execute=True, clock=lambda: now, runner=never, usage_reader=empty_usage)["reason"]
        == "stop_requested"
    )
    (root / "STOP").unlink()
    assert (
        m.run(
            p,
            execute=True,
            clock=lambda: now,
            runner=never,
            usage_reader=lambda *a: dict(requests=20, response_bytes=0),
        )["reason"]
        == "daily_resource_budget"
    )
    with (root / "registry.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already active"):
            m.run(p, execute=True, clock=lambda: now, runner=never, usage_reader=empty_usage)


def test_pin_changes_rejected_and_financial_usage_included(setup):
    m, p, c, now = setup
    root = Path(c["items"][0]["observation_root"])
    receipt = root / "days/2026-10-29/receipts/one.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            dict(
                attempts=[
                    dict(
                        started_at=now.isoformat(),
                        url="https://sina.com.cn",
                        response_sha256="x",
                        response_bytes=17,
                    )
                ]
            )
        )
    )
    assert m.usage(c, "2026-10-29") == dict(requests=1, response_bytes=17)
    Path(c["items"][0]["profile"]).write_text("{}")
    with pytest.raises(ValueError, match="frozen file"):
        m.run(p)


def test_weekly_profile_does_not_requery_on_every_day(setup):
    m, p, c, now = setup
    for item in c["items"]:
        item["frequency"] = "weekly"
    p.write_text(json.dumps(c))
    calls = []

    def runner(item, *args):
        calls.append(item["id"])
        return dict(complete=True, settled=True)

    m.run(p, execute=True, clock=lambda: now, runner=runner, usage_reader=empty_usage)
    assert len(calls) == 2
    m.run(
        p,
        execute=True,
        clock=lambda: now + timedelta(days=1),
        runner=runner,
        usage_reader=empty_usage,
    )
    assert len(calls) == 2


def test_reused_http_receipts_count_once_across_stores_and_on_receipt_day(setup):
    m,p,c,now=setup
    receipt=dict(received_at=now.isoformat(),url='https://example.test/data',sha256='x',bytes=17,protocol='http',request_method='GET')
    for item in c['items']:
        root=Path(item['observation_root'])
        root.mkdir()
        with sqlite3.connect(root/'observations.sqlite') as db:
            db.execute('CREATE TABLE runs(started_at TEXT,result_json TEXT)')
            for started in [now.isoformat(),(now+timedelta(days=1)).isoformat()]:
                db.execute('INSERT INTO runs VALUES(?,?)',(started,json.dumps({'receipts':[receipt]})))
    assert m.usage(c,'2026-10-29')==dict(requests=1,response_bytes=17)
    assert m.usage(c,'2026-10-30')==dict(requests=0,response_bytes=0)


def test_monthly_published_series_does_not_refetch_every_day_and_resumes_next_month(setup):
    m,p,c,now=setup
    c['items']=c['items'][:1]
    c['items'][0]['frequency']='monthly'
    p.write_text(json.dumps(c))
    calls=[]
    def runner(item,day,jobs,output):
        calls.append(day)
        return dict(complete=True,settled=True)
    first=m.run(p,execute=True,clock=lambda:now,runner=runner,usage_reader=empty_usage)
    assert first['items']['quotes']['due']=='2026-10-01'
    assert not m.run(p,execute=True,clock=lambda:now+timedelta(days=1),runner=runner,usage_reader=empty_usage)['work']
    m.run(p,execute=True,clock=lambda:now.replace(month=11,day=1),runner=runner,usage_reader=empty_usage)
    assert calls==['2026-10-29','2026-11-01']
