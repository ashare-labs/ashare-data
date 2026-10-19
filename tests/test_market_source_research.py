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


@pytest.mark.parametrize("source", ["auto", "free_stockdb"])
@pytest.mark.parametrize("with_observation", [True, False])
def test_missing_listing_keeps_coverage_unknown_even_on_known_calendar(
    market_library, tmp_path, with_observation, source
):
    from ashare_data.product.market_release import ReleasedDailyProduct
    from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection

    root, days = market_library
    target = tmp_path / "released"
    with ReleaseWriter(target, free_reserve_bytes=0) as writer:
        path = writer.root / "work/metadata.sqlite3"
        shutil.copyfile(root / "daily.sqlite3", path)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("DELETE FROM listing_facts WHERE symbol='sz000858'")
            db.executescript("""CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
              CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
              INSERT INTO release_instruments VALUES(1,'000858.XSHE','test');
              INSERT INTO release_identifiers VALUES('000858.XSHE',1);""")
            db.commit()
        facts = []
        if with_observation:
            c = connection(memory="128MB", threads=1)
            item = writer.parquet(
                c,
                f"""SELECT 5::INTEGER series_id,1::INTEGER instrument_id,
              DATE '{days[0]}' trade_date,10::DOUBLE open_price,11::DOUBLE high_price,
              9::DOUBLE low_price,10::DOUBLE close_price,100::BIGINT volume_shares,
              1000::DOUBLE turnover_cny,1::INTEGER batch_id,'sz000858' source_symbol,
              'free_stockdb' AS source""",
                [],
                "observation",
            )
            c.close()
            item.update(
                table="bars_1d",
                series_id=5,
                instrument_id=1,
                rows=1,
                first_date=days[0],
                last_date=days[0],
                duplicate_keys=0,
            )
            facts.append(item)
        writer.publish(
            dict(
                metadata=writer.object(path, "sqlite3"),
                files=facts,
                input_dispositions={"source": {"input_rows": len(facts), "adopted": len(facts)}},
            ),
            expected_parent=None,
        )
    r = MarketRelease(target)
    owner = ReleasedDailyProduct(
        {"market_release": {"root": str(target), "release_id": 1, "manifest_sha256": r.sha256}}
    )
    result = owner.prices(["000858.XSHE"], days[0], days[2], source=source)
    if source == "free_stockdb":
        assert result["access"] == "published_source_subset"
        assert result["coverage"][0]["source_metadata"]["access"] == "published_source_subset"
        assert all(row["source"] == "free_stockdb" for row in result["rows"])
    assert len(result["rows"]) == int(with_observation)
    assert len(result["calendar"]) == 3
    item = result["coverage"][0]
    assert item["expected_days"] is item["listing_days"] is item["missing_days"] is None
    assert item["expected_state"] == "unknown" and item["calendar_session_count"] == 3
    assert any(gap["code"] == "LISTING_STATE_UNKNOWN" for gap in result["gaps"])


def test_free_stockdb_requires_a_published_view(market_library):
    from ashare_data.product.daily import LocalDailyProduct

    root, days = market_library
    with pytest.raises(DataError, match="require a pinned market release"):
        LocalDailyProduct(root).prices([IDS[0]], days[0], days[2], source="free_stockdb")
