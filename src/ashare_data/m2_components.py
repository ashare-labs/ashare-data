"""Public, portable source components. Hash checks do not certify source truth."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import research
from ._bao_receipt.protocol import check_identity, decode_request, decode_response
from .d1 import _read, _safe_path
from .m2_types import canonical_bytes, canonical_hash, exact_day, json_loads, keys, sha
from .model import DataError, require
from .storage import identifier, immutable_write

SCHEMA = "m2.component.v1"
MAX_BYTES = 32 * 1024 * 1024
TABLE = "CREATE TABLE IF NOT EXISTS m2_components(id TEXT PRIMARY KEY, body TEXT NOT NULL)"
UNITS = {
    "prices": {"price": "CNY/share", "volume": "share", "amount": "CNY"},
    "states": {"price": "CNY/share"},
    "calendar": {},
    "evidence": {},
}


def raw_hash(blob):
    import hashlib

    return hashlib.sha256(blob).hexdigest()


def _time(value):
    from .m2_types import aware_time

    return None if value is None else aware_time(value)


def _refs(refs, names):
    require(type(refs) is list and bool(refs), "M2_FACT_MISSING", "须提供原件与定位")
    for ref in refs:
        keys(ref, ("document", "locator"))
        require(
            ref["document"] in names
            and type(ref["locator"]) is str
            and 0 < len(ref["locator"]) <= 2000,
            "M2_FACT_MISSING",
            "证据引用/定位无效",
        )


def _portable(value):
    if isinstance(value, dict):
        for v in value.values():
            _portable(v)
    elif isinstance(value, list):
        for v in value:
            _portable(v)
    elif isinstance(value, str):
        require(
            not any(marker in value for marker in ("/Users/", "/home/", "file://"))
            and not value.startswith(("/", "file:", "~", "\\\\"))
            and not (len(value) > 2 and value[1:3] in (":\\", ":/")),
            "M2_LOCAL_PATH",
            "组件元数据不能包含本机绝对路径",
        )


def _document(doc, objects):
    keys(doc, ("name", "role", "format", "units", "source_url", "observed_at", "artifacts"))
    require(
        type(doc["name"]) is str and 0 < len(doc["name"]) < 128,
        "M2_COMPONENT_SCHEMA",
        "逻辑文档名无效",
    )
    role, fmt = doc["role"], doc["format"]
    require(role in UNITS and doc["units"] == UNITS[role], "M2_UNIT_UNKNOWN", "不猜测/转换未知单位")
    artifacts = doc["artifacts"]
    require(type(artifacts) is dict, "M2_COMPONENT_SCHEMA", "artifacts须为对象")
    expected = {"request", "response", "receipt", "sdk"} if fmt == "baostock_capture" else {"raw"}
    require(set(artifacts) == expected, "M2_COMPONENT_SCHEMA", "原件角色不完整")
    for spec in artifacts.values():
        keys(spec, ("sha256", "bytes"))
        sha(spec["sha256"])
        blob = objects.get(spec["sha256"])
        require(
            type(spec["bytes"]) is int
            and 0 < spec["bytes"] <= MAX_BYTES
            and blob is not None
            and len(blob) == spec["bytes"]
            and raw_hash(blob) == spec["sha256"],
            "M2_COMPONENT_INTEGRITY",
            "原件缺失/大小或hash不符",
        )
    observed = _time(doc["observed_at"])
    if observed is not None:
        require(observed <= datetime.now(observed.tzinfo), "M2_COMPONENT_SCHEMA", "观测时间在未来")
    url = doc["source_url"]
    if url is not None:
        parsed = urlsplit(url)
        require(
            parsed.scheme in ("https", "http")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.fragment,
            "M2_COMPONENT_SCHEMA",
            "来源URL无效；不接受凭据",
        )
    if role == "evidence":
        require(
            fmt in ("official_document", "review_record"), "M2_COMPONENT_SCHEMA", "事实文档格式无效"
        )
        if fmt == "official_document":
            host = urlsplit(url or "").hostname or ""
            require(
                any(
                    host == x or host.endswith("." + x)
                    for x in ("sse.com.cn", "spdb.com.cn", "cninfo.com.cn")
                ),
                "M2_UNSUPPORTED_SOURCE",
                "本有限普通股事实仅接收已支持官方域名声明",
            )
        return None
    require(
        fmt in ("baostock_json", "baostock_capture"), "M2_UNSUPPORTED_SOURCE", "仅支持明确Bao源格式"
    )
    if fmt == "baostock_json":
        require(
            doc["observed_at"] is None, "M2_VISIBILITY", "旧JSON无接收凭证，不得补造observed_at"
        )
        payload = json_loads(objects[artifacts["raw"]["sha256"]])
        require(payload.get("error_code") == "0", "M2_SOURCE_CONFLICT", "源响应未成功")
        method, params, fields, rows = (payload[k] for k in ("method", "params", "fields", "rows"))
    else:
        blobs = {k: objects[v["sha256"]] for k, v in artifacts.items()}
        receipt = json_loads(blobs["receipt"])
        sent, received = decode_request(blobs["request"]), decode_response(blobs["response"])
        identity = check_identity(receipt["request"], sent, received, 1)
        require(
            not identity["issues"] and not identity["unknown"],
            "M2_SOURCE_CONFLICT",
            "wire请求/响应身份不符",
        )
        require(
            len(receipt["pages"]) == 1
            and received["rows"] == json_loads(blobs["sdk"]) == receipt["pages"][0]["rows"],
            "M2_SOURCE_CONFLICT",
            "wire/SDK/回执行不一致",
        )
        # A sidecar is not a historical availability proof. Compare its actual declared clock.
        finished = receipt.get("response_completed_at")
        if finished is None:
            finished = receipt.get("observed_at")
        require(observed == _time(finished), "M2_VISIBILITY", "捕获观测时间须与原回执一致")
        method, params = receipt["request"]["method"], receipt["request"]["params"]
        page = receipt["pages"][0]
        require(
            page["raw_response_sha256"] == raw_hash(blobs["response"])
            and page["raw_request_sha256"] == raw_hash(blobs["request"])
            and page["raw_response_bytes"] == len(blobs["response"]),
            "M2_SOURCE_CONFLICT",
            "捕获回执与原文字节引用不符",
        )
        fields, rows = received["fields"], received["rows"]
        if role == "calendar":
            require(receipt["query_complete"] is True, "M2_CALENDAR_MISSING", "源日历查询未闭合")
    start, end = exact_day(params["start_date"]), exact_day(params["end_date"])
    require(
        exact_day("2019-12-30") <= start <= end <= exact_day("2020-01-20"),
        "M2_ROLE_SCOPE",
        "首版只接收固定有限范围的原件，不能扩日期",
    )
    require(
        type(fields) is list
        and len(set(fields)) == len(fields)
        and type(rows) is list
        and 0 < len(rows) <= 32,
        "M2_COMPONENT_SCHEMA",
        "源字段/行数异常",
    )
    if role == "calendar":
        require(
            method == "query_trade_dates" and set(fields) == {"calendar_date", "is_trading_day"},
            "M2_SOURCE_CONFLICT",
            "须为源自然日日历",
        )
        date_key = "calendar_date"
    else:
        required = {"date", "code", "adjustflag"} | (
            {"open", "high", "low", "close", "volume", "amount", "tradestatus"}
            if role == "prices"
            else {"preclose", "isST"}
        )
        require(
            method == "query_history_k_data_plus"
            and params["code"] == "sh.600000"
            and params["frequency"] == "d"
            and params["adjustflag"] == "3"
            and params["fields"].split(",") == fields
            and required <= set(fields),
            "M2_SOURCE_CONFLICT",
            "证券/原价/日频/字段身份不符",
        )
        date_key = "date"
    previous = None
    for row in rows:
        require(
            type(row) is dict
            and set(row) == set(fields)
            and all(type(v) is str for v in row.values()),
            "M2_COMPONENT_SCHEMA",
            "须保留原字段字符串",
        )
        day = exact_day(row[date_key])
        require(
            start <= day <= end and (previous is None or day > previous),
            "M2_SOURCE_CONFLICT",
            "源行越界/重复/无序",
        )
        previous = day
        if role == "calendar":
            require(row["is_trading_day"] in ("0", "1"), "M2_CALENDAR_MISSING", "源开闭市状态未知")
        else:
            require(
                row["code"] == "sh.600000" and row["adjustflag"] == "3",
                "M2_SOURCE_CONFLICT",
                "源行证券或价基冲突",
            )
    if role == "calendar":
        require(len(rows) == (end - start).days + 1, "M2_CALENDAR_MISSING", "自然日日历有缺口")
    return {
        "role": role,
        "rows": rows,
        "request": {"method": method, "params": params},
        "source_kind": fmt,
        "observed_at": doc["observed_at"],
        "available_at": None,
        "units": doc["units"],
        "artifacts": artifacts,
        "capture_times": (
            {
                k: receipt.get(k)
                for k in (
                    "request_started_at",
                    "operation_completed_at",
                    "response_completed_at",
                    "local_visibility_at",
                )
            }
            if fmt == "baostock_capture"
            else None
        ),
        "object_sha256": artifacts["raw" if fmt == "baostock_json" else "response"]["sha256"],
    }


def validate(body, objects):
    try:
        return _validate(body, objects)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError("M2_COMPONENT_SCHEMA", "组件字段或原件格式无效") from exc


def _validate(body, objects):
    keys(body, ("schema", "kind", "security", "classification", "documents", "claims", "origin"))
    require(
        body["schema"] == SCHEMA
        and body["security"] == "600000.XSHG"
        and body["kind"] in ("market", "facts")
        and body["classification"] in ("user_supplied_source", "synthetic"),
        "M2_COMPONENT_SCHEMA",
        "组件类型/范围/分类无效",
    )
    _portable(body)
    docs = body["documents"]
    require(type(docs) is list and 0 < len(docs) <= 64, "M2_COMPONENT_SCHEMA", "组件文档数须1..64")
    require(
        sum(len(b) for b in objects.values()) <= MAX_BYTES, "M2_COMPONENT_SCHEMA", "组件超过32MiB"
    )
    names = [d["name"] for d in docs]
    require(len(set(names)) == len(names), "M2_COMPONENT_SCHEMA", "逻辑文档名重复")
    referenced = object_ids(body)
    origin = body["origin"]
    if origin is not None:
        keys(origin, ("dataset_id", "manifest"))
        spec = origin["manifest"]
        keys(spec, ("sha256", "bytes"))
        sha(origin["dataset_id"])
        raw = objects.get(spec["sha256"])
        require(
            raw is not None
            and raw_hash(raw) == origin["dataset_id"] == spec["sha256"]
            and type(spec["bytes"]) is int
            and len(raw) == spec["bytes"],
            "M2_COMPONENT_INTEGRITY",
            "研究来源清单hash/身份不符",
        )
        prior = json_loads(raw)
        require(prior.get("source") == "baostock_daily", "M2_UNSUPPORTED_SOURCE", "研究来源不支持")
        research._validate(prior, {h: b for h, b in objects.items() if h != origin["dataset_id"]})
    require(referenced == set(objects), "M2_COMPONENT_INTEGRITY", "组件原件闭包不匹配")
    observations = []
    for d in docs:
        require(
            (d["role"] == "evidence") == (body["kind"] == "facts"),
            "M2_COMPONENT_SCHEMA",
            "文档与组件类型不符",
        )
        obs = _document(d, objects)
        if obs is not None:
            observations.append(obs)
    claims = body["claims"]
    if body["kind"] == "market":
        require(claims is None, "M2_COMPONENT_SCHEMA", "市场组件不能夹带事实解释")
    else:
        keys(claims, ("facts", "events", "coverage"))
        require(
            type(claims["facts"]) is list
            and type(claims["events"]) is list
            and len(claims["facts"]) <= 32
            and len(claims["events"]) <= 128,
            "M2_COMPONENT_SCHEMA",
            "事实/事件集合无效",
        )
        ids = []
        for f in claims["facts"]:
            keys(f, ("id", "value", "status", "evidence"))
            require(
                f["status"] in ("source_document_claim_unverified", "unknown"),
                "M2_FACT_MISSING",
                "不接受用户输入冒充已核实事实",
            )
            _refs(f["evidence"], names)
            ids.append(f["id"])
        require(len(ids) == len(set(ids)), "M2_SOURCE_CONFLICT", "重复事实")
        for event in claims["events"]:
            _refs(event["evidence"], names)
        coverage = claims["coverage"]
        keys(
            coverage,
            (
                "start",
                "end",
                "reviewed_at",
                "known_event_completeness",
                "verified_absent",
                "historical_pit_verified",
                "window_domains",
                "checks",
                "evidence",
                "jan20_rule",
            ),
        )
        exact_day(coverage["start"])
        exact_day(coverage["end"])
        require(
            _time(coverage["reviewed_at"]) is not None
            and _time(coverage["reviewed_at"])
            <= datetime.now(_time(coverage["reviewed_at"]).tzinfo)
            and exact_day(coverage["start"]) <= exact_day(coverage["end"])
            and coverage["known_event_completeness"] == "unknown"
            and coverage["verified_absent"] is False
            and coverage["historical_pit_verified"] is False,
            "M2_FACT_MISSING",
            "有限解释不能升级完整性/PIT/无事件保证",
        )
        _refs(coverage["evidence"], names)
        keys(coverage["checks"], ("equity_events", "listing_exceptions", "rule_exceptions"))
        for item in coverage["checks"].values():
            keys(item, ("status", "rationale", "evidence"))
            require(
                item["status"] in ("bounded_review_no_conflict_identified", "unknown", "conflict")
                and type(item["rationale"]) is str
                and bool(item["rationale"].strip()),
                "M2_FACT_MISSING",
                "审阅检查无依据",
            )
            _refs(item["evidence"], names)
        rule = coverage["jan20_rule"]
        keys(rule, ("effective_date", "applies_to", "ordinary_a_share_resale", "evidence"))
        _refs(rule["evidence"], names)
    return observations


def load(store, component_id):
    identifier(component_id)
    with store._db() as db:
        present = db.execute("SELECT 1 FROM sqlite_master WHERE name='m2_components'").fetchone()
        row = (
            db.execute("SELECT body FROM m2_components WHERE id=?", (component_id,)).fetchone()
            if present
            else None
        )
    require(row is not None, "M2_COMPONENT_MISSING", "组件未导入", {"component_id": component_id})
    blob = _read(_safe_path(store.root, "m2-components/" + component_id + ".json"))
    require(
        raw_hash(blob) == component_id and blob == row["body"].encode(),
        "M2_COMPONENT_INTEGRITY",
        "组件清单不一致",
    )
    body = json_loads(blob)
    ids = object_ids(body)
    objects = {h: _read(_safe_path(store.root, "m2-objects/" + identifier(h))) for h in ids}
    validate(body, objects)
    return body, objects


def publish(store, body, objects):
    validate(body, objects)
    cid = canonical_hash(body)
    with store._writer():
        for name in ("m2-components", "m2-objects"):
            _safe_path(store.root, name).mkdir(exist_ok=True)
        for h, blob in objects.items():
            immutable_write(_safe_path(store.root, "m2-objects/" + h), blob)
        blob = canonical_bytes(body)
        immutable_write(_safe_path(store.root, "m2-components/" + cid + ".json"), blob)
        with store._db(write=True) as db:
            db.execute(TABLE)
            prior = db.execute("SELECT body FROM m2_components WHERE id=?", (cid,)).fetchone()
            require(
                prior is None or prior["body"] == blob.decode(),
                "M2_COMPONENT_INTEGRITY",
                "已有组件目录冲突",
            )
            db.execute("INSERT OR IGNORE INTO m2_components VALUES (?,?)", (cid, blob.decode()))
    load(store, cid)
    return cid


def object_ids(body):
    result = {v["sha256"] for d in body["documents"] for v in d["artifacts"].values()}
    if body.get("origin") is not None:
        result.add(body["origin"]["manifest"]["sha256"])
    return result


def import_component(store, directory):
    try:
        return _import_component(store, directory)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError("M2_COMPONENT_SCHEMA", "输入组件字段无效") from exc


def _import_component(store, directory):
    root = Path(directory).expanduser().absolute()
    config = json_loads(_read(_safe_path(root, "component.json")))
    require(
        type(config.get("documents")) is list and 0 < len(config["documents"]) <= 64,
        "M2_COMPONENT_SCHEMA",
        "组件文档数须1..64",
    )
    require(
        config.get("origin") is None,
        "M2_COMPONENT_SCHEMA",
        "研究origin由公开research导入生成，不能手填",
    )
    declared = [s for d in config["documents"] for s in d["artifacts"].values()]
    require(
        all(type(s["bytes"]) is int and 0 < s["bytes"] <= MAX_BYTES for s in declared)
        and sum(s["bytes"] for s in declared) <= MAX_BYTES,
        "M2_COMPONENT_SCHEMA",
        "输入超过有界大小",
    )
    objects = {}
    # Only path is removed; every declared content/semantic field remains identity-bearing.
    for d in config["documents"]:
        for spec in d["artifacts"].values():
            keys(spec, ("path", "sha256", "bytes"))
            blob = _read(_safe_path(root, spec.pop("path")))
            require(
                raw_hash(blob) == spec["sha256"] and len(blob) == spec["bytes"],
                "M2_COMPONENT_INTEGRITY",
                "输入原件hash/大小不符",
            )
            objects[spec["sha256"]] = blob
    return publish(store, config, objects)


def from_research(store, dataset_id):
    # Owner implementation may read its own validated component internals; downstream never does.
    view = store.research(dataset_id)
    require(
        view.descriptor()["source"] == "baostock_daily",
        "M2_UNSUPPORTED_SOURCE",
        "当前生产只支持Bao日线研究组件",
    )
    docs = []
    for h, blob in sorted(view._objects.items()):
        role = "calendar" if h == view._manifest.get("calendar_object") else "prices"
        docs.append(
            {
                "name": h,
                "role": role,
                "format": "baostock_json",
                "units": UNITS[role],
                "source_url": None,
                "observed_at": None,
                "artifacts": {"raw": {"sha256": h, "bytes": len(blob)}},
            }
        )
    body = {
        "schema": SCHEMA,
        "kind": "market",
        "security": "600000.XSHG",
        "classification": "user_supplied_source",
        "documents": docs,
        "claims": None,
        "origin": None,
    }
    original = _read(
        _safe_path(store.root, "research-manifests/" + identifier(dataset_id) + ".json")
    )
    body["origin"] = {
        "dataset_id": dataset_id,
        "manifest": {"sha256": raw_hash(original), "bytes": len(original)},
    }
    objects = {**view._objects, raw_hash(original): original}
    validate(body, objects)
    return body, objects


def import_sources(store, *, kind, documents, classification, claims=None):
    """Hash user-specified files without asking callers to assemble a hashed bundle."""
    import copy

    require(
        type(documents) is list and 0 < len(documents) <= 64, "M2_COMPONENT_SCHEMA", "文档数须1..64"
    )
    docs = copy.deepcopy(documents)
    objects = {}
    try:
        for doc in docs:
            for name, entry in list(doc["artifacts"].items()):
                keys(entry, ("path",))
                blob = _read(Path(entry["path"]).expanduser())
                h = raw_hash(blob)
                objects[h] = blob
                require(
                    sum(len(v) for v in objects.values()) <= MAX_BYTES,
                    "M2_COMPONENT_SCHEMA",
                    "组件合计超过32MiB",
                )
                doc["artifacts"][name] = {"sha256": h, "bytes": len(blob)}
        body = {
            "schema": SCHEMA,
            "kind": kind,
            "security": "600000.XSHG",
            "classification": classification,
            "documents": docs,
            "claims": claims,
            "origin": None,
        }
        return publish(store, body, objects)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError("M2_COMPONENT_SCHEMA", "来源配方字段无效") from exc
