import importlib.util
from pathlib import Path

import pytest

from ashare_data import Store

spec = importlib.util.spec_from_file_location("golden", Path(__file__).parents[1] / "examples" / "golden.py")
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)


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
