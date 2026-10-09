"""Portable M2 composition from public source components. Never acquires data."""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path

from . import m2_components as components
from . import m2_source as source
from .d1 import _read, _safe_path
from .m2_types import (
    M2Document,
    canonical_bytes,
    canonical_hash,
    exact_day,
    keys,
    json_loads,
)
from .model import DataError, require
from .storage import identifier, immutable_write, now

SCHEMA = "m2.dataset.v2"
REPORT_TABLE = "CREATE TABLE IF NOT EXISTS m2_reports(id TEXT PRIMARY KEY, body TEXT NOT NULL)"


def _code():
    from .m2 import code_identity

    return code_identity()


def _resolve(store, cid, allow_research=False):
    try:
        return components.load(store, cid)
    except DataError as exc:
        if not allow_research or exc.code != "M2_COMPONENT_MISSING":
            raise
    return components.from_research(store, cid)


def _selection(window_ids, mode):
    require(
        mode == "conditional_research" and type(mode) is str,
        "M2_EXPLICIT_MODE_REQUIRED",
        "须显式条件研究模式",
    )
    require(
        type(window_ids) in (tuple, list)
        and 0 < len(window_ids) <= 3
        and all(type(x) is str and x in ("m2a", "w1", "w2") for x in window_ids)
        and len(set(window_ids)) == len(window_ids),
        "M2_WINDOW_UNKNOWN",
        "须明确选择原窗口，不能重复",
    )
    return sorted(window_ids)


