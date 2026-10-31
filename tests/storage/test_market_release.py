"""The file-manifest protocol must survive interruption, repair and concurrent reads."""

from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from ashare_data.core.model import DataError
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection


def test_explicit_query_spill_directory_and_budget(tmp_path, monkeypatch):
    directory = tmp_path / "query-spill"
    monkeypatch.setenv("ASHARE_DUCKDB_TEMP_DIRECTORY", str(directory))
    monkeypatch.setenv("ASHARE_DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "32MB")
    monkeypatch.setenv("ASHARE_DUCKDB_MEMORY_LIMIT", "32MB")
    c = connection(memory="128MB", threads=1)
    try:
        assert c.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(directory)
        assert c.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0] == "30.5 MiB"
        assert c.execute("SELECT current_setting('memory_limit')").fetchone()[0] == "30.5 MiB"
    finally:
        c.close()
    explicit = tmp_path / "explicit"
    c = connection(explicit, memory="128MB", threads=1)
    try:
        assert c.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(explicit)
    finally:
        c.close()


def metadata(writer):
    path = writer.root / "work/reference.sqlite3"
    d = sqlite3.connect(path)
    d.executescript("""CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
        CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
        INSERT INTO release_instruments VALUES(1,'000001.XSHE','bank');
        INSERT INTO release_identifiers VALUES('000001.XSHE',1);""")
    d.executescript("""CREATE TABLE data_batches(batch_id INTEGER PRIMARY KEY,release_id INTEGER);
        INSERT INTO data_batches VALUES(1,1);
        CREATE TABLE source_assets(asset_id TEXT PRIMARY KEY,body TEXT);
        INSERT INTO source_assets VALUES('test','immutable-source');""")
    d.commit()
    d.close()
    return writer.object(path, "sqlite3")


def chunk(writer, price, batch=1):
    c = connection(memory="128MB", threads=1)
    query = f"""select 1::INTEGER series_id,1::INTEGER instrument_id,date '2024-01-02' trade_date,
      {price}::DECIMAL(18,6) open_price,{price}::DECIMAL(18,6) high_price,{price}::DECIMAL(18,6) low_price,
      {price}::DECIMAL(18,6) close_price,100::BIGINT volume_shares,1000::DOUBLE turnover_cny,{batch}::INTEGER batch_id"""
    result = writer.parquet(c, query, [], "test")
    c.close()
    return {
        **result,
        "table": "bars_1d",
        "series_id": 1,
        "instrument_id": 1,
        "rows": 1,
        "first_date": "2024-01-02",
        "last_date": "2024-01-02",
        "duplicate_keys": 0,
    }


def manifest(meta, files):
    return {
        "metadata": meta,
        "files": files,
        "input_dispositions": {"source": {"input_rows": 1, "adopted": 1}},
        "rules": {"fixed": "version-one"},
    }


def test_old_release_survives_replacement_and_reader_lifetime(tmp_path):
    with ReleaseWriter(tmp_path, budget_bytes=1024**3, free_reserve_bytes=0) as w:
        meta = metadata(w)
        a = chunk(w, 10)
        r1 = w.publish(manifest(meta, [a]), expected_parent=None)
        fixed = MarketRelease(tmp_path)
        b = chunk(w, 11)
        w.publish(manifest(meta, [b]), expected_parent=1)
        assert (
            fixed.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 10
        )
        assert (
            MarketRelease(tmp_path).read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0][
                "close_price"
            ]
            == 11
        )
        assert (
            MarketRelease(tmp_path, 1, expected_manifest_sha256=r1["sha256"]).sha256 == fixed.sha256
        )
        assert (tmp_path / a["path"]).exists()
        with pytest.raises(DataError, match="current parent"):
            w.publish(manifest(meta, [b]), expected_parent=1)


def test_unpublished_chunk_invisible_idempotent_object_and_tamper(tmp_path):
    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as w:
        meta = metadata(w)
        a = chunk(w, 10)
        b = chunk(w, 10)
        assert a["path"] == b["path"]
        assert not (tmp_path / "CURRENT.json").exists()
        w.publish(manifest(meta, [a]), expected_parent=None)
    reader = MarketRelease(tmp_path)
    path = tmp_path / a["path"]
    path.write_bytes(b"corrupt")
    with pytest.raises(DataError, match="Immutable object changed"):
        reader.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")


