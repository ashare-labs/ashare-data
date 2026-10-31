"""Serial anonymous daily batches using the already pinned BaoStock SDK.

Capture original frames; keep the old paginated protocol and receipts unchanged.
The daily APIs return one bulk frame, including when it contains exactly 2,000 rows.
"""

from contextlib import redirect_stdout, suppress
from datetime import datetime
import base64
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import time
from zoneinfo import ZoneInfo
import zlib

from ashare_data.providers.baostock_receipt.baostock_worker import BoundedSocket, PinnedLoader, sdk_identity
from ashare_data.providers.baostock_receipt.capture import SocketMonitor, utc_now
from ashare_data.providers.baostock_receipt.protocol import (
    END, SEP, MAX_BYTES, header, crc_checked_core, decode_request, decode_response, check_identity,
)
from ashare_data.storage.market_release import atomic_write, canonical

BULK = {
    "query_daily_history_k_AStock": ("98", "99"),
    "query_daily_history_k_ETF": ("9A", "9B"),
    "query_daily_adjust_factor": ("9C", "9D"),
}
PAGED = {"query_stock_basic", "query_all_stock", "query_trade_dates"}


def decode_bulk(raw, sent, request):
    """Check bounded bulk framing, request identity and row schema independently."""
    method, params = request["method"], request["params"]
    _, send_type, send_length = header(sent)
    core = crc_checked_core(sent[:-1])
    outgoing = core[21:].decode()
    parts = outgoing.split(SEP)
    if (not sent.endswith(b"\n") or len(outgoing) != send_length
            or send_type != BULK[method][0] or len(parts) != 3
            or parts[0] != method or parts[2] != params["date"]):
        raise ValueError("daily request identity mismatch")
    _, kind, size = header(raw)
    if kind != BULK[method][1] or not raw.endswith(END) or len(raw) > MAX_BYTES:
        raise ValueError("invalid daily response frame")
    compressed = raw[21:21 + size]
    decoder = zlib.decompressobj()
    body = decoder.decompress(compressed, MAX_BYTES + 1)
    if (len(body) > MAX_BYTES or not decoder.eof or decoder.unused_data
            or decoder.unconsumed_tail):
        raise ValueError("incomplete or oversized daily response")
    chunks = body.decode().rstrip("\n").split(SEP)
    if len(chunks) < 2 or chunks[0] != "0":
        raise ValueError("BaoStock daily error: " + repr(chunks[:2]))
    if len(chunks) != 6 or chunks[2] != method or chunks[3] != parts[1]:
        raise ValueError("daily response identity/schema mismatch")
    fields = chunks[5].split(",")
    if not all(fields) or len(set(fields)) != len(fields):
        raise ValueError("invalid daily fields")
    records = json.loads(chunks[4])["record"] if chunks[4] else []
    if not isinstance(records, list) or len(records) >= 20000:
        raise ValueError("daily bulk row bound reached")
    rows = []
    for row in records:
        if len(row) != len(fields) or not all(isinstance(x, str) for x in row):
            raise ValueError("invalid daily record")
        value = dict(zip(fields, row, strict=True))
        if "date" in value and value["date"] != params["date"]:
            raise ValueError("daily row outside requested date")
        rows.append(value)
    return rows, fields


