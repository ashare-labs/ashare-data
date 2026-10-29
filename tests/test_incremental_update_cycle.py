import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from ashare_data.providers.free_updates import implementation_identity


def module():
    scripts = Path(__file__).parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(
            "cycle", scripts / "run_incremental_update_cycle.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(scripts))


def profile(tmp_path):
    child = tmp_path / "securities.json"
    child.write_text('{"max_jobs":2}')
    data = {
        "schema": "ad.incremental-update-cycle.v1",
        "root": str(tmp_path / "store"),
        "implementation_sha256": implementation_identity(),
        "chunks_per_run": 1,
        "jobs_per_chunk": 2,
        "seconds_per_run": 30,
        "items": [
            {
                "id": "minutes",
                "kind": "securities",
                "profile": str(child),
                "profile_sha256": hashlib.sha256(child.read_bytes()).hexdigest(),
            }
        ],
    }
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(data))
    return p


def test_cross_midnight_resumes_tail_then_opens_new_cycle_without_repeat(tmp_path):
    m = module()
    p = profile(tmp_path)
    calls = []

    def source(path, **kwargs):
        calls.append(kwargs["as_of"])
        complete = len(calls) % 2 == 0
        return {
            "all_requested_jobs_received": complete,
            "attempted": 2,
            "ready_jobs": 4 if complete else 2,
            "pending_jobs": 0 if complete else 2,
            "result_path": "immutable-result",
        }

    assert m.run(p)["network_used"] is False and not (tmp_path / "store").exists()
    first = m.run(p, execute=True, today="2026-10-28", security_runner=source)
    later = m.run(p, execute=True, today="2026-10-29", security_runner=source)
    assert not first["complete"] and later["complete"] and calls == ["2026-10-28"] * 2
    current = m.run(p, execute=True, today="2026-10-29", security_runner=source)
    assert current["cycle_day"] == "2026-10-29"
    assert m.run(p, execute=True, today="2026-10-29", security_runner=source)["complete"]
    assert (
        m.run(p, execute=True, today="2026-10-29", security_runner=source)["chunks_executed"] == 0
    )
    assert len(calls) == 4


def test_cycle_failure_rotation_stop_and_profile_pin(tmp_path):
    m = module()
    p = profile(tmp_path)
    data = json.loads(p.read_text())
    data["items"].append(dict(data["items"][0], id="other"))
    p.write_text(json.dumps(data))

    def failed(*args, **kwargs):
        raise OSError("temporary source failure")

    first = m.run(p, execute=True, today="2026-10-29", security_runner=failed)
    second = m.run(p, execute=True, today="2026-10-29", security_runner=failed)
    assert first["work"][0]["id"] == "minutes" and second["work"][0]["id"] == "other"
    (tmp_path / "store/STOP").touch()
    assert (
        m.run(p, execute=True, today="2026-10-29", security_runner=failed)["chunks_executed"] == 0
    )
    (tmp_path / "securities.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        m.run(p)


def test_exhausted_old_date_preserves_failure_then_allows_new_day_retry(tmp_path):
    m = module()
    p = profile(tmp_path)
    calls = []

    def source(path, **kwargs):
        calls.append(kwargs["as_of"])
        return {
            "all_requested_jobs_received": False,
            "attempted": 0,
            "ready_jobs": 1,
            "pending_jobs": 1,
            "result_path": "captured-failure",
            "items": [{"status": "可读取"}, {"status": "未完成"}],
        }

    first = m.run(p, execute=True, today="2026-10-28", security_runner=source)
    assert not first["cycle_settled"] and not first["complete"]
    old = m.run(p, execute=True, today="2026-10-29", security_runner=source)
    assert old["cycle_settled"] and not old["complete"]
    assert old["unresolved_items"] == ["minutes"]
    today = m.run(p, execute=True, today="2026-10-29", security_runner=source)
    assert today["cycle_day"] == "2026-10-29" and not today["cycle_settled"]
    assert calls == ["2026-10-28", "2026-10-28", "2026-10-29"]
    prior = json.loads((tmp_path / "store/completed/2026-10-28.json").read_text())
    assert not prior["complete"] and prior["items"]["minutes"]["retired_unresolved"]


def test_unattempted_tail_is_not_retired_at_midnight(tmp_path):
    m = module()
    p = profile(tmp_path)

    def source(path, **kwargs):
        return {
            "all_requested_jobs_received": False,
            "attempted": 0,
            "ready_jobs": 1,
            "pending_jobs": 1,
            "result_path": "pending",
            "items": [{"status": "可读取"}, {"status": "未尝试"}],
        }

    m.run(p, execute=True, today="2026-10-28", security_runner=source)
    for day in ("2026-10-29", "2026-10-30"):
        result = m.run(p, execute=True, today=day, security_runner=source)
        assert result["cycle_day"] == "2026-10-28" and not result["cycle_settled"]
        assert not result["unresolved_items"]


def test_financial_failed_old_scope_does_not_hold_new_release_day(tmp_path):
    m = module()
    p = profile(tmp_path)
    config = json.loads(p.read_text())
    config["items"][0]["kind"] = "financial"
    p.write_text(json.dumps(config))

    def source(path, **kwargs):
        return {
            "audit": {
                "all_tasks_have_source_results": False,
                "pending_or_retryable": 1,
                "task_states": {"complete": 1, "failed_retryable": 1},
            },
            "reports": 1,
            "batch": "immutable-batch",
        }

    m.run(p, execute=True, today="2026-10-28", financial_runner=source)
    result = m.run(p, execute=True, today="2026-10-29", financial_runner=source)
    assert result["cycle_settled"] and not result["complete"]
    assert result["items"]["minutes"]["last_result"]["task_states"]["failed_retryable"] == 1
