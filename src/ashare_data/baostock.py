"""Explicit bounded BaoStock acquisition and immutable offline research views."""

from __future__ import annotations

import copy
import fcntl
import importlib.util
from itertools import islice
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import pandas as pd

from ._bao_receipt.capture import bh, sh, stored_locally_visible, utc_now
from ._bao_receipt.protocol import check_identity, decode_request, decode_response
from ._bao_receipt.timing import wall_clock_order, monotonic_duration
from .baostock_worker import sdk_identity
from .live import _security
from .model import DataError, canonical, require, timestamp
from .research import ResearchResult, _bao, _day, _decimal, _read
from .storage import identifier, immutable_write
from . import baostock_minutes as minutes
from . import daily_status
from . import preclose

FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag,tradestatus"
TABLE = """CREATE TABLE IF NOT EXISTS baostock_captures
(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL)"""
VERSION = "baostock-research-1/receipt-4.1"
METADATA_POLICY = "baostock-basic-validation-1"


def validate_basic(rows, code):
    require(len(rows) <= 1, "SOURCE_SCHEMA_ERROR", "证券资料应至多一行")
    for row in rows:
        require(
            set(row) == {"code", "code_name", "ipoDate", "outDate", "type", "status"}
            and all(isinstance(v, str) for v in row.values())
            and row["code"] == code,
            "SOURCE_SCHEMA_ERROR",
            "证券资料字段、字符串类型或代码异常",
        )
        # Supported reader profile, not a promise to recognize future source enums.
        require(
            row["type"] in {"1", "2", "3"} and row["status"] in {"0", "1"},
            "SOURCE_SCHEMA_ERROR",
            "证券type/status未获支持；未知不能映射为正常",
        )
        dates = {name: _day(row[name]) if row[name] else None for name in ("ipoDate", "outDate")}
        require(
            not all(dates.values()) or dates["ipoDate"] <= dates["outDate"],
            "SOURCE_SCHEMA_ERROR",
            "证券退出日期早于上市日期",
        )


def request(kind, security=None, start_date=None, end_date=None, frequency=None):
    require(
        kind in {"daily", "daily_status", "daily_preclose", "minute", "basic", "calendar"},
        "UNSUPPORTED_SOURCE",
        "仅支持日线、日状态、源前收、限定分钟、证券资料、日历",
    )
    if kind == "minute":
        require(
            isinstance(frequency, str) and frequency in minutes.FREQUENCIES,
            "UNSUPPORTED_FREQUENCY",
            "分钟仅支持5m/15m/30m/60m；不支持1m",
        )
    else:
        require(
            frequency is None
            or (kind in {"daily", "daily_status", "daily_preclose"} and frequency == "daily"),
            "INVALID_ARGUMENT",
            "周期参数与查询种类不匹配",
        )
    params = {}
    if kind != "calendar":
        code = _security(security)
        params["code"] = code[:2] + "." + code[2:]
    else:
        require(security is None, "INVALID_ARGUMENT", "日历查询不接受证券参数")
    if kind == "basic":
        require(
            start_date is None and end_date is None, "PIT_UNAVAILABLE", "证券资料仅为当前源资料"
        )
        params["code_name"] = ""
        method = "query_stock_basic"
    else:
        start, end = _day(start_date), _day(end_date)
        require(
            start <= end and (end - start).days < (2 if kind == "minute" else 31),
            "BOUNDED_REQUEST",
            "分钟须指定至多2个连续自然日；日线/日历至多31日",
        )
        params.update(start_date=start_date, end_date=end_date)
        method = "query_trade_dates"
        if kind in {"daily", "daily_status", "daily_preclose", "minute"}:
            method = "query_history_k_data_plus"
            params.update(
                fields=minutes.FIELDS
                if kind == "minute"
                else daily_status.FIELDS
                if kind == "daily_status"
                else preclose.FIELDS
                if kind == "daily_preclose"
                else FIELDS,
                frequency=minutes.FREQUENCIES[frequency] if kind == "minute" else "d",
                adjustflag="3",
            )
    return {"method": method, "params": params}


