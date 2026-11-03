"""Opt-in local cache of completed SHA256 checks for immutable published objects.

The cache assumes the same trusted local user/filesystem as the deployment. It is
not an authentication boundary against a user able to replace both data and cache.
A changed filesystem identity always requires a new full-content check.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from ashare_data.core.model import require


def fingerprint(path):
    value = Path(path).stat()
    return [value.st_size, value.st_mtime_ns, value.st_ino, value.st_ctime_ns, value.st_dev]


def verify_cached(path, expected, size, hash_file):
    """Return a verified fingerprint; cache writes happen only after a full hash."""
    path = Path(path).resolve()
    configured = os.environ.get("ASHARE_MARKET_VERIFY_CACHE")
    if not configured:
        before = fingerprint(path)
        require(before[0] == size and hash_file(path) == expected,
                "MARKET_OBJECT_INTEGRITY", "Immutable object changed: " + str(path))
        require(fingerprint(path) == before, "MARKET_OBJECT_INTEGRITY",
                "Object changed during verification")
        return before
    cache = Path(configured).expanduser()
    require(cache.is_absolute(), "MARKET_VERIFY_CACHE", "Verification cache must be absolute")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = cache.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) & 0o077 == 0,
            "MARKET_VERIFY_CACHE", "Verification cache must be a private owned directory")
    identity = {"path": str(path), "sha256": expected, "bytes": size}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    receipt = cache / (key + ".json")
    # The lock prevents independent CLI/API consumers from hashing a huge object
    # simultaneously. It never locks or writes the source library/publisher.
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    with os.fdopen(os.open(cache / (key + ".lock"), flags, 0o600), "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = fingerprint(path)
        require(before[0] == size, "MARKET_OBJECT_INTEGRITY", "Immutable object size changed")
        expected_receipt = {"schema": "ad-verified-object.v1", **identity, "fingerprint": before}
        try:
            info = receipt.lstat()
            if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) & 0o077 == 0
                    and info.st_size <= 16384
                    and json.loads(receipt.read_bytes()) == expected_receipt):
                require(fingerprint(path) == before, "MARKET_OBJECT_INTEGRITY",
                        "Object changed while loading verification receipt")
                return before
        except (OSError, ValueError, TypeError):
            pass
        require(hash_file(path) == expected, "MARKET_OBJECT_INTEGRITY",
                "Immutable object changed: " + str(path))
        require(fingerprint(path) == before, "MARKET_OBJECT_INTEGRITY",
                "Object changed during verification")
        descriptor, temporary = tempfile.mkstemp(prefix=key + ".", dir=cache)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(expected_receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, receipt)
            descriptor = os.open(cache, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return before
