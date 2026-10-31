"""Synthetic native envelopes test identity, units and offline replay boundaries."""

from copy import deepcopy
from datetime import datetime
import gzip
import hashlib
import json

import pytest

from ashare_data.providers.free_updates import free_update_tdx as native
from ashare_data.providers.free_updates.free_update_coverage import CapturedResponses
from ashare_data.providers.free_updates.free_update_semantics import numeric
from ashare_data.providers.free_updates.free_update_worker import Capture, SourceBlocked


def test_native_request_is_admitted_before_any_connection(tmp_path, monkeypatch):
    capture=Capture(tmp_path)
    class FullBudget:
        def reserve(self,*args):raise RuntimeError('local request budget full')
    capture.budget=FullBudget()
    monkeypatch.setattr(native.subprocess,'run',lambda *a,**k:pytest.fail('unadmitted connection'))
    monkeypatch.setattr(native.time,'sleep',lambda _:None)
    import sys
    params,_=fixture()
    with pytest.raises(RuntimeError,match='budget full'):
        native.capture_native(capture,native.parameters('tdx_native_bond_bars',params),sys.executable)
    assert capture.attempts==0


def fixture(period="1m", symbol="sh113001"):
    files = {"api/bars.py": "synthetic fixture, not live evidence"}
    sha = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    params = {
        "symbol": symbol,
        "host": native.HOST,
        "sdk_python": "/explicit/existing/python",
        "sdk_sha256": sha,
        "period": period,
        "count": 800,
    }
    bar = {
        "time": "2026-10-29T15:00:00+08:00",
        "record_hex": "aabb",
        "open": 113.001,
        "high": 113.035,
        "low": 113.001,
        "close": 113.035,
        "open_price_milli": 113001,
        "high_price_milli": 113035,
        "low_price_milli": 113001,
        "close_price_milli": 113035,
        "volume_raw": 10,
        "amount_raw": 20,
        "volume_wire_value": 100,
        "volume_lots": 1,
        "amount": 113035,
    }
    data = {
        "exchange": symbol[:2],
        "market_id": int(symbol.startswith("sh")),
        "code": symbol[2:],
        "period_name": period,
        "start": 0,
        "request_count": 800,
        "adjust_mode_raw": 0,
        "adjust_mode": "none",
        "bars": [bar],
        "raw_payload": "0100aabb",
    }
    envelope = {
        "ok": True,
        "request": native.parameters("tdx_native_bond_bars", params),
        "sdk_version": "3.0.3",
        "sdk_files": files,
        "data": data,
    }
    return params, envelope


def replay(tmp_path, params, envelope, name="tdx_native_bond_bars", receipt_day="2026-10-29"):
    body = json.dumps(envelope).encode()
    sha = hashlib.sha256(body).hexdigest()
    (tmp_path / "raw").mkdir(exist_ok=True)
    (tmp_path / "raw" / (sha + ".gz")).write_bytes(gzip.compress(body))
    receipt = {
        "sha256": sha,
        "protocol": "tdx-sdk",
        "status": 200,
        "received_at": receipt_day + "T08:00:00+00:00",
        "url": native.request_url(native.parameters(name, params)),
    }
    captured = CapturedResponses(
        tmp_path,
        {"receipts": [receipt]},
        datetime.fromisoformat(receipt_day + "T07:59:00+00:00"),
        datetime.fromisoformat(receipt_day + "T08:01:00+00:00"),
    )
    return native.collect(captured, name, params), captured


@pytest.mark.parametrize("period", sorted(native.PERIODS))
@pytest.mark.parametrize("symbol", ["sh113001", "sz127001"])
def test_bond_periods_identity_wire_and_price_scale(tmp_path, period, symbol):
    params, envelope = fixture(period, symbol)
    rows, captured = replay(tmp_path, params, envelope)
    checked = native.validate(
        rows, {"function": "tdx_native_bond_bars", "parameters": params}, "2026-10-29", numeric
    )
    assert rows[0]["close"] == "113.035"  # Never inherits quote parser's observed x100 error.
    assert rows[0]["native_fields"]["volume_lots"] == 1
    assert "volume" not in rows[0]  # No stock shares/lot assumption for bonds.
    assert checked["contract_reviewed"] and captured.index == 1


@pytest.mark.parametrize(
    "mutation", ["code", "period", "wire", "price", "future", "hash", "request"]
)
def test_reject_mismatched_native_capture(tmp_path, mutation):
    params, envelope = fixture()
    if mutation == "code":
        envelope["data"]["code"] = "113002"
    if mutation == "period":
        envelope["data"]["period_name"] = "5m"
    if mutation == "wire":
        envelope["data"]["raw_payload"] = "0200aabb"
    if mutation == "price":
        envelope["data"]["bars"][0]["close"] = 11303.5
    if mutation == "future":
        envelope["data"]["bars"][0]["time"] = "2026-10-30T15:00:00+08:00"
    if mutation == "hash":
        envelope["sdk_files"]["api/bars.py"] = "changed"
    if mutation == "request":
        envelope["request"]["symbol"] = "sh113002"
    with pytest.raises(ValueError):
        replay(tmp_path, params, envelope)