class BaoStockSource:
    def __init__(self, *, sdk_path=None, timeout=15):
        require(
            type(timeout) in {int, float} and 1 <= timeout <= 30,
            "INVALID_ARGUMENT",
            "总期限须为1..30秒",
        )
        if sdk_path is None:
            spec = importlib.util.find_spec("baostock")
            require(
                spec is not None and spec.origin,
                "SDK_UNAVAILABLE",
                "需已有官方 BaoStock 0.9.4 SDK；不自动安装",
            )
            sdk_path = Path(spec.origin).parent.parent
        self._sdk_path = str(Path(sdk_path).resolve())
        try:
            sdk_identity(self._sdk_path)
        except (OSError, ValueError) as exc:
            raise DataError("SDK_IDENTITY_MISMATCH", "已有 SDK 与固定官方源码不符") from exc
        self._timeout = timeout

    @staticmethod
    def capabilities():
        return {
            "source": "baostock",
            "kinds": ["daily", "daily_status", "daily_preclose", "minute", "basic", "calendar"],
            "frequencies": ["daily", *minutes.FREQUENCIES],
            "max_calendar_days": 31,
            "max_minute_calendar_days": 2,
            "max_securities": 1,
            "max_rows": 128,
            "max_pages": 2,
            "page_bytes": 4 * 1024 * 1024,
            "network": "explicit_fetch_only",
            "historical_pit": False,
            "full_history": False,
            "concurrent_connections": False,
            "daily_outer_integrity": "unverified",
            "minute_data": "bounded_research; compressed_outer_integrity_unverified",
            "one_minute": "unsupported",
            "minute_label_cutoff": "aware end, inclusive or exclusive; not closure/PIT",
            "basic_validation_policy": METADATA_POLICY,
            "daily_status_policy": daily_status.POLICY,
            "daily_status_fields": ["tradestatus", "isST"],
            "source_preclose_policy": preclose.POLICY,
            "source_preclose_fields": ["preclose"],
            "source_preclose_basis": preclose.BASIS,
        }

    def get_price(
        self,
        security,
        *,
        store,
        start_date,
        end_date,
        frequency="daily",
        end=None,
        end_inclusive=True,
    ):
        bound = minutes.cutoff(end, end_inclusive)
        require(
            bound is None or (isinstance(frequency, str) and frequency in minutes.FREQUENCIES),
            "UNSUPPORTED_FREQUENCY",
            "end源标签筛选仅支持分钟版本",
        )
        sid = self.fetch(
            store,
            kind="daily" if frequency == "daily" else "minute",
            security=security,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
        )
        return self._acquired(store.baostock(sid).get_price(end=end, end_inclusive=end_inclusive))

    def get_security_info(self, security, *, store):
        return self._acquired(
            store.baostock(self.fetch(store, kind="basic", security=security)).get_security_info()
        )

    def get_status(self, security, *, store, start_date, end_date, require_known=False):
        require(type(require_known) is bool, "INVALID_ARGUMENT", "require_known必须为bool")
        sid = self.fetch(
            store, kind="daily_status", security=security, start_date=start_date, end_date=end_date
        )
        # Frozen result reports its explicit acquisition without mutating the offline view.
        result = store.baostock(sid).get_status(require_known=require_known)
        report = dict(result.report, network_used=True, acquisition="explicit_baostock_query")
        return daily_status.DailyStatusResult(sid, result.rows, canonical(report).decode())

    def get_preclose(self, security, *, store, start_date, end_date, require_known=False):
        require(type(require_known) is bool, "INVALID_ARGUMENT", "require_known必须为bool")
        sid = self.fetch(
            store,
            kind="daily_preclose",
            security=security,
            start_date=start_date,
            end_date=end_date,
        )
        result = store.baostock(sid).get_preclose(require_known=require_known)
        report = dict(result.report, network_used=True, acquisition="explicit_baostock_query")
        return preclose.SourcePrecloseResult(sid, result.rows, canonical(report).decode())

    def get_trade_days(self, *, store, start_date, end_date):
        sid = self.fetch(store, kind="calendar", start_date=start_date, end_date=end_date)
        return self._acquired(store.baostock(sid).get_trade_days())

    @staticmethod
    def _acquired(result):
        result.report["network_used"] = True
        result.report["acquisition"] = "explicit_baostock_query"
        return result

    def fetch(self, store, *, kind, security=None, start_date=None, end_date=None, frequency=None):
        req = request(kind, security, start_date, end_date, frequency)
        # A per-user machine lock spans every store created by this library.
        lock = Path(tempfile.gettempdir()) / f"ashare-baostock-{os.getuid()}.lock"
        with lock.open("a+b") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DataError("SOURCE_BUSY", "已有 BaoStock 查询；禁止并发连接") from exc
            with tempfile.TemporaryDirectory(prefix=".baostock-", dir=store.root) as stage:
                config = {"sdk_path": self._sdk_path, "timeout": self._timeout, "request": req}
                boot = "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('ashare_data.baostock_worker',run_name='__main__')"
                try:
                    proc = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-c",
                            boot,
                            str(Path(__file__).parent.parent),
                            stage,
                        ],
                        input=json.dumps(config),
                        text=True,
                        capture_output=True,
                        timeout=self._timeout + 2,
                        env={
                            k: v
                            for k, v in os.environ.items()
                            if k in {"PATH", "SYSTEMROOT", "TMPDIR"}
                        },
                    )
                    failure = "worker_process_failed" if proc.returncode else None
                except subprocess.TimeoutExpired:
                    failure = "worker_deadline"
                out = Path(stage)
                if failure or not (out / "attempt.json").exists():
                    # Incomplete worker files are diagnostics, never admitted rows.
                    (out / "attempt.json").write_bytes(
                        canonical(
                            {
                                "request": req,
                                "status": failure or "worker_result_missing",
                                "worker_completed_at": utc_now(),
                                "sdk_identity": sdk_identity(self._sdk_path),
                                "endpoint": "public-api.baostock.com:10030",
                                "account": "anonymous",
                                "evidence_kind": "live_local_capture",
                            }
                        )
                    )
                blobs = {
                    str(p.relative_to(out)): p.read_bytes() for p in out.rglob("*") if p.is_file()
                }
                return _publish(store, blobs)


