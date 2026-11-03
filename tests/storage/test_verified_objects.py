"""Full-content checks are reused only while filesystem identity stays unchanged."""
import json
import os

import pytest

from ashare_data.core.model import DataError
from ashare_data.storage.market_release import sha_file
from ashare_data.storage.verified_objects import fingerprint, verify_cached


def test_full_hash_cache_reuse_and_same_mtime_tamper(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(cache))
    data = tmp_path / "object"
    data.write_bytes(b"original")
    expected = sha_file(data)
    calls = []
    def measured(path):
        calls.append(path)
        return sha_file(path)
    before = verify_cached(data, expected, 8, measured)
    assert verify_cached(data, expected, 8, measured) == before
    assert len(calls) == 1
    data.write_bytes(b"modified")
    os.utime(data, ns=(before[1], before[1]))
    assert fingerprint(data) != before
    with pytest.raises(DataError, match="Immutable object changed"):
        verify_cached(data, expected, 8, measured)
    assert len(calls) == 2


def test_bad_cache_rehashes_and_failed_hash_never_creates_receipt(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(cache))
    data = tmp_path / "object"
    data.write_bytes(b"original")
    with pytest.raises(DataError):
        verify_cached(data, "0" * 64, 8, sha_file)
    assert not list(cache.glob("*.json"))
    verify_cached(data, sha_file(data), 8, sha_file)
    receipt = next(cache.glob("*.json"))
    assert json.loads(receipt.read_bytes())["fingerprint"] == fingerprint(data)
    assert cache.stat().st_mode & 0o777 == 0o700
    assert receipt.stat().st_mode & 0o777 == 0o600
    receipt.write_text("invalid")
    calls = []
    verify_cached(data, sha_file(data), 8, lambda path: calls.append(path) or sha_file(path))
    assert len(calls) == 1


def test_changed_during_hash_never_creates_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(tmp_path / "cache"))
    data = tmp_path / "object"
    data.write_bytes(b"original")
    expected = sha_file(data)
    def changing(path):
        value = sha_file(path)
        path.write_bytes(b"modified")
        return value
    with pytest.raises(DataError, match="during verification"):
        verify_cached(data, expected, 8, changing)
    assert not list((tmp_path / "cache").glob("*.json"))
