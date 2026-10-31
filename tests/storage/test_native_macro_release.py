"""Synthetic protocol fixtures; real sample end-to-end evidence lives outside git."""

from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import sqlite3

import pytest

from ashare_data.core.model import DataError
from ashare_data.providers.free_updates import catalog, digest, encoded, implementation_identity
from ashare_data.providers.free_updates.free_update_macro_history import collect
from ashare_data.providers.free_updates.free_update_semantics import validate_semantics
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection
from ashare_data.storage.native_macro_release import (
    CONTRACT,
    checked_bundle,
    prepare,
    compose,
    controller_boundary,
)


def bundle(root, previous=None, *, value=50, sequence=0):
    root.mkdir(exist_ok=True)
    records = [
        {
            "REPORT_DATE": f"2026-{m:02}-01 00:00:00",
            "TIME": f"2026年{m:02}月份",
            "MAKE_INDEX": value if m == 9 else 50,
            "NMAKE_INDEX": 51,
            "MAKE_SAME": 1,
            "NMAKE_SAME": 2,
        }
        for m in (9, 8, 7)
    ]
    clock = datetime(2026, 10, 28, 10, tzinfo=timezone.utc) + timedelta(hours=sequence)
    receipts, bodies = [], {}

    class Fixture:
        def get(self, url):
            body = encoded({"success": True, "result": {"count": 3, "pages": 1, "data": records}})
            sha = hashlib.sha256(body).hexdigest()
            bodies["raw/" + sha + ".gz"] = gzip.compress(body, mtime=0)
            receipts.append(
                {
                    "url": url,
                    "protocol": "http",
                    "status": 200,
                    "sha256": sha,
                    "bytes": len(body),
                    "received_at": (clock + timedelta(seconds=1)).isoformat(),
                    "host": "datacenter-web.eastmoney.com",
                }
            )
            return body

    c = Fixture()
    spec = json.loads(
        files("ashare_data.providers.free_updates").joinpath("public-macro-requests.json").read_text()
    )["specs"]["macro_china_pmi"]
    data = collect(c, spec, {})
    job = {"function": "macro_china_pmi", "parameters": {}}
    sem = validate_semantics(data, job, "2026-10-28")
    rid = digest([value, sequence])[:32]
    run = {
        "status": "received",
        "started_at": clock.isoformat(),
        "finished_at": (clock + timedelta(seconds=2)).isoformat(),
        "records": data,
        "receipts": receipts,
        "coverage": c.dataset_metadata,
        "observation_as_of": "2026-10-28",
        "implementation_sha256": implementation_identity(),
    }
    bodies["runs/" + rid + ".json"] = encoded(run)
    snapshot = sorted(
        [
            {
                "key": digest([174, "macro_china_pmi", [r["REPORT_DATE"]]]),
                "menu_row": 174,
                "function": "macro_china_pmi",
                "business_key": {"REPORT_DATE": r["REPORT_DATE"]},
                "payload_sha256": digest(r),
                "data": r,
            }
            for r in data
        ],
        key=lambda x: x["key"],
    )
    before = (
        {
            r["key"]: r["payload_sha256"]
            for r in json.loads((previous / "snapshot.json").read_text())
        }
        if previous
        else {}
    )
    delta = [r for r in snapshot if before.get(r["key"]) != r["payload_sha256"]]
    bodies["snapshot.json"] = encoded(snapshot)
    bodies["delta.json"] = encoded(delta)
    manifest = {
        "schema": "ad.free-source-handoff.v1",
        "implementation_sha256": implementation_identity(),
        "workbook_sha256": catalog()["workbook_sha256"],
        "menu_rows": [174],
        "previous_bundle": previous.name if previous else None,
        "snapshot_records": 3,
        "delta_records": len(delta),
        "checks": [
            {
                "excel_row": 174,
                "function": "macro_china_pmi",
                "run_id": rid,
                "observed_at": run["finished_at"],
                "coverage": c.dataset_metadata,
                "semantics": sem,
            }
        ],
        "raw_receipts": receipts,
        "files": {n: hashlib.sha256(b).hexdigest() for n, b in bodies.items()},
    }
    manifest["bundle_id"] = digest(manifest)
    path = root / manifest["bundle_id"]
    path.mkdir()
    for name, body in {**bodies, "manifest.json": encoded(manifest)}.items():
        target = path / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(body)
    return path