def _verify(blobs):
    """Re-decode actual bytes. Recorded row lists and status flags are not authority."""
    try:
        return _verify_impl(blobs)
    except (KeyError, ValueError, TypeError, AttributeError, IndexError) as exc:
        raise DataError("INTEGRITY", "BaoStock 证据结构、身份或行校验失败") from exc


def _verify_impl(blobs):
    attempt = json.loads(blobs["attempt.json"])
    req = attempt["request"]
    methods = {
        "query_history_k_data_plus": "daily",
        "query_stock_basic": "basic",
        "query_trade_dates": "calendar",
    }
    kind = methods[req["method"]]
    params = req["params"]
    if (
        kind == "daily"
        and params.get("frequency") == "d"
        and params.get("fields") == daily_status.FIELDS
    ):
        kind = "daily_status"
    elif (
        kind == "daily"
        and params.get("frequency") == "d"
        and params.get("fields") == preclose.FIELDS
    ):
        kind = "daily_preclose"
    frequency = None
    if kind == "daily" and params.get("frequency") != "d":
        kind = "minute"
        frequency = str(params.get("frequency")) + "m"
    code = params.get("code")
    sec = (code[3:] + (".XSHG" if code[:2] == "sh" else ".XSHE")) if code else None
    require(
        req == request(kind, sec, params.get("start_date"), params.get("end_date"), frequency),
        "INTEGRITY",
        "请求超出固定范围",
    )
    pinned = json.loads(Path(__file__).with_name("baostock-sdk.json").read_text())
    require(
        attempt["sdk_identity"] == pinned
        and attempt["endpoint"] == "public-api.baostock.com:10030"
        and attempt["account"] == "anonymous",
        "INTEGRITY",
        "SDK 或入口身份不匹配",
    )
    if attempt["status"] != "captured":
        return attempt, None, None, [], "source_request_failed"
    receipt, storage = (
        json.loads(blobs["capture/" + name + ".json"]) for name in ("receipt", "storage-record")
    )
    core = dict(receipt)
    rid = core.pop("receipt_id")
    require(
        rid == sh(core)
        and receipt["request"] == req
        and receipt["sdk_identity"] == pinned
        and receipt["row_limit"] == 128
        and receipt["schema_version"] == 4
        and receipt["evidence_kind"] in {"live_local_capture", "offline_test_only"}
        and receipt["evidence_kind"] == attempt["evidence_kind"],
        "INTEGRITY",
        "回执身份不匹配",
    )
    core = dict(storage)
    storage_id = core.pop("storage_id")
    require(storage_id == sh(core) and storage["receipt_id"] == rid, "INTEGRITY", "存储回执不匹配")
    expected_names = {"receipt.json", "sdk-rows.json", "research-rows.json"}
    for n in range(1, len(receipt["pages"]) + 1):
        expected_names.update({f"page-{n:02d}.request.bin", f"page-{n:02d}.response.bin"})
    require(
        len(storage["artifacts"]) == len(expected_names)
        and {a["path"] for a in storage["artifacts"]} == expected_names,
        "INTEGRITY",
        "存储回执文件集合不匹配",
    )
    for a in storage["artifacts"]:
        b = blobs["capture/" + a["path"]]
        require(len(b) == a["bytes"] and bh(b) == a["sha256"], "INTEGRITY", "磁盘证据 hash 不匹配")
    sdk_rows = json.loads(blobs["capture/sdk-rows.json"])
    research = json.loads(blobs["capture/research-rows.json"])
    require(sh(sdk_rows) == receipt["sdk_rows_sha256"], "INTEGRITY", "SDK 行 hash 不匹配")
    events = receipt["wall_clock_events"]
    wall = wall_clock_order(events)
    mono = monotonic_duration(
        receipt["monotonic_evidence"]["start_ns"], receipt["monotonic_evidence"]["end_ns"]
    )
    clock_state = (
        "invalid"
        if "invalid" in (wall["state"], mono["state"])
        else "unknown"
        if "unknown" in (wall["state"], mono["state"])
        else "valid"
    )
    require(
        wall == receipt["wall_clock_evidence"]
        and mono == receipt["monotonic_evidence"]
        and clock_state == receipt["clock_order_state"]
        and (clock_state == "valid") == receipt["clock_order_valid"],
        "INTEGRITY",
        "时钟判定不是事件重算结果",
    )
    for event, field in (
        ("task_started", "task_started_at"),
        ("query_started", "request_started_at"),
        ("operation_completed", "operation_completed_at"),
        ("validation_completed", "validation_completed_at"),
    ):
        require(
            [e["at"] for e in events if e["event"] == event] == [receipt[field]],
            "INTEGRITY",
            "时钟边界与事件不一致",
        )
    rows, safe = [], bool(receipt["pages"])
    require(len(receipt["pages"]) <= 3, "INTEGRITY", "分页超限")
    for n, page in enumerate(receipt["pages"], 1):
        sent = blobs[f"capture/page-{n:02d}.request.bin"]
        raw = blobs[f"capture/page-{n:02d}.response.bin"]
        require(
            page["page_index"] == n
            and page["raw_request_sha256"] == bh(sent)
            and page["raw_response_sha256"] == (bh(raw) if raw else None)
            and page["raw_response_bytes"] == len(raw),
            "INTEGRITY",
            "分页原文 hash 不匹配",
        )
        try:
            decoded = decode_response(raw)
            identity = check_identity(req, decode_request(sent), decoded, n)
        except (ValueError, UnicodeError):
            safe = False
            continue
        require(
            page["rows"] == decoded["rows"] and page["decoded_rows_sha256"] == sh(decoded["rows"]),
            "INTEGRITY",
            "记录内容不是原文解码结果",
        )
        frame_state = (
            "complete_verified"
            if decoded["integrity"] == "length_terminator_crc_verified"
            else "complete_integrity_unknown"
        )
        require(
            page["frame_state"] == frame_state
            and page["sent_envelope"] == decode_request(sent)
            and page["response_metadata"] == {k: v for k, v in decoded.items() if k != "rows"}
            and page["identity"] == identity,
            "INTEGRITY",
            "原文身份或完整性声明不符",
        )
        safe &= (
            identity["state"] == "matched"
            and page["sent_envelope"]["version"] == "00.9.40"
            and decoded["version"] in {"00.9.10", "00.9.40"}
            and decoded["error_code"] == "0"
            and not page["faults"]
            and page["send_attempted"]
            and page["sent_bytes"] == len(sent)
        )
        safe &= (
            len(decoded["rows"]) == 2000
            if n < len(receipt["pages"])
            else len(decoded["rows"]) < 2000
        )
        rows.extend(decoded["rows"])
    safe &= (
        rows == sdk_rows
        and len(rows) <= 128
        and receipt["sdk_error_code"] == "0"
        and not receipt["sdk_exception"]
        and not receipt["transport_faults"]
    )
    computed_complete = bool(
        safe
        and clock_state == "valid"
        and all(p["frame_state"] == "complete_verified" for p in receipt["pages"])
    )
    require(
        computed_complete == receipt["query_complete"], "INTEGRITY", "完整性标记与原文/时钟不一致"
    )
    storage_clock = wall_clock_order(
        [
            {"event": "validation_completed", "at": receipt["validation_completed_at"]},
            {"event": "staging_recorded", "at": storage["staging_recorded_at"]},
        ]
    )
    eligible = computed_complete and storage_clock["state"] == "valid"
    require(
        storage["storage_clock_evidence"] == storage_clock
        and storage["storage_eligible"] == eligible
        and storage["stored_visibility_at"]
        == (storage["staging_recorded_at"] if eligible else None),
        "INTEGRITY",
        "存储时钟/可见性标记不一致",
    )
    for r in research:
        page_rows = receipt["pages"][r["page_index"] - 1]["rows"]
        require(
            r["receipt_id"] == rid
            and r["fields"] == page_rows[r["row_index"]]
            and r["raw_record_sha256"] == sh(r["fields"]),
            "INTEGRITY",
            "研究行追溯不匹配",
        )
    if not safe:
        return attempt, receipt, storage, [], "source_response_unusable"
    if kind == "daily_status":
        daily_status.page_received_times(receipt)
    elif kind == "daily_preclose":
        preclose.received_times(receipt)
    try:
        if kind == "daily" and rows:
            _bao(
                canonical(
                    {
                        "error_code": "0",
                        "method": req["method"],
                        "params": params,
                        "fields": FIELDS.split(","),
                        "rows": rows,
                    }
                )
            )
        elif kind == "daily_status":
            daily_status.validate(rows, params)
        elif kind == "daily_preclose":
            preclose.validate(rows, params)
        elif kind == "minute":
            minutes.validate(rows, params)
        elif kind == "calendar" and rows:
            _bao(
                canonical(
                    {
                        "error_code": "0",
                        "method": req["method"],
                        "params": params,
                        "fields": ["calendar_date", "is_trading_day"],
                        "rows": rows,
                    }
                ),
                calendar=True,
            )
        elif kind == "basic":
            validate_basic(rows, code)
    except DataError:
        return attempt, receipt, storage, [], "source_schema_invalid"
    return attempt, receipt, storage, rows, "research_rows" if rows else "empty_unknown"


