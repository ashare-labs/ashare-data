"""Portable production contracts. Synthetic fixtures never receive production admission."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from ashare_data import DataError, Store
from ashare_data.cli import main
from ashare_data.m2_types import canonical_hash


def rejects(code, fn):
    with pytest.raises(DataError) as error:
        fn()
    assert error.value.code == code
    return error.value


def write_config(root, body):
    (root / "component.json").write_text(json.dumps(body, ensure_ascii=False))


@pytest.fixture
def synthetic_market(tmp_path):
    root = tmp_path / "synthetic"
    root.mkdir()
    fields = "date,code,open,high,low,close,volume,amount,adjustflag,tradestatus"
    blob = json.dumps(
        {
            "method": "query_history_k_data_plus",
            "error_code": "0",
            "params": {
                "code": "sh.600000",
                "frequency": "d",
                "adjustflag": "3",
                "fields": fields,
                "start_date": "2020-01-02",
                "end_date": "2020-01-02",
            },
            "fields": fields.split(","),
            "rows": [
                dict(
                    zip(
                        fields.split(","),
                        [
                            "2020-01-02",
                            "sh.600000",
                            "10",
                            "10",
                            "10",
                            "10",
                            "100",
                            "1000",
                            "3",
                            "1",
                        ],
                    )
                )
            ],
        }
    ).encode()
    (root / "raw.json").write_bytes(blob)
    body = {
        "schema": "m2.component.v1",
        "kind": "market",
        "security": "600000.XSHG",
        "classification": "synthetic",
        "origin": None,
        "claims": None,
        "documents": [
            {
                "name": "explicit_synthetic_test",
                "role": "prices",
                "format": "baostock_json",
                "units": {"price": "CNY/share", "volume": "share", "amount": "CNY"},
                "source_url": None,
                "observed_at": None,
                "artifacts": {
                    "raw": {
                        "path": "raw.json",
                        "sha256": hashlib.sha256(blob).hexdigest(),
                        "bytes": len(blob),
                    }
                },
            }
        ],
    }
    write_config(root, body)
    return root, body


def test_public_synthetic_component_is_labelled_and_portable(synthetic_market, tmp_path):
    root, _ = synthetic_market
    a, b = Store.init(tmp_path / "a"), Store.init(tmp_path / "b")
    moved = tmp_path / "renamed"
    shutil.copytree(root, moved)
    aid = a.import_m2_component(root)
    assert aid == b.import_m2_component(moved) == a.import_m2_component(root)
    doc = a.m2_component(aid).to_dict()
    assert doc["classification"] == "synthetic" and doc["origin"] is None
    assert str(tmp_path) not in json.dumps(doc)
    assert "path" not in doc["documents"][0]["artifacts"]["raw"]


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("units", {"price": "CNY/share", "volume": "lot", "amount": "CNY"}, "M2_UNIT_UNKNOWN"),
        ("observed_at", "2026-01-01T00:00:00Z", "M2_VISIBILITY"),
        ("format", "arbitrary_csv", "M2_UNSUPPORTED_SOURCE"),
        ("source_url", "https://username:password@example.com/data", "M2_COMPONENT_SCHEMA"),
        ("name", "/Users/example/private", "M2_LOCAL_PATH"),
    ],
)
def test_source_declaration_rejects_unknowns(synthetic_market, tmp_path, field, value, code):
    root, body = synthetic_market
    body["documents"][0][field] = value
    write_config(root, body)
    rejects(code, lambda: Store.init(tmp_path / "store").import_m2_component(root))


@pytest.mark.parametrize(
    "fault",
    ["missing", "hash", "size", "escape", "unknown_key", "bad_json", "no_documents", "origin"],
)
def test_component_bad_input_never_publishes(synthetic_market, tmp_path, fault):
    root, body = synthetic_market
    spec = body["documents"][0]["artifacts"]["raw"]
    if fault == "missing":
        (root / "raw.json").unlink()
    elif fault == "hash":
        spec["sha256"] = "0" * 64
    elif fault == "size":
        spec["bytes"] = True
    elif fault == "escape":
        spec["path"] = "../outside.json"
    elif fault == "unknown_key":
        body["verified"] = True
    elif fault == "bad_json":
        (root / "component.json").write_text('{"schema":1,"schema":2}')
    elif fault == "no_documents":
        del body["documents"]
    else:
        body["origin"] = {"dataset_id": "0" * 64}
    if fault != "bad_json":
        write_config(root, body)
    store = Store.init(tmp_path / "store")
    with pytest.raises(DataError):
        store.import_m2_component(root)
    with store._db() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='m2_components'").fetchone()


@pytest.fixture(scope="session")
def private_components():
    value = os.environ.get("ASHARE_M2_COMPONENTS")
    if not value:
        pytest.skip("Private lawful source components required; set ASHARE_M2_COMPONENTS")
    path = Path(value)
    assert all((path / k / "component.json").is_file() for k in ("market", "facts"))
    return path


@pytest.fixture
def prepared(private_components, tmp_path):
    s = Store.init(tmp_path / "store")
    p = s.import_m2_component(private_components / "market")
    f = s.import_m2_component(private_components / "facts")
    return s, p, f


def request(p, f, windows=("m2a", "w1", "w2")):
    return {
        "price_dataset_id": p,
        "facts_component_id": f,
        "calendar_component_id": None,
        "state_component_id": None,
        "window_ids": list(windows),
        "mode": "conditional_research",
    }


def mutated_component(private_components, tmp_path, kind, mutate):
    dest = tmp_path / (kind + "-changed")
    shutil.copytree(private_components / kind, dest)
    doc = json.loads((dest / "component.json").read_text())
    mutate(doc)
    write_config(dest, doc)
    return dest


def test_real_compose_export_move_reopen_and_cli(prepared, tmp_path, capsys):
    s, p, f = prepared
    req = request(p, f)
    checked = s.validate_m2(**req).to_dict()
    assert checked["status"] == "READY_CONDITIONAL" and checked["gaps"] == []
    assert s.m2_report(checked["report_id"]).to_dict() == checked
    result = s.compose_m2(**req).to_dict()
    assert s.compose_m2(**req).to_dict() == result
    sid = result["dataset_id"]
    directory = tmp_path / "export"
    exported = s.export_m2(sid, directory)
    rejects("M2_EXPORT_EXISTS", lambda: s.export_m2(sid, directory))
    elsewhere = tmp_path / "moved"
    shutil.move(directory, elsewhere)
    fresh = Store.init(tmp_path / "fresh")
    assert fresh.import_m2(elsewhere) == fresh.import_m2(elsewhere) == sid
    assert fresh.m2_component(p) == s.m2_component(p)
    assert fresh.m2_component(f) == s.m2_component(f)
    assert fresh.m2_report(checked["report_id"]).to_dict() == checked
    assert fresh.compose_m2(**req).to_dict() == result
    assert [p.to_dict() for p in fresh.m2_profiles(sid)] == result["profiles"]
    for profile in result["profiles"]:
        old = s.m2(sid, profile_sha256=profile["profile_sha256"], mode="conditional_research")
        new = fresh.m2(sid, profile_sha256=profile["profile_sha256"], mode="conditional_research")
        for method in ("descriptor", "profile", "lineage", "quality"):
            assert getattr(old, method)() == getattr(new, method)()
            assert str(tmp_path) not in json.dumps(getattr(new, method)().to_dict())
        assert old.windows() == new.windows()
    assert exported.to_dict()["dataset_id"] == sid
    cli = tmp_path / "request.json"
    cli.write_text(json.dumps(req))
    assert main(["--store", str(s.root), "m2", "compose", "--request", str(cli)]) == 0
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("missing_preclose", "M2_STATE_UNKNOWN"),
        ("missing_calendar", "M2_CALENDAR_MISSING"),
        ("missing_prices", "M2_DATA_MISSING"),
        ("synthetic", "M2_SYNTHETIC_SOURCE"),
        ("missing_fact", "M2_FACT_MISSING"),
        ("unknown_fact", "M2_FACT_MISSING"),
        ("bad_lot", "M2_RULE_EXCEPTION"),
        ("coverage_unknown", "M2_FACT_MISSING"),
        ("coverage_domain", "M2_FACT_MISSING"),
        ("known_event_in_domain", "M2_RELATED_EVENT"),
        ("unknown_event_date", "M2_RELATED_EVENT"),
        ("jan20_conflict", "M2_RULE_EXCEPTION"),
    ],
)
def test_gaps_block_without_inference(prepared, private_components, tmp_path, fault, expected):
    s, p, f = prepared
    kind = (
        "market"
        if fault in ("missing_preclose", "missing_calendar", "missing_prices", "synthetic")
        else "facts"
    )

    def mutate(doc):
        if fault == "missing_preclose":
            doc["documents"] = [d for d in doc["documents"] if d["role"] != "states"]
        elif fault == "missing_calendar":
            doc["documents"] = [d for d in doc["documents"] if d["role"] != "calendar"]
        elif fault == "missing_prices":
            doc["documents"] = [d for d in doc["documents"] if d["name"] != "capture-daily"]
        elif fault == "synthetic":
            doc["classification"] = "synthetic"
        elif fault == "missing_fact":
            doc["claims"]["facts"] = [
                f for f in doc["claims"]["facts"] if f["id"] != "initial_listing_date"
            ]
        elif fault == "unknown_fact":
            doc["claims"]["facts"][0]["status"] = "unknown"
        elif fault == "bad_lot":
            next(f for f in doc["claims"]["facts"] if f["id"] == "buy_round_lot")["value"] = 100.0
        elif fault == "coverage_unknown":
            doc["claims"]["coverage"]["checks"]["equity_events"]["status"] = "unknown"
        elif fault == "coverage_domain":
            del doc["claims"]["coverage"]["window_domains"]["w2"]
        elif fault == "known_event_in_domain":
            e = next(e for e in doc["claims"]["events"] if e["event_type"] == "cash_dividend")
            e["record_date"], e["pay_date"] = "2020-01-10", "2020-02-01"
        elif fault == "unknown_event_date":
            doc["claims"]["events"][0]["record_date"] = None
        else:
            doc["claims"]["coverage"]["jan20_rule"]["ordinary_a_share_resale"] = "T+0"

    directory = mutated_component(private_components, tmp_path, kind, mutate)
    if fault == "bad_lot":
        # Generic canonical prohibits floating-point claims before publication.
        rejects("M2_ENCODING", lambda: s.import_m2_component(directory))
        return
    new = s.import_m2_component(directory)
    if kind == "market":
        p = new
    else:
        f = new
    r = s.validate_m2(**request(p, f)).to_dict()
    assert r["status"] == "BLOCKED"
    assert expected in {g["code"] for g in r["gaps"]}
    err = rejects("M2_COMPOSITION_BLOCKED", lambda: s.compose_m2(**request(p, f)))
    assert err.details["report_id"] == r["report_id"]
    assert not s.m2_snapshots()


@pytest.mark.parametrize(
    "fault", ["missing_raw", "raw_corruption", "mutated_report", "producer", "dataset"]
)
def test_export_tampering_cannot_rehash_into_admission(prepared, tmp_path, fault):
    s, p, f = prepared
    sid = s.compose_m2(**request(p, f)).to_dict()["dataset_id"]
    directory = tmp_path / "out"
    s.export_m2(sid, directory)
    m = json.loads((directory / "manifest.json").read_text())
    h = next(iter(m["body"]["files"]))
    if fault == "missing_raw":
        (directory / "objects" / h).unlink()
    elif fault == "raw_corruption":
        path = directory / "objects" / h
        path.chmod(0o600)
        path.write_bytes(b"damaged")
    elif fault == "mutated_report":
        m["body"]["validation_report"]["quality"]["finality"] = "VERIFIED"
        m["dataset_id"] = canonical_hash(m["body"])
    elif fault == "producer":
        m["body"]["producer_sha256"] = "0" * 64
        m["dataset_id"] = canonical_hash(m["body"])
    else:
        m["dataset_id"] = "0" * 64
    path = directory / "manifest.json"
    path.chmod(0o600)
    path.write_text(json.dumps(m))
    target = Store.init(tmp_path / "target")
    with pytest.raises(DataError):
        target.import_m2(directory)
    assert not target.m2_snapshots()


@pytest.mark.parametrize("fault", ["after_prepare", "after_manifest"])
def test_portable_publication_recovery(prepared, fault):
    from ashare_data.m2_compose import prepare, publish

    s, p, f = prepared
    _, body, objects, _ = prepare(s, **request(p, f))
    with pytest.raises(RuntimeError):
        publish(s, body, objects, _fault=fault)
    assert s.m2_snapshots()[0]["status"] == "prepared"
    s.recover_m2()
    assert s.m2_snapshots()[0]["status"] == "published"
    assert s.compose_m2(**request(p, f)).to_dict()["dataset_id"] == canonical_hash(body)


def test_existing_research_input_keeps_origin_identity(prepared, private_components, tmp_path):
    s, _, f = prepared
    market = json.loads((private_components / "market/component.json").read_text())

    def original(name):
        d = next(d for d in market["documents"] if d["name"] == name)
        return private_components / "market" / d["artifacts"]["raw"]["path"]

    pid = s.import_research(
        [original("legacy-daily")],
        format="baostock_daily",
        calendar_path=original("legacy-calendar"),
    )
    made = s.compose_m2(**request(pid, f, windows=("m2a",))).to_dict()
    out = tmp_path / "research-export"
    s.export_m2(made["dataset_id"], out)
    exported = json.loads((out / "manifest.json").read_text())
    origins = [
        b["origin"] for b in exported["body"]["components"].values() if b["origin"] is not None
    ]
    assert origins[0]["dataset_id"] == pid
    assert Store.init(tmp_path / "fresh").import_m2(out) == made["dataset_id"]
    assert len(made["windows"]) == 1


def test_all_windows_public_consumer_contract(prepared, tmp_path):
    import importlib.util

    path = Path(__file__).parents[1] / "examples/m2_demo.py"
    spec = importlib.util.spec_from_file_location("public_demo", path)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    s, p, f = prepared
    sid = s.compose_m2(**request(p, f)).to_dict()["dataset_id"]
    out = tmp_path / "all-windows"
    s.export_m2(sid, out)
    result = demo.replay(out, tmp_path / "consumer")
    assert result["reads_exercised"] == 361 and result["previous_close_consumptions"] == 22
    assert result["native_runs"] == 0 and result["backend_execution_authorized"] is False


def test_missing_component_has_durable_gap_report(tmp_path):
    s = Store.init(tmp_path / "store")
    r = s.validate_m2(**request("1" * 64, "2" * 64)).to_dict()
    assert r["status"] == "BLOCKED"
    assert {g["role"] for g in r["gaps"]} == {"prices", "states", "calendar", "facts"}
    assert s.m2_report(r["report_id"]).to_dict() == r
    rejects("M2_COMPOSITION_BLOCKED", lambda: s.compose_m2(**request("1" * 64, "2" * 64)))


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {},
        {"schema": "m2.export.v1"},
        {"schema": "m2.export.v1", "body": {}, "dataset_id": "0" * 64},
    ],
)
def test_malformed_export_clear_error(manifest, tmp_path):
    inp = tmp_path / "input"
    inp.mkdir()
    (inp / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DataError):
        Store.init(tmp_path / "store").import_m2(inp)


@pytest.mark.parametrize(
    "fault", ["security", "frequency", "adjustflag", "row_code", "date", "row_order"]
)
def test_raw_source_identity_rejected(synthetic_market, tmp_path, fault):
    root, config = synthetic_market
    path = root / "raw.json"
    body = json.loads(path.read_bytes())
    if fault == "security":
        body["params"]["code"] = "sh.600001"
    elif fault == "frequency":
        body["params"]["frequency"] = "5"
    elif fault == "adjustflag":
        body["params"]["adjustflag"] = "2"
    elif fault == "row_code":
        body["rows"][0]["code"] = "sh.600001"
    elif fault == "date":
        body["rows"][0]["date"] = "2020-01-03"
    else:
        body["rows"].append(body["rows"][0])
    blob = json.dumps(body).encode()
    path.write_bytes(blob)
    config["documents"][0]["artifacts"]["raw"].update(
        sha256=hashlib.sha256(blob).hexdigest(), bytes=len(blob)
    )
    write_config(root, config)
    rejects("M2_SOURCE_CONFLICT", lambda: Store.init(tmp_path / "store").import_m2_component(root))


def test_new_product_read_roles_stale_auth_and_selected_window(prepared):
    from dataclasses import replace
    from ashare_data import M2AD08Call, M2ConsumerBinding

    s, p, f = prepared
    result = s.compose_m2(**request(p, f)).to_dict()
    multi = next(p for p in result["profiles"] if "multiday" in p["version"])
    v = s.m2(
        result["dataset_id"], profile_sha256=multi["profile_sha256"], mode="conditional_research"
    )
    uses = {}
    for doc in v.windows():
        plan = doc.to_dict()
        wid = plan["geometry"]["window_id"]
        h = canonical_hash({"explicit_test_consumer": wid})
        b = M2ConsumerBinding(
            v.descriptor().sha256,
            wid,
            plan["window_plan_sha256"],
            h,
            "test-session",
            h,
            "test-strategy",
            h,
        )
        uses[wid] = v.use(
            wid, assumption_ack=[a.to_dict() for a in v.assumptions(wid)], consumer_binding=b
        )
    u = uses["w1"]
    c = u.context(
        current_date="2020-01-06",
        logical_at="2020-01-06T09:00:00+08:00",
        phase="DECIDE",
        query_end="2020-01-03T23:59:59.999999+08:00",
        visibility="assumed",
        consumer="strategy",
        event_cursor=0,
        restore_generation=0,
        epoch=0,
    )
    call = M2AD08Call(
        "600000.XSHG",
        "2020-01-06",
        "2020-01-03",
        "2020-01-06",
        "1d",
        "close",
        1,
        False,
        False,
        "pre",
    )
    old = u.decision_prev_close(c, call=call)
    newer = replace(c, restore_generation=1, epoch=1, event_cursor=1)
    rejects("M2_RECEIPT_MISMATCH", lambda: u.verify(old, context=newer))
    fresh = old.with_authorization(u.revalidate(old, context=newer))
    assert u.verify(fresh, context=newer) == fresh
    assert (old.query, old.result, old.sources, old.evidence) == (
        fresh.query,
        fresh.result,
        fresh.sources,
        fresh.evidence,
    )
    rejects("M2_IDENTITY_MISMATCH", lambda: uses["w2"].decision_prev_close(c, call=call))
    rejects("M2_ROLE_SCOPE", lambda: u.execution_bar(c))
    at15 = replace(
        c,
        phase="SUBMIT_MATCH",
        logical_at="2020-01-06T15:00:00+08:00",
        query_end="2020-01-06T15:00:00+08:00",
    )
    rejects("M2_ROLE_SCOPE", lambda: u.execution_bar(at15))
    assert (
        u.execution_bar(replace(at15, consumer="engine")).result.to_dict()["target_date"]
        == "2020-01-06"
    )


def test_direct_file_recipe_same_component_identity(prepared, private_components):
    s, p, f = prepared
    for kind, expected in (("market", p), ("facts", f)):
        body = json.loads((private_components / kind / "component.json").read_text())
        for doc in body["documents"]:
            for name, source in list(doc["artifacts"].items()):
                doc["artifacts"][name] = {"path": str(private_components / kind / source["path"])}
        assert (
            s.import_m2_sources(
                kind=kind,
                documents=body["documents"],
                claims=body["claims"],
                classification=body["classification"],
            )
            == expected
        )


def test_cli_blocked_validation_is_nonzero(tmp_path, capsys):
    store = Store.init(tmp_path / "store")
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request("1" * 64, "2" * 64)))
    assert main(["--store", str(store.root), "m2", "validate", "--request", str(request_file)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED" and result["report_id"]