def test_disposition_and_partition_publish_gates(tmp_path):
    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as w:
        meta = metadata(w)
        a = chunk(w, 10)
        with pytest.raises(DataError, match="Exactly one complete instrument"):
            w.publish(manifest(meta, [a, a]), expected_parent=None)
        invalid = manifest(meta, [a])
        invalid["input_dispositions"]["source"]["input_rows"] = 2
        with pytest.raises(DataError, match="do not balance"):
            w.publish(invalid, expected_parent=None)
        assert not (tmp_path / "CURRENT.json").exists()


def test_another_process_can_read_while_writer_builds(tmp_path):
    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as w:
        meta = metadata(w)
        a = chunk(w, 10)
        w.publish(manifest(meta, [a]), expected_parent=None)
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                "from ashare_data.storage.market_release import MarketRelease;import sys;print(MarketRelease(sys.argv[1]).read('bars_1d','000001.XSHE','2024-01-02','2024-01-02')[0]['close_price'])",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert process.returncode == 0, process.stderr
        assert process.stdout.strip() == "10.000000"
        chunk(w, 11)  # Only a candidate; does not change the reader-visible release.
        assert MarketRelease(tmp_path).release_id == 1


def test_crash_after_manifest_before_pointer_is_recoverable(tmp_path, monkeypatch):
    import ashare_data.storage.market_release as module

    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as w:
        meta = metadata(w)
        a = chunk(w, 10)
        candidate = manifest(meta, [a])
        real = module.atomic_write

        def crash(path, body):
            if Path(path).name == "CURRENT.json":
                raise OSError("injected power loss before publish")
            return real(path, body)

        monkeypatch.setattr(module, "atomic_write", crash)
        with pytest.raises(OSError, match="power loss"):
            w.publish(candidate, expected_parent=None)
        assert (tmp_path / "releases/00000001.json").exists()
        assert not (tmp_path / "CURRENT.json").exists()
        monkeypatch.setattr(module, "atomic_write", real)
        w.publish(candidate, expected_parent=None)
        assert MarketRelease(tmp_path).release_id == 1


def test_resume_backup_restore_old_and_current_values(tmp_path):
    from ashare_data.product.release_cli import backup

    source = tmp_path / "source"
    target = tmp_path / "backup"
    restored = tmp_path / "restored"
    with ReleaseWriter(source, free_reserve_bytes=0) as w:
        meta = metadata(w)
        w.publish(manifest(meta, [chunk(w, 10)]), expected_parent=None)
        w.publish(manifest(meta, [chunk(w, 11)]), expected_parent=1)
    first = backup(MarketRelease(source), target)
    assert backup(MarketRelease(source), target)["bytes_checked"] == first["bytes_checked"]
    backup(MarketRelease(target), restored)
    for rid, expected in ((1, 10), (2, 11)):
        assert (
            MarketRelease(restored, rid).read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[
                0
            ]["close_price"]
            == expected
        )


def test_minute_float_and_decimal_are_never_silently_coerced(tmp_path):
    from decimal import Decimal

    files = []
    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as w:
        meta = metadata(w)
        c = connection(memory="128MB", threads=1)
        for series, price in [
            (2, "8.350000381469727::DOUBLE"),
            (3, "'123456789.123456789012'::DECIMAL(38,12)"),
        ]:
            query = f"""select {series}::INTEGER series_id,1::INTEGER instrument_id,date '2024-01-02' trade_date,
              TIMESTAMPTZ '2024-01-02 01:31:00+00' label_at,{price} open_price,{price} high_price,
              {price} low_price,{price} close_price,100::BIGINT volume_shares,{price} turnover_cny,1::INTEGER batch_id"""
            obj = w.parquet(c, query, [], f"series-{series}")
            files.append(
                {
                    **obj,
                    "table": "bars_1m",
                    "series_id": series,
                    "instrument_id": 1,
                    "rows": 1,
                    "first_date": "2024-01-02",
                    "last_date": "2024-01-02",
                    "duplicate_keys": 0,
                }
            )
        c.close()
        w.publish(manifest(meta, files), expected_parent=None)
    r = MarketRelease(tmp_path)
    rows = r.read("bars_1m", "000001.XSHE", "2024-01-02T00:00:00Z", "2024-01-02T23:59:59Z")
    assert rows[0]["close_price"] == 8.350000381469727
    assert rows[1]["close_price"] == Decimal("123456789.123456789012")
    with r.sql(tables=("bars_1m",)) as c:
        values = c.execute("select close_price from market.bars_1m order by series_id").fetchall()
        assert values == [(8.350000381469727,), (Decimal("123456789.123456789012"),)]