def _publish(store, blobs):
    attempt, *_ = _verify(blobs)
    require(
        len(blobs) <= 20 and sum(map(len, blobs.values())) <= 20 * 1024 * 1024,
        "BOUNDED_IMPORT",
        "证据文件超限",
    )
    manifest = {
        "version": minutes.VERSION
        if attempt["request"]["params"].get("frequency") in minutes.FREQUENCIES.values()
        else daily_status.VERSION
        if attempt["request"]["params"].get("fields") == daily_status.FIELDS
        else preclose.VERSION
        if attempt["request"]["params"].get("fields") == preclose.FIELDS
        else VERSION,
        "artifacts": [
            {"name": n, "sha256": bh(b), "bytes": len(b)} for n, b in sorted(blobs.items())
        ],
    }
    body = canonical(manifest)
    sid = bh(body)
    with store._writer():
        for d in ("baostock-objects", "baostock-manifests"):
            (store.root / d).mkdir(exist_ok=True)
        with store._db(write=True) as db:
            db.execute(TABLE)
            prior = db.execute("SELECT status FROM baostock_captures WHERE id=?", (sid,)).fetchone()
        if prior:
            require(prior[0] == "published", "BAOSTOCK_RECOVERY_REQUIRED", "先显式恢复未完成发布")
            BaoStockView(store, sid)
            return sid
        for b in blobs.values():
            immutable_write(store.root / "baostock-objects" / bh(b), b)
        with store._db(write=True) as db:
            db.execute(
                "INSERT INTO baostock_captures VALUES (?,?,?)", (sid, body.decode(), "prepared")
            )
        immutable_write(store.root / "baostock-manifests" / (sid + ".json"), body)
        with store._db(write=True) as db:
            db.execute("UPDATE baostock_captures SET status='published' WHERE id=?", (sid,))
    return sid


