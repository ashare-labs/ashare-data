"""Pinned readers open the target release manifest directly without recursive parent chain traversal."""

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_data.storage.market_release import MarketRelease, ReleaseWriter
from test_market_release import chunk, manifest, metadata


@pytest.fixture
def chain(tmp_path):
    root = tmp_path / "market"
    with ReleaseWriter(root, free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        first = writer.publish(manifest(meta, [chunk(writer, 10)]), expected_parent=None)
        second = writer.publish(manifest(meta, [chunk(writer, 20)]), expected_parent=1)
    return SimpleNamespace(
        root=root,
        meta=meta,
        first=first,
        second=second,
        selected_path=root / first["manifest"],
        parent_path=root / second["manifest"],
    )


def test_pinned_release_reads_target_manifest_without_parent_traversal(chain, monkeypatch):
    reads = Counter()
    read_bytes = Path.read_bytes

    def read(path):
        reads[path] += 1
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    reader = MarketRelease(chain.root, 1)
    assert reader.release_id == 1
    assert reads[chain.selected_path] == 1
    assert reads[chain.parent_path] == 0
    assert reader.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 10


def test_current_release_reads_current_manifest_without_ancestor_traversal(chain, monkeypatch):
    reads = Counter()
    read_bytes = Path.read_bytes

    def read(path):
        reads[path] += 1
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    reader = MarketRelease(chain.root)
    assert reader.release_id == 2
    assert reads[chain.parent_path] == 1
    assert reads[chain.selected_path] == 0
    assert reader.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 20
