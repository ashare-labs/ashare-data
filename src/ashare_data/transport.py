"""Bounded public HTTP reads and an optional, content-addressed response cache."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .model import DataError, require

_LOCK = threading.Lock()
_LAST_REQUEST = 0.0
MAX_BYTES = 2 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_read(url, timeout):
    """No retries, authentication, cookies, alternate hosts or credential lookup."""
    global _LAST_REQUEST
    with _LOCK:
        time.sleep(max(0, 1.0 - (time.monotonic() - _LAST_REQUEST)))
        _LAST_REQUEST = time.monotonic()
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "ashare-data/0.2.0 (public market data)",
                "Referer": "https://finance.sina.com.cn/",
            })
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                require(response.status == 200, "SOURCE_HTTP_ERROR", "源没有返回完整 HTTP 200 响应",
                        {"http_status": response.status, "url": url})
                body = response.read(MAX_BYTES + 1)
            require(len(body) <= MAX_BYTES, "SOURCE_RESPONSE_TOO_LARGE", "响应超过 2 MiB 上限")
            return body
        except urllib.error.HTTPError as exc:
            code = ("RATE_LIMITED" if exc.code == 429 else
                    "SOURCE_ACCESS_DENIED" if exc.code in {401, 403, 451} or 300 <= exc.code < 400
                    else "SOURCE_HTTP_ERROR")
            raise DataError(code, "公开源拒绝或未完成请求；不自动重试或换源",
                            {"http_status": exc.code, "url": url}) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise DataError("NETWORK_ERROR", "网络连接失败；不代表该源不支持此数据",
                            {"reason": str(exc), "url": url}) from exc


class Transport:
    def __init__(self, *, cache=None, cache_mode="prefer", cache_ttl=300, timeout=15):
        require(cache_mode in {"prefer", "only", "refresh"}, "INVALID_REQUEST", "无效 cache_mode")
        require(cache is not None or cache_mode != "only", "INVALID_REQUEST", "only 模式须提供 cache 路径")
        require(isinstance(cache_ttl, (int, float)) and 0 <= cache_ttl <= 86400,
                "INVALID_REQUEST", "cache_ttl 须为 0..86400 秒")
        require(isinstance(timeout, (int, float)) and 0 < timeout <= 60,
                "INVALID_REQUEST", "timeout 须为 0..60 秒")
        self.root = Path(cache).expanduser() if cache is not None else None
        self.mode, self.ttl, self.timeout = cache_mode, cache_ttl, timeout

    def read(self, url, decode):
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.root / "requests" / (key + ".json") if self.root else None
        if path and self.mode != "refresh" and path.exists():
            try:
                entry = json.loads(path.read_text())
                require(entry["url"] == url, "CACHE_CORRUPT", "缓存请求不匹配")
                content_hash = entry["sha256"]
                require(len(content_hash) == 64 and all(c in "0123456789abcdef" for c in content_hash),
                        "CACHE_CORRUPT", "缓存内容编号无效")
                body = (self.root / "objects" / content_hash).read_bytes()
                require(len(body) <= MAX_BYTES and hashlib.sha256(body).hexdigest() == content_hash,
                        "CACHE_CORRUPT", "缓存内容校验失败；可显式 refresh 重新获取")
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["observed_at"])).total_seconds()
                require(age >= 0, "CACHE_CORRUPT", "缓存观测时间在未来")
            except (KeyError, ValueError, TypeError, OSError) as exc:
                raise DataError("CACHE_CORRUPT", "缓存不可读；可显式 refresh 重新获取") from exc
            if self.mode == "only" or age <= self.ttl:
                return decode(body), {**entry, "cache_hit": True, "cache_age_seconds": age,
                                     "network_used": False}
        require(self.mode != "only", "CACHE_MISS", "离线缓存中没有这个请求；先联网调用同一接口")
        request_started_at = datetime.now(timezone.utc).isoformat()
        body = public_read(url, self.timeout)
        decoded = decode(body)  # Invalid/empty source replies never enter the cache.
        entry = {"schema": 1, "url": url, "http_status": 200, "sha256": hashlib.sha256(body).hexdigest(),
                 "request_started_at": request_started_at,
                 "observed_at": datetime.now(timezone.utc).isoformat()}
        if path:
            self._write(self.root / "objects" / entry["sha256"], body)
            self._write(path, json.dumps(entry, sort_keys=True).encode())
        return decoded, {**entry, "cache_hit": False, "cache_age_seconds": 0, "network_used": True}

    @staticmethod
    def _write(path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as out:
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