def _load(store, body, sid):
    require(bh(body) == sid, "INTEGRITY", "清单 hash 不匹配")
    m = json.loads(body)
    require(
        m["version"] in {VERSION, minutes.VERSION, daily_status.VERSION, preclose.VERSION}
        and 0 < len(m["artifacts"]) <= 20,
        "INTEGRITY",
        "证据版本或文件数无效",
    )
    blobs = {}
    for a in m["artifacts"]:
        name = a["name"]
        require(
            re.fullmatch(r"(?:capture/)?[a-z0-9.-]+", name) and name not in blobs,
            "INTEGRITY",
            "证据路径无效或重复",
        )
        b = _read(store.root / "baostock-objects" / identifier(a["sha256"]), 8 * 1024 * 1024)
        require(len(b) == a["bytes"] and bh(b) == a["sha256"], "INTEGRITY", "证据内容 hash 不匹配")
        blobs[name] = b
    require(sum(map(len, blobs.values())) <= 20 * 1024 * 1024, "INTEGRITY", "证据总量超限")
    verified = _verify(blobs)
    expected_version = (
        minutes.VERSION
        if verified[0]["request"]["params"].get("frequency") in minutes.FREQUENCIES.values()
        else daily_status.VERSION
        if verified[0]["request"]["params"].get("fields") == daily_status.FIELDS
        else preclose.VERSION
        if verified[0]["request"]["params"].get("fields") == preclose.FIELDS
        else VERSION
    )
    require(m["version"] == expected_version, "INTEGRITY", "证据版本与周期契约不符")
    return m, verified


