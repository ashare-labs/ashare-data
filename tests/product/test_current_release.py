"""Current selection freezes one committed snapshot per reader."""
import json

import pytest

from ashare_data.core.model import DataError
from ashare_data.product.configuration import resolve
from ashare_data.product.daily import LocalDailyProduct
from ashare_data.product.minute import LocalMinuteProduct
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter
from test_market_release import chunk, manifest, metadata


def deployment(tmp_path):
    value = {
        "schema": "ashare-deployment.v1", "data_root": ".",
        "assets": {
            "old": {"kind": "local_daily", "path": "unopened-old"},
            "store": {"kind": "store", "path": "research"},
            "cmes": {"kind": "market_release", "path": "market"},
        },
        "profiles": {"product": {
            "kind": "product", "daily_asset": "old", "release_store_asset": "store",
            "market_release": {"asset": "cmes", "mode": "current"},
            "reference_sets": {"formal": {"kind": "market_release"}},
            "default_reference_set": "formal", "default_minute_source": "canonical",
        }},
    }
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(value))
    return path, value


def test_new_readers_advance_existing_readers_and_fixed_pins_reproduce(tmp_path):
    path, doc = deployment(tmp_path)
    with ReleaseWriter(tmp_path / "market", free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        first = writer.publish(manifest(meta, [chunk(writer, 10)]), expected_parent=None)
        config = resolve(path)
        daily = LocalDailyProduct.from_deployment(path)
        minute = LocalMinuteProduct(path)
        unpublished = chunk(writer, 20)
        assert resolve(path)["market_release"] == config["market_release"]
        assert config["references"]["release"] == config["market_release"]
        writer.publish(manifest(meta, [unpublished]), expected_parent=1)
        assert daily.release.release_id == minute.frozen_market_config["market_release"]["release_id"] == 1
        assert daily.release.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")[0]["close_price"] == 10
        assert LocalDailyProduct.from_deployment(path).release.release_id == 2
        assert LocalMinuteProduct(path).frozen_market_config["market_release"]["release_id"] == 2
        doc["profiles"]["product"]["market_release"] = {
            "asset": "cmes", "release_id": 1, "manifest_sha256": first["sha256"],
        }
        path.write_text(json.dumps(doc))
        assert LocalDailyProduct.from_deployment(path).release.release_id == 1
        assert not (tmp_path / "unopened-old").exists()


def test_missing_corrupt_and_conflicting_current_fail_without_fallback(tmp_path):
    path, doc = deployment(tmp_path)
    with pytest.raises(DataError, match="Cannot resolve"):
        resolve(path)
    with ReleaseWriter(tmp_path / "market", free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        writer.publish(manifest(meta, [chunk(writer, 10)]), expected_parent=None)
    doc["profiles"]["product"]["market_release"]["release_id"] = 1
    path.write_text(json.dumps(doc))
    with pytest.raises(DataError, match="cannot also declare"):
        resolve(path)
    assert not (tmp_path / "unopened-old").exists()


def test_shared_parquet_is_scoped_and_never_counted_twice(tmp_path):
    from ashare_data.storage.market_release import SCHEMA, atomic_write, canonical, connection, sha_file

    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        c = connection(memory="128MB", threads=1)
        item = writer.parquet(c, """SELECT 1::INTEGER series_id, i::INTEGER instrument_id,
            date '2024-01-02' trade_date, i::DECIMAL(18,6) open_price,
            i::DECIMAL(18,6) high_price,i::DECIMAL(18,6) low_price,
            i::DECIMAL(18,6) close_price,100::BIGINT volume_shares,
            1000::DOUBLE turnover_cny,1::INTEGER batch_id FROM range(1,3) t(i)""",
            [], "bundled")
        c.close()
        item.update(table="bars_1d", series_id=1, instrument_id=1, rows=1,
                    first_date="2024-01-02", last_date="2024-01-02", duplicate_keys=0)
        value = manifest(meta, [item])
        # External CMES publishes instrument descriptors for shared physical files.
        value.update(schema=SCHEMA, release_id=1, parent_release_id=None,
                     parent_manifest_sha256=None)
        target = tmp_path / "releases/00000001.json"
        atomic_write(target, canonical(value))
        atomic_write(tmp_path / "CURRENT.json", canonical(dict(
            schema=SCHEMA, release_id=1, manifest="releases/00000001.json", sha256=sha_file(target))))
    release = MarketRelease(tmp_path)
    rows = release.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")
    assert len(rows) == 1 and rows[0]["instrument_id"] == 1
    release.files.append(dict(item))
    c = release.sql(tables=("bars_1d",), securities=["000001.XSHE"])
    assert c.execute("select instrument_id,count(*) from market.bars_1d group by 1").fetchall() == [(1, 1)]
    c.close()


def test_minute_dispositions_do_not_embed_unrelated_import_ledger():
    from types import SimpleNamespace
    from ashare_data.product.market_release import minute_dispositions

    ledger = {str(i): {"input_rows": 1, "adopted": 1} for i in range(10000)}
    release = SimpleNamespace(manifest={"cmes": {}, "input_dispositions": ledger},
                              release_id=1, sha256="a" * 64)
    release.manifest["cmes"] = {"source": "cmes"}
    value = minute_dispositions(release, [{"source_asset_id": "1"}])
    assert value["input_dispositions"] == {"1": ledger["1"]}
    assert value["input_disposition_scope"]["published_source_assets"] == 10000
    assert len(json.dumps(value)) < 1000
    assert minute_dispositions(release, [])["input_dispositions"] == {}
