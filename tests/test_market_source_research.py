"""Source exclusions must not silently erase explicit research observations."""

import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from ashare_data.core.model import DataError
from ashare_data.product.market_release import FrozenDailyStore
from ashare_data.storage.market_release import fingerprint, sha_file
from ashare_data.storage.source_observations import daily_source
from test_local_daily_product import library, IDS  # noqa: F401


@pytest.fixture
def market_library(tmp_path):
    return library.__wrapped__(tmp_path)


def pinned_asset(root):
    (root / "writer.lock").touch()
    paths = [root / "daily.sqlite3"]
    paths.extend(p for p in (root / "daily.sqlite3-wal",) if p.exists())
    return {
        "files": {str(p): sha_file(p) for p in paths},
        "fingerprints": {str(p): fingerprint(p) for p in paths},
    }


def test_explicit_source_retains_rows_that_lost_canonical_selection(market_library, tmp_path):
    root, days = market_library
    asset = pinned_asset(root)
    meta = tmp_path / "frozen.sqlite3"
    shutil.copyfile(root / "daily.sqlite3", meta)
    with sqlite3.connect(meta) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("create table source_assets(asset_id text primary key,body text)")
        db.execute("insert into source_assets values (?,?)", ("daily-sqlite", json.dumps(asset)))
        db.commit()

    def canonical_only(*args, **kwargs):
        raise AssertionError(
            "Explicit source observations must not be filtered through the canonical winner"
        )

    release = SimpleNamespace(
        root=tmp_path, metadata_path=meta, receipt=lambda: {"release_id": 2}, read=canonical_only
    )
    store = FrozenDailyStore(release)
    try:
        actual = store.get_price(IDS[0], days[0], days[4], source="sina")
        with daily_source(asset) as original:
            expected = original.get_price(IDS[0], days[0], days[4], source="sina")
        assert actual.to_dict("records") == expected.to_dict("records")
        assert len(actual) == 5 and set(actual.source) == {"sina"}
        assert actual.attrs["access"] == "explicit_frozen_source_observation"
        assert not actual.attrs["canonical"]
    finally:
        store.close()


def test_source_change_fails_closed_instead_of_querying_current_values(market_library):
    root, _ = market_library
    asset = pinned_asset(root)
    with daily_source(asset) as store:
        assert store.db.execute("select count(*) from bars").fetchone()[0] > 0
    with sqlite3.connect(root / "daily.sqlite3") as db:
        db.execute("update bars set close='999' where source='sina'")
        db.commit()
    with pytest.raises(DataError, match="frozen source snapshot changed"):
        with daily_source(asset):
            pass


def test_restored_conflict_remains_denied_in_published_minute_reader(tmp_path):
    from ashare_data.product.market_release import minute_prices
    from ashare_data.storage.market_release import ReleaseWriter, MarketRelease, connection
    from test_market_release import metadata

    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as writer:
        original = metadata(writer)
        meta_path = writer.root / "work/restrictions.sqlite3"
        shutil.copyfile(writer.root / original["path"], meta_path)
        with sqlite3.connect(meta_path) as db:
            db.execute(
                "CREATE TABLE release_row_restrictions(series_id INTEGER,instrument_id INTEGER,label_at TEXT,price_status TEXT,rule TEXT,execution TEXT,fresh_valuation TEXT)"
            )
            db.execute(
                "INSERT INTO release_row_restrictions VALUES(2,1,'2013-11-07T07:00:00+00:00','conflict','existing_reference_conflict','deny','deny')"
            )
            db.commit()
        c = connection(memory="128MB", threads=1)
        item = writer.parquet(
            c,
            "SELECT 2::INTEGER series_id,1::INTEGER instrument_id,DATE '2013-11-07' trade_date,TIMESTAMPTZ '2013-11-07 07:00:00+00' label_at,10::DOUBLE open_price,11::DOUBLE high_price,9::DOUBLE low_price,10::DOUBLE close_price,100::BIGINT volume_shares,1000::DOUBLE turnover_cny,1::INTEGER batch_id",
            [],
            "restricted",
        )
        c.close()
        item.update(
            table="bars_1m",
            series_id=2,
            instrument_id=1,
            rows=1,
            first_date="2013-11-07",
            last_date="2013-11-07",
            duplicate_keys=0,
        )
        writer.publish(
            dict(
                metadata=writer.object(meta_path, "sqlite3"),
                files=[item],
                input_dispositions={"source": {"input_rows": 1, "adopted": 1}},
            ),
            expected_parent=None,
        )
    release = MarketRelease(tmp_path)
    config = {
        "market_release": {
            "root": str(tmp_path),
            "release_id": 1,
            "manifest_sha256": release.sha256,
        }
    }
    result = minute_prices(
        config, "000001.XSHE", "2013-11-07T14:59:00+08:00", "2013-11-07T15:00:00+08:00"
    )
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["close"] == 10
    assert row["price_status"] == "conflict"
    assert row["use_decision"]["execution"] == row["use_decision"]["fresh_valuation"] == "deny"
