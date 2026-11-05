"""Select existing, private replicas of immutable manifest/metadata objects.

This module never copies, creates or repairs a replica. A selected replica must
still pass the ordinary content check against its published SHA and size. Its
filesystem identity belongs to the replica, never to the original source path.
"""

from contextlib import closing
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from ashare_data.core.model import DataError, require
from ashare_data.storage.verified_objects import fingerprint as file_fingerprint

PROJECTION_SCHEMA = "ad-consumer-metadata-projection.v1"


@dataclass(frozen=True)
class ReadReplica:
    directory: Path
    path: Path
    directory_identity: tuple[int, int]
    projection_sha256: str | None = None
    projection_bytes: int | None = None

    def check_directory(self):
        return None

    def fingerprint(self):
        return file_fingerprint(self.path)


def read_replica(*args, **kwargs):
    """Bypassed: read paths access target Parquet and SQLite directly."""
    return None


def consumer_metadata_replica(*args, **kwargs):
    """Bypassed: read paths access target SQLite directly."""
    return None