def test_financial_unit_mapping_preserves_update_date_and_precision(tmp_path):
    params, envelope = fixture()
    params = {k: v for k, v in params.items() if k not in {"period", "count"}} | {
        "symbol": "sh600000"
    }
    record = {source: 12.125 for source, unit, factor in native.FINANCE_FIELDS.values()}
    record.update(
        exchange="sh", code="600000", market_id=1, updated_date="2026-08-28", record_hex="ccdd"
    )
    envelope.update(
        request=native.parameters("tdx_native_finance", params),
        data={"records": [record], "raw_payload": "0100ccdd"},
    )
    rows, _ = replay(tmp_path, params, envelope, "tdx_native_finance")
    assert rows[0]["metrics"]["total_shares"]["value"] == "121250.000"
    assert rows[0]["metrics"]["total_assets"]["value"] == "12125.000"
    assert rows[0]["metrics"]["eps"]["value"] == "12.125"
    assert rows[0]["report_period"] is rows[0]["announcement_date"] is None
    job = {"function": "tdx_native_finance", "parameters": params}
    assert native.validate(rows, job, "2026-10-29", numeric)["contract_reviewed"]
    changed = deepcopy(rows)
    changed[0]["announcement_date"] = changed[0]["source_updated_date"]
    with pytest.raises(ValueError):
        native.validate(changed, job, "2026-10-29", numeric)


def test_native_cooldown_does_not_execute_sdk(tmp_path, monkeypatch):
    params, _ = fixture()
    capture = Capture(tmp_path, blocked_hosts=[native.HOST.split(":")[0]])
    monkeypatch.setattr(
        native.subprocess, "run", lambda *a, **k: pytest.fail("cooldown must not launch SDK")
    )
    with pytest.raises(SourceBlocked):
        native.capture_native(
            capture, native.parameters("tdx_native_bond_bars", params), params["sdk_python"]
        )


def test_native_transport_failure_records_cooldown_without_host_switch(tmp_path, monkeypatch):
    params, _ = fixture()
    params["sdk_python"] = str(tmp_path / "python")
    (tmp_path / "python").touch()
    capture = Capture(tmp_path)
    monkeypatch.setattr(native.time, "sleep", lambda _: None)

    def fail(*a, **k):
        raise native.subprocess.TimeoutExpired("fixture", 22)

    monkeypatch.setattr(native.subprocess, "run", fail)
    with pytest.raises(SourceBlocked):
        native.capture_native(
            capture, native.parameters("tdx_native_bond_bars", params), params["sdk_python"]
        )
    assert capture.blocked == {native.HOST.split(":")[0]}
    assert capture.receipts[0]["status"] == "transport_error"


def test_native_batch_resume_readback_increment_and_export_recovery(tmp_path, monkeypatch):
    from ashare_data.providers.free_updates import free_update_batch as batch
    from ashare_data.providers.free_updates.free_update_manual import query_snapshot
    from ashare_data.storage import free_updates as store

    params, envelope = fixture()
    config = {
        "schema": "ad.security-update-batch.v1",
        "root": "observations",
        "rows": [38, 54],
        "instruments": [
            {
                "symbol": params["symbol"],
                "asset_type": "convertible_bond",
                "name": "Example bond",
                "identity_evidence": "synthetic test directory",
            }
        ],
        "tdx": {k: params[k] for k in ("host", "sdk_python", "sdk_sha256")},
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(config))
    calls, day = [], ["2026-10-28"]
    monkeypatch.setattr(store, "now", lambda: day[0] + "T08:00:00+00:00")

    def worker(root, request_path, output_path, journal_path, timeout):
        job = json.loads(request_path.read_text())["job"]
        calls.append(job["job_id"])
        p, value = fixture(job["parameters"]["period"])
        previous = deepcopy(value["data"]["bars"][0])
        previous["time"] = "2026-10-28T15:00:00+08:00"
        value["data"]["bars"] = [previous]
        if day[0] == "2026-10-29":
            current = deepcopy(previous)
            current["time"] = "2026-10-29T15:00:00+08:00"
            current["record_hex"] = "ccdd"
            value["data"]["bars"].append(current)
            value["data"]["raw_payload"] = "0200aabbccdd"
        records, capture = replay(root, p, value, receipt_day=day[0])
        result = {
            "status": "received",
            "records": records,
            "receipts": capture.receipts,
            "coverage": capture.dataset_metadata,
            "observation_as_of": day[0],
            "implementation_sha256": job["implementation_sha256"],
            "started_at": day[0] + "T08:00:00+00:00",
            "finished_at": day[0] + "T08:00:00+00:00",
        }
        output_path.write_text(json.dumps(result))
        return result

    monkeypatch.setattr(store, "_worker_result", worker)
    first = batch.run(path, execute=True, as_of=day[0], max_jobs=1)
    assert first["ready_jobs"] == 1 and first["pending_jobs"] == 1
    complete = batch.run(path, execute=True, as_of=day[0])
    assert complete["ready_jobs"] == 2 and len(calls) == 2
    assert batch.run(path, execute=True, as_of=day[0])["attempted"] == 0
    old = complete["items"][0]["snapshot"]
    assert query_snapshot(old)["total"] == 1
    day[0] = "2026-10-29"
    original = batch.write_snapshot

    def fail(*a, **k):
        raise OSError("injected export failure after capture commit")

    monkeypatch.setattr(batch, "write_snapshot", fail)
    interrupted = batch.run(path, execute=True, as_of=day[0])
    assert interrupted["ready_jobs"] == 0 and len(calls) == 4
    monkeypatch.setattr(batch, "write_snapshot", original)
    restored = batch.run(path, execute=True, as_of=day[0])
    assert restored["ready_jobs"] == 2 and restored["attempted"] == 0 and len(calls) == 4
    assert all(i["new_records"] == 1 for i in restored["items"])
    assert query_snapshot(old)["total"] == 1  # Historical pinned snapshot is unchanged.
