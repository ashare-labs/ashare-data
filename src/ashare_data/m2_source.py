"""Finite offline M2 inputs, independently versioned from D1/BR1 and Bao capture APIs."""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from importlib.resources import files
from pathlib import Path

from ._bao_receipt.protocol import check_identity, decode_request, decode_response
from .d1 import _read, _safe_path
from .m2_types import (
    canonical_bytes,
    canonical_hash,
    decimal_text,
    exact_day,
    exact_int,
    json_loads,
)
from .model import DataError, require
from .storage import identifier, immutable_write, now

TABLE = """CREATE TABLE IF NOT EXISTS m2_snapshots
(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL
 CHECK(status IN ('prepared','published','aborted')), created_at TEXT NOT NULL)"""


def raw_hash(blob):
    return hashlib.sha256(blob).hexdigest()


def source_volume(value):
    """Bound raw digits before int conversion; preserve the original source string."""
    require(
        type(value) is str and 0 < len(value) <= 24 and value.isascii() and value.isdigit(),
        "M2_INTEGER",
        "源股数须为1..24位ASCII非负整数字符串",
    )
    return exact_int(int(value))


def source_prices(row):
    o, hi, lo, c = [Decimal(decimal_text(row[k])) for k in ("open", "high", "low", "close")]
    require(0 < lo <= min(o, c) <= max(o, c) <= hi, "M2_SOURCE_CONFLICT", "OHLC关系错误")
    source_volume(row["volume"])
    if row.get("amount"):
        require(Decimal(decimal_text(row["amount"])) >= 0, "M2_SOURCE_CONFLICT", "amount非法")


def source_limits(preclose):
    """Validate both raw preclose and the actual consumer-derived decimal fields."""
    ref = Decimal(decimal_text(preclose))
    require(ref > 0, "M2_DECIMAL", "源前收须为正数")
    limits = tuple(
        (ref * Decimal(ratio)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        for ratio in ("0.9", "1.1")
    )
    for value in limits:
        decimal_text(value)
    return limits


def event_identities(events):
    """Check every event before security relevance filtering; identity is not truth proof."""
    require(type(events) is list and len(events) <= 128, "M2_COMPONENT_SCHEMA", "事件集合无效")
    ids = set()
    for event in events:
        require(type(event) is dict, "M2_COMPONENT_SCHEMA", "事件须为对象")
        for field in ("id", "event_type"):
            value = event.get(field)
            require(
                type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value),
                "M2_COMPONENT_SCHEMA",
                "事件ID/类型须为明确的非空标识符",
            )
        security = event.get("entitled_security")
        require(
            type(security) is str and re.fullmatch(r"[0-9]{6}\.(XSHG|XSHE|XBSE)", security),
            "M2_COMPONENT_SCHEMA",
            "事件权益证券须为明确代码；未知归属不能视作无关",
        )
        require(event["id"] not in ids, "M2_SOURCE_CONFLICT", "重复事件ID")
        ids.add(event["id"])
        for key in (
            "record_date",
            "ex_date",
            "pay_date",
            "effective_date",
            "subscription_date",
            "conversion_start",
            "new_share_listing_date",
        ):
            if key in event and event[key] is not None:
                require(type(event[key]) is str, "M2_COMPONENT_SCHEMA", "事件日期须为字符串或null")
                exact_day(event[key])


def policy():
    return json_loads(files("ashare_data").joinpath("m2-policy.json").read_bytes())


def source_identity():
    return canonical_hash(
        {
            name: raw_hash(files("ashare_data").joinpath(name).read_bytes())
            for name in ("m2_source.py", "m2_components.py", "m2_compose.py")
        }
    )


def _manifest(blob):
    p = policy()
    require(
        raw_hash(blob) == p["input_manifest_sha256"],
        "M2_UNREVIEWED_INPUT",
        "只接受该产品绑定的有限来源包；自行重算清单不构成准入",
    )
    m = json_loads(blob)
    require(
        m["schema"] == "m2.inputs.v1" and m["security"] == "600000.XSHG",
        "M2_SOURCE_CONFLICT",
        "来源类型不符",
    )
    return m


