"""Small release fixtures: invalidity, conflict and whole-day source selection."""

import json
import shutil
import sqlite3

import pytest

from ashare_data.product.market_release import minute_prices
from ashare_data.product.minute_quality import standard_quote
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection
from test_market_release import metadata


def released_fixture(root, *, retained_invalid=False, series=2, matching_hash=True):
    with ReleaseWriter(root, free_reserve_bytes=0) as writer:
        original = metadata(writer)
        meta = writer.root / "work/restrictions.sqlite3"
        shutil.copyfile(writer.root / original["path"], meta)
        review_path = writer.root / "work/review.json"
        review_path.write_text(
            json.dumps(
                {
                    "schema": "neigezhu-quality-overlay.v1",
                    "manifest_sha256": "a" * 64,
                    "ranges": [
                        {
                            "path": "data/stock_1m/SZ/000001.parquet",
                            "source_sha256": "b" * 64,
                            "first_row": 7,
                            "last_row": 7,
                            "price_status": "invalid",
                            "rule": "ohlc_envelope_error",
                        }
                    ],
                }
            )
        )
        review = writer.object(review_path, "json")
        with sqlite3.connect(meta) as db:
            db.executescript("""
                CREATE TABLE release_contracts(name TEXT,body TEXT);
                CREATE TABLE release_row_restrictions(series_id INTEGER,instrument_id INTEGER,
                  label_at TEXT,price_status TEXT,rule TEXT,execution TEXT,fresh_valuation TEXT);
                INSERT INTO release_row_restrictions VALUES
                  (2,1,'2014-12-22T01:34:00+00:00','invalid','ohlc_envelope_error','deny','deny'),
                  (2,1,'2014-12-22T01:36:00+00:00','conflict','reference_conflict','deny','deny');
            """)
            db.execute(
                "INSERT INTO release_contracts VALUES (?,?)",
                (
                    "source_observation_readers",
                    json.dumps(
                        {
                            "minute_sources": {
                                "neigezhu": {
                                    "review": review,
                                    "manifest": {"sha256": "a" * 64},
                                }
                            }
                        }
                    ),
                ),
            )
            # No raw file exists. The reader must use only the tiny sealed ledger.
            db.execute(
                "INSERT INTO source_assets VALUES (?,?)",
                (
                    "123",
                    json.dumps(
                        {
                            "instrument_id": 1,
                            "source_version": "a" * 64,
                            "path": "/not-opened/data/stock_1m/SZ/000001.parquet",
                            "sha256": ("b" if matching_hash else "c") * 64,
                        }
                    ),
                ),
            )
        c = connection(memory="128MB", threads=1)
        issue = writer.parquet(
            c,
            """
            SELECT 123::INTEGER asset_id,7::BIGINT file_row_number,
              TIMESTAMP '2014-12-22 09:34:00' AS timestamp,'quarantined' disposition,
              'pinned_review_restriction' reason
            UNION ALL SELECT 123,8,TIMESTAMP '2014-12-22 09:32:00',
              'quarantined','conflicting_duplicate_key'
        """,
            [],
            "issues",
        )
        issue.update(kind="issues", series_id=2, instrument_id=1, check_version="hf-observation-v1")
        minutes = [33, 35, 36] + ([34] if retained_invalid else [])
        item = writer.parquet(
            c,
            f"""
            SELECT {series}::INTEGER series_id,1::INTEGER instrument_id,
              DATE '2014-12-22' trade_date,
              TIMESTAMPTZ '2014-12-22 01:00:00+00' + n * INTERVAL '1 minute' label_at,
              10::DOUBLE open_price,CASE WHEN n=36 THEN 2326.53 ELSE 11 END::DOUBLE high_price,
              9::DOUBLE low_price,10::DOUBLE close_price,
              100::BIGINT volume_shares,1000::DOUBLE turnover_cny,1::INTEGER batch_id
            FROM unnest({minutes}) t(n)
        """,
            [],
            "bars",
        )
        c.close()
        item.update(
            table="bars_1m",
            series_id=series,
            instrument_id=1,
            rows=len(minutes),
            first_date="2014-12-22",
            last_date="2014-12-22",
            duplicate_keys=0,
        )
        writer.publish(
            dict(
                metadata=writer.object(meta, "sqlite3"),
                files=[item],
                evidence=[issue, review],
                input_dispositions={
                    "source": {"input_rows": len(minutes), "adopted": len(minutes)}
                },
            ),
            expected_parent=None,
        )
    release = MarketRelease(root)
    return {
        "market_release": {"root": str(root), "release_id": 1, "manifest_sha256": release.sha256}
    }


def read(config):
    return minute_prices(
        config, "000001.XSHE", "2014-12-22T09:32:00+08:00", "2014-12-22T09:36:00+08:00"
    )


@pytest.mark.parametrize("retained_invalid", [False, True])
def test_confirmed_invalid_preserves_slot_without_prices_or_fill(tmp_path, retained_invalid):
    result = read(released_fixture(tmp_path, retained_invalid=retained_invalid))
    rows = result["rows"]
    assert len(rows) == 4  # Unknown duplicate quarantine at 09:32 is not called invalid.
    bad = rows[1]
    assert bad["source_label"].endswith(("09:34:00+08:00", "01:34:00+00:00"))
    assert bad["quote_status"] == "missing" and bad["missing_reason"] == "confirmed_invalid_price"
    assert all(bad[f] is None for f in ("open", "high", "low", "close", "volume", "amount"))
    assert bad["diagnostics"] == ["ohlc_envelope_error"]
    assert bad["use_decision"]["execution"] == bad["use_decision"]["fresh_valuation"] == "deny"
    if not retained_invalid:
        assert bad["missing_evidence"][0]["source_row"] == 7
    assert rows[0]["close"] == rows[2]["close"] == 10
    assert rows[-1]["high"] == 2326.53 and rows[-1]["price_status"] == "conflict"
    assert rows[-1].get("quote_status") != "missing"
    assert result["report"]["unavailable_rows"] == 1 and result["report"]["repaired_rows"] == 0


@pytest.mark.parametrize("overrides", [{"series": 4}, {"matching_hash": False}])
def test_excluded_source_cannot_override_selected_day_or_other_hash(tmp_path, overrides):
    config = released_fixture(tmp_path, **overrides)
    result = read(config)
    assert len(result["rows"]) == 3
    assert result["report"]["unavailable_rows"] == 0
    # The winner may have different labels outside this one-minute request.
    point = minute_prices(
        config, "000001.XSHE", "2014-12-22T09:34:00+08:00", "2014-12-22T09:34:00+08:00"
    )
    assert point["rows"] == []


@pytest.mark.parametrize(
    "field,diagnostic,value",
    [
        ("volume", "negative_volume", -1),
        ("amount", "nonfinite_amount", float("inf")),
    ],
)
def test_confirmed_activity_defects_hide_only_affected_fields(field, diagnostic, value):
    raw = dict(
        open=10,
        high=11,
        low=9,
        close=10,
        volume=100,
        amount=1000,
        price_status="unverified",
        diagnostics=[diagnostic],
    )
    raw[field] = value
    row = standard_quote(raw)
    assert row[field] is None and raw[field] == value
    assert row["missing_fields"] == [field] and row["close"] == 10
    assert row["quote_status"] == "partial_missing"
    assert row["use_decision"]["execution"] == "deny"
    assert row["use_decision"]["fresh_valuation"] == "not_granted"