def seed(root):
    with ReleaseWriter(root, categorized=True, free_reserve_bytes=0) as writer:
        path = writer.work / "seed.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
                CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
                INSERT INTO release_instruments VALUES(42,'000001.XSHE','synthetic bank');
                INSERT INTO release_identifiers VALUES('000001.XSHE',42);""")
        metadata = writer.object(path, "sqlite3")
        with closing(connection(memory="128MB", threads=1)) as c:
            query = """SELECT 1::INTEGER series_id,42::INTEGER instrument_id,date '2024-01-02' trade_date,
                10::DECIMAL(18,6) open_price,10::DECIMAL(18,6) high_price,10::DECIMAL(18,6) low_price,
                10::DECIMAL(18,6) close_price,100::BIGINT volume_shares,1000::DOUBLE turnover_cny,1::INTEGER batch_id"""
            item = writer.parquet(c, query, [], "seed", table="market.bars_1d")
        item.update(
            table="bars_1d",
            series_id=1,
            instrument_id=42,
            rows=1,
            duplicate_keys=0,
            first_date="2024-01-02",
            last_date="2024-01-02",
        )
        writer.publish(
            {
                "metadata": metadata,
                "files": [item],
                "input_dispositions": {"synthetic": {"input_rows": 1, "adopted": 1}},
                "rules": {"source": "synthetic"},
            },
            expected_parent=None,
        )
    return MarketRelease(root)


@contextmanager
def coordinated(parent, candidate):
    with (parent.root / "driver.lock").open("a+b") as driver:
        fcntl.flock(driver, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ReleaseWriter(parent.root, categorized=True, free_reserve_bytes=0) as writer:
            status = {
                "writer_pid": os.getpid(),
                "boundary_available": True,
                "publication_readback_verified": True,
                "published": {"release_id": parent.release_id, "sha256": parent.sha256},
            }
            permit = controller_boundary(
                writer, parent, candidate, driver=driver, coordination=status
            )
            yield writer, permit


def publish(parent, source, output):
    candidate = prepare(parent, source, output)
    with coordinated(parent, candidate) as (writer, permit):
        manifest = compose(writer, parent, output, boundary=permit)
        writer.publish(
            manifest,
            expected_parent=parent.release_id,
            expected_parent_sha256=parent.sha256,
            native_macro_boundary=permit,
        )
    return MarketRelease(parent.root), candidate


def test_native_macro_full_chain_revisions_cutoffs_and_retained_market(tmp_path):
    parent = seed(tmp_path / "release")
    one = bundle(tmp_path / "bundles")
    r2, candidate = publish(parent, one, tmp_path / "candidate-1")
    reader = r2.native_macro()
    assert candidate["counts"] == {
        "input_rows": 3,
        "adopted": 3,
        "equal_duplicate": 0,
        "quarantined": 0,
    }
    assert len(reader.records(174, "2026-07-01", "2026-09-01")) == 3
    assert reader.point(174, "MAKE_INDEX", "2026-09-01")["value"] == 50
    assert reader.point(174, "MAKE_INDEX", "2026-09-01")["unit"] == "index_points"
    assert r2.identifiers == parent.identifiers == {"000001.XSHE": 42}
    assert r2.manifest["files"] == parent.manifest["files"]
    assert r2.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 10
    assert not (parent.root / "objects").exists() and not (parent.root / "work").exists()
    assert not (tmp_path / "candidate-1" / "CURRENT.json").exists()
    with pytest.raises(DataError, match="absent"):
        parent.native_macro()
    with pytest.raises(DataError, match="Historical publication"):
        reader.point(174, "MAKE_INDEX", "2026-09-01", visibility="historical_pit")
    assert (
        reader.point(174, "MAKE_INDEX", "2026-09-01", received_by="2026-10-28T09:00:00+00:00")[
            "status"
        ]
        == "missing"
    )
    repeated = prepare(r2, one, tmp_path / "unused")
    assert repeated["already_consumed"] and not (tmp_path / "unused").exists()
    two = bundle(tmp_path / "bundles", one, sequence=1)
    r3, nochange = publish(r2, two, tmp_path / "candidate-2")
    assert nochange["counts"]["adopted"] == 0 and nochange["counts"]["equal_duplicate"] == 3
    assert len(r3.native_macro().files) == 1
    three = bundle(tmp_path / "bundles", two, value=52, sequence=2)
    r4, revision = publish(r3, three, tmp_path / "candidate-3")
    assert revision["counts"]["adopted"] == 1
    four = bundle(tmp_path / "bundles", three, sequence=3)
    r5, reversion = publish(r4, four, tmp_path / "candidate-4")
    assert reversion["counts"]["adopted"] == 1
    assert r4.native_macro().point(174, "MAKE_INDEX", "2026-09-01")["value"] == 52
    current = r5.native_macro()
    assert current.point(174, "MAKE_INDEX", "2026-09-01")["value"] == 50
    assert (
        current.point(174, "MAKE_INDEX", "2026-09-01", received_by="2026-10-28T12:30:00+00:00")[
            "value"
        ]
        == 52
    )
    assert len(current.records(174, "2026-09-01", "2026-09-01", history=True)) == 3
    assert reader.point(174, "MAKE_INDEX", "2026-09-01")["value"] == 50
    assert (
        MarketRelease(parent.root, 2, expected_manifest_sha256=r2.sha256)
        .native_macro()
        .point(174, "MAKE_INDEX", "2026-09-01")["value"]
        == 50
    )


def test_parent_change_and_controller_boundary_are_not_inferred_from_free_lock(tmp_path):
    parent = seed(tmp_path / "release")
    one = bundle(tmp_path / "bundles")
    candidate = prepare(parent, one, tmp_path / "candidate")
    with ReleaseWriter(parent.root, categorized=True, free_reserve_bytes=0) as writer:
        with pytest.raises(DataError, match="live controller"):
            compose(writer, parent, tmp_path / "candidate", boundary=None)
        with (parent.root / "driver.lock").open("a+b") as driver:
            status = {
                "writer_pid": os.getpid(),
                "boundary_available": True,
                "publication_readback_verified": True,
                "published": {"release_id": 1, "sha256": parent.sha256},
            }
            with pytest.raises(DataError, match="already hold"):
                controller_boundary(writer, parent, candidate, driver=driver, coordination=status)
    marker = parent.root / ".work/import.json"
    marker.write_text("{}")
    with pytest.raises(DataError, match="released boundary"):
        with coordinated(parent, candidate):
            pass
    marker.unlink()
    with ReleaseWriter(parent.root, categorized=True, free_reserve_bytes=0) as writer:
        writer.publish(
            dict(parent.manifest), expected_parent=1, expected_parent_sha256=parent.sha256
        )
    with pytest.raises(DataError, match="Parent advanced"):
        with coordinated(parent, candidate):
            pass
    with pytest.raises(DataError, match="Parent advanced"):
        prepare(parent, one, tmp_path / "stale-output")
    assert not (tmp_path / "stale-output").exists()


def test_failed_pointer_update_recovers_without_duplicate_or_lost_history(tmp_path, monkeypatch):
    import ashare_data.storage.market_release as module

    parent = seed(tmp_path / "release")
    one = bundle(tmp_path / "bundles")
    candidate = prepare(parent, one, tmp_path / "candidate")
    original = module.atomic_write

    def fail(path, body):
        if Path(path).name == "CURRENT.json":
            raise OSError("injected pointer interruption")
        return original(path, body)

    with coordinated(parent, candidate) as (writer, permit):
        manifest = compose(writer, parent, tmp_path / "candidate", boundary=permit)
        monkeypatch.setattr(module, "atomic_write", fail)
        with pytest.raises(OSError, match="injected"):
            writer.publish(
                manifest,
                expected_parent=1,
                expected_parent_sha256=parent.sha256,
                native_macro_boundary=permit,
            )
        assert MarketRelease(parent.root).release_id == 1
        monkeypatch.setattr(module, "atomic_write", original)
        again = compose(writer, parent, tmp_path / "candidate", boundary=permit)
        assert again == manifest
        writer.publish(
            again,
            expected_parent=1,
            expected_parent_sha256=parent.sha256,
            native_macro_boundary=permit,
        )
    r2 = MarketRelease(parent.root)
    assert len(r2.native_macro().records(174, "2026-01-01", "2026-10-28")) == 3
    with ReleaseWriter(parent.root, categorized=True, free_reserve_bytes=0) as writer:
        removed = dict(r2.manifest)
        removed.pop(CONTRACT)
        with pytest.raises(DataError, match="contract"):
            writer.publish(removed, expected_parent=2, expected_parent_sha256=r2.sha256)
    assert MarketRelease(parent.root).release_id == 2


def test_bundle_integrity_cannot_be_replaced_with_a_claimed_success(tmp_path):
    source = bundle(tmp_path / "bundles")
    checked_bundle(source)
    snap = source / "snapshot.json"
    rows = json.loads(snap.read_text())
    rows[0]["data"]["MAKE_INDEX"] = 66
    rows[0]["payload_sha256"] = digest(rows[0]["data"])
    snap.write_bytes(encoded(rows))
    # Re-sign the outer manifest: the consumer must still replay original HTTP data.
    manifest = json.loads((source / "manifest.json").read_text())
    manifest.pop("bundle_id")
    manifest["files"]["snapshot.json"] = hashlib.sha256(snap.read_bytes()).hexdigest()
    (source / "delta.json").write_bytes(encoded(rows))
    manifest["files"]["delta.json"] = hashlib.sha256(
        (source / "delta.json").read_bytes()
    ).hexdigest()
    manifest["bundle_id"] = digest(manifest)
    (source / "manifest.json").write_bytes(encoded(manifest))
    target = source.parent / manifest["bundle_id"]
    source.rename(target)
    with pytest.raises(DataError, match="snapshot"):
        checked_bundle(target)