def _verify_objects(manifest_blob, objects):
    m = _manifest(manifest_blob)
    require(set(objects) == set(m["files"]), "M2_INTEGRITY", "原文引用闭包不完整")
    for name, spec in m["files"].items():
        require(
            raw_hash(objects[name]) == spec["sha256"] and len(objects[name]) == spec["bytes"],
            "M2_INTEGRITY",
            "原文字节/大小不符",
            {"name": name},
        )
    return m


def _wire(objects, kind):
    prefix = "new/" + kind + "/"
    r = json_loads(objects[prefix + "capture/receipt.json"])
    sent = decode_request(objects[prefix + "capture/page-01.request.bin"])
    received = decode_response(objects[prefix + "capture/page-01.response.bin"])
    identity = check_identity(r["request"], sent, received, 1)
    require(
        not identity["issues"] and not identity["unknown"], "M2_SOURCE_CONFLICT", "请求响应身份不符"
    )
    sdk = json_loads(objects[prefix + "capture/sdk-rows.json"])
    require(
        received["rows"] == sdk and len(r["pages"]) == 1 and r["pages"][0]["rows"] == sdk,
        "M2_SOURCE_CONFLICT",
        "SDK/协议/回执行不符",
    )
    expected = (
        ["calendar_date", "is_trading_day"]
        if kind == "calendar"
        else (
            ["date", "code", "preclose", "isST", "adjustflag"]
            if kind == "supplement"
            else [
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
            ]
        )
    )
    require(received["fields"] == expected, "M2_SOURCE_CONFLICT", "冻结源字段/顺序不符")
    if kind != "calendar":
        require(
            r["request"]["params"]["frequency"] == "d"
            and r["request"]["params"]["adjustflag"] == "3"
            and r["request"]["params"]["code"] == "sh.600000"
            and r["query_complete"] is False
            and r["available_at"] is None
            and received["integrity"] == "compressed_outer_integrity_unverified",
            "M2_SOURCE_CONFLICT",
            "日线价基/身份或原unknown改变",
        )
    else:
        require(r["query_complete"] is True, "M2_DATA_MISSING", "源日历请求未闭合")
    return sdk


