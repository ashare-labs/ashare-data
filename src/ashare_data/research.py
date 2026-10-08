"""Durable, bounded research datasets. Raw receipts, never a trading permission.

The original strict Parquet snapshots remain separate. This small vertical seals
source JSON plus a SQLite publication journal; it does not aggregate daily bars.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pandas as pd

from .contracts import CoverageContract
from .live import Client, DEFAULT_FIELDS, FREQUENCIES, KLINE_URL, _bars, _decimal, _dt, _security
from .model import DataError, TZ, canonical, digest, require, timestamp, validate_ohlc
from .storage import identifier, immutable_write, now
from .transport import MAX_BYTES, Transport

VERSIONS = {
    "schema_version": 1,
    "parser_version": "research-1",
    "policy_version": "research-1/dev4-selector",
}
TABLE = """CREATE TABLE IF NOT EXISTS research_snapshots
(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('prepared','published','aborted')), created_at TEXT NOT NULL)"""
MAX_OBJECTS = 128
MAX_ROWS = 10000


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _json(body):
    try:
        return json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise DataError("SOURCE_SCHEMA_ERROR", "源原文不是有效 JSON") from exc


def _read(path, limit=MAX_BYTES):
    try:
        path = Path(path)
        require(
            path.is_file() and path.stat().st_size <= limit,
            "BOUNDED_IMPORT",
            "文件缺失或超过有界大小",
        )
        with path.open("rb") as stream:
            value = stream.read(limit + 1)
        require(len(value) <= limit, "BOUNDED_IMPORT", "文件超过有界大小")
        return value
    except OSError as exc:
        raise DataError("SOURCE_FILE_ERROR", "本地输入无法读取") from exc


def _day(value):
    require(
        isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
        "SOURCE_SCHEMA_ERROR",
        "源日期须为 YYYY-MM-DD",
    )
    return _dt(value).date()


def _table_exists(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_snapshots'"
    ).fetchone()


def snapshots(store):
    with store._db() as db:
        if not _table_exists(db):
            return []
        return [
            dict(r)
            for r in db.execute(
                "SELECT id AS dataset_id,status,created_at FROM research_snapshots ORDER BY id"
            )
        ]


def _sina_entry(entry, body):
    require(isinstance(entry, dict), "INTEGRITY", "观测索引格式无效")
    try:
        parsed = urlsplit(entry["url"])
        params_list = parse_qsl(parsed.query)
        params = dict(params_list)
        require(
            parsed._replace(query="").geturl() == KLINE_URL
            and len(params_list) == len(params)
            and set(params) == {"symbol", "scale", "ma", "datalen"}
            and params["ma"] == "no"
            and params["scale"] in {"1", "5", "240"}
            and params["datalen"].isdigit()
            and 1 <= int(params["datalen"]) <= 1023,
            "UNSUPPORTED_SOURCE",
            "仅允许既有新浪 KLine 有界原文",
        )
        source_symbol = params["symbol"]
        require(re.fullmatch(r"(?:sh|sz)\d{6}", source_symbol), "SOURCE_SCHEMA_ERROR", "源代码无效")
        security = source_symbol[2:] + (".XSHG" if source_symbol[:2] == "sh" else ".XSHE")
        require(_security(security) == source_symbol, "SOURCE_SCHEMA_ERROR", "源代码不匹配")
        received = timestamp(entry["observed_at"])
        started = timestamp(entry.get("request_started_at", entry["observed_at"]))
        require(
            started <= received <= datetime.now(TZ)
            and entry.get("http_status") == 200
            and entry.get("sha256") == _sha(body),
            "INTEGRITY",
            "响应时间、状态或 hash 无效",
        )
        rows = _bars(body, int(params["scale"]))
        return security, "daily" if params["scale"] == "240" else params["scale"] + "m", rows
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError("INTEGRITY", "新浪观测索引字段无效") from exc


def _bao(body, *, calendar=False):
    payload = _json(body)
    try:
        require(
            isinstance(payload, dict) and payload.get("error_code") == "0",
            "SOURCE_INCOMPLETE_RESPONSE",
            "只接纳显式成功的旧本地响应；不赋予接收时间证据",
        )
        params, rows = payload["params"], payload["rows"]
        require(
            isinstance(params, dict) and isinstance(rows, list) and 0 < len(rows) <= MAX_ROWS,
            "SOURCE_SCHEMA_ERROR",
            "响应参数或记录数无效",
        )
        start, end = _day(params["start_date"]), _day(params["end_date"])
        require(
            start <= end and (end - start).days <= 366,
            "BOUNDED_IMPORT",
            "单个导入请求最多367个自然日",
        )
        fields = payload["fields"]
        require(
            isinstance(fields, list)
            and all(isinstance(f, str) for f in fields)
            and len(fields) == len(set(fields)),
            "SOURCE_SCHEMA_ERROR",
            "源字段声明无效",
        )
        if calendar:
            require(
                payload["method"] == "query_trade_dates"
                and {"calendar_date", "is_trading_day"} <= set(fields),
                "SOURCE_SCHEMA_ERROR",
                "只允许源交易日历响应",
            )
            days = {}
            for row in rows:
                require(
                    isinstance(row, dict) and set(row) == set(fields),
                    "SOURCE_SCHEMA_ERROR",
                    "日历字段不匹配",
                )
                d = _day(row["calendar_date"])
                require(
                    start <= d <= end
                    and d.isoformat() not in days
                    and row["is_trading_day"] in {"0", "1"},
                    "SOURCE_SCHEMA_ERROR",
                    "日历日期、重复或状态无效",
                )
                days[d.isoformat()] = row["is_trading_day"] == "1"
            require(
                len(days) == (end - start).days + 1,
                "SOURCE_SCHEMA_ERROR",
                "日历响应缺自然日，不能用于源网格检查",
            )
            return days
        require(
            payload["method"] == "query_history_k_data_plus"
            and params["frequency"] == "d"
            and params["adjustflag"] == "3",
            "UNSUPPORTED_SOURCE",
            "只接纳 Bao 原价日线 flag3",
        )
        code = params["code"]
        require(
            isinstance(code, str) and re.fullmatch(r"(?:sh|sz)\.\d{6}", code),
            "SOURCE_SCHEMA_ERROR",
            "请求代码无效",
        )
        security = code[3:] + (".XSHG" if code.startswith("sh.") else ".XSHE")
        require(
            _security(security) == code.replace(".", ""), "SOURCE_SCHEMA_ERROR", "请求代码不匹配"
        )
        required = {
            "date",
            "code",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "adjustflag",
            "tradestatus",
        }
        require(
            required <= set(fields) and params["fields"].split(",") == fields,
            "SOURCE_SCHEMA_ERROR",
            "请求与响应字段不符",
        )
        previous = None
        result = []
        for row in rows:
            require(
                isinstance(row, dict)
                and set(row) == set(fields)
                and all(isinstance(v, str) for v in row.values()),
                "SOURCE_SCHEMA_ERROR",
                "保留原始字符串字段；行结构不符",
            )
            label = _day(row["date"])
            require(
                row["code"] == code and row["adjustflag"] == "3",
                "SOURCE_IDENTITY_MISMATCH",
                "响应证券或价格口径与请求不符",
            )
            require(
                start <= label <= end and (previous is None or label > previous),
                "SOURCE_SCHEMA_ERROR",
                "行日期越界、重复或无序",
            )
            previous = label
            require(row["tradestatus"] in {"0", "1"}, "SOURCE_SCHEMA_ERROR", "交易状态字段无效")
            values = {key: _decimal(row[key]) for key in ("open", "high", "low", "close", "volume")}
            validate_ohlc(
                *(values[key] for key in ("open", "high", "low", "close")),
                code="SOURCE_SCHEMA_ERROR",
            )
            require(
                values["volume"] == values["volume"].to_integral_value(),
                "SOURCE_SCHEMA_ERROR",
                "成交量须为整数股",
            )
            if row["amount"]:
                _decimal(row["amount"])
            result.append(
                {
                    "security": security,
                    "source_label": row["date"],
                    "source_row": row,
                    "raw_record_sha256": digest(row),
                    "object_sha256": _sha(body),
                    "available_at": None,
                    "response_completed_at": None,
                    "bar_start": None,
                    "bar_end": None,
                }
            )
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError("SOURCE_SCHEMA_ERROR", "Bao 本地响应字段不完整或无效") from exc


def _validate(manifest, objects):
    try:
        return _validate_impl(manifest, objects)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError("INTEGRITY", "研究清单结构无效") from exc


def _validate_impl(manifest, objects):
    require(
        isinstance(manifest, dict)
        and all(
            type(manifest.get(k)) is type(v) and manifest.get(k) == v for k, v in VERSIONS.items()
        ),
        "RESEARCH_VERSION_UNSUPPORTED",
        "数据集 schema/parser/policy 版本不兼容",
    )
    require(
        manifest.get("source") in {"sina_public", "baostock_daily"},
        "UNSUPPORTED_SOURCE",
        "不支持的研究来源",
    )
    require(
        isinstance(manifest.get("objects"), list) and 0 < len(manifest["objects"]) <= MAX_OBJECTS,
        "INTEGRITY",
        "研究对象列表无效",
    )
    require(
        len(set(manifest["objects"])) == len(manifest["objects"])
        and set(manifest["objects"]) == set(objects),
        "INTEGRITY",
        "对象列表重复或不匹配",
    )
    for h, body in objects.items():
        identifier(h)
        require(len(body) <= MAX_BYTES and _sha(body) == h, "INTEGRITY", "研究原文 hash 不匹配")
    records, scopes = [], []
    if manifest["source"] == "sina_public":
        entries = manifest.get("observations")
        require(
            isinstance(entries, list) and 0 < len(entries) <= MAX_OBJECTS,
            "INTEGRITY",
            "新浪观测集合无效",
        )
        require(
            {e.get("sha256") for e in entries if isinstance(e, dict)} == set(objects),
            "INTEGRITY",
            "观测原文引用不匹配",
        )
        for entry in entries:
            security, frequency, rows = _sina_entry(entry, objects[entry["sha256"]])
            scopes.append(
                {
                    "security": security,
                    "frequency": frequency,
                    "first": rows[0]["day"],
                    "last": rows[-1]["day"],
                    "rows": len(rows),
                }
            )
        if manifest.get("coverage_contract") is not None:
            CoverageContract(manifest["coverage_contract"])
        calendar = None
    else:
        calendar_id = manifest.get("calendar_object")
        require(calendar_id is None or calendar_id in objects, "INTEGRITY", "日历原文缺失")
        calendar = _bao(objects[calendar_id], calendar=True) if calendar_id else None
        for h in manifest["objects"]:
            if h != calendar_id:
                rows = _bao(objects[h])
                records.extend(rows)
                scopes.append(
                    {
                        "security": rows[0]["security"],
                        "frequency": "daily",
                        "first": rows[0]["source_label"],
                        "last": rows[-1]["source_label"],
                        "rows": len(rows),
                    }
                )
        require(records and len(records) <= MAX_ROWS, "BOUNDED_IMPORT", "合计记录数须为1..10000")
        by_key = {}
        for row in records:
            key = (row["security"], row["source_label"])
            require(
                key not in by_key or by_key[key] == row["raw_record_sha256"],
                "VERSION_ORDER_UNKNOWN",
                "同标签异值旧回填缺修订顺序；请分别封存版本",
            )
            by_key[key] = row["raw_record_sha256"]
    require(len({s["security"] for s in scopes}) <= 10, "BOUNDED_IMPORT", "最多10个显式证券")
    return records, scopes, calendar


def _publish(store, manifest, objects):
    _validate(manifest, objects)
    body = canonical(manifest)
    sid = _sha(body)
    with store._writer():
        for dirname in ("research-objects", "research-manifests"):
            (store.root / dirname).mkdir(exist_ok=True)
        with store._db(write=True) as db:
            db.execute(TABLE)
            prior = db.execute(
                "SELECT status FROM research_snapshots WHERE id=?", (sid,)
            ).fetchone()
        if prior and prior[0] == "published":
            ResearchView(store, sid)
            return sid
        require(
            prior is None,
            "RESEARCH_RECOVERY_REQUIRED",
            "有未完成或已拒绝的发布；先显式 research-recover",
        )
        for h, value in objects.items():
            immutable_write(store.root / "research-objects" / h, value)
        with store._db(write=True) as db:
            db.execute(
                "INSERT INTO research_snapshots VALUES (?,?,?,?)",
                (sid, body.decode(), "prepared", now()),
            )
        immutable_write(store.root / "research-manifests" / (sid + ".json"), body)
        with store._db(write=True) as db:
            db.execute("UPDATE research_snapshots SET status='published' WHERE id=?", (sid,))
    return sid


def _objects(store, manifest):
    try:
        hashes = manifest["objects"]
        require(
            isinstance(hashes, list) and 0 < len(hashes) <= MAX_OBJECTS,
            "INTEGRITY",
            "对象引用集合无效",
        )
        return {
            identifier(h): _read(store.root / "research-objects" / identifier(h)) for h in hashes
        }
    except (KeyError, TypeError) as exc:
        raise DataError("INTEGRITY", "研究对象索引损坏") from exc


def recover(store):
    result = []
    with store._writer():
        with store._db() as db:
            if not _table_exists(db):
                return result
            entries = db.execute(
                "SELECT id,body FROM research_snapshots WHERE status='prepared' ORDER BY id"
            ).fetchall()
        for row in entries:
            sid, body = row["id"], row["body"].encode()
            try:
                identifier(sid)
                require(_sha(body) == sid, "INTEGRITY", "prepared 清单 hash 不匹配")
                manifest = _json(body)
                _validate(manifest, _objects(store, manifest))
                immutable_write(store.root / "research-manifests" / (sid + ".json"), body)
                status, error = "published", None
            except (DataError, OSError) as exc:
                status, error = "aborted", exc.code if isinstance(exc, DataError) else "IO_ERROR"
            with store._db(write=True) as db:
                db.execute("UPDATE research_snapshots SET status=? WHERE id=?", (status, sid))
            result.append({"dataset_id": sid, "status": status, "error": error})
    return result


def import_research(store, paths, *, format="baostock_daily", calendar_path=None):
    require(format == "baostock_daily", "UNSUPPORTED_SOURCE", "当前本地导入仅支持 baostock_daily")
    paths = [paths] if isinstance(paths, (str, Path)) else paths
    require(
        isinstance(paths, (list, tuple)) and 0 < len(paths) < MAX_OBJECTS,
        "BOUNDED_IMPORT",
        "须提供1..127个原文路径",
    )
    objects = {}
    for path in paths:
        body = _read(path)
        _bao(body)
        objects[_sha(body)] = body
    calendar_id = None
    if calendar_path is not None:
        body = _read(calendar_path)
        _bao(body, calendar=True)
        calendar_id = _sha(body)
        objects[calendar_id] = body
    manifest = {
        **VERSIONS,
        "source": "baostock_daily",
        "objects": sorted(objects),
        "calendar_object": calendar_id,
        "receipt_semantics": "legacy_backfill_unknown; ignore task/file timestamps",
        "upstream_query_snapshot_id": None,
    }
    return _publish(store, manifest, objects)


def save_client(client, store):
    transport = client.transport
    pinned = transport if transport._frozen is not None else transport.freeze()
    entries, objects = [], {}
    eligible = [
        e
        for e in pinned._frozen
        if isinstance(e.get("url"), str)
        and e["url"].split("?", 1)[0] == KLINE_URL
        and (pinned._received_by is None or timestamp(e["observed_at"]) <= pinned._received_by)
    ]
    require(len(eligible) <= MAX_OBJECTS, "BOUNDED_IMPORT", "最多128个观测")
    for entry in eligible:
        body, _ = pinned._cached(entry, entry["url"], lambda raw: raw)
        _sina_entry(entry, body)
        entries.append(copy.deepcopy(entry))
        objects[entry["sha256"]] = body
    require(
        entries, "SOURCE_NO_DATA", "没有可封存的新浪行情观测；先显式 fetch 或使用有观测的 Client"
    )
    require(len(entries) <= MAX_OBJECTS, "BOUNDED_IMPORT", "最多128个观测")
    manifest = {
        **VERSIONS,
        "source": "sina_public",
        "objects": sorted(objects),
        "observations": sorted(entries, key=digest),
        "coverage_contract": client.coverage_contract._data if client.coverage_contract else None,
        "upstream_query_snapshot_id": pinned.snapshot_id,
    }
    return _publish(store, manifest, objects)


def fetch_price(
    store, security, start_date=None, end_date=None, frequency="daily", count=None, timeout=15
):
    with tempfile.TemporaryDirectory(prefix=".research-fetch-", dir=store.root) as cache:
        client = Client(cache=cache, cache_mode="refresh", timeout=timeout)
        client.get_price(
            security, start_date=start_date, end_date=end_date, frequency=frequency, count=count
        )
        return save_client(client, store)


class _SealedTransport(Transport):
    def __init__(self, root, entries, objects, *, received_by=None):
        super().__init__(cache=root, cache_mode="only")
        self._frozen, self._objects = copy.deepcopy(entries), objects
        self._received_by = received_by
        self.snapshot_id = digest(
            {
                "observations": self._frozen,
                "received_by": received_by.isoformat() if received_by else None,
            }
        )

    def freeze(self, *, received_by=None):
        return _SealedTransport(self.root, self._frozen, self._objects, received_by=received_by)

    def _cached(self, entry, url, decode):
        body = self._objects[entry["sha256"]]
        return decode(body), {
            **copy.deepcopy(entry),
            "cache_hit": True,
            "cache_age_seconds": (
                datetime.now(TZ) - timestamp(entry["observed_at"])
            ).total_seconds(),
            "requested_url": url,
            "cache_window_reused": entry["url"] != url,
            "network_used": False,
        }


@dataclass
class ResearchResult:
    data: pd.DataFrame
    report: dict

    def to_dict(self):
        frame = self.data.reset_index() if self.data.index.name == "time" else self.data
        return {"rows": frame.to_dict("records"), "report": copy.deepcopy(self.report)}


class ResearchView:
    def __init__(self, store, dataset_id):
        sid = identifier(dataset_id)
        with store._db() as db:
            row = (
                db.execute(
                    "SELECT body,status FROM research_snapshots WHERE id=?", (sid,)
                ).fetchone()
                if _table_exists(db)
                else None
            )
        require(
            row is not None and row["status"] == "published",
            "RESEARCH_NOT_PUBLISHED",
            "没有该已发布研究版本",
        )
        body = _read(store.root / "research-manifests" / (sid + ".json"), 16 * 1024 * 1024)
        require(
            _sha(body) == sid and body == row["body"].encode(),
            "INTEGRITY",
            "研究清单或目录 hash 不符",
        )
        manifest = _json(body)
        objects = _objects(store, manifest)
        records, scopes, calendar = _validate(manifest, objects)
        self._store, self._id, self._manifest, self._objects = store, sid, manifest, objects
        self._records, self._scopes, self._calendar = records, scopes, calendar
        self._clock, self._visibility = None, "unrestricted_research"

    def descriptor(self):
        return copy.deepcopy(
            {
                **VERSIONS,
                "identity_kind": "immutable_research_dataset",
                "dataset_id": self._id,
                "manifest_sha256": self._id,
                "source": self._manifest["source"],
                "objects": [
                    {"sha256": h, "bytes": len(self._objects[h])} for h in sorted(self._objects)
                ],
                "scope": self._scopes,
                "upstream_query_snapshot_id": self._manifest.get("upstream_query_snapshot_id"),
                "reopen": {
                    "api": "Store(root).research(dataset_id)",
                    "dataset_id": self._id,
                    "network_required": False,
                },
                "guarantees": {
                    "immutable_objects": True,
                    "offline_reopen": True,
                    "historical_pit": False,
                    "finalized": False,
                    "full_history": False,
                    "execution_permission": False,
                },
                "units": {"price": "CNY/share", "volume": "share", "amount": "CNY"},
                "price_basis": "raw_unadjusted",
                "timezone": "Asia/Shanghai",
                "time_semantics": "inclusive source labels; interval boundaries unverified",
                "supported_queries": ["get_price", "coverage", "quality", "lineage"],
                "unsupported": [
                    "historical_universe",
                    "corporate_actions",
                    "adjustment_factors",
                    "price_limits",
                    "execution_projection",
                ],
            }
        )

    def quality(self):
        return {
            "dataset_id": self._id,
            "classification": "source_claim_unverified",
            "synthetic": None,
            "source_authenticity_verified": False,
            "point_in_time_verified": False,
            "finality": "UNVERIFIED",
            "trade_totals_verified": False,
            "network_used": False,
            "receipt_time": "local_complete_response"
            if self._manifest["source"] == "sina_public"
            else "unknown_legacy_backfill",
            "assumptions": [
                "source-reported units and raw price basis",
                "no paused fill or inferred OHLC",
                "no historical vendor visibility proof",
                "research access is not trading admission",
            ],
            "unknown": [
                "historical membership",
                "corporate action completeness",
                "publication finality",
                "verified interval boundaries",
            ],
        }

    def lineage(self):
        return {
            "descriptor": self.descriptor(),
            "observations": copy.deepcopy(self._manifest.get("observations", [])),
            "raw_records": copy.deepcopy(self._records),
            "source_calendar": copy.deepcopy(self._calendar),
            "coverage_contract": copy.deepcopy(self._manifest.get("coverage_contract")),
            "receipt_semantics": self._manifest.get(
                "receipt_semantics", "local complete response, not historical PIT"
            ),
        }

    def at(self, as_of, *, visibility="verified"):
        clock = timestamp(as_of)
        require(clock <= datetime.now(TZ), "INVALID_TIME", "策略时钟不能在未来")
        require(
            visibility in {"verified", "assumed", "received"},
            "INVALID_VISIBILITY",
            "可见性等级无效",
        )
        require(
            visibility != "verified",
            "PIT_UNAVAILABLE",
            "此数据集没有历史 PIT 证据；须显式选择研究假设",
        )
        require(
            visibility != "received" or self._manifest["source"] == "sina_public",
            "RECEIPT_TIME_UNKNOWN",
            "旧回填没有逐响应接收完成时间",
        )
        context = copy.copy(self)
        context._clock, context._visibility = clock, visibility
        return context

    def _client(self):
        contract = self._manifest.get("coverage_contract")
        client = Client(
            cache=self._store.root,
            cache_mode="only",
            coverage_contract=CoverageContract(contract) if contract else None,
        )
        client.transport = _SealedTransport(
            self._store.root, self._manifest["observations"], self._objects
        )
        return (
            client.at(self._clock, visibility=self._visibility)
            if self._clock is not None
            else client
        )

    def coverage(self, security, start_date, end_date, frequency="daily"):
        _security(security)
        require(
            isinstance(frequency, str) and frequency in FREQUENCIES,
            "UNSUPPORTED_FREQUENCY",
            "频率无效",
        )
        start, end = _dt(start_date), _dt(end_date, end=True)
        require(start <= end, "INVALID_RANGE", "start 不得晚于 end")
        if self._manifest["source"] == "sina_public":
            report = self._client().coverage(security, start_date, end_date, frequency=frequency)
            return {**report, "dataset_id": self._id, "network_used": False}
        require(
            FREQUENCIES[frequency] == 240, "UNSUPPORTED_FREQUENCY", "导入数据只有日线，无分钟聚合"
        )
        rows = self._bao_visible(security)
        actual = sorted({r["source_label"] for r in rows if start <= _dt(r["source_label"]) <= end})
        require((end.date() - start.date()).days <= 366, "BOUNDED_QUERY", "覆盖检查最多367个自然日")
        needed = [
            (start.date() + timedelta(days=i)).isoformat()
            for i in range((end.date() - start.date()).days + 1)
        ]
        require(len(needed) <= 367, "BOUNDED_QUERY", "覆盖检查最多367个自然日")
        known = self._calendar is not None and all(d in self._calendar for d in needed)
        expected = [d for d in needed if self._calendar[d]] if known else None
        missing = sorted(set(expected) - set(actual)) if known else None
        extra = sorted(set(actual) - set(expected)) if known else None
        return {
            "dataset_id": self._id,
            "security": security,
            "complete": False,
            "status": "unknown",
            "source_grid_complete": not missing and not extra if known else None,
            "source_calendar_verified": False,
            "actual_labels": actual,
            "expected_labels": expected,
            "missing_labels": missing,
            "unexpected_labels": extra,
            "reason": "source calendar is not verified PIT/session/status evidence",
            "network_used": False,
        }

    def _bao_visible(self, security):
        records = [r for r in self._records if r["security"] == security]
        require(records, "SOURCE_NO_DATA", "此研究版本未包含该证券")
        if self._clock is not None:
            records = [
                r
                for r in records
                if datetime.combine(_dt(r["source_label"]).date(), time.max, TZ) <= self._clock
            ]
        return records

    def get_price(
        self,
        security,
        start_date=None,
        end_date=None,
        frequency="daily",
        fields=None,
        count=None,
        fq=None,
        *,
        require_complete=False,
        require_fresh=False,
        require_final=False,
        require_tradable=False,
    ):
        flags = (require_complete, require_fresh, require_final, require_tradable)
        require(all(type(v) is bool for v in flags), "INVALID_REQUEST", "准入要求须为布尔值")
        require(not require_final, "BAR_NOT_FINAL", "缺最终发布及修订水位证据")
        require(fq is None, "UNSUPPORTED_ADJUSTMENT", "只提供原价，不伪造因子")
        securities = [security] if isinstance(security, str) else security
        require(
            isinstance(securities, (list, tuple)) and 0 < len(securities) <= 10,
            "REQUEST_TOO_LARGE",
            "只允许1..10个显式证券",
        )
        for code in securities:
            _security(code)
        require(len(set(securities)) == len(securities), "INVALID_REQUEST", "证券不可重复")
        require(
            isinstance(frequency, str) and frequency in FREQUENCIES,
            "UNSUPPORTED_FREQUENCY",
            "频率无效",
        )
        wanted = DEFAULT_FIELDS.copy() if fields is None else fields
        require(
            isinstance(wanted, (list, tuple))
            and wanted
            and all(isinstance(f, str) for f in wanted)
            and len(set(wanted)) == len(wanted),
            "INVALID_REQUEST",
            "fields须非空且不重复",
        )
        require(
            set(wanted) <= set(DEFAULT_FIELDS + ["money", "amount"]),
            "UNSUPPORTED_FIELD",
            "不支持的行情字段",
        )
        require(
            not ({"money", "amount"} <= set(wanted)),
            "INVALID_REQUEST",
            "money和amount是别名，不可同传",
        )
        records, provenance = [], []
        if self._manifest["source"] == "sina_public":
            frame = self._client().get_price(
                security,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                fields=["money" if f == "amount" else f for f in wanted],
                count=count,
                fq=fq,
                require_complete=require_complete,
                require_fresh=require_fresh,
                require_tradable=require_tradable,
            )
            source_report = copy.deepcopy(frame.attrs)
            for item in source_report["provenance"]:
                for raw in item["source_rows"]:
                    records.append(self._record(item["security"], raw["day"], raw, wanted))
                row_evidence = {r["source_label"]: r for r in item["row_observations"]}
                evidence = [
                    {
                        "security": item["security"],
                        "source_label": raw["day"],
                        "source_row": raw,
                        "raw_record_sha256": digest(raw),
                        "object_sha256": row_evidence[raw["day"]]["sha256"],
                        "observation_id": row_evidence[raw["day"]]["observation_id"],
                        "response_completed_at": row_evidence[raw["day"]]["observed_at"],
                        "available_at": row_evidence[raw["day"]]["observed_at"]
                        if self._visibility == "received"
                        else None,
                        "bar_start": None,
                        "bar_end": None,
                    }
                    for raw in item["source_rows"]
                ]
                provenance.append(
                    {
                        **item,
                        "records": evidence,
                        "raw_record_sha256": [digest(r) for r in item["source_rows"]],
                    }
                )
        else:
            require(
                FREQUENCIES[frequency] == 240,
                "UNSUPPORTED_FREQUENCY",
                "只有原生daily，无分钟或聚合日线",
            )
            require(not require_complete, "COVERAGE_UNKNOWN", "源日历格匹配不等于完整交易证据")
            require(not require_fresh, "FRESHNESS_UNKNOWN", "旧回填无逐响应接收时间及当前状态")
            require(
                not require_tradable,
                "TRADING_STATUS_UNKNOWN",
                "原文tradestatus不证明策略时刻交易准入",
            )
            require(
                start_date is None or count is None, "INVALID_REQUEST", "start_date和count不能同传"
            )
            if start_date is None and count is None:
                count = 20
            require(
                count is None or type(count) is int and 1 <= count <= 1000,
                "INVALID_COUNT",
                "count须为1..1000",
            )
            end = (
                _dt(end_date, end=True) if end_date is not None else self._clock or datetime.now(TZ)
            )
            start = _dt(start_date) if start_date is not None else None
            require(
                end.date() <= (self._clock or datetime.now(TZ)).date(),
                "INVALID_DATE",
                "不接受未来日期",
            )
            require(start is None or start <= end, "INVALID_RANGE", "start不得晚于end")
            for code in securities:
                chosen = [
                    r
                    for r in self._bao_visible(code)
                    if (start is None or _dt(r["source_label"]) >= start)
                    and _dt(r["source_label"]) <= end
                ]
                unique = {r["source_label"]: r for r in chosen}
                chosen = [unique[k] for k in sorted(unique)]
                if count:
                    require(len(chosen) >= count, "COVERAGE_INCOMPLETE", "记录不足count；不补数")
                    chosen = chosen[-count:]
                require(chosen, "COVERAGE_INCOMPLETE", "范围内没有可见记录")
                for row in chosen:
                    records.append(
                        self._record(code, row["source_label"], row["source_row"], wanted)
                    )
                provenance.append(
                    {
                        "security": code,
                        "source_rows": [r["source_row"] for r in chosen],
                        "records": chosen,
                        "coverage_report": self.coverage(
                            code,
                            start_date or chosen[0]["source_label"],
                            end_date or chosen[-1]["source_label"],
                        ),
                        "available_at": None,
                        "response_completed_at": None,
                    }
                )
            source_report = {
                "source": "baostock_daily",
                "visibility_level": self._visibility,
                "as_of": self._clock.isoformat() if self._clock else None,
                "available_at": None,
            }
        result = pd.DataFrame(records).sort_values(["time", "code"], ignore_index=True)
        if isinstance(security, str):
            result = result.drop(columns="code").set_index("time")
        report = {
            **source_report,
            "dataset_id": self._id,
            "descriptor": self.descriptor(),
            "quality": self.quality(),
            "provenance": provenance,
            "network_used": False,
            "bar_start": None,
            "bar_end": None,
            "interval_boundaries_verified": False,
            "research_only": True,
        }
        result.attrs = copy.deepcopy(report)
        return ResearchResult(result, report)

    @staticmethod
    def _record(code, label, raw, wanted):
        record = {"time": _dt(label).replace(tzinfo=None), "code": code}
        for field in wanted:
            key = "amount" if field in {"money", "amount"} else field
            require(
                raw.get(key) not in (None, ""),
                "SOURCE_FIELD_MISSING",
                "所选行缺少请求字段",
                {"security": code, "label": label, "field": field},
            )
            record[field] = int(_decimal(raw[key])) if field == "volume" else _decimal(raw[key])
        return record
