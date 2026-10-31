"""Portable production contracts. Synthetic fixtures never receive production admission."""

import hashlib
import json
import shutil

import pytest

from ashare_data import DataError, Store
from ashare_data.cli import main


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
        "schema": "portable.component.v1",
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
    aid = a.import_portable_component(root)
    assert aid == b.import_portable_component(moved) == a.import_portable_component(root)
    doc = a.portable_component(aid).to_dict()
    assert doc["classification"] == "synthetic" and doc["origin"] is None
    assert str(tmp_path) not in json.dumps(doc)
    assert "path" not in doc["documents"][0]["artifacts"]["raw"]


@pytest.mark.parametrize(
    "field,value,code",
    [
        (
            "units",
            {"price": "CNY/share", "volume": "lot", "amount": "CNY"},
            "PORTABLE_UNIT_UNKNOWN",
        ),
        ("observed_at", "2026-01-01T00:00:00Z", "PORTABLE_VISIBILITY"),
        ("format", "arbitrary_csv", "PORTABLE_UNSUPPORTED_SOURCE"),
        ("source_url", "https://username:password@example.com/data", "PORTABLE_COMPONENT_SCHEMA"),
        ("name", "/absolute/example/private", "PORTABLE_LOCAL_PATH"),
    ],
)
def test_source_declaration_rejects_unknowns(synthetic_market, tmp_path, field, value, code):
    root, body = synthetic_market
    body["documents"][0][field] = value
    write_config(root, body)
    rejects(code, lambda: Store.init(tmp_path / "store").import_portable_component(root))


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
        store.import_portable_component(root)
    with store._db() as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='portable_components'"
        ).fetchone()


def request(p, f, windows=("two_day", "w1", "w2")):
    return {
        "price_dataset_id": p,
        "facts_component_id": f,
        "calendar_component_id": None,
        "state_component_id": None,
        "window_ids": list(windows),
        "mode": "conditional_research",
    }


def test_missing_component_has_durable_gap_report(tmp_path):
    s = Store.init(tmp_path / "store")
    r = s.validate_portable(**request("1" * 64, "2" * 64)).to_dict()
    assert r["status"] == "BLOCKED"
    assert r["quality"]["calendar_query_complete"] is None
    assert {g["role"] for g in r["gaps"]} == {"prices", "states", "calendar", "facts"}
    assert s.portable_report(r["report_id"]).to_dict() == r
    rejects(
        "PORTABLE_COMPOSITION_BLOCKED", lambda: s.compose_portable(**request("1" * 64, "2" * 64))
    )


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {},
        {"schema": "portable.export.v1"},
        {"schema": "portable.export.v1", "body": {}, "dataset_id": "0" * 64},
    ],
)
def test_malformed_export_clear_error(manifest, tmp_path):
    inp = tmp_path / "input"
    inp.mkdir()
    (inp / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DataError):
        Store.init(tmp_path / "store").import_portable(inp)


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
    rejects(
        "PORTABLE_SOURCE_CONFLICT",
        lambda: Store.init(tmp_path / "store").import_portable_component(root),
    )


def test_cli_blocked_validation_is_nonzero(tmp_path, capsys):
    store = Store.init(tmp_path / "store")
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request("1" * 64, "2" * 64)))
    assert (
        main(["--store", str(store.root), "portable", "validate", "--request", str(request_file)])
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED" and result["report_id"]
