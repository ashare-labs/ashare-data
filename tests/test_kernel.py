import copy
import fcntl
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
from decimal import Decimal

import pytest

from ashare_data import DataError, Store
from ashare_data.compat import JQStyle

SYM = "000001.XSHE"
A = "2026-04-09T14:53:00+08:00"
B = "2026-04-09T14:56:00+08:00"


def code(expected, call):
    with pytest.raises(DataError) as error:
        call()
    assert error.value.code == expected
    return error.value


def hashes(store):
    return {str(p.relative_to(store.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in store.root.rglob("*") if p.is_file()}


def test_end_to_end_units_source_fields_and_half_open(published):
    store, _, _, sid = published
    with store.snapshot(sid) as view:
        result = view.bars([SYM], A, B, quality="synthetic")
        assert result["snapshot_id"] == sid
        assert len(result["rows"]) == 3
        row = result["rows"][0]
        assert row["bar_start"] == A and row["bar_end"] == "2026-04-09T14:54:00+08:00"
        assert row["volume"] == Decimal(200) and row["amount"] == Decimal(2000)
        assert row["source_label_kind"] == "end" and row["source_label"] == row["bar_end"]
        assert row["source_fields"]["volume"] == "2"
        assert row["source_fields"]["provider_extra"] == "preserved"
        one = view.bars([SYM], A, row["bar_end"], quality="synthetic")
        assert len(one["rows"]) == 1


def test_idempotent_import_publish_and_old_snapshot(published, bundle, requirements):
    store, bid, report, sid = published
    before = hashes(store)
    assert store.import_bundle(bundle) == bid
    assert store.publish(report["id"]) == sid
    assert hashes(store) == before
    revised = copy.deepcopy(bundle)
    revised["bars"][0]["close"] = "10.15"
    other = store.import_bundle(revised)
    newer = store.publish(store.validate([other], requirements=requirements, policy="synthetic")["id"])
    assert newer != sid
    with store.snapshot(sid) as old, store.snapshot(newer) as new:
        assert old.bars([SYM], A, B, quality="synthetic")["rows"][0]["close"] == Decimal("10.1")
        assert new.bars([SYM], A, B, quality="synthetic")["rows"][0]["close"] == Decimal("10.15")
    assert (store.root / "objects" / f"{bid}.parquet").read_bytes()
    for path, value in before.items():
        if path.startswith(("objects/", "manifests/")):
            assert hashes(store)[path] == value


def test_gap_blocks_publication_and_reports_missing(store, bundle, requirements):
    bundle["bars"].pop(1)
    bid = store.import_bundle(bundle)
    report = store.validate([bid], requirements=requirements, policy="synthetic")
    assert not report["passed"]
    assert report["coverage"][0]["missing_count"] == 1
    assert report["coverage"][0]["missing"][0]["bar_start"] == "2026-04-09T14:54:00+08:00"
    code("VALIDATION_FAILED", lambda: store.publish(report["id"]))
    assert store.snapshots() == []


def test_quality_isolation_and_explicit_research(store, bundle, requirements):
    bundle["source"]["kind"] = "local_reconstruction"
    for row in bundle["bars"]:
        row["quality"] = "inferred"
    bid = store.import_bundle(bundle)
    assert not store.validate([bid], requirements=requirements)["passed"]
    report = store.validate([bid], requirements=requirements, policy="research")
    sid = store.publish(report["id"])
    with store.snapshot(sid) as view:
        err = code("COVERAGE_GAP", lambda: view.bars([SYM], A, B))
        assert len(err.details["excluded"]) == 3
        partial = view.bars([SYM], A, B, strict=False)
        assert partial["rows"] == [] and not partial["coverage"]["complete"]
        assert {r["quality"] for r in view.bars([SYM], A, B, quality="research")["rows"]} == {"inferred"}


def test_end_label_does_not_upgrade_unverified(store, bundle, requirements):
    bundle["source"].update(kind="local_observation", time_semantics="unverified")
    for row in bundle["bars"]:
        row["quality"] = "unverified"
    bid = store.import_bundle(bundle)
    assert not store.validate([bid], requirements=requirements)["passed"]
    bundle["bars"][0]["quality"] = "observed"
    code("INVALID_QUALITY", lambda: store.import_bundle(bundle))


def test_synthetic_cannot_be_default_observed(published):
    store, _, _, sid = published
    with store.snapshot(sid) as view:
        code("COVERAGE_GAP", lambda: view.bars([SYM], A, B))


@pytest.mark.parametrize("stage", ["after_prepare", "after_manifest"])
def test_interrupted_publication_recovers(store, bundle, requirements, stage):
    bid = store.import_bundle(bundle)
    report = store.validate([bid], requirements=requirements, policy="synthetic")
    with pytest.raises(RuntimeError):
        store.publish(report["id"], _fault=stage)
    sid = store.snapshots()[0]["id"]
    code("SNAPSHOT_NOT_PUBLISHED", lambda: store.snapshot(sid))
    assert store.recover()[0]["status"] == "published"
    assert store.recover() == []
    with store.snapshot(sid) as view:
        assert len(view.bars([SYM], A, B, quality="synthetic")["rows"]) == 3
    assert store.publish(report["id"]) == sid


def test_process_exit_recovery(published, bundle, requirements, tmp_path):
    store, _, report, _ = published
    # A fresh report / snapshot is interrupted by actual os._exit, leaving no Python cleanup.
    bundle["source"]["id"] += "-crash"
    bid = store.import_bundle(bundle)
    report = store.validate([bid], requirements=requirements, policy="synthetic")
    script = """import os,sys
from ashare_data import Store
s=Store(sys.argv[1])
try: s.publish(sys.argv[2], _fault='after_manifest')
except RuntimeError: os._exit(17)
"""
    child = subprocess.run([sys.executable, "-c", script, str(store.root), report["id"]], capture_output=True)
    assert child.returncode == 17, child.stderr
    assert store.recover()[0]["status"] == "published"


def test_corruption_aborts_recovery(store, bundle, requirements):
    bid = store.import_bundle(bundle)
    report = store.validate([bid], requirements=requirements, policy="synthetic")
    with pytest.raises(RuntimeError):
        store.publish(report["id"], _fault="after_prepare")
    p = store.root / "objects" / f"{bid}.parquet"
    p.chmod(0o644)
    p.write_bytes(b"corrupt")
    result = store.recover()[0]
    assert result["status"] == "aborted" and result["error"]["code"] == "INTEGRITY"
    code("SNAPSHOT_NOT_PUBLISHED", lambda: store.snapshot(result["snapshot_id"]))


@pytest.mark.parametrize("target", ["parquet", "json", "manifest"])
def test_tamper_is_detected(published, target):
    store, bid, _, sid = published
    p = store.root / (f"manifests/{sid}.json" if target == "manifest" else f"objects/{bid}.{target}")
    p.chmod(0o644)
    p.write_bytes(p.read_bytes()+b" ")
    code("INTEGRITY", lambda: store.snapshot(sid))


def test_source_conflict_rejected(store, bundle, requirements):
    bid = store.import_bundle(bundle)
    second = copy.deepcopy(bundle)
    second["source"]["id"] += "-other"
    other = store.import_bundle(second)
    report = store.validate([bid, other], requirements=requirements, policy="synthetic")
    assert not report["passed"] and len(report["conflicts"]) == 3
    code("VALIDATION_FAILED", lambda: store.publish(report["id"]))


def test_single_writer_lock(store, bundle):
    with open(store.root / "writer.lock", "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code("WRITER_BUSY", lambda: store.import_bundle(bundle))


def test_queries_offline_read_only(published, monkeypatch):
    store, _, _, sid = published
    before = hashes(store)
    def deny(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    with store.snapshot(sid) as view:
        view.bars([SYM], A, B, quality="synthetic")
        view.lineage()
        view.quality()
        view.sessions("2026-04-09")
    store.snapshots()
    assert before == hashes(store)


def test_missing_calendar_and_pit_are_explicit(published):
    store, _, _, sid = published
    with store.snapshot(sid) as view:
        code("CALENDAR_UNKNOWN", lambda: view.calendar("2026-04-09", "2026-04-10"))
        code("PIT_UNAVAILABLE", lambda: view.instruments(as_of="2026-04-10T14:55:00+08:00"))
        assert view.instruments(as_of="2026-04-09T14:55:00+08:00")["scope"] == "sample"
        code("UNIVERSE_INCOMPLETE", lambda: JQStyle(view).get_all_securities(date="2026-04-09"))
        result = view.coverage([SYM], "2026-04-10T14:53:00+08:00", "2026-04-10T14:56:00+08:00")
        assert not result["complete"] and result["unknown_calendar_dates"] == ["2026-04-10"]


def test_1455_closed_visibility(published):
    store, _, _, sid = published
    t = "2026-04-09T14:55:00+08:00"
    with store.snapshot(sid) as view:
        result = JQStyle(view, quality="synthetic").get_price(SYM, t, t)
        assert len(result["rows"]) == 1
        assert result["rows"][0]["bar_start"] == "2026-04-09T14:54:00+08:00"
        assert result["rows"][0]["time"] == t
        assert result["rows"][0]["money"] == Decimal("2000")
        r = view.bars([SYM], A, B, as_of=t, quality="synthetic", strict=False)
        assert len(r["rows"]) == 2
        assert "not_closed" in r["coverage"]["excluded"][0]["reasons"]


def test_missing_historical_visibility_is_not_backfilled(store, bundle, requirements):
    for row in bundle["bars"]:
        row["available_at"] = None
    bid = store.import_bundle(bundle)
    sid = store.publish(store.validate([bid], requirements=requirements, policy="synthetic")["id"])
    with store.snapshot(sid) as view:
        code("COVERAGE_GAP", lambda: view.bars([SYM], A, B, as_of=B, quality="synthetic"))


@pytest.mark.parametrize("kwargs", [dict(fq="pre"), dict(fq="post"), dict(count=1), dict(panel=True),
                                    dict(skip_paused=True), dict(fill_paused=True), dict(round=True),
                                    dict(frequency="daily"), dict(fields=["factor"]), dict(fields=["paused"])])
def test_jq_unsupported_parameters_rejected(published, kwargs):
    store, _, _, sid = published
    with store.snapshot(sid) as view:
        code("UNSUPPORTED", lambda: JQStyle(view).get_price(SYM, A, B, **kwargs))


def test_trade_days_and_absent_corporate_actions(published):
    store, _, _, sid = published
    with store.snapshot(sid) as view:
        assert JQStyle(view).get_trade_days("2026-04-09", "2026-04-09")["days"] == ["2026-04-09"]
        code("UNSUPPORTED", lambda: view.corporate_actions())
        code("UNSUPPORTED", lambda: view.adjustment_factors())
        code("UNSUPPORTED", lambda: view.bars([SYM], A, B, adjustment="pre"))
        code("INVALID_ID", lambda: store.snapshot("latest"))


@pytest.mark.parametrize("mutation,expected", [
    (lambda b: b["bars"][0].update(timestamp="2026-04-09T14:54:00"), "INVALID_TIME"),
    (lambda b: b["bars"][0].update(high="9"), "INVALID_OHLC"),
    (lambda b: b["bars"][0].update(volume="-1"), "INVALID_NUMBER"),
    (lambda b: b["bars"][0].update(amount="NaN"), "INVALID_NUMBER"),
    (lambda b: b["bars"].append(copy.deepcopy(b["bars"][0])), "DUPLICATE_BAR"),
    (lambda b: b["source"].update(volume_unit="unknown"), "UNSUPPORTED"),
    (lambda b: b["source"].update(price_basis="pre"), "UNSUPPORTED"),
    (lambda b: b["bars"][0].update(available_at="2026-04-09T14:53:00+08:00"), "INVALID_VISIBILITY"),
])
def test_invalid_inputs_fail_closed(store, bundle, mutation, expected):
    mutation(bundle)
    code(expected, lambda: store.import_bundle(bundle))


def test_cli_error_json_and_no_silent_options(published):
    store, _, _, sid = published
    result = subprocess.run([sys.executable, "-m", "ashare_data.cli", "--store", str(store.root),
                             "query", "--snapshot", sid, "bars", "--symbol", SYM, "--start", A, "--end", B],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == "COVERAGE_GAP"
    result = subprocess.run([sys.executable, "-m", "ashare_data.cli", "--store", str(store.root),
                             "query", "--snapshot", sid, "lineage", "--start", A], capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == "INVALID_REQUEST"


def test_existing_directory_and_read_connection_protection(tmp_path, published):
    path = tmp_path / "existing"
    path.mkdir()
    (path / "keep").write_text("keep")
    code("DIRECTORY_NOT_EMPTY", lambda: Store.init(path))
    assert (path / "keep").read_text() == "keep"
    store, _, _, _ = published
    with store._db() as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM snapshots")


def test_raw_and_inferred_rows_remain_separate(store, bundle, requirements):
    bundle["source"]["kind"] = "local_observation"
    for row in bundle["bars"]:
        row["quality"] = "observed"
    bundle["bars"][2]["quality"] = "inferred"
    bid = store.import_bundle(bundle)
    default = store.validate([bid], requirements=requirements)
    assert not default["passed"] and default["coverage"][0]["accepted"] == 2
    report = store.validate([bid], requirements=requirements, policy="research")
    sid = store.publish(report["id"])
    with store.snapshot(sid) as view:
        strict_window = view.bars([SYM], A, "2026-04-09T14:55:00+08:00")
        assert len(strict_window["rows"]) == 2
        assert {r["quality"] for r in strict_window["rows"]} == {"observed"}
        partial = view.bars([SYM], A, B, strict=False)
        assert partial["coverage"]["missing_count"] == 1
        assert partial["coverage"]["excluded"][0]["quality"] == "inferred"


def test_no_vacuous_publication(store, bundle):
    bid = store.import_bundle(bundle)
    empty_window = [{"symbols": [SYM], "start": "2026-04-09T12:00:00+08:00", "end": "2026-04-09T12:01:00+08:00"}]
    report = store.validate([bid], requirements=empty_window, policy="synthetic")
    assert not report["passed"] and report["coverage"][0]["expected"] == 0
    code("VALIDATION_FAILED", lambda: store.publish(report["id"]))
    code("INVALID_REQUEST", lambda: store.validate([bid], requirements=[]))


def test_lunch_and_close_boundaries(store, bundle):
    labels = ["11:30", "13:01", "15:00"]
    for row, label in zip(bundle["bars"], labels):
        row["timestamp"] = row["available_at"] = f"2026-04-09T{label}:00+08:00"
    windows = [("11:29", "11:30"), ("13:00", "13:01"), ("14:59", "15:00")]
    reqs = [{"symbols": [SYM], "start": f"2026-04-09T{a}:00+08:00", "end": f"2026-04-09T{b}:00+08:00"}
            for a, b in windows]
    bid = store.import_bundle(bundle)
    sid = store.publish(store.validate([bid], requirements=reqs, policy="synthetic")["id"])
    with store.snapshot(sid) as view:
        for req in reqs:
            assert len(view.bars(**req, quality="synthetic")["rows"]) == 1
        lunch = view.coverage([SYM], "2026-04-09T11:30:00+08:00", "2026-04-09T13:00:00+08:00", quality="synthetic")
        assert lunch["expected"] == 0 and lunch["complete"]


def test_pit_universe_available_time_and_sample_scope(store, bundle, requirements):
    bundle["instrument_sets"][0].update(scope="all_a_shares", available_at="2026-04-09T15:00:00+08:00")
    bid = store.import_bundle(bundle)
    sid = store.publish(store.validate([bid], requirements=requirements, policy="synthetic")["id"])
    with store.snapshot(sid) as view:
        code("PIT_UNAVAILABLE", lambda: view.instruments(as_of="2026-04-09T14:55:00+08:00"))
        result = JQStyle(view).get_all_securities(date="2026-04-09")
        assert result["scope"] == "all_a_shares" and result["snapshot_id"] == sid
        code("INVALID_REQUEST", lambda: JQStyle(view).get_all_securities())
        code("UNSUPPORTED", lambda: JQStyle(view).get_all_securities(types=["fund"], date="2026-04-09"))


def test_metadata_conflicts_block_publish(store, bundle, requirements):
    first = store.import_bundle(bundle)
    other = copy.deepcopy(bundle)
    other["bars"] = other["bars"][:1]
    other["bars"][0]["symbol"] = "600000.XSHG"
    other["calendar"][0]["evidence"] += " changed"
    second = store.import_bundle(other)
    report = store.validate([first, second], requirements=requirements, policy="synthetic")
    assert not report["passed"]
    assert any(c.get("dataset") == "calendar" for c in report["conflicts"])


def test_report_tampering_blocks_publish(store, bundle, requirements):
    bundle["bars"].pop()
    bid = store.import_bundle(bundle)
    report = store.validate([bid], requirements=requirements, policy="synthetic")
    with store._db(write=True) as db:
        row = json.loads(db.execute("SELECT body FROM reports WHERE id=?", (report["id"],)).fetchone()[0])
        row["passed"] = True
        db.execute("UPDATE reports SET body=? WHERE id=?", (json.dumps(row), report["id"]))
    code("INTEGRITY", lambda: store.publish(report["id"]))


def test_query_does_not_extrapolate_published_scope(published):
    store, _, _, sid = published
    with store.snapshot(sid) as view:
        error = code("COVERAGE_GAP", lambda: view.bars([SYM], A, "2026-04-09T15:00:00+08:00", quality="synthetic"))
        assert error.details["missing_count"] == 4
        source = view.lineage()["batches"][0]["source"]
        assert source["volume_unit"] == "lots"
        assert view.quality()["validation"]["requirements"]


def test_import_rejects_non_json_values_and_requirement_errors(store, bundle):
    bundle["bars"][0]["amount"] = float("nan")
    code("INVALID_BUNDLE", lambda: store.import_bundle(bundle))
    bundle["bars"][0]["amount"] = "0.2"
    bid = store.import_bundle(bundle)
    code("INVALID_REQUEST", lambda: store.validate([bid], requirements=[{}]))


def test_datetime_requirements_are_normalized(store, bundle, requirements):
    from datetime import datetime
    bid = store.import_bundle(bundle)
    spec = copy.deepcopy(requirements)
    spec[0]["start"] = datetime.fromisoformat(spec[0]["start"])
    spec[0]["end"] = datetime.fromisoformat(spec[0]["end"])
    report = store.validate([bid], requirements=spec, policy="synthetic")
    assert report["requirements"] == requirements
    assert store.publish(report["id"])


def test_duckdb_network_extension_paths_disabled(published):
    store, _, _, sid = published
    with store.snapshot(sid) as view:
        for setting in ["enable_external_access", "autoinstall_known_extensions", "autoload_known_extensions"]:
            assert view._conn.execute("SELECT current_setting(?)", [setting]).fetchone()[0] is False
