"""Warm manifest reads retain integrity and independent mutable reader state."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
from threading import Barrier, Event, current_thread

import pytest

from ashare_data.core.model import DataError
from ashare_data.storage import market_release as module
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter
from test_market_release import chunk, manifest, metadata


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(tmp_path / "verified"))
    monkeypatch.setattr(module, "_manifest_cache", None)
    with ReleaseWriter(tmp_path / "market", free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        writer.publish(manifest(meta, [chunk(writer, 10)]), expected_parent=None)
    return tmp_path / "market"


def measured_reads(monkeypatch, target):
    original, calls = Path.read_bytes, []

    def read(path):
        if path == target:
            calls.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    return calls


def test_warm_read_reuses_raw_bytes_without_sharing_mutable_manifest(published, monkeypatch):
    calls = measured_reads(monkeypatch, published / "releases/00000001.json")
    first, second = MarketRelease(published), MarketRelease(published)
    assert len(calls) == 1
    first.manifest["rules"]["fixed"] = "caller edit"
    first.files[0]["rows"] = 999
    first.files.append({"caller": "private"})
    assert second.manifest["rules"]["fixed"] == "version-one"
    assert len(second.files) == 1 and second.files[0]["rows"] == 1
    assert MarketRelease(published).manifest == second.manifest
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["same_size_restored_mtime", "replacement", "truncate"])
def test_changed_manifest_is_rehashed_and_refused(published, monkeypatch, change):
    path = published / "releases/00000001.json"
    original, info = path.read_bytes(), path.stat()
    calls = measured_reads(monkeypatch, path)
    first = MarketRelease(published)
    changed = original.replace(b"version-one", b"version-two")
    assert changed != original and len(changed) == len(original)
    if change == "replacement":
        replacement = path.with_suffix(".new")
        replacement.write_bytes(changed)
        os.replace(replacement, path)
    else:
        path.write_bytes(changed if change != "truncate" else b"{}")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(DataError, match="manifest chain changed"):
        MarketRelease(published)
    assert len(calls) == 2
    # A reader already bound to the original validated snapshot stays independent.
    assert first.manifest["rules"]["fixed"] == "version-one"
    assert module._manifest_cache is None


def test_current_and_expected_hash_are_never_served_from_old_cache(published):
    first = MarketRelease(published)
    with ReleaseWriter(published, free_reserve_bytes=0) as writer:
        writer.publish(manifest(first.manifest["metadata"], [chunk(writer, 20)]), expected_parent=1)
    second = MarketRelease(published)
    assert (first.release_id, second.release_id) == (1, 2)
    assert second.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 20
    assert first.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 10
    assert MarketRelease(published, 1, expected_manifest_sha256=first.sha256).sha256 == first.sha256
    pointer = published / "CURRENT.json"
    value = json.loads(pointer.read_bytes())
    value["sha256"] = "0" * 64
    pointer.write_text(json.dumps(value))
    with pytest.raises(DataError, match="manifest chain changed"):
        MarketRelease(published)


def test_concurrent_cold_read_checks_one_file_and_each_reader_keeps_own_dict(published, monkeypatch):
    calls = measured_reads(monkeypatch, published / "releases/00000001.json")
    barrier = Barrier(6)

    def read(_):
        barrier.wait(timeout=5)
        return MarketRelease(published)

    with ThreadPoolExecutor(max_workers=6) as pool:
        readers = list(pool.map(read, range(6)))
    assert len(calls) == 1
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


def test_manifest_changed_during_verification_is_not_cached(published, monkeypatch):
    path = published / "releases/00000001.json"
    original_read = Path.read_bytes

    def changing(target):
        raw = original_read(target)
        if target == path:
            target.write_bytes(raw + b" ")
        return raw

    monkeypatch.setattr(Path, "read_bytes", changing)
    with pytest.raises(DataError, match="during verification"):
        MarketRelease(published)
    assert module._manifest_cache is None


def test_cache_hit_rechecks_fingerprint_after_lookup(published, monkeypatch):
    MarketRelease(published)
    path = published / "releases/00000001.json"
    original, calls = module.fingerprint, []

    def changed(target):
        value = original(target)
        if target == path:
            calls.append(target)
            if len(calls) == 2:
                return [value[0] + 1, *value[1:]]
        return value

    monkeypatch.setattr(module, "fingerprint", changed)
    with pytest.raises(DataError, match="during cache access"):
        MarketRelease(published)
    assert module._manifest_cache is None


def test_cached_manifest_does_not_skip_metadata_object_verification(published, monkeypatch):
    calls = measured_reads(monkeypatch, published / "releases/00000001.json")
    first = MarketRelease(published)
    path = first.metadata_path
    raw, before = path.read_bytes(), path.stat()
    path.write_bytes(b"x" + raw[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(DataError, match="Immutable object changed"):
        MarketRelease(published)
    assert len(calls) == 1


def test_uncached_and_oversize_manifests_still_read_normally(published, monkeypatch):
    path = published / "releases/00000001.json"
    calls = measured_reads(monkeypatch, path)
    monkeypatch.setattr(module, "_MANIFEST_CACHE_BYTES", 1)
    assert MarketRelease(published).sha256 == MarketRelease(published).sha256
    assert len(calls) == 2 and module._manifest_cache is None
    monkeypatch.delenv("ASHARE_MARKET_VERIFY_CACHE")
    assert MarketRelease(published).sha256 == MarketRelease(published).sha256
    assert len(calls) == 4 and module._manifest_cache is None


def test_cache_retains_only_one_verified_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(tmp_path / "verified"))
    monkeypatch.setattr(module, "_manifest_cache", None)
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    first.write_bytes(b'{"one":1}')
    second.write_bytes(b'{"two":2}')
    calls = measured_reads(monkeypatch, first)
    for path in (first, second, first):
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        # Exclude this test's own digest read from observed reader I/O.
        before = len(calls)
        module._manifest_bytes(path, expected)
        if path == first:
            assert len(calls) == before + 1
