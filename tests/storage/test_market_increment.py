"""Increment normalization and publication boundaries, not live network tests."""

import json
import sqlite3
from decimal import Decimal

import pytest

from ashare_data.core.model import DataError
from ashare_data.market.increment import exact_number, minute_rows
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, canonical, connection
from ashare_data.product.market_release import minute_prices, released_minute_sources
from test_market_release import metadata


def native(day="2026-10-20 09:31:00", **changes):
    return dict(
        day=day,
        open="10.000000000001",
        high="11",
        low="9",
        close="10.1",
        volume="123",
        amount="1230.0001",
        **changes,
    )


def normalize(rows):
    return minute_rows(json.dumps(rows).encode(), after_date="2026-10-16", end_date="2026-10-20")


def test_exact_precision_nulls_and_exclusive_destinations():
    zero = native()
    zero["volume"] = "0"
    zero["amount"] = None
    invalid = native("2026-10-20 09:32:00")
    invalid["low"] = "12"
    fraction = native("2026-10-20 09:33:00")
    fraction["volume"] = "1.1"
    rows = normalize(
        [
            native("2026-10-16 15:00:00"),
            zero,
            invalid,
            fraction,
            native("2026-10-21 09:31:00"),
            native("broken"),
        ]
    )
    assert [r["disposition"] for r in rows] == [
        "resolved_unused",
        "adopted",
        "quarantined",
        "quarantined",
        "quarantined",
        "format_rejected",
    ]
    assert rows[1]["open_price"] == Decimal("10.000000000001")
    assert rows[1]["volume_shares"] == 0 and rows[1]["turnover_cny"] is None
    assert rows[1]["label_at"].isoformat() == "2026-10-20T09:31:00+08:00"
    with pytest.raises(ValueError):
        exact_number("0.0000000000001")
    with pytest.raises(ValueError):
        exact_number(1.25)
    assert exact_number("0.01000000000000") == Decimal("0.01")
    assert exact_number("99999999999999999999999999.999999999999") == Decimal(
        "99999999999999999999999999.999999999999"
    )


def test_conflicting_labels_never_last_write_win():
    one = native()
    two = {**one, "close": "10.2"}
    assert [r["disposition"] for r in normalize([one, two])] == ["quarantined", "quarantined"]
    assert [r["disposition"] for r in normalize([one, one])] == ["adopted", "equal_duplicate"]
    invalid = {**one, "low": "0"}
    assert all(r["disposition"] == "quarantined" for r in normalize([one, invalid]))
    other_invalid = {**one, "volume": "1.5"}
    assert all(r["disposition"] == "quarantined" for r in normalize([invalid, other_invalid]))
    assert all(r["disposition"] == "quarantined" for r in normalize([invalid, invalid]))


def make_minute(writer, c, series, day):
    result = writer.parquet(
        c,
        f"""select {series}::USMALLINT series_id,1::UINTEGER instrument_id,date '{day}' trade_date,
      timestamptz '{day} 01:31:00+00' label_at,10.000000000001::DECIMAL(38,12) open_price,
      11::DECIMAL(38,12) high_price,9::DECIMAL(38,12) low_price,10.1::DECIMAL(38,12) close_price,
      123::BIGINT volume_shares,NULL::DECIMAL(38,12) turnover_cny,1::UINTEGER batch_id""",
        [],
        f"minute-{series}-{day}",
    )
    result.update(
        table="bars_1m",
        series_id=series,
        instrument_id=1,
        rows=1,
        first_date=day,
        last_date=day,
        duplicate_keys=0,
    )
    return result


