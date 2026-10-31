"""Recipe regression tests; synthetic responses are never live evidence."""
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


spec = importlib.util.spec_from_file_location("financial_scope", Path(__file__).resolve().parent / "support/run_legacy_financial_scope.py")
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


def profile(tmp_path):
    universe = tmp_path / "universe.json"
    universe.write_text(json.dumps({"instruments": [{"symbol": "sh600000", "ipo": "1999-11-10", "out": None, "board": "main"}],
                                    "code_events": [], "daily_metadata": {"source": "synthetic"}}))
    config = {"schema": "ad.legacy-financial-scope.v1", "source_universe": str(universe),
              "source_universe_sha256": hashlib.sha256(universe.read_bytes()).hexdigest(),
              "symbols": ["sh600000"], "start": "2025-01-01", "root": str(tmp_path / "scope")}
    target = tmp_path / "profile.json"
    target.write_text(json.dumps(config))
    return target


def fetch(url):
    kind = parse_qs(urlsplit(url).query)["source"][0]
    report = {"publish_date": "20261029", "rCurrency": "CNY", "rType": "合并期末",
              "data": [{"item_field": "unknown", "item_title": "原生未知字段", "item_value": "12.30", "item_source": kind}]}
    return 200, json.dumps({"result": {"status": {"code": 0}, "data": {"report_count": 1,
        "report_date": [{"date_value": "20260930"}], "report_list": {"20260930": report}}}}).encode(), {}


def test_daily_resume_and_new_day_preserve_source_fields(tmp_path):
    path = profile(tmp_path)
    first = recipe.run_profile(path, execute=True, today="2026-10-29", fetcher=fetch)
    assert first["reports"] == 3 and first["readback_verified"]
    exported = Path(first["batch"]) / "reports.jsonl"
    original = exported.read_bytes()
    def no_network(url):
        raise AssertionError("successful same-day jobs must not be downloaded again")
    repeated = recipe.run_profile(path, execute=True, today="2026-10-29", fetcher=no_network)
    assert repeated["audit"]["task_states"] == {"complete": 3} and exported.read_bytes() == original
    second = recipe.run_profile(path, execute=True, today="2026-10-30", fetcher=fetch)
    assert second["batch"] != first["batch"] and exported.read_bytes() == original
    record = json.loads(original.splitlines()[0])
    assert record["announcement_date"] == "2026-10-29" and record["published_at"] is None
    assert record["fields"][0]["raw_value"] == "12.30"


def test_fixed_identity_production_and_concurrent_scope_guards(tmp_path):
    path = profile(tmp_path)
    config = json.loads(path.read_text())
    root = Path(config["root"])
    root.mkdir()
    with (root / "scope.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            recipe.run_profile(path)
    (root / "CURRENT").write_text("existing writer")
    with pytest.raises(ValueError, match="production"):
        recipe.run_profile(path)
    (root / "CURRENT").unlink()
    Path(config["source_universe"]).write_text("{}")
    with pytest.raises(ValueError, match="universe changed"):
        recipe.run_profile(path)


def test_more_than_six_companies_bounded_resume_and_cross_day_refusal(tmp_path):
    path = profile(tmp_path)
    config = json.loads(path.read_text())
    universe_path = Path(config["source_universe"])
    universe = json.loads(universe_path.read_text())
    universe["instruments"] = [dict(universe["instruments"][0], symbol=f"sh60000{i}") for i in range(8)]
    universe_path.write_text(json.dumps(universe))
    config.update(symbols=[i["symbol"] for i in universe["instruments"]], max_jobs=3,
                  source_universe_sha256=hashlib.sha256(universe_path.read_bytes()).hexdigest())
    path.write_text(json.dumps(config))
    first=recipe.run_profile(path,execute=True,today="2026-10-29",fetcher=fetch)
    assert first["planned_jobs"]==24 and first["audit"]["task_states"]=={"complete":3,"pending":21}
    second=recipe.run_profile(path,execute=True,today="2026-10-29",fetcher=fetch,max_jobs=1)
    assert second["audit"]["task_states"]=={"complete":4,"pending":20}
    state={"quotes.sina.cn":{"blocked":"explicit source refusal","cooldown_until":9999999999}}
    (Path(first["batch"])/"source-state.json").write_text(json.dumps(state))
    def forbidden(url):
        raise AssertionError("a new day cannot bypass source refusal")
    later=recipe.run_profile(path,execute=True,today="2026-10-30",fetcher=forbidden)
    assert later["audit"]["task_states"]=={"pending":24}
