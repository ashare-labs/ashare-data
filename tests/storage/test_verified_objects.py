"""Verified object helper checks regular file existence without full-file SHA-256 or SSD receipt cache."""

import pytest

from ashare_data.core.model import DataError
from ashare_data.storage.verified_objects import fingerprint, verify_cached


def test_verify_cached_returns_fingerprint_without_hashing_or_receipts(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(cache))
    data = tmp_path / "object"
    data.write_bytes(b"original")
    calls = []

    def unexpected(path):
        calls.append(path)
        return "0" * 64

    result = verify_cached(data, "a" * 64, 8, unexpected)
    assert result == fingerprint(data)
    assert calls == []
    assert not cache.exists()


def test_verify_cached_rejects_missing_file(tmp_path):
    with pytest.raises(DataError):
        verify_cached(tmp_path / "missing", "a" * 64, 8, lambda p: "a" * 64)