def import_capture(store, directory):
    """Explicit offline import of worker receipts; stored paths never drive external reads."""
    root = Path(directory)
    require(root.is_dir() and not root.is_symlink(), "BOUNDED_IMPORT", "需真实证据目录")
    paths = list(islice(root.rglob("*"), 22))
    require(len(paths) <= 21, "BOUNDED_IMPORT", "证据目录项超限")
    require(not any(p.is_symlink() for p in paths), "BOUNDED_IMPORT", "证据不得包含符号链接")
    files = [p for p in paths if p.is_file()]
    require(0 < len(files) <= 20, "BOUNDED_IMPORT", "证据文件数超限")
    require(
        sum(p.stat().st_size for p in files) <= 20 * 1024 * 1024, "BOUNDED_IMPORT", "证据总量超限"
    )
    blobs = {}
    for p in files:
        name = p.relative_to(root).as_posix()
        require(
            re.fullmatch(r"(?:capture/)?[a-z0-9.-]+", name) is not None,
            "BOUNDED_IMPORT",
            "证据路径无效",
        )
        blobs[name] = _read(p, 8 * 1024 * 1024)
    require(sum(map(len, blobs.values())) <= 20 * 1024 * 1024, "BOUNDED_IMPORT", "证据总量超限")
    return _publish(store, blobs)


def snapshots(store):
    with store._db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='baostock_captures'").fetchone():
            return []
        return [
            dict(r)
            for r in db.execute("SELECT id AS capture_id,status FROM baostock_captures ORDER BY id")
        ]


def recover(store):
    result = []
    with store._writer():
        for row in snapshots(store):
            if row["status"] != "prepared":
                continue
            sid = row["capture_id"]
            with store._db() as db:
                body = (
                    db.execute("SELECT body FROM baostock_captures WHERE id=?", (sid,))
                    .fetchone()[0]
                    .encode()
                )
            try:
                _load(store, body, sid)
                immutable_write(store.root / "baostock-manifests" / (sid + ".json"), body)
                status = "published"
            except (DataError, OSError, ValueError, KeyError, TypeError):
                status = "aborted"
            with store._db(write=True) as db:
                db.execute("UPDATE baostock_captures SET status=? WHERE id=?", (status, sid))
            result.append({"capture_id": sid, "status": status})
    return result


