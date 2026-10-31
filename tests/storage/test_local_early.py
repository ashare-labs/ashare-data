"""Controls for the early-history append boundary and diagnostic separation."""

import gzip
import hashlib
import json

import pytest

from ashare_data.storage.local_daily import LocalDailyStore
from ashare_data.market.early_history import (
    SCHEMA,
    ArchiveHTTP,
    add_job,
    ingest_http,
    observations,
    pending,
    plan_tencent,
)


@pytest.fixture
def store(tmp_path):
    s = LocalDailyStore(tmp_path)
    s.db.executescript(SCHEMA)
    yield s
    s.close()


def bar(day="1991-06-08", close="7.5"):
    return dict(
        date=day,
        open="7.95",
        high="7.95",
        low="7.4",
        close=close,
        volume="200",
        amount=None,
        preclose=None,
        tradestatus=None,
        is_st=None,
    )


def job(store):
    jid = add_job(store, "1990-1991", "tencent", "sz000002", "1991-01-01", "1991-12-31")
    return dict(store.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def result(rows):
    return dict(rows=rows, source="tencent", receipts=[], replayed=True)


def test_weekends_and_listing_uncertainty_are_preserved_without_certification(store):
    ingest_http(store, job(store), result([bar()]))
    row = observations(store, "000002.XSHE", "1991-06-08", "1991-06-08")["rows"][0]
    assert row["date"] == "1991-06-08"
    assert row["weekend_observation"]
    assert row["listing_phase"] == "outside_candidate_episode_unknown"
    assert row["strict_usable"] is False
    assert store.db.execute("SELECT count(*) FROM calendar").fetchone()[0] == 0


@pytest.mark.parametrize("day", ["2010-01-01", "2026-01-01", "1990-12-01"])
def test_ingest_rejects_rows_outside_job_window_without_writing_any(store, day):
    with pytest.raises(ValueError, match="refusing early import"):
        ingest_http(store, job(store), result([bar(), bar(day)]))
    assert store.db.execute("SELECT count(*) FROM bars").fetchone()[0] == 0


def test_repeat_is_idempotent_and_changed_source_row_cannot_overwrite(store):
    j = job(store)
    ingest_http(store, j, result([bar()]))
    before = tuple(store.db.execute("SELECT * FROM bars").fetchone())
    ingest_http(store, j, result([bar()]))
    with pytest.raises(ValueError, match="append-only conflict"):
        ingest_http(store, j, result([bar(close="7.6")]))
    assert tuple(store.db.execute("SELECT * FROM bars").fetchone()) == before
    assert store.db.execute("SELECT count(*) FROM bar_revisions").fetchone()[0] == 0


def test_unknown_baostock_is_visible_only_when_diagnostics_requested(store):
    store.db.execute(
        "INSERT INTO early_diagnostics VALUES(?,?,?,?,?,?)",
        (
            "sz000002",
            "1991-06-08",
            "baostock",
            json.dumps(bar()),
            "receipt",
            "validation_unknown:compressed_outer_integrity_unverified",
        ),
    )
    assert observations(store, "000002.XSHE", "1991-01-01", "1991-12-31")["rows"] == []
    rows = observations(store, "000002.XSHE", "1991-01-01", "1991-12-31", diagnostics=True)["rows"]
    assert rows[0]["quality"].startswith("validation_unknown")
    assert rows[0]["strict_usable"] is False
    assert store.db.execute("SELECT count(*) FROM bars").fetchone()[0] == 0


def test_resume_changes_only_interrupted_jobs_in_selected_stage(store):
    a = job(store)
    b = add_job(store, "2000-2009", "sina", "sz000002", "2000-01-01", "2009-12-31")
    store.db.execute("UPDATE jobs SET state='running'")
    rows = pending(store, "1990-1991", ["tencent"])
    assert [r["id"] for r in rows] == [a["id"]]
    assert store.db.execute("SELECT state FROM jobs WHERE id=?", (b,)).fetchone()[0] == "running"


def test_empty_and_failed_responses_are_not_coverage(store):
    j = job(store)
    ingest_http(store, j, result([]))
    assert store.db.execute("SELECT state FROM jobs").fetchone()[0] == "empty_unknown"
    assert pending(store, "1990-1991", ["tencent"]) == []
    assert store.db.execute("SELECT count(*) FROM bars").fetchone()[0] == 0


def test_invalid_sohu_row_remains_diagnostic_with_original_negative_amount(store):
    from ashare_data.providers.daily_sources import sohu_daily

    class HTTP:
        def get(self, *args):
            return (
                b'historySearchHandler([{"code":"cn_000002","status":0,"hq":[["1991-06-10","7","7.5","0","0","6","8","10","-214.75","0"],["1991-06-11","7","7.5","0","0","6","8","10","1.2","0"]]}]);',
                [],
            )

    with pytest.raises(ValueError, match="negative amount"):
        sohu_daily(HTTP(), "sz000002", "1991-01-01", "1991-12-31")
    invalid = []
    rows, receipts = sohu_daily(
        HTTP(), "sz000002", "1991-01-01", "1991-12-31", invalid_rows=invalid
    )
    assert len(rows) == len(invalid) == 1
    assert invalid[0]["row"]["amount"] == "-2147500.00"
    j = job(store)
    outcome = dict(rows=rows, source="sohu", receipts=receipts, quarantined=invalid)
    ingest_http(store, j, outcome)
    assert len(observations(store, "000002.XSHE", "1991-01-01", "1991-12-31")["rows"]) == 1
    diagnostic = observations(store, "000002.XSHE", "1991-01-01", "1991-12-31", diagnostics=True)[
        "rows"
    ][0]
    assert diagnostic["amount"] == "-2147500.00"
    assert diagnostic["quality"] == "invalid_source_row:negative amount"
    assert not diagnostic["strict_usable"]
    ingest_http(store, j, outcome)
    assert store.db.execute("SELECT count(*) FROM early_diagnostics").fetchone()[0] == 1


def test_failed_ingest_rolls_back_diagnostic_and_quality_with_the_checkpoint(store, monkeypatch):
    j = job(store)
    store.db.commit()
    outcome = dict(result([bar()]), quarantined=[dict(row=bar("1991-06-09"), error="test")])
    original = store.receipts
    calls = 0

    def fail_second(receipts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("simulated ingestion failure")
        return original(receipts)

    monkeypatch.setattr(store, "receipts", fail_second)
    with pytest.raises(ValueError, match="simulated ingestion failure"):
        ingest_http(store, j, outcome)
    assert store.db.execute("SELECT count(*) FROM early_diagnostics").fetchone()[0] == 0
    assert store.db.execute("SELECT quality FROM early_jobs").fetchone()[0] is None


def test_late_diagnostic_boundary_error_cannot_leave_partial_transaction(store):
    j = job(store)
    store.db.commit()
    invalid = [dict(row=bar(), error="retained"), dict(row=bar("2010-01-01"), error="bad date")]
    with pytest.raises(ValueError, match="refusing diagnostic"):
        ingest_http(store, j, dict(result([]), quarantined=invalid))
    assert store.db.execute("SELECT count(*) FROM early_diagnostics").fetchone()[0] == 0


def test_absent_completed_bao_capture_is_failure_not_a_future_http_job(store):
    import runpy
    from pathlib import Path

    module = runpy.run_path(str(Path(__file__).parents[2] / "scripts/collect_early_baostock.py"))
    add_job(store, "1990-1991", "baostock", "sz000002", "1991-01-01", "1991-12-31")
    outcome = module["import_diagnostics"](store, "1990-1991")
    assert outcome["absent"] == 1
    assert store.db.execute("SELECT state FROM jobs").fetchone()[0] == "failed"
    assert store.db.execute("SELECT quality FROM early_jobs").fetchone()[0] == "capture_absent"
    assert store.db.execute("SELECT count(*) FROM early_diagnostics").fetchone()[0] == 0


def test_diagnostic_status_does_not_suppress_tencent_gap_request(store):
    store.db.execute(
        "INSERT INTO early_episodes VALUES(?,?,?,?,?,?)",
        ("episode", "sz000002", "1991-01-29", None, "{}", "receipt"),
    )
    store.db.execute(
        "INSERT INTO early_calendar_observations VALUES(?,?,?,?,?,?)",
        ("SH", "2000-01-04", "sina_trading_dates_sh", "1", "receipt", "proxy"),
    )
    row = dict(bar("2000-01-04"), tradestatus="0")
    store.db.execute(
        "INSERT INTO early_diagnostics VALUES(?,?,?,?,?,?)",
        ("sz000002", row["date"], "baostock", json.dumps(row), "receipt", "validation_unknown"),
    )
    assert plan_tencent(store, "2000-2009")["added"] == 1
    assert plan_tencent(store, "2000-2009")["added"] == 0


def test_persistent_service_errors_pause_only_remaining_jobs_without_fake_empty_data(
    store, monkeypatch
):
    from ashare_data.market import early_history as local_early

    for number in range(6):
        add_job(store, "2000-2009", "tencent", f"sz00000{number}", "2000-01-01", "2000-12-31")

    def unavailable(http, job):
        return dict(
            error="HTTP 501 after finite retries", receipts=[dict(url=job["symbol"], status=501)]
        )

    monkeypatch.setattr(local_early, "fetch", unavailable)
    outcome = local_early.collect_http(store, "2000-2009", ["tencent"], workers=1)
    assert outcome["finished"] == 3
    assert "tencent" in outcome["paused_sources"]
    assert dict(store.db.execute("SELECT state,count(*) FROM jobs GROUP BY state")) == {
        "failed": 3,
        "deferred_source": 3,
    }
    assert local_early.collect_http(store, "2000-2009", ["tencent"], workers=1)["finished"] == 0


def test_replay_verifies_body_hash_and_uses_no_network(store, monkeypatch):
    url = "https://finance.sina.com.cn/realstock/company/sz000002/hisdata/klc_kl.js"
    body = b"synthetic raw bytes"
    sha = hashlib.sha256(body).hexdigest()
    (store.root / "raw").mkdir()
    raw = store.root / "raw" / (sha + ".gz")
    raw.write_bytes(gzip.compress(body))
    store.receipts([dict(url=url, sha256=sha, received_at="2026-10-14T00:00:00+00:00", status=200)])
    client = ArchiveHTTP(store, network=False)
    client.history.receipts = []
    assert client.get(url)[0] == body
    raw.write_bytes(gzip.compress(b"tampered"))
    with pytest.raises(ValueError, match="hash mismatch"):
        client.get(url)


def test_persisted_source_block_prevents_network_across_stages(store, monkeypatch):
    from ashare_data.providers.daily_sources import SourceBlocked

    directory = store.root / "raw" / "early-transport"
    directory.mkdir(parents=True)
    (directory / "source-blocks.json").write_text(
        json.dumps({"web.ifzq.gtimg.cn": {"reason": "confirmed WAF"}})
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: pytest.fail("blocked host requested")
    )
    for _ in range(2):
        client = ArchiveHTTP(store)
        client.history.receipts = []
        with pytest.raises(SourceBlocked, match="confirmed WAF"):
            client.get("https://web.ifzq.gtimg.cn/explicit-test-path")


@pytest.mark.parametrize("start,end", [("1989-01-01", "1990-12-31"), ("2000-01-01", "2010-01-01")])
def test_early_job_scope_cannot_cross_boundary(store, start, end):
    with pytest.raises(ValueError, match="outside authorized"):
        add_job(store, "test", "sina", "sz000001", start, end)


def test_archive_only_collector_never_opens_or_publishes_a_library(tmp_path, monkeypatch):
    from ashare_data.providers import baostock_collection as local_baostock

    (tmp_path / "raw").mkdir()
    monkeypatch.setattr(
        local_baostock, "publish_available", lambda root: pytest.fail("unexpected publication")
    )
    result = local_baostock.collect(tmp_path, [], archive_only=True)
    assert result["completed"] == 0
    assert not (tmp_path / "daily.sqlite3").exists()


def test_installed_cli_route_is_readonly_and_includes_uncertainty(store, monkeypatch):
    import argparse
    from ashare_data.storage.local_daily import add_parser, command

    ingest_http(store, job(store), result([bar()]))
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "local-daily",
            "query-early",
            "--root",
            str(store.root),
            "--symbol",
            "000002.XSHE",
            "--start",
            "1991-06-08",
            "--end",
            "1991-06-08",
        ]
    )
    monkeypatch.setattr(LocalDailyStore, "writer", lambda self: pytest.fail("query tried to write"))
    output = command(args)
    assert output["rows"][0]["strict_usable"] is False
    assert "PIT" in output["limitations"]["scope"]