def test_incremental_revision_and_duplicate_input_are_idempotent(tmp_path):
    import json
    import shutil
    from ashare_data.product.release_cli import apply
    from ashare_data.storage.market_release import sha_file

    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as w:
        meta = metadata(w)
        first = chunk(w, 10)
        w.publish(manifest(meta, [first]), expected_parent=None)
        second = chunk(w, 11, batch=2)
    changed_meta = tmp_path / "work/new-reference.sqlite3"
    shutil.copyfile(tmp_path / meta["path"], changed_meta)
    with sqlite3.connect(changed_meta) as db:
        db.execute("insert into data_batches values(2,2)")
        db.commit()
    evidence = tmp_path / "work/correction.json"
    evidence.write_text(json.dumps({"reason": "test source correction", "old": 10, "new": 11}))
    current = MarketRelease(tmp_path)
    plan = {
        "schema": "ad-market-replacement-plan.v1",
        "parent_manifest_sha256": current.sha256,
        "change_reason": "verified correction test",
        "chunks": [
            {
                "table": "bars_1d",
                "series_id": 1,
                "instrument_id": 1,
                "parquet": str(tmp_path / second["path"]),
                "sha256": second["sha256"],
            }
        ],
        "metadata": {"path": str(changed_meta), "sha256": sha_file(changed_meta)},
        "evidence": [{"path": str(evidence), "sha256": sha_file(evidence)}],
        "input_dispositions": {"patch": {"input_rows": 1, "adopted": 1}},
    }
    assert apply(current, plan)["release_id"] == 2
    # A crash after publication but before the caller records success must not
    # create another release or reject an already committed identical plan.
    assert apply(current, plan)["already_applied"]
    assert apply(MarketRelease(tmp_path), plan)["already_applied"]
    assert (
        current.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 10
    )
    current = MarketRelease(tmp_path)
    plan["parent_manifest_sha256"] = current.sha256
    assert apply(current, plan)["unchanged"]
    assert MarketRelease(tmp_path).release_id == 2


