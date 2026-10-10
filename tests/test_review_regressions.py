"""rc2 regressions for independently reported envelope and observation-time defects."""
import copy
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from ashare_data import DataError, Store
from ashare_data.compat import JQStyle

SYM = "000001.XSHE"
START = "2026-04-09T14:53:00+08:00"
END = "2026-04-09T14:56:00+08:00"
CUTOFF = "2026-04-09T14:55:00+08:00"
ROOT = Path(__file__).parents[1]


def rejected(code, call):
    with pytest.raises(DataError) as exc:
        call()
    assert exc.value.code == code


def published(store, bundle, requirements, policy="synthetic"):
    batch = store.import_bundle(bundle)
    report = store.validate([batch], requirements=requirements, policy=policy)
    assert report["passed"]
    return store.publish(report["id"])


@pytest.mark.parametrize("injected", [None, "forged-system-value", {"nested": [None, "forged"]}])
def test_all_extra_metadata_is_namespaced_and_native_jq_envelopes_are_authoritative(
        store, bundle, requirements, injected):
    # Includes current system fields, namespace name itself, and a future unknown field.
    extra_names = {"snapshot_id", "as_of", "source_fields", "rows", "coverage", "units",
                   "quality", "timezone", "frequency", "adjustment", "compatibility",
                   "batches", "validation", "network", "future_system_metadata"}
    raw = bundle["instrument_sets"][0]
    raw.update({name: copy.deepcopy(injected) for name in extra_names})
    raw["scope"] = "all_a_shares"
    sid = published(store, bundle, requirements)
    expected_keys = {"snapshot_id", "as_of", "source_fields", "effective_date", "available_at",
                     "observed_at", "evidence", "scope", "instruments"}
    with store.snapshot(sid) as view:
        native = view.instruments(as_of=CUTOFF)
        jq = JQStyle(view, quality="synthetic").get_all_securities(date="2026-04-09")
        for result, cutoff in ((native, CUTOFF), (jq, "2026-04-09T23:59:59.999999+08:00")):
            assert set(result) == expected_keys
            assert result["snapshot_id"] == sid and result["as_of"] == cutoff
            assert result["source_fields"] == raw
            assert result["instruments"] == raw["instruments"]
        assert native["snapshot_id"] == view.lineage()["snapshot_id"]
        # Neither source dictionaries nor caller-mutated results can mutate the view.
        native["source_fields"]["snapshot_id"] = "mutated"
        native["instruments"][0]["name"] = "mutated"
        assert view.instruments(as_of=CUTOFF)["source_fields"] == raw


@pytest.mark.parametrize("quality,kind,semantics", [
    ("observed", "local_observation", "verified"),
    ("inferred", "local_reconstruction", "verified"),
    ("unverified", "local_observation", "unverified"),
    ("synthetic", "synthetic", "verified"),
])
@pytest.mark.parametrize("absent", [True, False])
def test_every_complete_bar_must_close_before_observation_even_without_visibility(
        store, bundle, quality, kind, semantics, absent):
    bundle["source"].update(kind=kind, time_semantics=semantics, observed_at="2026-04-09T14:00:00+08:00")
    for row in bundle["bars"]:
        row["quality"] = quality
        row["observed_at"] = "2026-10-08T12:00:00+08:00"  # Cannot replace the source observation time.
        if absent:
            row.pop("available_at")
        else:
            row["available_at"] = None
    rejected("INVALID_VISIBILITY", lambda: store.import_bundle(bundle))
    assert store.snapshots() == []
    assert list((store.root / "objects").iterdir()) == []


@pytest.mark.parametrize("observation", ["2026-04-09T14:56:00+08:00",
                                          "2026-04-09T06:56:00+00:00",
                                          "2026-10-08T12:00:00+08:00"])