def cli_args(store, action, *options):
    import argparse
    from ashare_data.storage.local_daily import add_parser

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(
        ["local-daily", action, "--root", str(store.root), "--symbol", "000002.XSHE", *options]
    )


def test_early_cli_omitted_dates_include_both_authorized_boundaries(store, monkeypatch):
    from ashare_data.storage.local_daily import command

    for stage, day in (("1990-1991", "1990-12-01"), ("2000-2009", "2009-12-31")):
        jid = add_job(store, stage, "tencent", "sz000002", day, day)
        j = dict(store.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        ingest_http(store, j, result([bar(day)]))
    store.set_meta("end", "2026-10-14")
    store.db.commit()
    monkeypatch.setattr(LocalDailyStore, "writer", lambda self: pytest.fail("query tried to write"))

    output = command(cli_args(store, "query-early"))

    assert (output["start"], output["end"]) == ("1990-12-01", "2009-12-31")
    assert [r["date"] for r in output["rows"]] == ["1990-12-01", "2009-12-31"]
    assert all(r["strict_usable"] is False for r in output["rows"])


@pytest.mark.parametrize(
    "options,start,end",
    [
        (["--end", "1991-06-08"], "1990-12-01", "1991-06-08"),
        (["--start", "1991-06-08"], "1991-06-08", "2009-12-31"),
        (
            ["--start", "1991-06-08", "--end", "1991-06-08"],
            "1991-06-08",
            "1991-06-08",
        ),
    ],
)
def test_early_cli_preserves_each_explicit_date(store, options, start, end):
    from ashare_data.storage.local_daily import command

    ingest_http(store, job(store), result([bar()]))
    output = command(cli_args(store, "query-early", *options))
    assert (output["start"], output["end"]) == (start, end)
    assert [r["date"] for r in output["rows"]] == ["1991-06-08"]


def test_early_cli_does_not_replace_explicit_modern_start_with_early_default(store):
    from ashare_data.storage.local_daily import command

    with pytest.raises(ValueError, match="pre-2010 interval"):
        command(cli_args(store, "query-early", "--start", "2010-01-01"))


@pytest.mark.parametrize("mode", ["single", "compatible"])
def test_modern_cli_omitted_start_keeps_2010_boundary(store, monkeypatch, mode):
    import pandas as pd
    from ashare_data.storage.local_daily import command

    received = []

    def get_price(self, security, start, end, source, **options):
        received.append((security, start, end, source, options["mode"]))
        return pd.DataFrame()

    monkeypatch.setattr(LocalDailyStore, "get_price", get_price)
    command(cli_args(store, "query", "--mode", mode, "--end", "2010-01-08"))
    assert received == [("000002.XSHE", "2010-01-01", "2010-01-08", "auto", mode)]