class BaoStockView:
    def __init__(self, store, capture_id):
        sid = identifier(capture_id)
        require(
            any(r == {"capture_id": sid, "status": "published"} for r in snapshots(store)),
            "BAOSTOCK_NOT_PUBLISHED",
            "不存在该已发布证据版本",
        )
        with store._db() as db:
            catalog = (
                db.execute("SELECT body FROM baostock_captures WHERE id=?", (sid,))
                .fetchone()[0]
                .encode()
            )
        body = _read(store.root / "baostock-manifests" / (sid + ".json"))
        require(body == catalog, "INTEGRITY", "磁盘与目录清单不一致")
        try:
            self._manifest, values = _load(store, body, sid)
        except (ValueError, KeyError, TypeError) as exc:
            raise DataError("INTEGRITY", "证据清单无效") from exc
        self._attempt, self._receipt, self._storage, self._rows, self._status = values
        self._id, self._visibility = sid, "unrestricted_research"
        self._label_end = None

    def _selected_rows(self):
        return [
            r
            for r in self._rows
            if self._label_end is None or minutes.within(minutes.label(r), self._label_end)
        ]

    def _with_end(self, end, inclusive):
        bound = minutes.cutoff(end, inclusive)
        require(
            bound is None or self._manifest["version"] == minutes.VERSION,
            "UNSUPPORTED_FREQUENCY",
            "end/source_label筛选仅支持分钟版本",
        )
        view = copy.copy(self)
        view._label_end = minutes.intersect(self._label_end, bound)
        return view

    def descriptor(self):
        return copy.deepcopy(
            {
                "capture_id": self._id,
                "version": self._manifest["version"],
                "source": "baostock",
                "status": self._status,
                "request": self._attempt["request"],
                "rows": len(self._selected_rows()),
                "artifacts": self._manifest["artifacts"],
                "quality": self.quality(),
                "coverage": self.coverage(),
                "offline_reopen": True,
            }
        )

    def lineage(self):
        if self._label_end is not None:
            # Full receipts contain future raw rows. A bounded view exposes only
            # selected row hashes; unrestricted audit access requires a new view.
            return copy.deepcopy(
                {
                    "capture_id": self._id,
                    "selection": minutes.selection(self._label_end),
                    "rows": [
                        {"source_row": r, "raw_record_sha256": sh(r)} for r in self._selected_rows()
                    ],
                    "receipt_scope": "full receipt omitted from label-bounded view",
                }
            )
        return copy.deepcopy(
            {
                "capture_id": self._id,
                "attempt": self._attempt,
                "receipt": self._receipt,
                "storage": self._storage,
                "rows": [{"source_row": r, "raw_record_sha256": sh(r)} for r in self._rows],
            }
        )

    def quality(self):
        return {
            "source_authenticity_verified": False,
            "synthetic": True
            if self._attempt.get("evidence_kind") == "offline_test_only"
            else None,
            "historical_pit": False,
            "finality": "UNVERIFIED",
            "execution_permission": False,
            "classification": "offline_test_only"
            if self._attempt.get("evidence_kind") == "offline_test_only"
            else "source_claim_unverified",
            "visibility": self._visibility,
            "compressed_outer_integrity": "unverified"
            if self._attempt["request"]["method"] == "query_history_k_data_plus"
            else "not_applicable",
            "units": {"price": "CNY/share", "volume": "share", "amount": "CNY"},
            "price_basis": preclose.BASIS
            if self._manifest["version"] == preclose.VERSION
            else "raw_unadjusted",
            "bar_start": None,
            "bar_end": None,
            "timezone": "Asia/Shanghai",
            "available_at": None,
            "selection": minutes.selection(self._label_end),
            "basic_validation_policy": METADATA_POLICY,
            "basic_validation": (
                "invalid"
                if self._status == "source_schema_invalid"
                else "valid_source_fields"
                if self._rows
                else "unknown"
            )
            if self._attempt["request"]["method"] == "query_stock_basic"
            else "not_applicable",
            "missing_basic_dates": [
                name
                for r in self._rows
                for name in ("ipoDate", "outDate")
                if name in r and r[name] == ""
            ],
        }

    def coverage(self):
        result = {
            "rows": len(self._selected_rows()),
            "source_status": self._status,
            "query_complete": bool(self._receipt and self._receipt["query_complete"]),
            "query_status": self._receipt["query_status"]
            if self._receipt
            else self._attempt["status"],
            "complete_history": False,
            "confirmed_no_events": False,
            "market_coverage": "unknown",
            "calendar_is_source_claim": True,
        }
        if self._manifest["version"] == minutes.VERSION:
            result["minute_labels"] = minutes.diagnostics(
                self._selected_rows(), self._attempt["request"]["params"], self._label_end
            )
            result["selection"] = minutes.selection(self._label_end)
        return result

    def at(self, as_of, *, visibility="received", inclusive=True):
        if visibility == "source_label":
            require(as_of is not None, "INVALID_TIME", "source_label必须提供as_of")
            view = self._with_end(as_of, inclusive)
            view._visibility = "source_label"
            return view
        require(
            type(inclusive) is bool and inclusive,
            "INVALID_ARGUMENT",
            "inclusive=False只适用于source_label",
        )
        require(
            isinstance(visibility, str) and visibility in {"received", "verified"},
            "INVALID_ARGUMENT",
            "仅支持 received/verified/source_label",
        )
        require(visibility != "verified", "PIT_UNAVAILABLE", "无历史发布时刻证据")
        timestamp(as_of)
        r, s = self._receipt, self._storage
        require(
            r is not None
            and s is not None
            and r["evidence_kind"] == "live_local_capture"
            and self._status in {"research_rows", "empty_unknown"}
            and r["data_completion_status"] in {"complete_rows", "complete_empty_unknown"}
            and all(p["frame_state"] == "complete_verified" for p in r["pages"])
            and wall_clock_order(r["wall_clock_events"])["state"] == "valid"
            and monotonic_duration(
                r["monotonic_evidence"]["start_ns"], r["monotonic_evidence"]["end_ns"]
            )["state"]
            == "valid"
            and stored_locally_visible(r, s, as_of),
            "RECEIPT_NOT_VISIBLE",
            "完整回执或本地可见时间证据不足",
        )
        view = copy.copy(self)
        view._visibility = "received"
        return view

    def _result(self, method):
        require(
            self._attempt["request"]["method"] == method,
            "QUERY_KIND_MISMATCH",
            "证据版本类型不匹配",
        )
        require(
            not (method == "query_stock_basic" and self._status == "source_schema_invalid"),
            "SOURCE_SCHEMA_ERROR",
            "证券日期、枚举或字段校验失败；原始证据已保留",
            {"capture_id": self._id, "policy": METADATA_POLICY, "status": self._status},
        )
        require(
            self._status in {"research_rows", "empty_unknown"},
            "SOURCE_REQUEST_FAILED",
            "源响应未达到研究读取条件",
            {"capture_id": self._id, "coverage": self.coverage()},
        )
        rows = copy.deepcopy(self._selected_rows())
        if method == "query_history_k_data_plus":
            is_minute = self._manifest["version"] == minutes.VERSION
            for r in rows:
                r["source_row"] = copy.deepcopy(r)
                r["raw_record_sha256"] = sh(r["source_row"])
                r["security"] = r["code"][3:] + (
                    ".XSHG" if r["code"].startswith("sh.") else ".XSHE"
                )
                r["source_label"] = (
                    minutes.label(r).isoformat(timespec="milliseconds") if is_minute else r["date"]
                )
                if is_minute:
                    r["source_time"] = r.pop("time")
                for f in ("open", "high", "low", "close", "amount"):
                    r[f] = _decimal(r[f]) if r[f] else None
                r["volume"] = int(_decimal(r["volume"]))
            data = pd.DataFrame(
                rows,
                columns=(
                    ["source_time" if f == "time" else f for f in minutes.FIELDS.split(",")]
                    if is_minute
                    else FIELDS.split(",")
                )
                + ["security", "source_label", "source_row", "raw_record_sha256"],
            )
            data.index = pd.DatetimeIndex(pd.to_datetime(data["source_label"]), name="time")
            if data.empty:
                data["volume"] = pd.Series(index=data.index, dtype="int64")
        else:
            columns = (
                ["calendar_date", "is_trading_day"]
                if method == "query_trade_dates"
                else ["code", "code_name", "ipoDate", "outDate", "type", "status"]
            )
            data = pd.DataFrame(rows, columns=columns)
        return ResearchResult(
            data,
            {
                "capture_id": self._id,
                "network_used": False,
                "quality": self.quality(),
                "coverage": self.coverage(),
            },
        )

    def get_price(self, *, end=None, end_inclusive=True):
        require(
            self._manifest["version"] not in {daily_status.VERSION, preclose.VERSION},
            "QUERY_KIND_MISMATCH",
            "该字段证据不含OHLCV；请使用get_status/get_preclose",
        )
        return self._with_end(end, end_inclusive)._result("query_history_k_data_plus")

    def get_preclose(self, *, require_known=False):
        return preclose.project(self, require_known=require_known)

    def get_status(self, *, require_known=False):
        return daily_status.project(self, require_known=require_known)

    def get_security_info(self):
        return self._result("query_stock_basic")

    def get_trade_days(self):
        result = self._result("query_trade_dates")
        if not result.data.empty:
            result.data = result.data.loc[result.data["is_trading_day"] == "1"].copy()
        return result
