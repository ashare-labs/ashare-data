import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def load(name):
    path = Path(__file__).resolve().parents[1] / "support" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = load("run_verified_free_updates")
minute = load("audit_futures_minute_alignment")


def frozen(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text('version="fixture"')
    config, plist, python = [tmp_path / name for name in ("config.json", "plist", "python")]
    config.write_text(json.dumps({"root": "staging", "rows": [155]}))
    plist.write_text("fixture only")
    python.write_text("fixture only; never executed")
    runtime = {"uid": os.getuid(), "editable": False, "package_origin": str(source), "python": str(python),
               "frozen_package_files": {"__init__.py": manual.sha(source)}, "version": "fixture",
               "config_sha256": manual.sha(config), "plist_sha256": manual.sha(plist)}
    bounds = {"config": str(config), "plist": str(plist), "staging_root": str(tmp_path / "staging"), "rows": [155]}
    runtime_path, boundary_path = tmp_path / "runtime.json", tmp_path / "boundary.json"
    runtime_path.write_text(json.dumps(runtime))
    boundary_path.write_text(json.dumps(bounds))
    return runtime_path, boundary_path, source


@pytest.mark.parametrize("change", ["package", "extra_module", "config", "uid"])
def test_drift_is_detected_before_any_collector_call(tmp_path, monkeypatch, change):
    runtime, bounds, source = frozen(tmp_path)
    if change == "package":
        source.write_text("changed")
    if change == "extra_module":
        (source.parent / "unreviewed.py").write_text("changed")
    if change == "config":
        (tmp_path / "config.json").write_text("changed")
    if change == "uid":
        monkeypatch.setattr(manual.os, "getuid", lambda: -1)
    monkeypatch.setattr(manual.subprocess, "run", lambda *a, **k: pytest.fail("must not start collector"))
    with pytest.raises(ValueError):
        manual.preflight(runtime, bounds)


def test_partial_iteration_is_failure_and_does_not_export_old_data(tmp_path, monkeypatch):
    runtime, bounds, _ = frozen(tmp_path)
    monkeypatch.setattr(manual.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=2, stdout='{"all_requested_jobs_received":false}\n', stderr="fixture timeout"))
    output = tmp_path / "out"
    result = manual.main(["--runtime-manifest", str(runtime), "--boundaries", str(bounds),
                          "--output", str(output), "--execute"])
    assert result == 2 and not list(output.rglob("*.csv"))
    pointer = json.loads((output / "latest.json").read_text())
    assert pointer["status"] == "未完成"
    assert not json.loads(Path(pointer["result"]).read_text())["timer_installed"]


def bar(label, volume="1"):
    return dict(datetime="2026-10-28 " + label, open="10", high="11", low="9", close="10", volume=volume, hold="8")


def test_minute_comparison_catches_volume_disagreement_and_skips_incomplete_buckets():
    one = [bar("09:01:00"), bar("09:02:00"), bar("09:03:00")]
    result = minute.compare(one, [bar("09:03:00", "99")], 3, "2026-10-29")
    assert result["right_closed"]["mismatch_fields"] == {"volume": 1}
    assert result["right_closed"]["exact_matches"] == 0
    result = minute.compare(one[:1] + one[2:], [bar("09:03:00", "3")], 3, "2026-10-29")
    assert result["right_closed"]["excluded"]["incomplete_bucket_including_session_breaks"] == 1


def test_current_calendar_day_cannot_supply_closed_bar_evidence():
    result = minute.compare([bar("09:01:00")], [bar("09:01:00")], 1, "2026-10-28")
    assert result["right_closed"]["excluded"] == {"current_or_later_calendar_day": 1}
    assert result["right_closed"].get("compared", 0) == 0
