"""Synthetic protocol batches exercise actual persistence and snapshot readback."""

import fcntl
import gzip
import hashlib
import json
from urllib.parse import urlsplit

import pytest

from ashare_data.providers import free_update_batch as batch
from ashare_data.providers.free_update_manual import query_snapshot
from ashare_data.providers.free_update_worker import direct_records
from ashare_data.storage import free_updates as store


def profile(tmp_path, rows=None, instruments=None):
    value = {
        "schema": "ad.security-update-batch.v1",
        "root": "observations",
        "max_jobs": 10,
        "rows": rows or [12, 33, 43],
        "instruments": instruments
        or [
            {"asset_type": "stock", "symbol": "sh600000"},
            {"asset_type": "etf", "symbol": "sh510300"},
            {"asset_type": "index", "symbol": "sh000300"},
        ],
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(value))
    return path


def test_public_bond_reference_does_not_require_native_sdk_settings(tmp_path):
    bond = {"asset_type": "convertible_bond", "symbol": "sh113042", "name": "fixture bond",
            "identity_evidence": "explicit source directory receipt"}
    path = profile(tmp_path, [27], [bond])
    _, _, prepared, _ = batch.prepare(path, as_of="2026-10-29")
    assert len(prepared["jobs"]) == 1
    assert prepared["jobs"][0]["function"] == "bond_zh_cov_info"
    assert prepared["jobs"][0]["parameters"]["symbol"] == "113042"
    path = profile(tmp_path, [38], [bond])
    with pytest.raises(ValueError):
        batch.prepare(path, as_of="2026-10-29")


def protocol_worker(monkeypatch):
    calls, day = [], ["2026-10-28"]
    monkeypatch.setattr(store, "now", lambda: day[0] + "T08:00:00+00:00")

    def worker(root, request_path, output_path, journal_path, timeout):
        request = json.loads(request_path.read_text())
        job = request["job"]
        calls.append(job["job_id"])
        assert not request["blocked_hosts"]
        labels = (
            ["2026-10-27 14:59:00", "2026-10-28 14:59:00"]
            if day[0] == "2026-10-28"
            else ["2026-10-28 14:59:00", "2026-10-29 14:59:00"]
        )
        native = [
            {
                "day": d,
                "open": "3.00",
                "close": "3.01",
                "low": "2.99",
                "high": "3.02",
                "volume": "100",
                "amount": "300",
            }
            for d in labels
        ]
        if day[0] == "2026-10-29":
            native[0]["volume"] = "110"
        raw = json.dumps(native).encode()
        sha = hashlib.sha256(raw).hexdigest()
        (root / "raw" / (sha + ".gz")).write_bytes(gzip.compress(raw))

        class Capture:
            receipts = []
            dataset_metadata = {}

            def get(self, url, headers=None):
                self.receipts = [
                    {
                        "url": url,
                        "sha256": sha,
                        "status": 200,
                        "protocol": "http",
                        "host": urlsplit(url).hostname,
                        "received_at": day[0] + "T08:00:00+00:00",
                    }
                ]
                return raw

        capture = Capture()
        records = direct_records(capture, job["function"], job["parameters"])
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
    return calls, day


def test_bounded_resume_cross_asset_readback_and_rolling_revisions(tmp_path, monkeypatch):
    path = profile(tmp_path)
    calls, day = protocol_worker(monkeypatch)
    first = batch.run(path, execute=True, as_of=day[0], max_jobs=1)
    assert first["attempted"] == first["ready_jobs"] == 1 and first["pending_jobs"] == 2
    saved = first["items"][0]["snapshot"]
    second = batch.run(path, execute=True, as_of=day[0], max_jobs=2)
    assert second["ready_jobs"] == 3 and second["resumed_without_download"] == 1
    repeated = batch.run(path, execute=True, as_of=day[0])
    assert repeated["attempted"] == 0 and len(calls) == 3
    assert query_snapshot(saved)["total"] == 2
    day[0] = "2026-10-29"
    later = batch.run(path, execute=True, as_of=day[0])
    assert later["ready_jobs"] == 3 and len(calls) == 6
    assert all(
        i["new_records"] == i["revised_records"] == i["left_current_window"] == 1
        for i in later["items"]
    )
    assert query_snapshot(saved)["records"][1]["volume"] == "100"
    assert len(store.query(tmp_path / "observations", 12, as_of="2026-10-29T09:00:00+00:00")) == 4


def test_committed_capture_recovers_export_failure_without_new_request(tmp_path, monkeypatch):
    path = profile(tmp_path, rows=[12], instruments=[{"asset_type": "stock", "symbol": "sh600000"}])
    calls, day = protocol_worker(monkeypatch)
    original = batch.write_snapshot

    def fail(*args, **kwargs):
        raise OSError("injected interrupted export")

    monkeypatch.setattr(batch, "write_snapshot", fail)
    first = batch.run(path, execute=True, as_of=day[0])
    assert first["ready_menu_scopes"] == 0 and not first["all_requested_jobs_received"]
    monkeypatch.setattr(batch, "write_snapshot", original)
    recovered = batch.run(path, execute=True, as_of=day[0])
    assert recovered["ready_menu_scopes"] == 1 and recovered["attempted"] == 0 and len(calls) == 1