def _assemble(inputs, manifests, objects):
    """Rebuild every semantic field from component bytes; no normalized cache is trusted."""
    gaps = []

    def gap(code, **scope):
        gaps.append({"code": code, **scope})

    for cid, body in manifests.items():
        require(canonical_hash(body) == cid, "M2_COMPONENT_INTEGRITY", "组件身份不符")
        selected = components.object_ids(body)
        components.validate(body, {h: objects[h] for h in selected})
        if body["classification"] == "synthetic":
            gap("M2_SYNTHETIC_SOURCE", component_id=cid)
    seen_objects = set().union(*(components.object_ids(b) for b in manifests.values()))
    require(set(objects) == seen_objects, "M2_COMPONENT_INTEGRITY", "产品原件闭包不匹配")
    observations = {}
    for role in ("prices", "states", "calendar"):
        cid = inputs[role]
        body = manifests[cid]
        require(body["kind"] == "market", "M2_COMPONENT_SCHEMA", "价格/状态/日历须为market组件")
        selected = components.object_ids(body)
        all_obs = components.validate(body, {h: objects[h] for h in selected})
        observations[role] = [
            o
            for o in all_obs
            if o["role"] == role
            or (
                role == "states"
                and o["role"] == "prices"
                and any("preclose" in x and "isST" in x for x in o["rows"])
            )
        ]
    bars, states, calendar, refs, calendar_refs = {}, {}, {}, {}, []
    identities = []
    for role, entries in observations.items():
        target = {"prices": bars, "states": states, "calendar": calendar}[role]
        for obs in entries:
            identities.append({k: v for k, v in obs.items() if k != "rows"})
            if role == "calendar":
                calendar_refs.append(obs["object_sha256"])
            for row in obs["rows"]:
                day = row["calendar_date" if role == "calendar" else "date"]
                if day in target:
                    gap("M2_SOURCE_CONFLICT", role=role, date=day, reason="duplicate_source_row")
                    continue
                if role == "states" and not {"preclose", "isST"} <= set(row):
                    continue
                target[day] = row["is_trading_day"] == "1" if role == "calendar" else row
                if role != "calendar":
                    prefix = "price" if role == "prices" else "state"
                    refs.setdefault(day, {}).update(
                        {
                            prefix + "_object_sha256": obs["object_sha256"],
                            prefix + "_row_sha256": canonical_hash(row),
                        }
                    )
    for day, row in bars.items():
        try:
            source.source_prices(row)
        except DataError as exc:
            gap(exc.code, role="prices", date=day, field="OHLCV/amount")
    for day, row in states.items():
        try:
            source.source_limits(row["preclose"])
        except DataError as exc:
            gap(exc.code, role="states", date=day, field="preclose/limits")
    fact_body = manifests[inputs["facts"]]
    require(fact_body["kind"] == "facts", "M2_COMPONENT_SCHEMA", "须提供facts组件")
    claims = fact_body["claims"]
    facts = {f["id"]: f for f in claims["facts"]}
    required = {
        "security_identity": {"security": "600000.XSHG", "kind": "ordinary_A_share"},
        "initial_listing_date": "1999-11-10",
        "buy_round_lot": 100,
        "price_tick": "0.01",
        "normal_daily_limit_ratio": "0.10",
        "same_day_resale": False,
    }
    for name, expected in required.items():
        f = facts.get(name)
        if f is None or f["status"] != "source_document_claim_unverified" or f["value"] is None:
            gap("M2_FACT_MISSING", field=name)
        elif name == "security_identity":
            if type(f["value"]) is not dict or any(
                f["value"].get(k) != v for k, v in expected.items()
            ):
                gap("M2_RULE_EXCEPTION", field=name)
        elif type(f["value"]) is not type(expected) or f["value"] != expected:
            gap("M2_RULE_EXCEPTION", field=name)
    coverage = claims["coverage"]
    for kind, check in coverage["checks"].items():
        if check["status"] != "bounded_review_no_conflict_identified":
            gap(
                "M2_FACT_MISSING" if check["status"] == "unknown" else "M2_SOURCE_CONFLICT",
                field=kind,
            )
    rule = coverage["jan20_rule"]
    if (rule["effective_date"], rule["applies_to"], rule["ordinary_a_share_resale"]) != (
        "2020-01-20",
        "commodity_futures_etf_only",
        "T+1",
    ):
        gap("M2_RULE_EXCEPTION", field="jan20_rule")
    p = copy.deepcopy(source.policy())
    p["version"] = "m2.portable.policy.v1"
    p["windows"] = {wid: p["windows"][wid] for wid in inputs["window_ids"]}
    p["profiles"] = {n: [w for w in ws if w in p["windows"]] for n, ws in p["profiles"].items()}
    p["profiles"] = {n: ws for n, ws in p["profiles"].items() if ws}
    p["input_manifest_sha256"] = canonical_hash(inputs)
    p["annex_sha256"] = canonical_hash(
        {"facts_component_id": inputs["facts"], "coverage": coverage}
    )
    for wid, spec in p["windows"].items():
        start, end = exact_day(spec["warmup"]), exact_day(spec["settlement_successor"])
        anchor = "2019-12-31" if spec["warmup"] == "2020-01-02" else "2020-01-02"
        for i in range((end - exact_day(anchor)).days + 1):
            day = (exact_day(anchor) + timedelta(days=i)).isoformat()
            if day not in calendar:
                gap("M2_CALENDAR_MISSING", window_id=wid, date=day, role="natural_day")
        for day in spec["listing_read_dates"]:
            if day not in bars:
                gap(
                    "M2_DATA_MISSING",
                    window_id=wid,
                    role="listing_or_price",
                    date=day,
                    field="OHLCV/tradestatus",
                )
            if day not in states:
                gap(
                    "M2_STATE_UNKNOWN",
                    window_id=wid,
                    role="listing_or_state",
                    date=day,
                    field="preclose/isST",
                )
            elif states[day].get("isST") != "0":
                gap("M2_STATE_UNKNOWN", window_id=wid, date=day, field="isST")
            if day in bars and bars[day].get("tradestatus") != "1":
                gap("M2_STATE_UNKNOWN", window_id=wid, date=day, field="tradestatus")
        if anchor not in bars:
            gap(
                "M2_DATA_MISSING",
                window_id=wid,
                role="price_basis_anchor",
                date=anchor,
                field="raw_close",
            )
        expected_domains = {
            "price_basis": spec["price_basis_domain"],
            "registration": spec["potential_holding_registration_domain"],
            "listing": {"start": str(start), "end": str(end)},
        }
        if coverage["window_domains"].get(wid) != expected_domains or not (
            exact_day(coverage["start"]) <= start <= end <= exact_day(coverage["end"])
        ):
            gap("M2_FACT_MISSING", window_id=wid, field="coverage_domains")
        spec["owner_validation_dependencies"] = {
            "security": "600000.XSHG",
            "raw_price_dates": sorted({anchor, *spec["listing_read_dates"]}),
            "state_dates": list(spec["listing_read_dates"]),
            "calendar": {"start": anchor, "end": spec["settlement_successor"]},
            "consumer_read_permission": False,
        }
        spec["calendar_evidence_references"] = sorted(set(calendar_refs))
        spec["evidence_references"] = {
            "input_manifest_sha256": p["input_manifest_sha256"],
            "expansion_annex_sha256": p["annex_sha256"],
            "facts_component_sha256": inputs["facts"],
        }
    annex = {
        "window_domains": coverage["window_domains"],
        "verified_absent": False,
        "historical_pit_verified": False,
        "facts_component_sha256": inputs["facts"],
        "coverage": coverage,
        "evidence_status": "source_document_claim_unverified",
    }
    data = {
        "bars": bars,
        "states": states,
        "row_sources": refs,
        "calendar": calendar,
        "opened": sorted(d for d, is_open in calendar.items() if is_open),
        "events": claims["events"],
        "annex": annex,
        "facts": facts,
        "identities": {
            "components": inputs,
            "observations": identities,
            "fact_source_kind": "source_document_claim_unverified",
            "fact_documents": fact_body["documents"],
        },
        "_policy": p,
    }
    # The original finite policy enforces known events, source prevclose, links and restrictions.
    if not gaps:
        for wid, spec in p["windows"].items():
            try:
                source.check_window(data, spec)
            except DataError as exc:
                gap(exc.code, window_id=wid, reason=exc.message, details=exc.details)
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                gap(
                    "M2_FACT_MISSING",
                    window_id=wid,
                    reason="required_fact_or_row_invalid",
                    detail=type(exc).__name__,
                )
    # Do not describe an absent calendar as complete merely because a policy expects completeness.
    report_quality = dict(p["quality"])
    if any(g["code"] == "M2_CALENDAR_MISSING" or g.get("role") == "calendar" for g in gaps):
        report_quality["calendar_query_complete"] = None
    # A deterministic report, including the original unknowns, is not an independent PASS.
    report = {
        "schema": "m2.composition-report.v1",
        "status": "BLOCKED" if gaps else "READY_CONDITIONAL",
        "producer_sha256": _code(),
        "inputs": inputs,
        "gaps": sorted(gaps, key=canonical_bytes),
        "quality": report_quality,
        "external_product_review": "PENDING",
        "execution_permission": False,
    }
    return report, data


