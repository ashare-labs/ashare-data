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
from urllib.parse import parse_qsl, urlsplit
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .model import DataError, canonical, digest, require, timestamp

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
        self._frozen = None
        self._received_by = None
        self.snapshot_id = None

    def freeze(self, *, received_by=None):
        """Pin the immutable observation ledger, including older bounded windows."""
        require(self.root is not None, "CACHE_REQUIRED", "策略时钟查询须先显式采集到缓存，再固定本地观测版本")
        paths = list((self.root / "requests").glob("*.json")) + list((self.root / "observations").glob("*.json"))
        require(len(paths) <= 10000, "BOUNDED_QUERY", "观测索引过多，请使用独立小范围缓存")
        entries, timestamps = {}, {}
        try:
            for path in paths:
                require(path.stat().st_size <= 65536, "CACHE_CORRUPT", "观测索引过大")
                entry = json.loads(path.read_text())
                observed = timestamp(entry["observed_at"])
                key = (entry["url"], observed)
                previous = timestamps.get(key)
                require(previous is None or previous == entry["sha256"], "OBSERVATION_CONFLICT", "同一请求和观测时刻存在冲突版本")
                timestamps[key] = entry["sha256"]
                entries[digest(entry)] = entry
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DataError("CACHE_CORRUPT", "不能固定观测索引") from exc
        frozen = Transport(cache=self.root, cache_mode="only", cache_ttl=self.ttl, timeout=self.timeout)
        frozen._frozen = sorted(entries.values(), key=lambda e: (e["url"], timestamp(e["observed_at"]), digest(e)))
        frozen._received_by = received_by
        frozen.snapshot_id = digest({"observations": frozen._frozen,
                                     "received_by": received_by.isoformat() if received_by else None})
        return frozen

    @staticmethod
    def _stream(url):
        parsed = urlsplit(url)
        params = dict(parse_qsl(parsed.query))
        size = params.pop("datalen", None)
        return (parsed.scheme, parsed.netloc, parsed.path, sorted(params.items())), size

    def _cached(self, entry, url, decode):
        try:
            entry = json.loads(canonical(entry))
            content_hash = entry["sha256"]
            require(len(content_hash) == 64 and all(c in "0123456789abcdef" for c in content_hash),
                    "CACHE_CORRUPT", "缓存内容编号无效")
            body = (self.root / "objects" / content_hash).read_bytes()
            require(len(body) <= MAX_BYTES and hashlib.sha256(body).hexdigest() == content_hash,
                    "CACHE_CORRUPT", "缓存内容校验失败；可显式 refresh 重新获取")
            age = (datetime.now(timezone.utc) - timestamp(entry["observed_at"])).total_seconds()
            require(age >= 0, "CACHE_CORRUPT", "缓存观测时间在未来")
        except (KeyError, ValueError, TypeError, OSError) as exc:
            raise DataError("CACHE_CORRUPT", "缓存不可读；可显式 refresh 重新获取") from exc
        return decode(body), {**entry, "cache_hit": True, "cache_age_seconds": age,
                              "requested_url": url, "cache_window_reused": entry["url"] != url,
                              "network_used": False}

    def read_windows(self, url, decode):
        """All pinned same-stream receipts; the bar layer resolves per-label versions.

        Exact URL and datalen do not imply recency or sufficient query coverage.
        Normal non-context reads retain their exact-request cache/refresh policy.
        """
        if self._frozen is None:
            return [self.read(url, decode)]
        signature, size = self._stream(url)
        candidates = [e for e in self._frozen if e["url"] == url or
                      (size is not None and self._stream(e["url"])[0] == signature
                       and str(self._stream(e["url"])[1]).isdigit())]
        require(candidates, "CACHE_MISS", "固定观测集合中没有这个来源/证券/频率的响应")
        available = [e for e in candidates if self._received_by is None or
                     timestamp(e["observed_at"]) <= self._received_by]
        # Retain one late receipt only to explain why no observation is visible.
        selected = available or [min(candidates, key=lambda e: timestamp(e["observed_at"]))]
        require(len(selected) <= 128, "BOUNDED_QUERY", "同一流最多合并128个固定观测；请缩小缓存范围")
        selected.sort(key=lambda e: (timestamp(e["observed_at"]), digest(e)))
        return [self._cached(e, url, decode) for e in selected]

    def read(self, url, decode):
        if self._frozen is not None:
            windows = self.read_windows(url, decode)
            require(len(windows) == 1, "WINDOW_SELECTION_REQUIRED", "多个固定观测须由行情查询层按标签选择，不能按窗口大小择一")
            return windows[0]
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.root / "requests" / (key + ".json") if self.root else None
        if path and self.mode != "refresh" and path.exists():
            try:
                entry = json.loads(path.read_text())
                require(entry["url"] == url, "CACHE_CORRUPT", "缓存请求不匹配")
            except (KeyError, ValueError, TypeError, OSError) as exc:
                raise DataError("CACHE_CORRUPT", "缓存请求索引不可读") from exc
            body, meta = self._cached(entry, url, lambda value: value)
            if self.mode == "only" or meta["cache_age_seconds"] <= self.ttl:
                return decode(body), meta
        require(self.mode != "only", "CACHE_MISS", "离线缓存中没有这个请求；先联网调用同一接口")
        request_started_at = datetime.now(timezone.utc).isoformat()
        body = public_read(url, self.timeout)
        decoded = decode(body)  # Invalid/empty source replies never enter the cache.
        entry = {"schema": 1, "url": url, "http_status": 200, "sha256": hashlib.sha256(body).hexdigest(),
                 "request_started_at": request_started_at,
                 "observed_at": datetime.now(timezone.utc).isoformat()}
        if path:
            previous = None
            if path.exists():
                try:
                    previous = json.loads(path.read_text()).get("observation_id")
                except (OSError, ValueError, AttributeError):
                    pass  # Explicit refresh may repair a broken index; it never rewrites an observation.
            entry["supersedes_observation"] = previous
            entry["observation_id"] = digest(entry)
            self._write(self.root / "objects" / entry["sha256"], body, immutable=True)
            self._write(self.root / "observations" / (entry["observation_id"] + ".json"), canonical(entry), immutable=True)
            self._write(path, json.dumps(entry, sort_keys=True).encode())
        return decoded, {**entry, "cache_hit": False, "cache_age_seconds": 0, "network_used": True}

    @staticmethod
    def _write(path, body, *, immutable=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as out:
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
            if immutable:
                try:
                    os.link(temp, path)
                except FileExistsError:
                    require(path.read_bytes() == body, "CACHE_CORRUPT", "不可变缓存对象已损坏，禁止覆盖")
            else:
                os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