def test_minute_selection_is_frozen_and_keeps_exact_decimal(tmp_path):
    c = connection(memory="128MB", threads=1)
    with ReleaseWriter(tmp_path, free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        old = make_minute(writer, c, 4, "2026-10-16")
        writer.publish(
            dict(
                metadata=meta,
                files=[old],
                input_dispositions={"test": {"input_rows": 1, "adopted": 1}},
            ),
            expected_parent=None,
        )
        r1 = MarketRelease(tmp_path)
        path = writer.root / "work/new-meta.sqlite3"
        import shutil

        shutil.copyfile(writer.root / meta["path"], path)
        with sqlite3.connect(path) as db:
            db.execute("create table release_contracts(name text primary key,body text)")
            db.execute(
                "insert into release_contracts values(?,?)",
                (
                    "minute_selection",
                    canonical(
                        {
                            "sources": {"neigezhu": 2, "free_stockdb": 4, "tdx": 3, "sina": 6},
                            "canonical_series": [2, 4, 6],
                        }
                    ).decode(),
                ),
            )
        fresh = make_minute(writer, c, 6, "2026-10-20")
        writer.publish(
            dict(
                metadata=writer.object(path, "sqlite3"),
                files=[old, fresh],
                input_dispositions={"test": {"input_rows": 2, "adopted": 2}},
            ),
            expected_parent=1,
            verify_unchanged=False,
        )
    r2 = MarketRelease(tmp_path)
    assert released_minute_sources(r1)[1] == [2, 4]

    def config(r):
        return {
            "market_release": {
                "root": str(tmp_path),
                "release_id": r.release_id,
                "manifest_sha256": r.sha256,
            }
        }

    args = ("000001.XSHE", "2026-10-16T09:31:00+08:00", "2026-10-20T09:31:00+08:00")
    before = minute_prices(config(r1), *args)
    after = minute_prices(config(r2), *args)
    assert len(before["rows"]) == 1 and [r["source"] for r in after["rows"]] == [
        "free_stockdb",
        "sina",
    ]
    assert after["rows"][1]["open"] == "10.000000000001"
    assert after["rows"][1]["amount"] is None
    assert after["rows"][1]["use_decision"]["execution"] == "deny"
    with pytest.raises(DataError):
        minute_prices(config(r1), *args, source="sina")


@pytest.mark.parametrize("batch_response", [False, True])
def test_real_pipeline_overlap_preserves_unchanged_minute_and_promotes_daily_priority(
    tmp_path, batch_response
):
    """Use provider-shaped raw bodies through the actual bounded build script."""
    import gzip
    import hashlib
    import importlib.util
    from argparse import Namespace
    from pathlib import Path
    from urllib.parse import urlencode
    from ashare_data.storage.market_release import sha_file

    spec = importlib.util.spec_from_file_location(
        "increment_builder", Path(__file__).parents[2] / "scripts/build_market_increment.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    market = tmp_path / "market"
    root = tmp_path / "incoming-run"
    (root / "incoming/raw").mkdir(parents=True)
    (root / "jobs").mkdir()
    c = connection(memory="128MB", threads=1)
    with ReleaseWriter(market, free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        import shutil

        full_metadata = writer.root / "work/full-reference.sqlite3"
        shutil.copyfile(writer.root / meta["path"], full_metadata)
        with sqlite3.connect(full_metadata) as db:
            from ashare_data.storage.local_daily import SCHEMA

            db.executescript(SCHEMA)
            db.executescript("""
                DROP TABLE data_batches;
                CREATE TABLE data_batches(batch_id INTEGER PRIMARY KEY,batch_key TEXT NOT NULL,release_id INTEGER NOT NULL,
                    parser_sha256 TEXT NOT NULL,mapping_sha256 TEXT NOT NULL,body TEXT NOT NULL);
                INSERT INTO data_batches VALUES(1,'old',1,'parser','mapping','{}');
                CREATE TABLE release_batches(batch_key TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE batch_inputs(batch_id INTEGER REFERENCES data_batches(batch_id),asset_id TEXT REFERENCES source_assets(asset_id),PRIMARY KEY(batch_id,asset_id));
                CREATE TABLE source_aliases(source_version TEXT,source_identifier TEXT,instrument_id INTEGER,interpretation TEXT,PRIMARY KEY(source_version,source_identifier));
                CREATE TABLE series(series_id INTEGER PRIMARY KEY,series_code TEXT UNIQUE,frequency_code TEXT);
                CREATE TABLE series_contracts(series_id INTEGER PRIMARY KEY,body TEXT);
                CREATE TABLE release_contracts(name TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE release_daily_summary(instrument_id INTEGER,source TEXT,rows INTEGER,first TEXT,last TEXT,PRIMARY KEY(instrument_id,source));
            """)
        meta = writer.object(full_metadata, "sqlite3")
        daily = writer.parquet(
            c,
            """select 1::USMALLINT series_id,1::UINTEGER instrument_id,date '2026-10-20' trade_date,
          10::DECIMAL(38,12) open_price,11::DECIMAL(38,12) high_price,9::DECIMAL(38,12) low_price,10::DECIMAL(38,12) close_price,
          100::BIGINT volume_shares,NULL::DECIMAL(38,12) turnover_cny,1::UINTEGER batch_id,
          'sz000001'::VARCHAR source_symbol,'tencent'::VARCHAR as source,NULL::VARCHAR row_hash,NULL::VARCHAR response_id,
          NULL::VARCHAR observed_at,NULL::VARCHAR preclose,NULL::VARCHAR tradestatus,NULL::VARCHAR is_st""",
            [],
            "daily",
        )
        daily.update(
            table="bars_1d",
            series_id=1,
            instrument_id=1,
            rows=1,
            first_date="2026-10-20",
            last_date="2026-10-20",
            duplicate_keys=0,
        )
        minute = make_minute(writer, c, 6, "2026-10-20")
        writer.publish(
            dict(
                metadata=meta,
                files=[daily, minute],
                input_dispositions={"test": {"input_rows": 2, "adopted": 2}},
            ),
            expected_parent=None,
        )
    r = MarketRelease(market)
    before = sha_file(r.root / daily["path"])
    scope = dict(
        parent=r.receipt(),
        instruments=[{"instrument_id": 1, "security": "000001.XSHE"}],
        start="2026-10-20",
        end="2026-10-20",
        source_priority={"sohu": 3, "tencent": 4},
    )
    (root / "scope.json").write_bytes(canonical(scope))
    (root / "collection-report.json").write_text("{}")
    minute_data = [{**native(), "amount": None}, native("2026-10-20 09:32:00")]
    bodies = {
        "sohu": (
            "historySearchHandler("
            + json.dumps(
                [
                    {
                        "code": "cn_000001",
                        "status": 0,
                        "hq": [["2026-10-20", "10", "10", "0", "0%", "9", "11", "1", "0.1", "0"]],
                    }
                ]
            )
            + ")"
        ).encode(),
        "tencent": json.dumps({"code": 0, "data": {"sz000001": {"day": []}}}).encode(),
        "sina_minute": json.dumps(minute_data).encode(),
    }
    urls = {
        "sohu": "https://q.stock.sohu.com/hisHq?"
        + urlencode(
            dict(
                code="cn_000001",
                start="20261020",
                end="20261020",
                stat="1",
                order="D",
                period="d",
                callback="historySearchHandler",
                rt="jsonp",
            )
        ),
        "tencent": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        + urlencode({"param": "sz000001,day,2026-10-20,2026-10-20,640,"}),
        "sina_minute": "test-minute",
    }
    if batch_response:
        payload = json.loads(
            bodies["sohu"].decode().removeprefix("historySearchHandler(").removesuffix(")")
        )[0]
        bodies["sohu"] = (
            "historySearchHandler("
            + json.dumps([{"code": "cn_000002", "status": 0, "hq": []}, payload])
            + ")"
        ).encode()
        urls["sohu"] = urls["sohu"].replace("code=cn_000001", "code=cn_000002%2Ccn_000001")
    for source, body in bodies.items():
        sha = hashlib.sha256(body).hexdigest()
        (root / "incoming/raw" / (sha + ".gz")).write_bytes(gzip.compress(body))
        receipt = dict(
            url=urls[source],
            sha256=sha,
            received_at="2026-10-20T10:00:00+00:00",
            status=200,
            bytes=len(body),
        )
        rows = []
        if source == "sohu":
            rows = [
                dict(
                    date="2026-10-20",
                    open="10",
                    close="10",
                    low="9",
                    high="11",
                    volume="100",
                    amount="1000.0",
                    preclose=None,
                    tradestatus=None,
                    is_st=None,
                )
            ]
        value = dict(
            job_key=source + ":000001.XSHE",
            source=source,
            security="000001.XSHE",
            instrument_id=1,
            status="ok",
            completed_at="2026-10-20T10:00:00+00:00",
            receipts=[receipt],
            normalized_rows=rows,
        )
        if source == "sohu" and batch_response:
            value["response_block"] = 1
        (root / "jobs" / (source + ".json")).write_bytes(canonical(value))
    args = Namespace(root=root, preview=False, limit=None, build_name=None)
    builder.build(args)
    report = json.loads((root / "build/build-report.json").read_bytes())
    assert report["new_rows"] == {"bars_1d": 0, "bars_1m": 1}
    assert report["input_dispositions"]["adopted"] == 2
    assert report["input_dispositions"]["equal_duplicate"] == 1
    with sqlite3.connect(root / "build/checkpoint.sqlite3") as db:
        v = json.loads(db.execute("select body from batches").fetchone()[0])
    assert next(x for x in v["chunks"] if x["table"] == "bars_1d")["replaced_rows"] == 1
    candidate = next(x for x in v["chunks"] if x["table"] == "bars_1m")
    actual = c.execute(
        "select batch_id from read_parquet(?) order by label_at", [candidate["parquet"]]
    ).fetchall()
    assert actual[0] == (1,) and actual[1][0] != 1
    hashes = {x["parquet"]: sha_file(x["parquet"]) for x in v["chunks"]}
    report_hash = sha_file(root / "build/build-report.json")
    builder.build(args)
    assert sha_file(root / "build/build-report.json") == report_hash
    assert all(sha_file(path) == digest for path, digest in hashes.items())
    assert MarketRelease(market).sha256 == r.sha256 and sha_file(r.root / daily["path"]) == before
    spec = importlib.util.spec_from_file_location(
        "increment_prepare", Path(__file__).parents[2] / "scripts/prepare_market_increment.py"
    )
    preparation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparation)
    plan = preparation.prepare(root)
    assert preparation.prepare(root) == plan
    from ashare_data.product.release_cli import apply
    from ashare_data.storage.source_observations import incremental_daily

    published = apply(r, plan)
    fresh = MarketRelease(market)
    assert published["release_id"] == 2
    from ashare_data.product.market_release import ReleasedDailyProduct

    product = ReleasedDailyProduct({"market_release": fresh.receipt()})
    answer = product.prices(["000001.XSHE"], "2026-10-20", "2026-10-20")
    assert len(answer["rows"]) == 1
    assert answer["receipt"]["missing_response_ids"] == []
    response = answer["receipt"]["responses"][0]
    assert response["id"] == answer["rows"][0]["response_id"]
    assert response["sha256"] == hashlib.sha256(bodies["sohu"]).hexdigest()
    assert response["frozen_source_assets"][0]["record_scope"] == {
        "native_block_index": 1 if batch_response else None,
        "requested_security": "000001.XSHE",
    }
    with product._read() as store:
        unknown = product._receipt(store, "prices", {"securities": ["000001.XSHE"]},
                                   [{"response_id": "unknown"}], [], [])
    assert unknown["missing_response_ids"] == ["unknown"]
    assert apply(r, plan)["already_applied"] is True
    assert apply(fresh, plan)["already_applied"] is True
    observations, refs = incremental_daily(fresh, "000001.XSHE", "2026-10-20", "2026-10-20", "sohu")
    assert len(observations) == 1 and observations.iloc[0]["amount"] == "1000.0"
    assert refs and incremental_daily(r, "000001.XSHE", "2026-10-20", "2026-10-20", "sohu")[0].empty
    actual = fresh.read(
        "bars_1m",
        "000001.XSHE",
        "2026-10-20T09:30:00+08:00",
        "2026-10-20T15:00:00+08:00",
        series_id=6,
    )
    assert len(actual) == 2 and actual[0]["batch_id"] == 1
    assert sha_file(r.root / daily["path"]) == before
    c.close()


def test_collector_resumes_successes_and_does_not_retry_access_denials():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "increment_collector", Path(__file__).parents[2] / "scripts/collect_market_increment.py"
    )
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    assert collector.needs_request(None, False)
    assert not collector.needs_request({"status": "ok"}, True)
    assert not collector.needs_request({"status": "error"}, False)
    assert collector.needs_request({"status": "error"}, True)
    for status in (401, 403, 429):
        assert not collector.needs_request(
            {"status": "deferred", "pause_evidence": {"receipts": [{"status": status}]}}, True
        )
    assert collector.needs_request(
        {"status": "deferred", "pause_evidence": {"receipts": [{"status": 501}]}}, True
    )


def test_sohu_batch_maps_native_blocks_and_preserves_missing_scope_evidence():
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "batch_collector", Path(__file__).parents[2] / "scripts/collect_market_increment.py"
    )
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    native_row = ["2026-10-20", "10", "10", "0", "0%", "9", "11", "1", "0.1", "0"]
    body = (
        "historySearchHandler("
        + json.dumps(
            [
                {"code": "cn_000002", "status": 0, "hq": [native_row]},
                {"code": "cn_000001", "status": 0, "hq": [native_row]},
            ]
        )
        + ")"
    ).encode()

    class HTTP:
        history = SimpleNamespace()

        def get(self, url, referer=None):
            return body, [{"url": url, "sha256": "raw-response-hash"}]

    jobs = [
        ("sohu:" + code + ".XSHE", "sohu", {"security": code + ".XSHE", "instrument_id": n})
        for n, code in enumerate(("000001", "000002", "000003"), 1)
    ]
    values = collector.batch_sohu(HTTP(), jobs, "2026-10-20", "2026-10-20", "producer")
    assert [v["status"] for v in values] == ["ok", "ok", "error"]
    assert [v["response_block"] for v in values[:2]] == [1, 0]
    assert values[0]["normalized_rows"][0]["volume"] == "100"
    assert (
        values[2]["receipts"] == []
        and values[2]["batch_response_evidence"][0]["sha256"] == "raw-response-hash"
    )


def test_sohu_batches_isolate_unresolved_codes_and_preserve_all_jobs():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "collector_grouping", Path(__file__).parents[2] / "scripts/collect_market_increment.py"
    )
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    jobs = [(str(n), "sohu", {"security": str(n)}) for n in range(9)]
    successful = {str(n) for n in (0, 1, 3, 4, 5, 7, 8)}
    groups = list(collector.sohu_groups(jobs, successful, 3))
    assert [job for group in groups for job in group] == jobs
    assert [len(group) for group in groups] == [2, 1, 3, 1, 2]
    assert all(len(group) == 1 for group in groups if group[0][0] not in successful)
