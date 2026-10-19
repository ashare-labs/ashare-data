import sys
import importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest

from ashare_data import Store

spec = importlib.util.spec_from_file_location(
    "golden", Path(__file__).parents[1] / "examples" / "golden.py"
)
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)


@pytest.fixture(autouse=True)
def isolated_deployment_environment(monkeypatch, tmp_path):
    for key in ("ASHARE_DEPLOYMENT", "ASHARE_DATA_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))


@pytest.fixture
def bundle():
    return golden.bundle()


@pytest.fixture
def requirements():
    return golden.REQUIREMENTS


@pytest.fixture
def store(tmp_path):
    return Store.init(tmp_path / "store")


@pytest.fixture
def published(store, bundle, requirements):
    bid = store.import_bundle(bundle)
    report = store.validate([bid], requirements=requirements, policy="synthetic")
    sid = store.publish(report["id"])
    return store, bid, report, sid


@pytest.fixture
def synthetic_action_baseline(tmp_path, monkeypatch):
    """Explicit test-only pin substitution; never loaded by production defaults."""
    from ashare_data.observations import action_baseline as action_scope
    from synthetic_pilot import write_baselines

    root = tmp_path / "synthetic-actions"
    pins = write_baselines(root)
    monkeypatch.setenv(action_scope.EVIDENCE_ENV, str(root))
    monkeypatch.setattr(action_scope, "BASELINE_SHA256", pins["original"])
    monkeypatch.setattr(action_scope, "REPLAN_SHA256", pins["replan"])
    return root
