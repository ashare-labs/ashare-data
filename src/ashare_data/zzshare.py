"""Opt-in, anonymous zzshare daily research. No SDK, fallback, PIT or execution."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .model import DataError, canonical, day, require, symbol, timestamp
from .storage import identifier, immutable_write, now
from .zzshare_types import SourceClaimedLimits, ZzshareDailyResult, ZzshareDailyRow, ZzshareValue

VERSION = "zzshare-research-1"
MAX_BYTES = 2 * 1024 * 1024
MAX_DAYS = 31
ENDPOINT = "https://api.zizizaizai.com/v3/market/kline/day/"
TABLE = """CREATE TABLE IF NOT EXISTS zzshare_captures
(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL
CHECK(status IN ('prepared','published','aborted')))"""
FILES = {"business.raw": MAX_BYTES, "business.receipt.json": 65536}


def _enabled(value):
    require(value is True, "ZZSHARE_DISABLED", "zzshare研究源默认关闭；需显式启用")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


class _Number(str):
    """The exact JSON number token, not a binary float or a quoted source string."""


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        require(key not in out, "ZZSHARE_SCHEMA", "源JSON包含重复键")
        out[key] = value
    return out


def _invalid_constant(value):
    raise ValueError("non-finite JSON constant")


def _json(raw, *, numbers=False):
    try:
        return json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=_invalid_constant,
            **({"parse_float": _Number, "parse_int": _Number} if numbers else {}),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DataError("ZZSHARE_SCHEMA", "不是有界有效JSON") from exc


def _source_json(value):
    if isinstance(value, _Number):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + _source_json(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_source_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _read(path, limit):
    try:
        p = Path(path)
        require(
            not p.is_symlink() and p.is_file() and p.stat().st_size <= limit,
            "ZZSHARE_INPUT",
            "原文文件缺失、超限或为符号链接",
        )
        with p.open("rb") as stream:
            raw = stream.read(limit + 1)
        require(len(raw) <= limit, "ZZSHARE_INPUT", "原文文件超限")
        return raw
    except OSError as exc:
        raise DataError("ZZSHARE_INPUT", "不能读取本地原文") from exc


def _scope(security, start, end):
    symbol(security)
    a, b = date.fromisoformat(day(start)), date.fromisoformat(day(end))
    require(0 <= (b - a).days < MAX_DAYS, "BOUNDED_REQUEST", "单次仅支持1–31自然日")
    code = security[:6] + (".SH" if security.endswith(".XSHG") else ".SZ")
    return code, a, b


def _request(url):
    require(isinstance(url, str), "ZZSHARE_RECEIPT", "请求URL无效")
    parts = urllib.parse.urlsplit(url)
    require(
        parts.scheme == "https"
        and parts.netloc == "api.zizizaizai.com"
        and not parts.fragment
        and parts.path.startswith("/v3/market/kline/day/"),
        "ZZSHARE_RECEIPT",
        "只接受已知匿名日线入口",
    )
    code = parts.path.removeprefix("/v3/market/kline/day/")
    require(re.fullmatch(r"\d{6}\.(SH|SZ)", code), "ZZSHARE_RECEIPT", "源证券无效")
    security = code[:6] + (".XSHG" if code.endswith(".SH") else ".XSHE")
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    params = dict(pairs)
    require(
        len(pairs) == len(params)
        and set(params) == {"candle_mode", "get_type", "start_date", "end_date", "limit"}
        and params["candle_mode"] == "0"
        and params["get_type"] == "range",
        "ZZSHARE_RECEIPT",
        "请求必须是显式模式0有界区间",
    )
    days = []
    for name in ("start_date", "end_date"):
        value = params[name]
        require(re.fullmatch(r"\d{8}", value), "ZZSHARE_RECEIPT", "请求日期无效")
        days.append(value[:4] + "-" + value[4:6] + "-" + value[6:])
    _, a, b = _scope(security, *days)
    require(
        re.fullmatch(r"[0-9]{1,2}", params["limit"])
        and (b - a).days + 1 <= int(params["limit"]) <= MAX_DAYS,
        "ZZSHARE_RECEIPT",
        "响应上限需覆盖请求自然日且不超过31",
    )
    return dict(
        security=security,
        source_security=code,
        start_date=a.isoformat(),
        end_date=b.isoformat(),
        mode=0,
        limit=int(params["limit"]),
    )


def _receipt(blobs):
    try:
        receipt = _json(blobs["business.receipt.json"])
        require(isinstance(receipt, dict), "ZZSHARE_RECEIPT", "回执应为对象")
        scope = _request(receipt["url"])
        require(
            receipt["method"] == "GET"
            and receipt["automatic_retries"] == 0
            and receipt["redirects"] is False
            and receipt["credentials_loaded"] is False
            and receipt["cookies_sent"] is False,
            "ZZSHARE_RECEIPT",
            "回执访问边界无效",
        )
        headers = receipt["request_headers"]
        require(
            isinstance(headers, dict)
            and headers.get("sdk-key") == "anonymous"
            and set(headers) <= {"sdk-key", "User-Agent", "Accept"},
            "ZZSHARE_RECEIPT",
            "仅允许公开匿名请求头",
        )
        require(
            type(receipt["body_bytes"]) is int
            and receipt["body_bytes"] == len(blobs["business.raw"])
            and receipt["body_sha256"] == _sha(blobs["business.raw"])
            and type(receipt["body_truncated"]) is bool,
            "INTEGRITY",
            "响应摘要、长度或截断标记无效",
        )
        require(
            receipt["state"] in {"response_saved", "request_failed_no_retry"},
            "ZZSHARE_RECEIPT",
            "未知请求状态",
        )
        require(
            timestamp(receipt["started_at"]) <= timestamp(receipt["finished_at"]),
            "ZZSHARE_RECEIPT",
            "请求时钟顺序无效",
        )
        if receipt["state"] == "response_saved":
            require(
                type(receipt["http_status"]) is int and 100 <= receipt["http_status"] <= 599,
                "ZZSHARE_RECEIPT",
                "HTTP状态无效",
            )
        return receipt, scope
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError("ZZSHARE_RECEIPT", "缺少或无效回执字段") from exc


def _rows(receipt, scope, raw):
    require(receipt["state"] == "response_saved", "ZZSHARE_NETWORK", "源连接失败，无重试")
    status = receipt["http_status"]
    details = {"http_status": status, "retry_after": receipt.get("retry_after")}
    require(status not in {401, 403}, "ZZSHARE_ACCESS_DENIED", "源拒绝访问，已停止", details)
    require(status != 429, "ZZSHARE_RATE_LIMITED", "源限频；未重试", details)
    require(not 300 <= status < 400, "ZZSHARE_REDIRECT", "拒绝自动重定向", details)
    require(status == 200, "ZZSHARE_HTTP", "源HTTP请求失败", details)
    require(not receipt["body_truncated"], "ZZSHARE_TRUNCATED", "源响应超过有界大小")
    headers = receipt.get("response_headers", {})
    require(isinstance(headers, dict), "ZZSHARE_RECEIPT", "响应头记录无效")
    lengths = [value for key, value in headers.items() if key.lower() == "content-length"]
    if lengths:
        require(
            len(lengths) == 1
            and isinstance(lengths[0], str)
            and re.fullmatch(r"[0-9]{1,20}", lengths[0])
            and int(lengths[0]) == len(raw),
            "ZZSHARE_LENGTH_MISMATCH",
            "HTTP声明长度与实际捕获不符；不视为完整响应",
        )
    data = _json(raw, numbers=True)
    try:
        require(
            isinstance(data, dict)
            and data.get("code") == _Number("200")
            and isinstance(data.get("code"), _Number),
            "ZZSHARE_API",
            "源业务代码不是200",
        )
        info = data["data"]
        require(
            isinstance(info, dict)
            and info.get("ts_code") == scope["source_security"]
            and type(info.get("ts_code")) is str
            and info.get("candle_mode") == _Number("0")
            and isinstance(info.get("candle_mode"), _Number),
            "ZZSHARE_SCHEMA",
            "证券或模式未匹配",
        )
        rows = info["list"]
        require(
            isinstance(rows, list)
            and len(rows) <= scope["limit"]
            and isinstance(info.get("count"), _Number)
            and info["count"] == str(len(rows)),
            "ZZSHARE_SCHEMA",
            "响应数量无效",
        )
        previous = None
        for row in rows:
            require(
                isinstance(row, dict) and row.get("ts_code") == scope["source_security"],
                "ZZSHARE_SCHEMA",
                "行证券不匹配",
            )
            value = row.get("trade_date")
            require(
                type(value) is str and re.fullmatch(r"\d{8}", value), "ZZSHARE_SCHEMA", "行日期无效"
            )
            d = day(value[:4] + "-" + value[4:6] + "-" + value[6:])
            require(
                scope["start_date"] <= d <= scope["end_date"]
                and (previous is None or previous < d),
                "ZZSHARE_SCHEMA",
                "行日期越界、重复或无序",
            )
            previous = d
        return rows
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError("ZZSHARE_SCHEMA", "源响应结构无效") from exc


def _value(row, field, *, flag=False, zero=False):
    if field not in row:
        return ZzshareValue(field, None, None, "field_missing")
    raw = row[field]
    if raw is None:
        return ZzshareValue(field, "null", None, "source_null")
    token = _source_json(raw)
    if isinstance(raw, _Number) and len(raw) <= 64:
        try:
            val = Decimal(raw)
            if flag and raw in ("0", "1"):
                return ZzshareValue(field, token, int(val), "source_claimed")
            if (
                not flag
                and val.is_finite()
                and (val >= 0 if zero else val > 0)
                and val < Decimal("1e17")
            ):
                return ZzshareValue(field, token, val, "source_claimed")
        except InvalidOperation:
            pass
    return ZzshareValue(field, token, None, "invalid_numeric_or_flag")


def _blobs(store, body, sid):
    require(_sha(body) == sid, "INTEGRITY", "清单摘要不匹配")
    try:
        manifest = _json(body)
        require(
            manifest["version"] == VERSION and len(manifest["artifacts"]) == 2,
            "INTEGRITY",
            "清单版本或文件数无效",
        )
        out = {}
        for item in manifest["artifacts"]:
            name = item["name"]
            require(name in FILES and name not in out, "INTEGRITY", "清单文件名无效")
            raw = _read(store.root / "zzshare-objects" / identifier(item["sha256"]), FILES[name])
            require(
                len(raw) == item["bytes"] and _sha(raw) == item["sha256"],
                "INTEGRITY",
                "对象摘要无效",
            )
            out[name] = raw
        _receipt(out)
        return manifest, out
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError("INTEGRITY", "清单无效") from exc


def snapshots(store, *, enable_research=False):
    _enabled(enable_research)
    with store._db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='zzshare_captures'").fetchone():
            return []
        return [
            dict(row)
            for row in db.execute(
                "SELECT id AS capture_id,status FROM zzshare_captures ORDER BY id"
            )
        ]


def _publish(store, blobs):
    _receipt(blobs)
    body = canonical(
        {
            "version": VERSION,
            "artifacts": [
                {"name": n, "sha256": _sha(b), "bytes": len(b)} for n, b in sorted(blobs.items())
            ],
        }
    )
    sid = _sha(body)
    with store._writer():
        for name in ("zzshare-objects", "zzshare-manifests"):
            (store.root / name).mkdir(exist_ok=True)
        with store._db(write=True) as db:
            db.execute(TABLE)
            prior = db.execute("SELECT status FROM zzshare_captures WHERE id=?", (sid,)).fetchone()
        if prior:
            require(prior[0] == "published", "ZZSHARE_RECOVERY_REQUIRED", "先显式恢复未完成发布")
            ZzshareView(store, sid, enable_research=True)
            return sid
        for raw in blobs.values():
            immutable_write(store.root / "zzshare-objects" / _sha(raw), raw)
        with store._db(write=True) as db:
            db.execute(
                "INSERT INTO zzshare_captures VALUES (?,?,?)", (sid, body.decode(), "prepared")
            )
        immutable_write(store.root / "zzshare-manifests" / (sid + ".json"), body)
        with store._db(write=True) as db:
            db.execute("UPDATE zzshare_captures SET status='published' WHERE id=?", (sid,))
    return sid


def import_capture(store, directory, *, enable_research=False):
    _enabled(enable_research)
    root = Path(directory)
    require(root.is_dir() and not root.is_symlink(), "ZZSHARE_INPUT", "需要真实本地捕获目录")
    blobs = {name: _read(root / name, size) for name, size in FILES.items()}
    return _publish(store, blobs)


def recover(store, *, enable_research=False):
    _enabled(enable_research)
    out = []
    with store._writer():
        for row in snapshots(store, enable_research=True):
            if row["status"] != "prepared":
                continue
            sid = identifier(row["capture_id"])
            with store._db() as db:
                body = (
                    db.execute("SELECT body FROM zzshare_captures WHERE id=?", (sid,))
                    .fetchone()[0]
                    .encode()
                )
            try:
                _blobs(store, body, sid)
                immutable_write(store.root / "zzshare-manifests" / (sid + ".json"), body)
                status = "published"
            except (DataError, OSError, ValueError, TypeError):
                status = "aborted"
            with store._db(write=True) as db:
                db.execute("UPDATE zzshare_captures SET status=? WHERE id=?", (status, sid))
            out.append({"capture_id": sid, "status": status})
    return out


class ZzshareView:
    def __init__(self, store, capture_id, *, enable_research=False):
        _enabled(enable_research)
        sid = identifier(capture_id)
        require(
            {"capture_id": sid, "status": "published"} in snapshots(store, enable_research=True),
            "ZZSHARE_NOT_PUBLISHED",
            "不存在该已发布的zzshare版本",
        )
        with store._db() as db:
            body = (
                db.execute("SELECT body FROM zzshare_captures WHERE id=?", (sid,))
                .fetchone()[0]
                .encode()
            )
        require(
            body == _read(store.root / "zzshare-manifests" / (sid + ".json"), 65536),
            "INTEGRITY",
            "磁盘清单与目录不一致",
        )
        self._manifest, self._data = _blobs(store, body, sid)
        self._receipt, self._scope = _receipt(self._data)
        self._id = sid

    def _source_rows(self):
        try:
            return _rows(self._receipt, self._scope, self._data["business.raw"])
        except DataError as exc:
            raise DataError(exc.code, exc.message, {**exc.details, "capture_id": self._id}) from exc

    def coverage(self):
        a, b = (date.fromisoformat(self._scope[k]) for k in ("start_date", "end_date"))
        expected = [(a + timedelta(days=n)).isoformat() for n in range((b - a).days + 1)]
        try:
            rows = self._source_rows()
            present = [
                r["trade_date"][:4] + "-" + r["trade_date"][4:6] + "-" + r["trade_date"][6:]
                for r in rows
            ]
            failure = None
        except DataError as exc:
            present, failure = [], exc.as_dict()
        return {
            "requested_dates": expected,
            "returned_dates": present,
            "missing_dates": [d for d in expected if d not in present],
            "requested_dates_present": failure is None and present == expected,
            "complete_market_coverage": False,
            "calendar_semantics": "missing natural day is UNKNOWN",
            "failure": failure,
        }

    def quality(self):
        return {
            "classification": "service_reported_unverified",
            "upstream_provider": "UNKNOWN",
            "server_limit_generation": "UNKNOWN",
            "source_limits": "source_claimed",
            "rule_derived_limits": None,
            "exchange_certified": False,
            "historical_pit": False,
            "historical_available_at": None,
            "complete_market_coverage": False,
            "events_complete": False,
            "execution_permission": False,
            "price_basis": "source_claimed_mode0",
            "receipt_authenticity": "locally_recorded_unattested",
        }

    def descriptor(self):
        return {
            "capture_id": self._id,
            "version": VERSION,
            "provider": "zzshare",
            "request": dict(self._scope),
            "network_used": False,
            "offline_reopen": True,
            "quality": self.quality(),
            "coverage": self.coverage(),
        }

    def lineage(self):
        return {
            "capture_id": self._id,
            "provider": "zzshare",
            "service": "api.zizizaizai.com",
            "upstream_provider": "UNKNOWN",
            "receipt": _json(self._data["business.receipt.json"]),
            "raw_response_sha256": _sha(self._data["business.raw"]),
            "raw_response_bytes": len(self._data["business.raw"]),
            "network_used": False,
            "evidence_status": "local HTTP capture, not external certification",
        }

    def get_daily(self, *, strict=True):
        require(type(strict) is bool, "INVALID_ARGUMENT", "strict必须是bool")
        records = self._source_rows()
        rows, unknown = [], []
        for index, row in enumerate(records):
            values = [_value(row, k) for k in ("open", "high", "low", "close", "prev_close")]
            values += [_value(row, k, zero=True) for k in ("volume", "turnover")]
            values += [_value(row, k, flag=True) for k in ("is_st", "is_paused")]
            upper, lower = _value(row, "high_limit"), _value(row, "low_limit")
            unknown.extend(
                {"date": row["trade_date"], "field": v.source_field, "state": v.state}
                for v in (*values, upper, lower)
                if v.value is None
            )
            o, h, low, close = (v.value for v in values[:4])
            require(
                any(x is None for x in (o, h, low, close))
                or low <= min(o, close) <= max(o, close) <= h,
                "ZZSHARE_PRICE_INVALID",
                "源OHLC关系无效",
                {"capture_id": self._id},
            )
            raw_day = row["trade_date"]
            d = date.fromisoformat(raw_day[:4] + "-" + raw_day[4:6] + "-" + raw_day[6:])
            rows.append(
                ZzshareDailyRow(
                    self._scope["security"],
                    d,
                    *values,
                    SourceClaimedLimits(upper, lower),
                    self._id,
                    _sha(self._data["business.raw"]),
                    f"/data/list/{index}",
                    self._receipt["started_at"],
                    self._receipt["finished_at"],
                    _source_json(row),
                )
            )
        coverage = self.coverage()
        report = {
            "capture_id": self._id,
            "provider": "zzshare",
            "network_used": False,
            "coverage": coverage,
            "quality": self.quality(),
            "unknown_fields": unknown,
            "raw_response_sha256": _sha(self._data["business.raw"]),
        }
        require(
            not strict or not coverage["missing_dates"],
            "ZZSHARE_COVERAGE_UNKNOWN",
            "请求自然日缺行；不推断交易日状态",
            report,
        )
        require(not strict or not unknown, "ZZSHARE_FIELDS_UNKNOWN", "存在缺失或无效源字段", report)
        return ZzshareDailyResult(self._id, tuple(rows), canonical(report).decode())

    def get_price(self, *, strict=True):
        return self.get_daily(strict=strict)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ZzshareSource:
    def __init__(self, *, enabled=False, timeout=15):
        require(type(enabled) is bool, "INVALID_ARGUMENT", "enabled必须是bool")
        require(
            type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30,
            "INVALID_ARGUMENT",
            "timeout需为0–30秒有限正数",
        )
        self._enabled, self._timeout = enabled, timeout

    @staticmethod
    def capabilities():
        return {
            "provider": "zzshare",
            "default_enabled": False,
            "sdk_required": False,
            "frequency": ["daily"],
            "mode": [0],
            "max_securities": 1,
            "max_natural_days": MAX_DAYS,
            "automatic_retries": 0,
            "automatic_source_fallback": False,
            "redirects": False,
            "historical_pit": False,
            "official_limits": False,
            "complete_market_coverage": False,
            "execution_permission": False,
            "raw_numbers": "JSON token + Decimal",
            "purpose": "explicit opt-in local research",
        }

    def fetch(self, store, *, security, start_date, end_date):
        _enabled(self._enabled)
        code, a, b = _scope(security, start_date, end_date)
        url = (
            ENDPOINT
            + code
            + "?"
            + urllib.parse.urlencode(
                dict(
                    candle_mode=0,
                    get_type="range",
                    start_date=a.strftime("%Y%m%d"),
                    end_date=b.strftime("%Y%m%d"),
                    limit=MAX_DAYS,
                )
            )
        )
        receipt = {
            "url": url,
            "method": "GET",
            "request_headers": {
                "sdk-key": "anonymous",
                "User-Agent": "ashare-data-research/0.8.5",
                "Accept": "application/json",
            },
            "started_at": now(),
            "timeout_seconds": self._timeout,
            "automatic_retries": 0,
            "redirects": False,
            "credentials_loaded": False,
            "cookies_sent": False,
            "state": "request_failed_no_retry",
            "body_truncated": False,
            "retry_after": None,
        }
        raw = b""
        try:
            # No environmental proxy credentials, cookie jar, token loading or auth handler.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            request = urllib.request.Request(url, headers=receipt["request_headers"], method="GET")
            try:
                response = opener.open(request, timeout=self._timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                receipt.update(
                    http_status=response.code,
                    response_headers={
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() not in {"set-cookie", "cookie", "authorization"}
                    },
                    retry_after=response.headers.get("Retry-After"),
                )
                raw = response.read(MAX_BYTES + 1)
                receipt["body_truncated"] = len(raw) > MAX_BYTES
                raw = raw[:MAX_BYTES]
                receipt["state"] = "response_saved"
        except (OSError, ValueError, http.client.HTTPException) as exc:
            partial = getattr(exc, "partial", b"")
            if isinstance(partial, bytes):
                raw = partial[:MAX_BYTES]
                receipt["body_truncated"] = len(partial) > MAX_BYTES
            receipt["error_type"] = type(exc).__name__
        receipt.update(finished_at=now(), body_bytes=len(raw), body_sha256=_sha(raw))
        sid = _publish(store, {"business.raw": raw, "business.receipt.json": canonical(receipt)})
        # Source failure never looks like a successful acquisition of prices.
        ZzshareView(store, sid, enable_research=True).get_daily()
        return sid