class DailySession:
    """One connection, shared machine lock, bounded timeout and persistent send quota."""

    def __init__(self, sdk_path, root, *, timeout=90, interval=1.0, request_limit=48000, quota_path=None):
        if not 1 <= request_limit <= 50000 or interval < 1 or timeout <= 0:
            raise ValueError("invalid free-source budget")
        self.sdk_path, self.root = Path(sdk_path), Path(root)
        self.timeout, self.interval, self.request_limit = timeout, interval, request_limit
        self.quota_path = Path(quota_path) if quota_path else self.root / "request-quota.sqlite3"
        self.deadline, self.next_request = 0, 0
        self.root.mkdir(parents=True, exist_ok=True)

    def reserve(self):
        date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with self.quota:
            count = self.quota.execute("SELECT sends FROM quota WHERE day=?", (date,)).fetchone()
            if count and count[0] >= self.request_limit:
                raise RuntimeError("BaoStock daily request budget reached")
            self.quota.execute("INSERT INTO quota VALUES(?,1) ON CONFLICT(day) "
                               "DO UPDATE SET sends=sends+1", (date,))
        delay = max(0, self.next_request - time.monotonic())
        time.sleep(delay)
        self.next_request = time.monotonic() + self.interval

    def __enter__(self):
        try:
            return self._open()
        except BaseException:
            self.__exit__()
            raise

    def _open(self):
        self.lock = (Path(tempfile.gettempdir()) / f"ashare-baostock-{os.getuid()}.lock").open("a+b")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.quota = sqlite3.connect(self.quota_path)
        self.quota.execute("CREATE TABLE IF NOT EXISTS quota(day TEXT PRIMARY KEY,sends INTEGER)")
        self.identity = sdk_identity(self.sdk_path)
        self.loader = PinnedLoader(self.sdk_path, self.identity["files"])
        sys.meta_path.insert(0, self.loader)
        import baostock as bs
        import baostock.common.context as ctx
        import baostock.common.contants as constants
        if hasattr(ctx, "apiKey") or constants.BAOSTOCK_SERVER_IP != "public-api.baostock.com":
            raise ValueError("anonymous free endpoint required")
        self.bs, self.ctx = bs, ctx
        self.original_socket = socket.socket
        owner = self

        class TimedSocket(BoundedSocket):
            def _limit(self):
                left = owner.deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("BaoStock total request deadline")
                self.sock.settimeout(min(30, left))

            def send(self, data, *a, **kw):
                owner.reserve()
                return super().send(data, *a, **kw)

        socket.socket = lambda *a, **kw: TimedSocket(self.original_socket(*a, **kw), 0)
        self.deadline = time.monotonic() + self.timeout
        transcript = io.StringIO()
        try:
            with redirect_stdout(transcript):
                response = bs.login()  # Public SDK's documented anonymous defaults only.
            if response.error_code != "0":
                raise ConnectionError("BaoStock anonymous login: " + response.error_code)
        except BaseException as exc:
            body = canonical(dict(status="login_failed", error=str(exc),
                                  sdk_message=transcript.getvalue(), observed_at=utc_now(),
                                  endpoint="public-api.baostock.com:10030", account="anonymous"))
            atomic_write(self.root / (hashlib.sha256(body).hexdigest() + ".failure.json"), body)
            self.__exit__()
            raise
        return self

    def __exit__(self, *args):
        if hasattr(self, "ctx"):
            with suppress(Exception):
                self.ctx.default_socket.close()
        if hasattr(self, "original_socket"):
            socket.socket = self.original_socket
        if hasattr(self, "loader") and self.loader in sys.meta_path:
            sys.meta_path.remove(self.loader)
        if hasattr(self, "quota"):
            self.quota.close()
        if hasattr(self, "lock"):
            self.lock.close()

    def query(self, method, params):
        if method not in BULK and method not in PAGED:
            raise ValueError("unsupported daily-update API")
        request = dict(method=method, params=params)
        self.deadline = time.monotonic() + self.timeout
        original = self.ctx.default_socket
        monitor = SocketMonitor(original, utc_now, 1 if method in BULK else 20)
        self.ctx.default_socket = monitor
        started = utc_now()
        error = None
        try:
            with redirect_stdout(io.StringIO()):
                result = getattr(self.bs, method)(**params)
                if result.error_code != "0":
                    raise RuntimeError("BaoStock query error " + result.error_code + ": " + result.error_msg)
                if method not in BULK:
                    count = 0
                    while result.next():
                        result.get_row_data()
                        count += 1
                        if count >= 30000:
                            raise ValueError("universe query row budget reached")
                    if result.error_code != "0":
                        raise RuntimeError("incomplete BaoStock page")
            if monitor.faults or not monitor.exchanges:
                raise ConnectionError("incomplete BaoStock transport")
            rows, fields = [], []
            for number, ex in enumerate(monitor.exchanges, 1):
                raw, sent = b"".join(ex["chunks"]), ex["request_bytes"]
                if method in BULK:
                    page, fields = decode_bulk(raw, sent, request)
                else:
                    decoded = decode_response(raw)
                    identity = check_identity(request, decode_request(sent), decoded, number)
                    if decoded["error_code"] != "0" or identity["issues"]:
                        raise ValueError("universe response identity mismatch")
                    page, fields = decoded["rows"], decoded["fields"]
                    if number < len(monitor.exchanges) and len(page) != 2000:
                        raise ValueError("invalid universe page sequence")
                rows.extend(page)
            if method not in BULK and len(page) >= 2000:
                raise ValueError("universe terminal page missing")
        except Exception as exc:
            error = str(exc)
            rows, fields = [], []
        finally:
            self.ctx.default_socket = original
        envelope = dict(schema="ad-baostock-daily-capture.v1", request=request,
                        started_at=started, observed_at=utc_now(), status="error" if error else "ok",
                        error=error, rows=rows, fields=fields, sdk_version=self.identity["version"],
                        sdk_wheel_sha256=self.identity["wheel_sha256"],
                        endpoint="public-api.baostock.com:10030", account="anonymous",
                        frame_integrity="bounded original frames; compressed outer CRC unverified",
                        frames=[dict(request=base64.b64encode(ex["request_bytes"]).decode(),
                                     response=base64.b64encode(b"".join(ex["chunks"])).decode())
                                for ex in monitor.exchanges])
        body = canonical(envelope)
        digest = hashlib.sha256(body).hexdigest()
        path = self.root / (digest + ".json.gz")
        if not path.exists():
            atomic_write(path, gzip.compress(body, mtime=0))
        return dict(envelope, capture_path=str(path), capture_sha256=digest,
                    capture_hash_scope="uncompressed capture envelope")