@pytest.mark.parametrize("absent", [True, False])
def test_equal_close_or_late_backfill_is_valid_but_unknown_visibility_stays_unknown(
        store, bundle, requirements, observation, absent):
    bundle["source"].update(kind="local_observation", observed_at=observation)
    for row in bundle["bars"]:
        row["quality"] = "observed"
        if absent:
            row.pop("available_at")
        else:
            row["available_at"] = None
    sid = published(store, bundle, requirements, policy="observed")
    with store.snapshot(sid) as view:
        result = view.bars([SYM], START, END)
        assert len(result["rows"]) == 3
        assert all(r["available_at"] is None for r in result["rows"])
        for r in result["rows"]:
            assert datetime.fromisoformat(r["bar_end"]) <= datetime.fromisoformat(r["observed_at"])
        # Even a cutoff at observation time cannot infer an unknown available_at.
        rejected("COVERAGE_GAP", lambda: view.bars([SYM], START, END, as_of=observation))
        rejected("COVERAGE_GAP", lambda: JQStyle(view).get_price(SYM, CUTOFF, CUTOFF))


@pytest.mark.parametrize("available,observation,valid", [
    ("2026-04-09T14:54:00+08:00", "2026-04-09T14:54:00+08:00", True),
    ("2026-04-09T14:55:00+08:00", "2026-04-09T15:10:00+08:00", True),
    ("2026-04-09T14:53:59+08:00", "2026-04-09T15:10:00+08:00", False),
    ("2026-04-09T15:10:01+08:00", "2026-04-09T15:10:00+08:00", False),
])
def test_known_availability_orders_close_available_and_observed(
        store, bundle, available, observation, valid):
    bundle["source"].update(kind="local_observation", observed_at=observation)
    bundle["bars"] = bundle["bars"][:1]
    bundle["bars"][0].update(quality="observed", available_at=available)
    if not valid:
        rejected("INVALID_VISIBILITY", lambda: store.import_bundle(bundle))
        return
    close = "2026-04-09T14:54:00+08:00"
    req = [{"symbols": [SYM], "start": START, "end": close}]
    sid = published(store, bundle, req, policy="observed")
    with store.snapshot(sid) as view:
        assert len(view.bars(**req[0], as_of=available)["rows"]) == 1
        if available != close:
            rejected("COVERAGE_GAP", lambda: view.bars(**req[0], as_of=close))


@pytest.mark.parametrize("available", ["", False, 0])
def test_only_null_or_omitted_availability_means_unknown(store, bundle, available):
    bundle["bars"][0]["available_at"] = available
    rejected("INVALID_TIME", lambda: store.import_bundle(bundle))


def test_rc1_published_invalid_time_is_blocked_without_rewriting_old_snapshot(
        tmp_path, bundle, requirements):
    old_source = tmp_path / "rc1-src"
    fixture = ROOT / "tests/fixtures/legacy_rc1/src"
    shutil.copytree(fixture, old_source)
    bundle["source"].update(kind="local_observation", observed_at="2026-04-09T14:00:00+08:00")
    for row in bundle["bars"]:
        row.update(quality="observed", available_at=None)
    data = tmp_path / "input.json"
    data.write_text(json.dumps(bundle))
    root = tmp_path / "legacy"
    script = """import json,sys
from ashare_data import Store
s=Store.init(sys.argv[1]);b=s.import_bundle(json.load(open(sys.argv[2])))
r=s.validate([b],requirements=json.loads(sys.argv[3]))
assert r['passed']
print(json.dumps({'batch':b,'report':r['id'],'snapshot':s.publish(r['id'])}))
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "tests/fixtures/legacy_rc1/guard"), str(old_source)]),
               PYTHONDONTWRITEBYTECODE="1")
    child = subprocess.run([sys.executable, "-B", "-c", script, str(root), str(data), json.dumps(requirements)],
                           env=env, cwd=tmp_path, check=True, capture_output=True, text=True)
    ids = json.loads(child.stdout)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    current = Store(root)
    rejected("INVALID_VISIBILITY", lambda: current.snapshot(ids["snapshot"]))
    rejected("INVALID_VISIBILITY", lambda: current.validate([ids["batch"]], requirements=requirements))
    rejected("INVALID_VISIBILITY", lambda: current.publish(ids["report"]))
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