def _report(store, body):
    rid = canonical_hash(body)
    blob = canonical_bytes(body)
    with store._writer():
        _safe_path(store.root, "m2-reports").mkdir(exist_ok=True)
        immutable_write(_safe_path(store.root, "m2-reports/" + rid + ".json"), blob)
        with store._db(write=True) as db:
            db.execute(REPORT_TABLE)
            prior = db.execute("SELECT body FROM m2_reports WHERE id=?", (rid,)).fetchone()
            require(
                prior is None or prior["body"] == blob.decode(), "M2_INTEGRITY", "已有报告目录冲突"
            )
            db.execute("INSERT OR IGNORE INTO m2_reports VALUES (?,?)", (rid, blob.decode()))
    return M2Document.of({"report_id": rid, **body})


def report(store, report_id):
    identifier(report_id)
    with store._db() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='m2_reports'").fetchone()
        row = (
            db.execute("SELECT body FROM m2_reports WHERE id=?", (report_id,)).fetchone()
            if exists
            else None
        )
    require(row is not None, "M2_REPORT_MISSING", "没有该报告")
    blob = _read(_safe_path(store.root, "m2-reports/" + report_id + ".json"))
    require(
        source.raw_hash(blob) == report_id and blob == row["body"].encode(),
        "M2_INTEGRITY",
        "报告hash/目录不符",
    )
    return M2Document.of({"report_id": report_id, **json_loads(blob)})