@pytest.mark.parametrize("incremental_validation", [False, True])
def test_increment_refreshes_expected_and_new_days_then_delete(
    tmp_path, monkeypatch, incremental_validation
):
    import json
    import shutil
    import ashare_data.storage.market_release as module
    from ashare_data.product.release_cli import apply
    from ashare_data.storage.market_release import sha_file, sql_literal

    c = connection(memory="128MB", threads=1)
    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as w:
        meta = metadata(w)
        first = chunk(w, 10)
        check = w.parquet(
            c,
            "select 1 series_id,1 instrument_id,date '2024-01-02' trade_date,'expected' expected_state,1::BIGINT expected_count,1::BIGINT observed_count,0::BIGINT missing_count,0 missing_day",
            [],
            "coverage",
        )
        check.update(kind="bar_day_checks", series_id=1, instrument_id=1)
        value = {
            **manifest(meta, [first]),
            "evidence": [check],
            "coverage": {
                "counts_by_series": {
                    "1": {
                        "expected_days": 1,
                        "expected_observed_days": 1,
                        "expected_missing_days": 0,
                        "expected_observed_rows": 1,
                    }
                }
            },
            "created_at": "old",
            "capacity": {"obsolete": True},
        }
        w.publish(value, expected_parent=None)
    r1 = MarketRelease(tmp_path)
    proposed = tmp_path / "work/proposed.sqlite3"
    shutil.copyfile(tmp_path / meta["path"], proposed)
    with sqlite3.connect(proposed) as db:
        db.execute("insert into data_batches values(2,2)")
    replacement = tmp_path / "work/new-days.parquet"
    old = "select * from read_parquet(" + sql_literal(tmp_path / first["path"]) + ")"
    c.execute(
        "copy ("
        + old
        + " union all select * replace(date '2024-01-03' as trade_date,2::INTEGER as batch_id) from ("
        + old
        + ")) to "
        + sql_literal(replacement)
        + " (format parquet)"
    )
    proof = tmp_path / "work/proof.json"
    proof.write_text('{"test_only":true}')
    key = {"table": "bars_1d", "series_id": 1, "instrument_id": 1}
    plan = {
        "schema": "ad-market-replacement-plan.v1",
        "parent_manifest_sha256": r1.sha256,
        "change_reason": "isolated append test",
        "incremental_validation": incremental_validation,
        "chunks": [{**key, "parquet": str(replacement), "sha256": sha_file(replacement)}],
        "metadata": {"path": str(proposed), "sha256": sha_file(proposed)},
        "evidence": [{"path": str(proof), "sha256": sha_file(proof)}],
        "input_dispositions": {"patch": {"input_rows": 1, "adopted": 1}},
    }
    with pytest.raises(DataError, match="One replacement"):
        apply(r1, {**plan, "chunks": plan["chunks"] * 2})
    real = module.atomic_write

    def crash(path, body):
        if Path(path).name == "CURRENT.json":
            raise OSError("injected pointer failure")
        return real(path, body)

    monkeypatch.setattr(module, "atomic_write", crash)
    with pytest.raises(OSError, match="pointer failure"):
        apply(r1, plan)
    assert MarketRelease(tmp_path).release_id == 1
    monkeypatch.setattr(module, "atomic_write", real)
    assert apply(r1, plan)["release_id"] == 2
    r2 = MarketRelease(tmp_path)

    def coverage_rows(release):
        item = next(x for x in release.manifest["evidence"] if x.get("kind") == "bar_day_checks")
        return c.execute(
            "select cast(trade_date as varchar),expected_state,expected_count,observed_count,missing_day from read_parquet("
            + sql_literal(release.verify(item))
            + ") order by trade_date"
        ).fetchall()

    assert coverage_rows(r2) == [
        ("2024-01-02", "expected", 1, 1, 0),
        ("2024-01-03", "unknown", None, 1, 0),
    ]
    assert coverage_rows(r1) == [("2024-01-02", "expected", 1, 1, 0)]
    assert r2.manifest["coverage"]["counts_by_series"]["1"]["unknown_observed_rows"] == 1
    assert "obsolete" not in r2.manifest["capacity"]
    assert r2.manifest["created_at"] != "old"
    with r2.metadata() as db:
        assert (
            json.loads(
                db.execute("select body from release_contracts where name='coverage'").fetchone()[0]
            )
            == r2.manifest["coverage"]
        )
    deletion = {
        **plan,
        "parent_manifest_sha256": r2.sha256,
        "chunks": [{**key, "delete": True}],
        "metadata": {
            "path": str(tmp_path / r2.manifest["metadata"]["path"]),
            "sha256": r2.manifest["metadata"]["sha256"],
        },
        "input_dispositions": {"patch": {"input_rows": 0}},
    }
    assert apply(r2, deletion)["release_id"] == 3
    r3 = MarketRelease(tmp_path)
    assert coverage_rows(r3) == [
        ("2024-01-02", "expected", 1, 0, 1),
        ("2024-01-03", "unknown", None, 0, 0),
    ]
    assert r3.manifest["target_unique_keys"]["bars_1d"] == 0
    assert r3.manifest["coverage"]["counts_by_series"]["1"]["expected_missing_days"] == 1
    assert len(r1.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-03")) == 1
    assert len(r2.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-03")) == 2
    deletion["parent_manifest_sha256"] = r3.sha256
    assert apply(r3, deletion)["unchanged"]
    c.close()


def test_daily_supplement_float_and_decimal_remain_exact(tmp_path):
    from decimal import Decimal

    files = []
    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as w:
        meta = metadata(w)
        c = connection(memory="128MB", threads=1)
        for series, price in [
            (1, "8.350000381469727::DOUBLE"),
            (5, "'123456789.123456789012'::DECIMAL(38,12)"),
        ]:
            query = f"""select {series}::INTEGER series_id,1::INTEGER instrument_id,date '2024-01-02' trade_date,
              {price} open_price,{price} high_price,
              {price} low_price,{price} close_price,100::BIGINT volume_shares,{price} turnover_cny,1::INTEGER batch_id"""
            obj = w.parquet(c, query, [], f"series-{series}")
            files.append(
                {
                    **obj,
                    "table": "bars_1d",
                    "series_id": series,
                    "instrument_id": 1,
                    "rows": 1,
                    "first_date": "2024-01-02",
                    "last_date": "2024-01-02",
                    "duplicate_keys": 0,
                }
            )
        c.close()
        w.publish(manifest(meta, files), expected_parent=None)
    r = MarketRelease(tmp_path)
    rows = r.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")
    assert rows[0]["close_price"] == 8.350000381469727
    assert rows[1]["close_price"] == Decimal("123456789.123456789012")
    with r.sql(tables=("bars_1d",)) as c:
        values = c.execute("select close_price from market.bars_1d order by series_id").fetchall()
        assert values == [(8.350000381469727,), (Decimal("123456789.123456789012"),)]