def parse_inputs(objects):
    """Re-derive facts from raw rows, not the author's sample summary. No truth promotion."""
    old = json_loads(objects["old/daily.json"])
    require(
        old["params"]["code"] == "sh.600000"
        and old["params"]["frequency"] == "d"
        and old["params"]["adjustflag"] == "3",
        "M2_SOURCE_CONFLICT",
        "旧源身份不符",
    )
    daily, supplement, newcal = (_wire(objects, k) for k in ("daily", "supplement", "calendar"))
    bars, states, row_sources = {}, {}, {}
    for rows, source in (
        (old["rows"], "old/daily.json"),
        (daily, "new/daily/capture/sdk-rows.json"),
    ):
        for row in rows:
            day = exact_day(row["date"]).isoformat()
            require(
                day not in bars and row["code"] == "sh.600000" and row["adjustflag"] == "3",
                "M2_SOURCE_CONFLICT",
                "重复/冲突源日行",
            )
            prices = [Decimal(decimal_text(row[k])) for k in ("open", "high", "low", "close")]
            opening, high, low, close = prices
            require(
                0 < low <= min(opening, close) <= max(opening, close) <= high,
                "M2_SOURCE_CONFLICT",
                "OHLC关系不符",
            )
            require(
                type(row["volume"]) is str and row["volume"].isascii() and row["volume"].isdigit(),
                "M2_UNIT_UNKNOWN",
                "源volume须为非负整数字符串，规范单位股",
            )
            if row.get("amount") not in (None, ""):
                require(
                    Decimal(decimal_text(row["amount"])) >= 0, "M2_SOURCE_CONFLICT", "amount非法"
                )
            bars[day] = row
            row_sources[day] = {
                "price_object_sha256": raw_hash(objects[source]),
                "price_row_sha256": canonical_hash(row),
            }
            if "preclose" in row:
                states[day] = row
    for row in supplement:
        day = exact_day(row["date"]).isoformat()
        require(
            day not in states
            and day in bars
            and row["code"] == "sh.600000"
            and row["adjustflag"] == "3",
            "M2_SOURCE_CONFLICT",
            "补字段join身份不符",
        )
        states[day] = row
    for day, row in states.items():
        source = "old/daily.json" if day <= "2020-01-07" else "new/supplement/capture/sdk-rows.json"
        row_sources[day].update(
            state_object_sha256=raw_hash(objects[source]), state_row_sha256=canonical_hash(row)
        )
        require(
            Decimal(decimal_text(row["preclose"])) > 0, "M2_SOURCE_CONFLICT", "源preclose须正有限"
        )
    calendar = {}
    for row in [*json_loads(objects["old/calendar.json"])["rows"], *newcal]:
        d = exact_day(row["calendar_date"]).isoformat()
        require(
            d not in calendar and row["is_trading_day"] in ("0", "1"),
            "M2_SOURCE_CONFLICT",
            "源日历冲突",
        )
        calendar[d] = row["is_trading_day"] == "1"
    opened = sorted(d for d, v in calendar.items() if v)
    events = json_loads(objects["official/events.json"])
    annex = json_loads(objects["expansion-annex.json"])
    for name, h in annex["official_and_review_objects"].items():
        require(
            name in objects and raw_hash(objects[name]) == h,
            "M2_INTEGRITY",
            "扩窗事实/规则引用缺失",
        )
    facts = {f["id"]: f for f in json_loads(objects["official/facts.json"])["facts"]}
    for name, value in [
        ("buy_round_lot", 100),
        ("price_tick", "0.01"),
        ("normal_daily_limit_ratio", "0.10"),
        ("same_day_resale", False),
    ]:
        require(facts[name]["value"] == value, "M2_RULE_EXCEPTION", "原规则事实改变")
    exact_int(facts["buy_round_lot"]["value"], minimum=100, maximum=100)
    require(facts["same_day_resale"]["value"] is False, "M2_RULE_EXCEPTION", "T+1事实类型不符")
    require(
        facts["security_identity"]["value"]["security"] == "600000.XSHG"
        and facts["security_identity"]["value"]["kind"] == "ordinary_A_share"
        and facts["initial_listing_date"]["value"] == "1999-11-10",
        "M2_RULE_EXCEPTION",
        "证券/普通股/初始挂牌事实冲突",
    )
    return {
        "bars": bars,
        "states": states,
        "row_sources": row_sources,
        "calendar": calendar,
        "opened": opened,
        "events": events,
        "annex": annex,
        "facts": facts,
        "identities": json_loads(objects["source-identities.json"]),
    }


