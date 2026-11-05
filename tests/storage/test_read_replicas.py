"""Read replicas are bypassed so readers access target Parquet and SQLite directly."""

from types import SimpleNamespace

import pytest

from ashare_data.storage.market_release import MarketRelease, ReleaseWriter
from ashare_data.storage.read_replicas import consumer_metadata_replica, read_replica
from test_market_release import chunk, manifest, metadata


def copy_replica(directory, source, expected, suffix, mode=0o400):
    directory.mkdir(exist_ok=True, mode=0o700)
    target = directory / (expected + suffix)
    target.write_bytes(source.read_bytes())
    target.chmod(mode)
    return target


@pytest.fixture
def published(tmp_path):
    root = tmp_path / "market"
    with ReleaseWriter(root, free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        bar = chunk(writer, 10)
        pointer = writer.publish(manifest(meta, [bar]), expected_parent=None)
    return SimpleNamespace(root=root, meta=meta, bar=bar, pointer=pointer, directory=tmp_path / "replicas")


def test_read_replicas_bypassed_and_original_objects_used_directly(published, monkeypatch):
    monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(published.directory))
    assert read_replica("a" * 64, 10, ".json", None) is None
    assert consumer_metadata_replica("a" * 64) is None
    reader = MarketRelease(published.root)
    assert reader.metadata_path == published.root / published.meta["path"]
    assert reader.verify(reader.files[0]) == published.root / published.bar["path"]
    assert reader.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 10
