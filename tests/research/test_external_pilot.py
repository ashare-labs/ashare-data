"""Public packages require separately held evidence and never trust test pins."""

from datetime import datetime

import pytest

from ashare_data.observations import action_baseline as action
from ashare_data.observations import phases as phase
from ashare_data.observations import intraday as intraday
from ashare_data.core.model import DataError
from synthetic_pilot import write_baselines


def test_default_requires_explicit_location(monkeypatch):
    monkeypatch.delenv(action.EVIDENCE_ENV, raising=False)
    with pytest.raises(DataError) as error:
        action.load_baseline()
    assert error.value.code == "ACTION_BASELINE_REQUIRED"


@pytest.mark.parametrize("replan", [False, True])
def test_missing_external_manifest_is_structured(tmp_path, monkeypatch, replan):
    monkeypatch.setenv(action.EVIDENCE_ENV, str(tmp_path))
    with pytest.raises(DataError) as error:
        action.load_baseline(replan=replan)
    assert error.value.code == "ACTION_BASELINE_MISSING"


@pytest.mark.parametrize("replan", [False, True])
def test_synthetic_corpus_cannot_replace_production_pins(tmp_path, monkeypatch, replan):
    write_baselines(tmp_path)
    monkeypatch.setenv(action.EVIDENCE_ENV, str(tmp_path))
    with pytest.raises(DataError) as error:
        action.load_baseline(replan=replan)
    assert error.value.code == "ACTION_BASELINE_CHANGED"


@pytest.mark.parametrize(
    "schema,kind",
    [
        (phase.SCHEMA, "admission"),
        (phase.CONDITIONAL_SCHEMA, "admission"),
        (phase.CONDITIONAL_SCHEMA, "opening"),
    ],
)
def test_missing_corpus_blocks_phase_before_network(tmp_path, monkeypatch, schema, kind):
    monkeypatch.delenv(action.EVIDENCE_ENV, raising=False)
    clock = "2026-10-14T08:55:00+08:00" if kind == "admission" else "2026-10-14T09:31:00+08:00"
    monkeypatch.setattr(phase, "now", lambda: datetime.fromisoformat(clock))
    calls = []
    monkeypatch.setattr(phase, "fetch", lambda *args: calls.append(args))
    result = phase.capture(tmp_path, kind, "2026-10-14", schema=schema)
    assert result["status"] == "blocked" and result["capture_id"] is None
    assert result["error"]["code"] == "ACTION_BASELINE_REQUIRED"
    assert calls == []


def test_missing_corpus_blocks_intraday_before_network(tmp_path, monkeypatch):
    monkeypatch.delenv(action.EVIDENCE_ENV, raising=False)
    monkeypatch.setattr(
        intraday, "now", lambda: datetime.fromisoformat("2026-10-14T10:03:30+08:00")
    )
    calls = []
    monkeypatch.setattr(phase, "fetch", lambda *args: calls.append(args))
    result = intraday.capture(tmp_path, "admission", "2026-10-14", "2026-10-14T10:00:00+08:00")
    assert result["status"] == "blocked" and result["capture_id"] is None
    assert result["error"]["code"] == "ACTION_BASELINE_REQUIRED"
    assert calls == []


def test_explicit_replan_uses_original_from_same_family(
    synthetic_action_baseline, monkeypatch, tmp_path
):
    monkeypatch.setenv(action.EVIDENCE_ENV, str(tmp_path / "unrelated"))
    manifest, _ = action.load_baseline(
        synthetic_action_baseline / "replan-20261014-15", replan=True
    )
    assert manifest["through_day"] == "2026-10-15"


def test_missing_blob_is_structured(synthetic_action_baseline):
    manifest, _ = action.load_baseline(synthetic_action_baseline)
    source = next(iter(manifest["sources"].values()))
    (synthetic_action_baseline / "blobs" / source["sha256"]).unlink()
    with pytest.raises(DataError) as error:
        action.load_baseline(synthetic_action_baseline)
    assert error.value.code == "ACTION_BASELINE_MISSING"