def check_window(data, spec):
    start, end = exact_day(spec["warmup"]), exact_day(spec["settlement_successor"])
    require((end - start).days <= 30, "M2_ROLE_SCOPE", "最多31自然日")
    for i in range((end - start).days + 1):
        require(
            (start + timedelta(days=i)).isoformat() in data["calendar"],
            "M2_DATA_MISSING",
            "缺自然日源日历",
        )
    days = [d for d in data["opened"] if spec["start"] <= d <= spec["end"]]
    require(
        days == spec["execution_days"] and 0 < len(days) <= 20, "M2_DATA_MISSING", "执行日栅格不符"
    )
    require(
        spec["warmup"] == spec["calendar_links"][0]["previous_open"]
        and spec["settlement_successor"] == spec["calendar_links"][-1]["next_open"]
        and spec["listing_read_dates"] == [spec["warmup"], *days, spec["settlement_successor"]],
        "M2_ROLE_SCOPE",
        "warmup/末后继/挂牌域不等于逐日源链",
    )
    for link in spec["calendar_links"]:
        i = data["opened"].index(link["current"])
        require(
            i > 0
            and i + 1 < len(data["opened"])
            and (link["previous_open"], link["next_open"])
            == (data["opened"][i - 1], data["opened"][i + 1]),
            "M2_SUCCESSOR_INVALID",
            "P/严格后继不等于源日历",
        )
    for d in spec["listing_read_dates"]:
        require(d in data["bars"] and d in data["states"], "M2_DATA_MISSING", "必要行/状态缺失")
        r, s = data["bars"][d], data["states"][d]
        source_prices(r)
        source_limits(s["preclose"])
        require(
            r.get("tradestatus") == "1" and s.get("isST") == "0",
            "M2_STATE_UNKNOWN",
            "停牌/ST为真或未知，本profile停止",
        )
        prev = data["opened"][data["opened"].index(d) - 1]
        require(prev in data["bars"], "M2_DATA_MISSING", "价基锚点缺失")
        require(
            Decimal(decimal_text(s["preclose"]))
            == Decimal(decimal_text(data["bars"][prev]["close"])),
            "M2_SOURCE_CONFLICT",
            "preclose不等于P raw close；不得选择一个值或静默改价基",
        )
    domains = data["annex"]["window_domains"][spec["window_id"]]
    require(
        domains["price_basis"] == spec["price_basis_domain"]
        and domains["registration"] == spec["potential_holding_registration_domain"],
        "M2_ROLE_SCOPE",
        "扩窗事实域与profile不符",
    )
    screen_start = spec["warmup"]
    screen_end = spec["settlement_successor"]
    event_identities(data["events"])
    for e in data["events"]:
        if e["entitled_security"] != "600000.XSHG":
            continue
        # Explicit known-event schema: malformed/unknown types or missing key dates cannot mean absence.
        typ = e["event_type"]
        mandatory = (
            ("record_date", "ex_date", "pay_date")
            if typ == "cash_dividend"
            else (
                ("record_date", "subscription_date", "conversion_start")
                if typ == "convertible_priority_subscription"
                else ()
            )
        )
        require(
            mandatory and all(e.get(k) for k in mandatory),
            "M2_RELATED_EVENT",
            "已知相关事件关键日期或类型未知",
        )
        for k in (
            "record_date",
            "ex_date",
            "pay_date",
            "effective_date",
            "subscription_date",
            "conversion_start",
            "new_share_listing_date",
        ):
            if e.get(k):
                d = exact_day(e[k]).isoformat()
                require(
                    not screen_start <= d <= screen_end,
                    "M2_RELATED_EVENT",
                    "已知权益日期落入价基/登记/后继域",
                    {"event_id": e["id"], "field": k},
                )
    require(
        data["annex"]["verified_absent"] is False
        and data["annex"]["historical_pit_verified"] is False,
        "M2_SOURCE_CONFLICT",
        "不允许升级unknown",
    )


def body_for(manifest_blob):
    m = _manifest(manifest_blob)
    return {
        "schema": "m2.dataset.v1",
        "input_manifest_sha256": raw_hash(manifest_blob),
        "parser_sha256": source_identity(),
        "policy_sha256": canonical_hash(policy()),
        "files": m["files"],
        "execution_permission": False,
    }


def _exists(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='m2_snapshots' AND type='table'"
    ).fetchone()


def _load_objects(store, body):
    if body.get("schema") == "m2.dataset.v2":
        from .m2_compose import validate_product

        objects = {
            h: _read(_safe_path(store.root, "m2-objects/" + identifier(h))) for h in body["files"]
        }
        return validate_product(body, objects)
    require(
        body
        == body_for(_read(_safe_path(store.root, "m2-objects/" + body["input_manifest_sha256"]))),
        "M2_IDENTITY_MISMATCH",
        "数据清单不符当前固定来源/解析器/policy",
    )
    manifest_blob = _read(_safe_path(store.root, "m2-objects/" + body["input_manifest_sha256"]))
    objects = {
        n: _read(_safe_path(store.root, "m2-objects/" + s["sha256"]))
        for n, s in body["files"].items()
    }
    _verify_objects(manifest_blob, objects)
    data = parse_inputs(objects)
    for w in policy()["windows"].values():
        check_window(data, w)
    return data