def prepare(
    store,
    price_dataset_id,
    *,
    facts_component_id,
    calendar_component_id=None,
    state_component_id=None,
    window_ids,
    mode,
):
    selected = _selection(window_ids, mode)
    ids = {
        "prices": price_dataset_id,
        "facts": facts_component_id,
        "calendar": calendar_component_id or price_dataset_id,
        "states": state_component_id or price_dataset_id,
    }
    manifests, objects, resolved, missing = {}, {}, {}, []
    for role, cid in ids.items():
        identifier(cid)
        try:
            body, obs = _resolve(store, cid, allow_research=role != "facts")
        except DataError as exc:
            if exc.code not in ("M2_COMPONENT_MISSING", "RESEARCH_NOT_PUBLISHED"):
                raise
            missing.append({"code": "M2_COMPONENT_MISSING", "role": role, "component_id": cid})
            continue
        identity = canonical_hash(body)
        resolved[role] = identity
        manifests[identity] = body
        objects.update(obs)
    if missing:
        r = {
            "schema": "m2.composition-report.v1",
            "status": "BLOCKED",
            "producer_sha256": _code(),
            "inputs": {**ids, "window_ids": selected, "mode": mode},
            "gaps": missing,
            "quality": {**source.policy()["quality"], "calendar_query_complete": None},
            "external_product_review": "PENDING",
            "execution_permission": False,
        }
        return _report(store, r), None, {}, None
    inputs = {**resolved, "window_ids": selected, "mode": mode}
    r, data = _assemble(inputs, manifests, objects)
    doc = _report(store, r)
    body = {
        "schema": SCHEMA,
        "producer_sha256": _code(),
        "inputs": inputs,
        "components": manifests,
        "files": {h: {"sha256": h, "bytes": len(v)} for h, v in sorted(objects.items())},
        "validation_report": r,
        "execution_permission": False,
    }
    return doc, body, objects, data


def validate(store, price_dataset_id, **kwargs):
    return prepare(store, price_dataset_id, **kwargs)[0]


def validate_product(body, objects):
    keys(
        body,
        (
            "schema",
            "producer_sha256",
            "inputs",
            "components",
            "files",
            "validation_report",
            "execution_permission",
        ),
    )
    require(
        body["schema"] == SCHEMA
        and body["producer_sha256"] == _code()
        and body["execution_permission"] is False,
        "M2_IDENTITY_MISMATCH",
        "产品代码身份/类型不匹配",
    )
    inputs = body["inputs"]
    keys(inputs, ("prices", "states", "calendar", "facts", "window_ids", "mode"))
    require(
        inputs["window_ids"] == _selection(inputs["window_ids"], inputs["mode"]),
        "M2_SCHEMA",
        "窗口列表须规范排序",
    )
    require(
        set(body["components"]) == {inputs[k] for k in ("prices", "states", "calendar", "facts")},
        "M2_COMPONENT_INTEGRITY",
        "产品组件闭包不匹配",
    )
    require(
        body["files"]
        == {h: {"sha256": source.raw_hash(b), "bytes": len(b)} for h, b in objects.items()},
        "M2_INTEGRITY",
        "原件清单/hash不符",
    )
    r, data = _assemble(inputs, body["components"], objects)
    require(
        r == body["validation_report"] and not r["gaps"],
        "M2_COMPOSITION_BLOCKED",
        "派生报告不符/产品仍有缺口",
        {"gaps": r["gaps"]},
    )
    return data


def publish(store, body, objects, *, _fault=None):
    validate_product(body, objects)
    sid, blob = canonical_hash(body), canonical_bytes(body)
    with store._writer():
        for name in ("m2-manifests", "m2-objects"):
            _safe_path(store.root, name).mkdir(exist_ok=True)
        with store._db(write=True) as db:
            db.execute(source.TABLE)
            prior = db.execute("SELECT body,status FROM m2_snapshots WHERE id=?", (sid,)).fetchone()
        if prior and prior["status"] == "published":
            source.load(store, sid)
            return sid
        require(prior is None, "M2_RECOVERY_REQUIRED", "已有未完成发布，须显式恢复")
        for h, content in objects.items():
            immutable_write(_safe_path(store.root, "m2-objects/" + h), content)
        with store._db(write=True) as db:
            db.execute(
                "INSERT INTO m2_snapshots VALUES (?,?,?,?)", (sid, blob.decode(), "prepared", now())
            )
        if _fault == "after_prepare":
            raise RuntimeError("injected M2 composition after_prepare")
        immutable_write(_safe_path(store.root, "m2-manifests/" + sid + ".json"), blob)
        if _fault == "after_manifest":
            raise RuntimeError("injected M2 composition after_manifest")
        with store._db(write=True) as db:
            db.execute("UPDATE m2_snapshots SET status='published' WHERE id=?", (sid,))
    return sid


