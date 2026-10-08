"""One disposable, pinned SDK process. No credentials, retries or alternate servers."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import time

from ._bao_receipt.capture import bh, capture_query, save_capture, sh, utc_now


class BoundedSocket:
    """Also bound login, whose SDK loop otherwise swallows EOF indefinitely."""

    def __init__(self, sock, deadline):
        self.sock, self.deadline, self.received = sock, deadline, 0

    def _limit(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("total SDK deadline")
        self.sock.settimeout(min(5, left))

    def connect(self, address):
        if address != ("public-api.baostock.com", 10030):
            raise ValueError("only the anonymous free endpoint is allowed")
        self._limit()
        return self.sock.connect(address)

    def send(self, value, *args, **kwargs):
        self._limit()
        self.received = 0
        sent = self.sock.send(value, *args, **kwargs)
        if sent != len(value):
            raise ConnectionError("partial SDK send")
        return sent

    def recv(self, count, *args, **kwargs):
        self._limit()
        value = self.sock.recv(min(count, 8192), *args, **kwargs)
        if not value:
            raise ConnectionError("EOF before SDK frame completion")
        self.received += len(value)
        if self.received > 4 * 1024 * 1024:
            raise ValueError("SDK response byte limit")
        return value

    def __getattr__(self, name):
        return getattr(self.sock, name)


def sdk_identity(root):
    spec = json.loads(Path(__file__).with_name("baostock-sdk.json").read_text())
    root = Path(root).resolve()
    actual = {str(p.relative_to(root)) for p in (root / "baostock").rglob("*.py")}
    if actual != set(spec["files"]):
        raise ValueError("SDK source file set differs from pinned official 0.9.4")
    for name, expected in spec["files"].items():
        p = root / name
        if p.is_symlink() or p.stat().st_size > 2 * 1024 * 1024 or bh(p.read_bytes()) != expected:
            raise ValueError("SDK source differs from pinned official 0.9.4: " + name)
    # Bytecode is ignored: the verified bytes are explicitly compiled below.
    return spec


class PinnedLoader:
    def __init__(self, root, hashes):
        self.root, self.hashes = root, hashes

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "baostock" and not fullname.startswith("baostock."):
            return None
        base = fullname.replace(".", "/")
        name = base + "/__init__.py" if base + "/__init__.py" in self.hashes else base + ".py"
        if name not in self.hashes:
            raise ImportError("unlisted SDK module")
        spec = importlib.util.spec_from_loader(
            fullname, self, is_package=name.endswith("/__init__.py")
        )
        spec.origin = str(self.root / name)
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        p = Path(module.__spec__.origin)
        data = p.read_bytes()
        if bh(data) != self.hashes[str(p.relative_to(self.root))]:
            raise ImportError("SDK changed after validation")
        module.__file__ = str(p)
        exec(compile(data, str(p), "exec"), module.__dict__)


def run(config, out):
    out = Path(out)
    started = utc_now()
    identity = sdk_identity(config["sdk_path"])
    sys.meta_path.insert(0, PinnedLoader(Path(config["sdk_path"]).resolve(), identity["files"]))
    import baostock as bs
    import baostock.common.context as ctx
    import baostock.common.contants as constants

    if hasattr(ctx, "apiKey") or constants.BAOSTOCK_SERVER_IP != "public-api.baostock.com":
        raise ValueError("unexpected SDK context or free endpoint")
    deadline = time.monotonic() + config["timeout"]
    socket.setdefaulttimeout(min(5, config["timeout"]))
    socket_type = socket.socket
    socket.socket = lambda *a, **kw: BoundedSocket(socket_type(*a, **kw), deadline)
    meta = {
        "request": config["request"],
        "task_started_at": started,
        "sdk_identity": identity,
        "endpoint": "public-api.baostock.com:10030",
        "account": "anonymous",
        "retry_count": 0,
        "alternate_source": False,
        "evidence_kind": "live_local_capture",
    }
    transcript = io.StringIO()
    try:
        with contextlib.redirect_stdout(transcript):
            login = bs.login()
        meta["login"] = {
            "error_code": login.error_code,
            "error_msg": login.error_msg,
            "completed_at": utc_now(),
        }
        if login.error_code != "0":
            meta["status"] = "login_failed"
        else:
            # Wrap the live socket with a total monotonic deadline and EOF/byte guard.
            original = ctx.default_socket

            class DeadlineSocket:
                def send(self, data, *args, **kwargs):
                    self._limit()
                    return original.send(data, *args, **kwargs)

                def recv(self, count, *args, **kwargs):
                    self._limit()
                    return original.recv(count, *args, **kwargs)

                def _limit(self):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("total query deadline")
                    original.settimeout(min(5, left))

            ctx.default_socket = DeadlineSocket()
            result = capture_query(
                bs, ctx, config["request"], task_started_at=started, max_rows=128, max_pages=2
            )
            result["receipt"]["sdk_identity"] = identity
            result["receipt"].pop("receipt_id")
            result["receipt"]["receipt_id"] = sh(result["receipt"])
            for row in result["research_rows"]:
                row["receipt_id"] = result["receipt"]["receipt_id"]
            save_capture(out / "capture", result)
            meta["status"] = "captured"
            ctx.default_socket = original
    except Exception as exc:
        meta["status"] = "worker_error"
        meta["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        # Closing the anonymous connection requires no further request and is bounded.
        sock = getattr(ctx, "default_socket", None)
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()
        meta["worker_completed_at"] = utc_now()
        (out / "attempt.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        (out / "login-transcript.txt").write_text(transcript.getvalue())


if __name__ == "__main__":
    run(json.loads(sys.stdin.read()), sys.argv[1])
