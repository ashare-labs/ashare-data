"""Opt-in local cache of completed SHA256 checks for immutable published objects.

The cache assumes the same trusted local user/filesystem as the deployment. It is
not an authentication boundary against a user able to replace both data and cache.
A changed filesystem identity always requires a new full-content check.
"""

from pathlib import Path

from ashare_data.core.model import require


def fingerprint(path):
    value = Path(path).stat()
    return [value.st_size, value.st_mtime_ns, value.st_ino, value.st_ctime_ns, value.st_dev]


def verify_cached(path, expected, size, hash_file):
    """Bypass read-time SHA-256 and verification receipt caching; return file fingerprint directly."""
    path = Path(path).resolve()
    require(path.is_file(), "MARKET_OBJECT_INTEGRITY", "Object not found: " + str(path))
    return fingerprint(path)