def test_stop_boundary_and_resume(tmp_path, monkeypatch):
    path = profile(tmp_path)
    calls, day = protocol_worker(monkeypatch)
    stop = tmp_path / "observations/STOP"
    first = batch.run(path, execute=True, as_of=day[0], progress=lambda r: stop.write_text("stop"))
    assert first["attempted"] == 1 and first["stop_requested"]
    stop.unlink()
    assert batch.run(path, execute=True, as_of=day[0])["ready_jobs"] == 3
    assert len(calls) == 3


def test_preview_identity_budget_and_lock_guards(tmp_path):
    path = profile(tmp_path)
    assert batch.run(path)["planned_jobs"] == 3 and not (tmp_path / "observations").exists()
    with pytest.raises(ValueError, match="reduce"):
        batch.run(path, max_jobs=11)
    root = tmp_path / "observations"
    with store.writer(root):
        pass
    with (root / "batch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            batch.run(path, execute=True)
    data = json.loads(path.read_text())
    data["instruments"][1]["symbol"] = "sh600000"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="type"):
        batch.prepare(path)


def test_daily_packaging_shares_request_and_stable_window_identity(tmp_path):
    path = profile(
        tmp_path,
        rows=[8, 9, 10, 60, 61],
        instruments=[{"asset_type": "stock", "symbol": "sh600000"}],
    )
    _, _, first, _ = batch.prepare(path, as_of="2026-10-28")
    _, _, next_day, _ = batch.prepare(path, as_of="2026-10-29")
    assert len(first["jobs"]) == 4
    daily = next(j for j in first["jobs"] if j["function"] == "sina_raw_daily")
    following = next(j for j in next_day["jobs"] if j["function"] == "sina_raw_daily")
    assert daily["menu_rows"] == [60, 61] and daily["job_id"] != following["job_id"]
    assert batch.snapshot_scope(daily) == batch.snapshot_scope(following)


def test_verified_empty_announcements_finish_the_requested_scope(tmp_path, monkeypatch):
    path = profile(
        tmp_path, rows=[111], instruments=[{"asset_type": "stock", "symbol": "sh600000"}]
    )
    day = "2026-10-29"
    monkeypatch.setattr(store, "now", lambda: day + "T08:00:00+00:00")

    def worker(root, request_path, output_path, journal_path, timeout):
        req = json.loads(request_path.read_text())
        job = req["job"]

        class Capture:
            def __init__(self):
                self.receipts = []
                self.dataset_metadata = {}

            def response(self, url, data, method, body=b""):
                raw = json.dumps(data).encode()
                sha = hashlib.sha256(raw).hexdigest()
                (root / "raw" / (sha + ".gz")).write_bytes(gzip.compress(raw))
                self.receipts.append(
                    {
                        "url": url,
                        "sha256": sha,
                        "status": 200,
                        "protocol": "http",
                        "received_at": day + "T08:00:00+00:00",
                        "request_method": method,
                        "request_body_sha256": hashlib.sha256(body).hexdigest(),
                    }
                )
                return raw

            def get(self, url):
                return self.response(
                    url,
                    {"stockList": [{"code": "600000", "orgId": "official", "category": "A股"}]},
                    "GET",
                )

            def post(self, url, data):
                from urllib.parse import urlencode

                return self.response(
                    url,
                    {"totalAnnouncement": 0, "hasMore": False, "announcements": []},
                    "POST",
                    urlencode(data).encode(),
                )

        c = Capture()
        records = direct_records(c, job["function"], job["parameters"])
        result = {
            "status": "empty_source_window",
            "records": records,
            "receipts": c.receipts,
            "coverage": c.dataset_metadata,
            "observation_as_of": day,
            "implementation_sha256": job["implementation_sha256"],
            "started_at": day + "T08:00:00+00:00",
            "finished_at": day + "T08:00:00+00:00",
        }
        output_path.write_text(json.dumps(result))
        return result

    monkeypatch.setattr(store, "_worker_result", worker)
    result = batch.run(path, execute=True, as_of=day)
    assert result["all_requested_jobs_received"] and result["effective_records"] == 0, result
    assert query_snapshot(result["items"][0]["snapshot"])["total"] == 0
    assert batch.run(path, execute=True, as_of=day)["attempted"] == 0


def test_shareholder_batch_binds_each_company_reporting_period(tmp_path):
    companies = [{'asset_type': 'stock', 'symbol': 'sh600000', 'report_date': '20260630'},
                 {'asset_type': 'stock', 'symbol': 'sh601318', 'report_date': '20260930'}]
    path = profile(tmp_path, rows=[23, 24], instruments=companies)
    _, _, prepared, _ = batch.prepare(path, as_of='2026-10-29')
    assert len(prepared['jobs']) == 4
    assert {(j['parameters']['symbol'], j['parameters']['date']) for j in prepared['jobs']} == {
        ('sh600000', '20260630'), ('sh601318', '20260930')}
    config = json.loads(path.read_text())
    config['instruments'][0].pop('report_date')
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='explicit report_date'):
        batch.prepare(path, as_of='2026-10-29')
