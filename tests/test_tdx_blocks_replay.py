"""The block protocol can replay raw pages without network or SDK execution."""

from datetime import datetime
import gzip
import hashlib
import struct
import zlib

import pytest

from ashare_data.providers import tdx_blocks as blocks
from ashare_data.providers.free_update_coverage import CapturedResponses
from ashare_data.providers.free_update_worker import Capture, SourceBlocked


def wire(data, compressed=False):
    raw = zlib.compress(data) if compressed else data
    header = bytearray(16)
    struct.pack_into("<HH", header, 12, len(raw), len(data))
    return bytes(header) + raw


def member(code="600001"):
    data = bytearray(26 + 68)
    struct.pack_into("<H", data, 24, 1)
    struct.pack_into("<H", data, 26, 1)
    data[28:34] = code.encode()
    data[50:54] = b"Test"
    return bytes(data)


def test_block_replay_pins_url_protocol_hash_time_and_complete_window(tmp_path, monkeypatch):
    monkeypatch.setattr(
        blocks.socket,
        "create_connection",
        lambda *a, **k: pytest.fail("offline replay opened a socket"),
    )
    raw = wire(member(), True)
    sha = hashlib.sha256(raw).hexdigest()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / f"{sha}.gz").write_bytes(gzip.compress(raw))
    body = bytearray(43)
    struct.pack_into("<I", body, 0, blocks.exchange_code("880001"))
    body[19] = 80
    rec = {
        "url": f"tcp://{blocks.HOST}:7709/122c?request={body.hex()}",
        "protocol": "tdx",
        "status": 200,
        "received_at": "2026-10-29T08:00:00+00:00",
        "sha256": sha,
    }
    c = CapturedResponses(
        tmp_path,
        {"receipts": [rec]},
        datetime.fromisoformat("2026-10-29T07:00:00+00:00"),
        datetime.fromisoformat("2026-10-29T09:00:00+00:00"),
    )
    rows = blocks.collect(c, "tdx_members", {"block_code": "880001"})
    assert rows[0]["security"] == "sh600001" and c.index == 1
    assert (
        c.dataset_metadata["complete_returned_window"] and c.dataset_metadata["source_count"] == 1
    )
    assert not c.dataset_metadata["historical_constituents"]


def test_connection_failure_is_recorded_once_without_host_fallback(tmp_path, monkeypatch):
    calls = []

    def fail(target, **kwargs):
        calls.append(target)
        raise OSError("synthetic connection failure")

    monkeypatch.setattr(blocks.socket, "create_connection", fail)
    c = Capture(tmp_path)
    with pytest.raises(SourceBlocked, match="no host failover"):
        blocks.Client(c)
    with pytest.raises(SourceBlocked, match="cooling"):
        blocks.Client(c)
    assert calls == [(blocks.HOST, 7709)] and c.receipts[0]["status"] == "transport_error"


def test_truncated_compressed_protocol_does_not_pass():
    class Replay:
        def tdx(self, url):
            return wire(member(), True)[:-1]

    with pytest.raises(ValueError, match="byte count"):
        blocks.collect(Replay(), "tdx_members", {"block_code": "880001"})
