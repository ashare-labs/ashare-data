"""Manifest reads keep independent mutable reader state and direct release access."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, current_thread

import pytest

from ashare_data.storage.market_release import MarketRelease, ReleaseWriter
from test_market_release import chunk, manifest, metadata


@pytest.fixture
def published(tmp_path):
    with ReleaseWriter(tmp_path / "market", free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        writer.publish(manifest(meta, [chunk(writer, 10)]), expected_parent=None)
    return tmp_path / "market"


def test_reads_do_not_share_mutable_manifest(published):
    first, second = MarketRelease(published), MarketRelease(published)
    first.manifest["rules"]["fixed"] = "caller edit"
    first.files[0]["rows"] = 999
    first.files.append({"caller": "private"})
    assert second.manifest["rules"]["fixed"] == "version-one"
    assert len(second.files) == 1 and second.files[0]["rows"] == 1
    assert MarketRelease(published).manifest == second.manifest


def test_current_and_pinned_releases_read_directly(published):
    first = MarketRelease(published)
    with ReleaseWriter(published, free_reserve_bytes=0) as writer:
        writer.publish(manifest(first.manifest["metadata"], [chunk(writer, 20)]), expected_parent=1)
    second = MarketRelease(published)
    assert (first.release_id, second.release_id) == (1, 2)
    assert second.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 20
    assert first.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 10
    assert MarketRelease(published, 1, expected_manifest_sha256=first.sha256).sha256 == first.sha256


def test_concurrent_reads_keep_own_dict(published):
    barrier = Barrier(6)

    def read(_):
        barrier.wait(timeout=5)
        return MarketRelease(published)

    with ThreadPoolExecutor(max_workers=6) as pool:
        readers = list(pool.map(read, range(6)))
    assert {reader.sha256 for reader in readers} == {readers[0].sha256}
    assert len({id(reader.manifest) for reader in readers}) == 6


def test_current_advance_during_read_keeps_each_snapshot_fixed(published, monkeypatch):
    first = MarketRelease(published)
    captured, proceed = Event(), Event()
    original_read = Path.read_bytes

    def paused(path):
        raw = original_read(path)
        if path == published / "CURRENT.json" and current_thread().name.startswith("old-pin"):
            captured.set()
            assert proceed.wait(5)
        return raw

    monkeypatch.setattr(Path, "read_bytes", paused)
    with ReleaseWriter(published, free_reserve_bytes=0) as writer:
        second_file = chunk(writer, 20)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-pin") as pool:
            pending = pool.submit(MarketRelease, published)
            try:
                assert captured.wait(5)
                writer.publish(manifest(first.manifest["metadata"], [second_file]), expected_parent=1)
            finally:
                proceed.set()
            old = pending.result(timeout=5)
    current = MarketRelease(published)
    assert (old.release_id, current.release_id) == (1, 2)
    assert old.sha256 == first.sha256 != current.sha256
    assert old.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 10
    assert current.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 20


def test_large_manifest_cache_reuses_parsed_index_and_detects_caller_edits(published, monkeypatch):
    import ashare_data.storage.market_release as module

    monkeypatch.setattr(module, "_SMALL_MANIFEST_BYTES", 0)
    module._parsed_release_cache.clear()
    first = MarketRelease(published)
    second = MarketRelease(published)
    assert first.manifest is not second.manifest
    assert first.files is not second.files
    assert first._file_index is second._file_index
    first.manifest["rules"]["fixed"] = "caller edit"
    assert second.manifest["rules"]["fixed"] == "version-one"
    assert second.selected_files("bars_1d", 1, "2024-01-02", "2024-01-02") == [second.files[0]]
    assert second.selected_files("bars_1d", 999, "2024-01-02", "2024-01-02") == []
    first.files[0] = {
        **first.files[0],
        "instrument_id": 999,
        "first_date": "2024-01-02",
        "last_date": "2024-01-02",
    }
    assert len(first.selected_files("bars_1d", 999, "2024-01-02", "2024-01-02")) == 1
    assert first.selected_files("bars_1d", 1, "2024-01-02", "2024-01-02") == []
    assert second.selected_files("bars_1d", 1, "2024-01-02", "2024-01-02") == [second.files[0]]
    assert second.selected_files("bars_1d", 999, "2024-01-02", "2024-01-02") == []


def test_historical_release_cold_miss_rejects_wrong_expected_sha256(published):
    import ashare_data.storage.market_release as module
    from ashare_data.core.model import DataError

    r1_sha = MarketRelease(published).sha256
    with ReleaseWriter(published, free_reserve_bytes=0) as writer:
        writer.publish(
            manifest(MarketRelease(published).manifest["metadata"], [chunk(writer, 20)]),
            expected_parent=1,
        )
    module._parsed_release_cache.clear()
    with pytest.raises(DataError) as exc:
        MarketRelease(published, 1, expected_manifest_sha256="0" * 64)
    assert exc.value.code == "MARKET_RELEASE_INTEGRITY"
    assert MarketRelease(published, 1).sha256 == r1_sha
    assert MarketRelease(published, 1, expected_manifest_sha256=r1_sha).sha256 == r1_sha


def test_metadata_db_pool_handles_concurrency_and_state_reset(published):
    import sqlite3

    release = MarketRelease(published)
    pool_key, db = release.open_metadata_db()
    db.row_factory = None
    release.close_metadata_db(pool_key, db)

    pool_key2, db2 = release.open_metadata_db()
    assert db2.row_factory is sqlite3.Row
    db2.close()
    release.close_metadata_db(pool_key2, db2)

    def query_meta(_):
        r = MarketRelease(published)
        with r.metadata() as conn:
            row = conn.execute("SELECT security FROM release_instruments WHERE instrument_id=1").fetchone()
            return row["security"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(query_meta, range(16)))
    assert results == ["000001.XSHE"] * 16