def _load(store, dataset_id):
    identifier(dataset_id)
    with store._db() as db:
        require(_exists(db), "M2_NOT_FOUND", "无M2来源包")
        row = db.execute(
            "SELECT body,status FROM m2_snapshots WHERE id=?", (dataset_id,)
        ).fetchone()
    require(
        row is not None and row["status"] == "published", "M2_NOT_PUBLISHED", "M2版本未发布/不存在"
    )
    blob = _read(_safe_path(store.root, "m2-manifests/" + dataset_id + ".json"))
    require(
        raw_hash(blob) == dataset_id and blob == row["body"].encode(),
        "M2_INTEGRITY",
        "目录/清单hash不符",
    )
    body = json_loads(blob)
    return body, _load_objects(store, body)


def load(store, dataset_id):
    try:
        return _load(store, dataset_id)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise DataError("M2_INTEGRITY", "M2清单或原文引用损坏") from exc


def import_inputs(store, directory, *, _fault=None):
    root = Path(directory).expanduser().absolute()
    manifest_blob = _read(_safe_path(root, "manifest.json"))
    candidate = json_loads(manifest_blob)
    require(type(candidate) is dict, "M2_SCHEMA", "输入清单须为对象")
    if candidate.get("schema") == "m2.export.v1":
        from .m2_compose import import_export

        return import_export(store, root, candidate)
    manifest = _manifest(manifest_blob)
    objects = {n: _read(_safe_path(root, n)) for n in manifest["files"]}
    _verify_objects(manifest_blob, objects)
    data = parse_inputs(objects)
    for w in policy()["windows"].values():
        check_window(data, w)
    body = body_for(manifest_blob)
    blob, sid = canonical_bytes(body), canonical_hash(body)
    with store._writer():
        for name in ("m2-objects", "m2-manifests"):
            _safe_path(store.root, name).mkdir(exist_ok=True)
        with store._db(write=True) as db:
            db.execute(TABLE)
            prior = db.execute("SELECT status FROM m2_snapshots WHERE id=?", (sid,)).fetchone()
        if prior and prior["status"] == "published":
            load(store, sid)
            return sid
        require(prior is None, "M2_RECOVERY_REQUIRED", "未完成/已拒发布须显式恢复或新隔离目录")
        for data_blob in [manifest_blob, *objects.values()]:
            immutable_write(_safe_path(store.root, "m2-objects/" + raw_hash(data_blob)), data_blob)
        with store._db(write=True) as db:
            db.execute(
                "INSERT INTO m2_snapshots VALUES (?,?,?,?)", (sid, blob.decode(), "prepared", now())
            )
        if _fault == "after_prepare":
            raise RuntimeError("injected M2 after_prepare")
        immutable_write(_safe_path(store.root, "m2-manifests/" + sid + ".json"), blob)
        if _fault == "after_manifest":
            raise RuntimeError("injected M2 after_manifest")
        with store._db(write=True) as db:
            db.execute("UPDATE m2_snapshots SET status='published' WHERE id=?", (sid,))
    return sid


def snapshots(store):
    with store._db() as db:
        return (
            []
            if not _exists(db)
            else [
                dict(r)
                for r in db.execute(
                    "SELECT id AS dataset_id,status,created_at FROM m2_snapshots ORDER BY id"
                )
            ]
        )


def recover(store):
    result = []
    with store._writer():
        with store._db() as db:
            pending = (
                []
                if not _exists(db)
                else db.execute(
                    "SELECT * FROM m2_snapshots WHERE status='prepared' ORDER BY id"
                ).fetchall()
            )
        for row in pending:
            try:
                require(
                    raw_hash(row["body"].encode()) == row["id"], "M2_INTEGRITY", "prepared摘要不符"
                )
                _load_objects(store, json_loads(row["body"]))
                immutable_write(
                    _safe_path(store.root, "m2-manifests/" + identifier(row["id"]) + ".json"),
                    row["body"].encode(),
                )
                status, error = "published", None
            except (DataError, OSError, KeyError, TypeError, ValueError) as exc:
                status, error = (
                    "aborted",
                    exc.code if isinstance(exc, DataError) else "M2_INTEGRITY",
                )
            with store._db(write=True) as db:
                db.execute("UPDATE m2_snapshots SET status=? WHERE id=?", (status, row["id"]))
            result.append({"dataset_id": row["id"], "status": status, "error": error})
    return result
