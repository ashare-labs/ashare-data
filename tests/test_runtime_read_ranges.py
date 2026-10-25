"""Published reads return requested data, not test-sized truncations."""
from datetime import datetime, timedelta, timezone
import json
import hashlib

from test_market_release import metadata, manifest
from test_local_minute_product import deployment as deployment
from ashare_data.product.minute import LocalMinuteProduct
from ashare_data.product.released_references import ReleasedReferences
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection


def test_published_minutes_beyond_one_hundred_thousand_are_complete(tmp_path):
    count = 100_003
    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        c = connection()
        try:
            part = writer.parquet(c, """select 2::INTEGER series_id,1::INTEGER instrument_id,
              (timestamp '2024-01-01 09:30:00' + i*interval '1 minute')::date trade_date,
              timestamp '2024-01-01 09:30:00' + i*interval '1 minute' label_at,
              10::DECIMAL(18,6) close_price,1::INTEGER batch_id from range(?) t(i)""", [count], "long")
            part.update(table="bars_1m",series_id=2,instrument_id=1,rows=count,
                        first_date="2024-01-01",last_date="2024-03-10",duplicate_keys=0)
            writer.publish(manifest(meta,[part]), expected_parent=None)
        finally:
            c.close()
    release = MarketRelease(tmp_path)
    rows = release.read("bars_1m", "000001.XSHE", "2024-01-01", "2024-04-01", series_id=2)
    assert len(rows) == count
    assert rows[-1]["label_at"] - rows[0]["label_at"] == timedelta(minutes=count-1)
    assert release.read("bars_1m", "000001.XSHE", "2025-01-01", "2025-04-01") == []


def test_reference_query_returns_all_rows_and_keeps_selection():
    reader = ReleasedReferences.__new__(ReleasedReferences)
    reader.connection = connection()
    reader.view = lambda table: table
    try:
        reader.connection.execute("CREATE TABLE facts AS SELECT i, i % 2 AS parity FROM range(100003) t(i)")
        rows = reader.query("facts", "true", [])
        assert len(rows) == 100003 and {r['i'] for r in rows} == set(range(100003))
        assert len(reader.query("facts", "parity=?", [1])) == 50001
    finally:
        reader.close()


def test_local_minute_entry_does_not_restore_sixty_day_or_row_gate(deployment):
    source = deployment.parent / "assets/tdx.json"
    begin = datetime(2024,1,1,9,31,tzinfo=timezone(timedelta(hours=8)))
    rows = [{"security":"600000.XSHG", "source_label":(begin+timedelta(minutes=i*6)).isoformat(),
             "open":"12.34"} for i in range(16001)]
    source.write_text(json.dumps({"schema":"ad-tdx-minute-session.v1", "minute":rows}))
    config=json.loads(deployment.read_text())
    config['profiles']['product']['minute_sources']['tdx']['content']['sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
    deployment.write_text(json.dumps(config))
    owner=LocalMinuteProduct(deployment)
    result=owner.prices('600000.XSHG',rows[0]['source_label'],rows[-1]['source_label'])
    assert result['rows']==rows
    assert 'query_bounds' not in owner.sources()