def compose(store, price_dataset_id, **kwargs):
    r, body, objects, _ = prepare(store, price_dataset_id, **kwargs)
    rd = r.to_dict()
    require(
        rd["status"] == "READY_CONDITIONAL",
        "M2_COMPOSITION_BLOCKED",
        "M2所需组件存在缺口；未发布",
        {"report_id": rd["report_id"], "gaps": rd["gaps"]},
    )
    sid = publish(store, body, objects)
    profiles = store.m2_profiles(sid)
    windows = []
    for p in profiles:
        view = store.m2(
            sid, profile_sha256=p.to_dict()["profile_sha256"], mode="conditional_research"
        )
        for plan in view.windows():
            wid = plan.to_dict()["geometry"]["window_id"]
            windows.append(
                {
                    "plan": plan.to_dict(),
                    "assumption_refs": [a.to_dict() for a in view.assumptions(wid)],
                }
            )
    return M2Document.of(
        {
            "dataset_id": sid,
            "manifest_sha256": sid,
            "report_id": rd["report_id"],
            "profiles": [p.to_dict() for p in profiles],
            "windows": windows,
            "quality": rd["quality"],
            "external_product_review": "PENDING",
            "execution_permission": False,
        }
    )


def export(store, dataset_id, directory):
    body, _ = source.load(store, dataset_id)
    require(body["schema"] == SCHEMA, "M2_EXPORT_UNSUPPORTED", "公开导出仅支持新可移植v2产品")
    target = Path(directory).expanduser().absolute()
    require(not target.exists(), "M2_EXPORT_EXISTS", "拒绝覆盖已有目录")
    # Do not hide an interrupted export: absence of a final manifest prevents import.
    target.mkdir(parents=True)
    (target / "objects").mkdir()
    for h in sorted(body["files"]):
        blob = _read(_safe_path(store.root, "m2-objects/" + identifier(h)))
        immutable_write(target / "objects" / h, blob)
    manifest = {"schema": "m2.export.v1", "dataset_id": dataset_id, "body": body}
    immutable_write(target / "manifest.json", canonical_bytes(manifest))
    return M2Document.of(
        {
            "dataset_id": dataset_id,
            "export_manifest_sha256": canonical_hash(manifest),
            "objects": len(body["files"]),
            "format": "m2.export.v1",
        }
    )


def import_export(store, root, manifest):
    try:
        return _import_export(store, root, manifest)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError("M2_SCHEMA", "可移植导出清单字段无效") from exc


def _import_export(store, root, manifest):
    keys(manifest, ("schema", "dataset_id", "body"))
    body = manifest["body"]
    require(
        manifest["schema"] == "m2.export.v1" and canonical_hash(body) == manifest["dataset_id"],
        "M2_INTEGRITY",
        "导出dataset身份不符",
    )
    require(
        type(body["files"]) is dict and 0 < len(body["files"]) <= 256,
        "M2_SCHEMA",
        "导出对象数须1..256",
    )
    require(
        all(
            type(s["bytes"]) is int and 0 < s["bytes"] <= components.MAX_BYTES
            for s in body["files"].values()
        )
        and sum(s["bytes"] for s in body["files"].values()) <= 2 * components.MAX_BYTES,
        "M2_SCHEMA",
        "产品总大小超过有界值",
    )
    objects = {h: _read(_safe_path(root, "objects/" + identifier(h))) for h in body["files"]}
    validate_product(body, objects)
    for component in body["components"].values():
        components.publish(
            store, component, {h: objects[h] for h in components.object_ids(component)}
        )
    _report(store, body["validation_report"])
    return publish(store, body, objects)
