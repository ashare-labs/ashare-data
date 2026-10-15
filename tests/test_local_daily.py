"""Offline maintenance contracts; no real market data is distributed."""

import json

import pytest

from ashare_data.local_daily import LocalDailyStore
from ashare_data.local_sources import validate_bars, eastmoney


def setup_store(tmp_path):
    store = LocalDailyStore(tmp_path)
    records = [
        dict(
            symbol="sh600001",
            name="synthetic",
            ipo="2010-01-04",
            out="2010-01-08",
            board="main",
            source="fixture",
            receipt="x",
        ),
        dict(
            symbol="sz002001",
            name="synthetic late IPO",
            ipo="2010-01-06",
            out=None,
            board="sme_until_2021-04-06",
            source="fixture",
            receipt="y",
        ),
    ]
    store.save_universe(records, [], "2010-01-01", "2010-01-08")
    store.save_calendar(
        ["2010-01-04", "2010-01-05", "2010-01-06", "2010-01-07", "2010-01-08"],
        [dict(url="fixture", sha256="f")],
    )
    store.plan("2010-01-01", "2010-01-08")
    return store


def bar(day, close="10"):
    return dict(
        date=day,
        open="10",
        high="11",
        low="9",
        close=close,
        volume="100",
        amount="1000",
        tradestatus=None,
        is_st=None,
        preclose=None,
    )


def test_listing_calendar_and_missing_not_suspension(tmp_path):
    s = setup_store(tmp_path)
    jobs = [dict(r) for r in s.db.execute("SELECT * FROM jobs WHERE kind='bars' ORDER BY symbol")]
    assert (jobs[0]["start"], jobs[0]["end"]) == ("2010-01-04", "2010-01-07")
    assert jobs[1]["start"] == "2010-01-06"
    s.ingest(
        jobs[0], dict(rows=[bar("2010-01-04"), bar("2010-01-06")], source="eastmoney", receipts=[])
    )
    report = s.report()
    assert report["coverage"]["missing_security_days"] == 5
    assert report["main_board_at_date"]["expected_security_days"] == 4
    assert report["main_board_at_date"]["missing_security_days"] == 2
    assert report["main_board_at_date"]["securities"] == 1
    assert {r[0] for r in s.db.execute("SELECT kind FROM gaps")} == {
        "awaiting_collection",
        "missing_unknown_not_proven_suspended",
    }
    s.close()


def test_idempotence_revision_and_factor_version(tmp_path):
    s = setup_store(tmp_path)
    jobs = {r["kind"]: dict(r) for r in s.db.execute("SELECT * FROM jobs WHERE symbol='sh600001'")}
    data = dict(rows=[bar("2010-01-04")], source="eastmoney", receipts=[])
    s.ingest(jobs["bars"], data)
    s.ingest(jobs["bars"], data)
    assert s.summary()["bar_rows"] == 1
    assert s.summary()["bar_revisions"] == 0
    data["rows"][0]["close"] = "10.5"
    s.ingest(jobs["bars"], data)
    assert s.summary()["bar_rows"] == 1 and s.summary()["bar_revisions"] == 1
    factors = dict(rows=[dict(date="2009-01-01", factor="2")], source="sina_hfq", receipts=[])
    s.ingest(jobs["factor"], factors)
    s.ingest(jobs["factor"], factors)
    assert s.summary()["factor_rows"] == 1
    factors["rows"][0]["factor"] = "3"
    s.ingest(jobs["factor"], factors)
    assert s.summary()["factor_rows"] == 2
    s.close()


def test_issuer_exit_correction_survives_newer_list_refresh_and_keeps_relisting(tmp_path):
    s = setup_store(tmp_path)
    base = dict(
        symbol="sh600001",
        name="synthetic",
        ipo="2010-01-04",
        out="2010-01-08",
        board="main",
        source="sse",
        receipt="list",
    )
    s.save_universe(
        [dict(base, observed_at="2026-10-01T00:00:00+00:00")], [], "2010-01-01", "2026-10-14"
    )
    correction = dict(
        base,
        out="2010-01-07",
        source="issuer_exit_notice",
        receipt="notice",
        observed_at="2026-10-02T00:00:00+00:00",
    )
    s.save_universe([correction], [], "2010-01-01", "2026-10-14")
    s.save_universe(
        [dict(base, observed_at="2026-10-15T00:00:00+00:00")], [], "2010-01-01", "2026-10-15"
    )
    dates = ["2010-01-06", "2010-01-07", "2010-01-08", "2020-01-06"]
    assert s.expected("sh600001", dates[0], dates[-1], dates) == ["2010-01-06"]
    assert (
        s.db.execute("SELECT out FROM instruments WHERE symbol='sh600001'").fetchone()[0]
        == "2010-01-07"
    )
    assert (
        s.db.execute("SELECT count(*) FROM listing_facts WHERE receipt='list'").fetchone()[0] == 2
    )
    s.save_universe(
        [dict(base, ipo="2020-01-06", out=None, observed_at="2026-10-16T00:00:00+00:00")],
        [],
        "2010-01-01",
        "2026-10-16",
    )
    assert s.expected("sh600001", dates[0], dates[-1], dates) == ["2010-01-06", "2020-01-06"]
    assert (
        s.db.execute("SELECT ipo FROM instruments WHERE symbol='sh600001'").fetchone()[0]
        == "2020-01-06"
    )
    s.close()


def test_exit_notice_requires_reviewed_digest_and_is_imported_once(tmp_path, monkeypatch):
    import hashlib
    from ashare_data import local_code_history as module
    from ashare_data.local_sources import SourceUnavailable

    s = setup_store(tmp_path)
    payload = b"%PDF-synthetic issuer document"
    sha = hashlib.sha256(payload).hexdigest()
    record = dict(
        symbol="sh600001",
        name="synthetic",
        ipo="2010-01-04",
        out="2010-01-07",
        board="main",
        source="issuer_exit_notice",
        receipt=sha,
        url="https://issuer.test/exit.pdf",
    )
    monkeypatch.setattr(module, "CODE_CHANGES", [])
    monkeypatch.setattr(module, "LISTING_CORRECTIONS", [record])

    class HTTP:
        calls = 0
        valid = False

        def get(self, url):
            self.calls += 1
            return payload, [
                dict(
                    url=url,
                    sha256=sha if self.valid else "unexpected",
                    received_at="2026-10-15T00:00:00+00:00",
                )
            ]

    http = HTTP()
    with pytest.raises(SourceUnavailable, match="notice changed"):
        module.refresh_code_history(s, http)
    http.valid = True
    assert module.refresh_code_history(s, http) == 1
    assert module.refresh_code_history(s, http) == 0
    assert http.calls == 2
    assert (
        s.db.execute("SELECT count(*) FROM listing_facts WHERE receipt=?", (sha,)).fetchone()[0]
        == 1
    )
    s.close()


def test_later_verified_code_change_corrects_a_historical_only_universe(tmp_path, monkeypatch):
    import hashlib
    from ashare_data import local_code_history as module

    s = LocalDailyStore(tmp_path)
    s.set_meta("start", "2012-01-16")
    s.set_meta("end", "2017-12-29")
    s.save_universe(
        [
            dict(
                symbol="sh601360",
                name="current",
                ipo="2012-01-16",
                out=None,
                board="main",
                source="sse",
                receipt="list",
            )
        ],
        [],
        "2012-01-16",
        "2017-12-29",
    )
    payload = b"%PDF-synthetic code change"
    sha = hashlib.sha256(payload).hexdigest()
    event = dict(module.CODE_CHANGES[1], receipt=sha, url="https://issuer.test/code.pdf")
    monkeypatch.setattr(module, "CODE_CHANGES", [event])
    monkeypatch.setattr(module, "LISTING_CORRECTIONS", [])

    class HTTP:
        def get(self, url):
            return payload, [dict(url=url, sha256=sha, received_at="2026-10-14T00:00:00+00:00")]

    assert module.refresh_code_history(s, HTTP()) == 1
    assert module.refresh_code_history(s, HTTP()) == 0
    assert s.code_bounds("sh601360") == ("2018-02-28", None)
    assert s.code_bounds("sh601313") == (None, "2018-02-28")
    assert [r["symbol"] for r in s.universe("2017-12-29")] == ["sh601313"]
    assert s.meta("end") == "2017-12-29"
    s.close()


def test_historical_command_resolves_identifiers_before_planning(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from ashare_data import local_daily as module, local_code_history

    payload = b"%PDF-synthetic code change"
    sha = hashlib.sha256(payload).hexdigest()
    event = dict(
        local_code_history.CODE_CHANGES[1], receipt=sha, url="https://issuer.test/code.pdf"
    )
    monkeypatch.setattr(local_code_history, "CODE_CHANGES", [event])
    monkeypatch.setattr(local_code_history, "LISTING_CORRECTIONS", [])

    class HTTP:
        history = SimpleNamespace()

        def get(self, url):
            return payload, [dict(url=url, sha256=sha, received_at="2026-10-14T00:00:00+00:00")]

    monkeypatch.setattr(module, "PublicHTTP", lambda *a, **k: HTTP())
    monkeypatch.setattr(
        module,
        "exchange_universe",
        lambda *a: (
            [
                dict(
                    symbol="sh601360",
                    name="current",
                    ipo="2012-01-16",
                    out=None,
                    board="main",
                    source="sse",
                    receipt="list",
                )
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        module, "calendar", lambda *a: (["2017-12-29"], [dict(url="calendar", sha256="c")])
    )
    monkeypatch.setattr(module.LocalDailyStore, "run", lambda *a, **k: None)
    module.command(
        SimpleNamespace(
            action="update",
            root=tmp_path,
            start="2017-12-29",
            end="2017-12-29",
            workers=1,
            interval=0,
            timeout=1,
            refresh_metadata=False,
            repair_gaps=False,
            retry_failed=False,
            symbol=None,
            limit_jobs=None,
        )
    )
    s = LocalDailyStore(tmp_path, readonly=True)
    assert {r[0] for r in s.db.execute("SELECT symbol FROM jobs")} == {"sh601313"}
    assert [r["symbol"] for r in s.universe("2017-12-29")] == ["sh601313"]
    s.close()


def test_known_future_code_event_recovers_old_instrument_when_window_crosses_ipo(
    tmp_path, monkeypatch
):
    import hashlib
    from ashare_data import local_code_history as module

    s = LocalDailyStore(tmp_path)
    s.set_meta("start", "2010-01-01")
    s.set_meta("end", "2011-12-30")
    payload = b"%PDF-synthetic future code change"
    sha = hashlib.sha256(payload).hexdigest()
    event = dict(module.CODE_CHANGES[1], receipt=sha, url="https://issuer.test/code.pdf")
    monkeypatch.setattr(module, "CODE_CHANGES", [event])
    monkeypatch.setattr(module, "LISTING_CORRECTIONS", [])

    class HTTP:
        calls = 0

        def get(self, url):
            self.calls += 1
            return payload, [dict(url=url, sha256=sha, received_at="2026-10-14T00:00:00+00:00")]

    http = HTTP()
    assert module.refresh_code_history(s, http) == 1
    assert s.db.execute("SELECT count(*) FROM instruments").fetchone()[0] == 0
    s.set_meta("end", "2017-12-29")
    s.save_universe(
        [
            dict(
                symbol="sh601360",
                name="current",
                ipo="2012-01-16",
                out=None,
                board="main",
                source="sse",
                receipt="list",
            )
        ],
        [],
        "2010-01-01",
        "2017-12-29",
    )
    assert module.refresh_code_history(s, http) == 0
    assert [r["symbol"] for r in s.universe("2017-12-29")] == ["sh601313"]
    assert http.calls == 1
    assert s.db.execute("SELECT count(*) FROM code_events").fetchone()[0] == 1
    s.close()


@pytest.mark.parametrize("state", ["empty_unknown", "failed", "blocked", "running", "pending"])
def test_http_maintenance_does_not_reset_or_consume_baostock_jobs(tmp_path, monkeypatch, state):
    from ashare_data import local_daily

    s = setup_store(tmp_path)
    s.db.execute("UPDATE jobs SET state='done'")
    s.db.execute(
        "INSERT INTO jobs(id,kind,symbol,start,end,source,state,nrows,response_ids) "
        "VALUES ('bao-request','bars','sh600001','2010-01-04','2010-01-07','baostock',?,0,'[\"bao-receipt\"]')",
        (state,),
    )
    s.db.commit()
    before = dict(s.db.execute("SELECT * FROM jobs WHERE id='bao-request'").fetchone())
    s.plan("2010-01-01", "2010-01-08", retry_failed=True, symbols=["sh600001"])
    monkeypatch.setattr(local_daily, "fetch_job", lambda *a: pytest.fail("HTTP dispatched Bao job"))
    s.run(None, workers=1, symbols=["sh600001"])
    assert dict(s.db.execute("SELECT * FROM jobs WHERE id='bao-request'").fetchone()) == before
    s.close()


def test_http_dispatch_rejects_a_dedicated_collector_source_without_a_request():
    from types import SimpleNamespace
    from ashare_data.local_sources import fetch_job

    class HTTP:
        history = SimpleNamespace()

        def get(self, *a):
            pytest.fail("Unsupported collector fell through to an HTTP source")

    result = fetch_job(
        HTTP(),
        dict(
            kind="bars", symbol="sh600001", source="baostock", start="2010-01-04", end="2010-01-07"
        ),
    )
    assert "dedicated collector: baostock" in result["error"]
    assert not result["receipts"]
    assert "source" not in result


def test_targeted_horizon_update_retries_only_selected_http_jobs(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ashare_data import local_daily as module, local_code_history

    s = setup_store(tmp_path)
    s.db.execute("UPDATE jobs SET state='failed'")
    s.db.execute(
        "INSERT INTO jobs(id,kind,symbol,start,end,source,state,nrows,response_ids) "
        "VALUES ('bao-empty','bars','sh600001','2010-01-04','2010-01-07','baostock','empty_unknown',0,'[\"bao-receipt\"]')"
    )
    s.db.commit()
    retained = [
        dict(r)
        for r in s.db.execute(
            "SELECT * FROM jobs WHERE symbol='sz002001' OR source='baostock' ORDER BY id"
        )
    ]
    s.close()
    monkeypatch.setattr(
        module, "PublicHTTP", lambda *a, **k: SimpleNamespace(history=SimpleNamespace())
    )
    monkeypatch.setattr(module, "exchange_universe", lambda *a: ([], []))
    monkeypatch.setattr(
        module, "calendar", lambda *a: (["2010-01-11"], [dict(url="calendar", sha256="c")])
    )
    monkeypatch.setattr(local_code_history, "refresh_code_history", lambda *a: 0)
    monkeypatch.setattr(module.LocalDailyStore, "run", lambda *a, **k: None)
    module.command(
        SimpleNamespace(
            action="update",
            root=tmp_path,
            start="2010-01-01",
            end="2010-01-11",
            workers=1,
            interval=0,
            timeout=1,
            refresh_metadata=False,
            repair_gaps=False,
            retry_failed=False,
            symbol=["600001.XSHG"],
            limit_jobs=None,
        )
    )
    s = LocalDailyStore(tmp_path, readonly=True)
    assert [
        dict(r)
        for r in s.db.execute(
            "SELECT * FROM jobs WHERE symbol='sz002001' OR source='baostock' ORDER BY id"
        )
    ] == retained
    assert {
        r[0]
        for r in s.db.execute("SELECT state FROM jobs WHERE symbol='sh600001' AND source IS NULL")
    } == {"pending"}
    s.close()


def test_resume_incremental_and_failed_retry(tmp_path):
    s = setup_store(tmp_path)
    n = s.db.execute("SELECT count(*) FROM jobs").fetchone()[0]
    s.db.execute("UPDATE jobs SET state='running' WHERE kind='bars'")
    s.db.commit()
    s.plan("2010-01-01", "2010-01-08")
    assert s.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == n
    assert not s.db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone()
    j = dict(s.db.execute("SELECT * FROM jobs WHERE kind='bars' LIMIT 1").fetchone())
    s.ingest(j, dict(error="temporary network error", receipts=[]))
    s.plan("2010-01-01", "2010-01-08")
    assert s.db.execute("SELECT state FROM jobs WHERE id=?", (j["id"],)).fetchone()[0] == "failed"
    s.plan("2010-01-01", "2010-01-08", retry_failed=True)
    assert s.db.execute("SELECT state FROM jobs WHERE id=?", (j["id"],)).fetchone()[0] == "pending"
    s.save_calendar(["2010-01-11"], [dict(url="fixture2", sha256="a")])
    s.plan("2010-01-01", "2010-01-11")
    assert s.db.execute(
        "SELECT start,end FROM jobs WHERE kind='bars' AND end='2010-01-11'"
    ).fetchall()[0][:] == ("2010-01-11", "2010-01-11")
    s.close()


def test_targeted_retry_and_run_leave_other_symbols_pending_or_failed(tmp_path, monkeypatch):
    from ashare_data import local_daily

    s = setup_store(tmp_path)
    s.db.execute("UPDATE jobs SET state='failed'")
    s.db.commit()
    s.plan("2010-01-01", "2010-01-08", retry_failed=True, symbols=["sh600001"])
    assert {r[0] for r in s.db.execute("SELECT state FROM jobs WHERE symbol='sz002001'")} == {
        "failed"
    }
    s.db.execute("UPDATE jobs SET state='pending' WHERE symbol='sz002001'")
    s.db.commit()
    fetched = []

    def fetch(http, job):
        fetched.append(job["symbol"])
        return dict(
            source="sina",
            receipts=[],
            rows=[dict(date="2010-01-04", factor="1")]
            if job["kind"] == "factor"
            else [bar("2010-01-04")],
        )

    monkeypatch.setattr(local_daily, "fetch_job", fetch)
    s.run(None, workers=1, symbols=["sh600001"])
    assert fetched == ["sh600001", "sh600001"]
    assert {r[0] for r in s.db.execute("SELECT state FROM jobs WHERE symbol='sz002001'")} == {
        "pending"
    }
    assert {r[0] for r in s.db.execute("SELECT state FROM jobs WHERE symbol='sh600001'")} == {
        "done"
    }
    s.close()


def test_transaction_rolls_back_partial_import(tmp_path):
    s = setup_store(tmp_path)
    job = dict(s.db.execute("SELECT * FROM jobs WHERE kind='bars' LIMIT 1").fetchone())
    with pytest.raises(KeyError):
        s.ingest(job, dict(rows=[bar("2010-01-04"), {}], source="eastmoney", receipts=[]))
    assert s.summary()["bar_rows"] == 0
    assert (
        s.db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == "pending"
    )
    s.close()


def test_source_contract_units_and_validation():
    class HTTP:
        def get(self, *args):
            return json.dumps(
                dict(
                    rc=0,
                    data=dict(code="600001", klines=["2010-01-04,10,10.5,11,9,123,123000,0,0,0,0"]),
                )
            ).encode(), []

    rows, _ = eastmoney(HTTP(), "sh600001", "2010-01-04", "2010-01-04")
    assert rows[0]["volume"] == "12300" and rows[0]["amount"] == "123000"
    with pytest.raises(ValueError):
        validate_bars([rows[0], rows[0]], "2010-01-04", "2010-01-04")
    assert rows[0]["tradestatus"] is None


def test_gap_slices_reuse_exact_snapshot_without_sharing_mutable_rows(monkeypatch):
    from ashare_data import local_sources as module

    module._sina_history.cache_clear()
    calls = []

    def decode(body, variable):
        calls.append((body, variable))
        return [bar("2010-01-04"), bar("2010-01-05"), bar("2010-01-06")]

    class HTTP:
        def get(self, *a):
            return b"synthetic source bytes", []

    monkeypatch.setattr(module, "decode_sina", decode)
    first, _ = module.sina_daily(HTTP(), "sh600001", "2010-01-04", "2010-01-04")
    first[0]["close"] = "999"
    second, _ = module.sina_daily(HTTP(), "sh600001", "2010-01-04", "2010-01-05")
    assert len(calls) == 1 and [r["date"] for r in second] == ["2010-01-04", "2010-01-05"]
    assert second[0]["close"] == "10"
    module._sina_history.cache_clear()


def test_concurrent_gap_requests_fetch_one_snapshot_per_horizon(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from ashare_data import local_sources

    calls = []
    barrier = threading.Barrier(4)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            time.sleep(0.03)
            return b"one source history"

    def urlopen(*args, **kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr(local_sources.urllib.request, "urlopen", urlopen)
    http = local_sources.PublicHTTP(tmp_path, interval=0)

    def gap():
        http.history.cache_scope = "2026-10-14"
        barrier.wait()
        return http.get("https://example.test/full-history")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: gap(), range(4)))
    assert len(calls) == 1
    assert all(r == results[0] for r in results)
    http.history.cache_scope = "2026-10-15"
    http.get("https://example.test/full-history")
    assert len(calls) == 2


def test_sohu_raw_units_identity_and_no_remote_code_execution():
    from ashare_data.local_sources import sohu_daily, SourceUnavailable

    class HTTP:
        body = b'historySearchHandler([{"status":0,"code":"cn_600001","hq":[["2010-01-04","10","10.5","0.5","5%","9","11","123","45.67","1%"]]}]);'

        def get(self, *args):
            return self.body, []

    http = HTTP()
    rows, _ = sohu_daily(http, "sh600001", "2010-01-01", "2010-01-05")
    assert rows[0]["volume"] == "12300" and rows[0]["amount"] == "456700.00"
    assert rows[0]["tradestatus"] is None and rows[0]["preclose"] is None
    with pytest.raises(ValueError, match="security mismatch"):
        sohu_daily(http, "sh600002", "2010-01-01", "2010-01-05")
    http.body += b"alert(1)"
    with pytest.raises((SourceUnavailable, ValueError)):
        sohu_daily(http, "sh600001", "2010-01-01", "2010-01-05")


def test_tencent_raw_identity_units_and_year_bound():
    from ashare_data.local_sources import tencent_daily, SourceUnavailable

    class HTTP:
        data = {
            "code": 0,
            "data": {"sh600001": {"day": [["2010-01-04", "10", "10.5", "11", "9", "123.000"]]}},
        }

        def get(self, url, referer):
            assert "640%2C" in url
            return json.dumps(self.data).encode(), []

    http = HTTP()
    rows, _ = tencent_daily(http, "sh600001", "2010-01-01", "2010-12-31")
    assert rows[0]["volume"] == "12300.000"
    assert rows[0]["amount"] is None and rows[0]["tradestatus"] is None
    http.data["data"]["sh600001"]["day"][0].append(
        {"cqr": "2010-01-04", "FHcontent": "synthetic event"}
    )
    annotated, _ = tencent_daily(http, "sh600001", "2010-01-01", "2010-12-31")
    assert annotated == rows
    with pytest.raises(SourceUnavailable, match="security payload absent"):
        tencent_daily(http, "sh600002", "2010-01-01", "2010-12-31")
    with pytest.raises(ValueError, match="calendar year"):
        tencent_daily(http, "sh600001", "2010-01-01", "2011-01-01")
    http.data["data"]["sh600001"]["qfqday"] = http.data["data"]["sh600001"].pop("day")
    with pytest.raises(SourceUnavailable, match="raw daily"):
        tencent_daily(http, "sh600001", "2010-01-01", "2010-12-31")


def test_tencent_source_jobs_split_years_and_keep_receipts_separate(tmp_path):
    s = setup_store(tmp_path)
    s.save_calendar(["2010-12-31", "2011-01-04"], [dict(url="fixture", sha256="x")])
    s.plan_source("tencent", "2010-01-01", "2011-01-04", symbols=["sz002001"])
    jobs = [
        dict(r) for r in s.db.execute("SELECT * FROM jobs WHERE source='tencent' ORDER BY start")
    ]
    assert [(r["start"], r["end"]) for r in jobs] == [
        ("2010-01-06", "2010-12-31"),
        ("2011-01-04", "2011-01-04"),
    ]
    assert {r["symbol"] for r in jobs} == {"sz002001"}
    s.plan_source("tencent", "2010-01-01", "2011-01-04", symbols=["sz002001"])
    assert s.db.execute("SELECT count(*) FROM jobs WHERE source='tencent'").fetchone()[0] == 2
    s.close()


def test_targeted_supplement_does_not_enable_market_wide_future_source(tmp_path, monkeypatch):
    import argparse
    from types import SimpleNamespace
    from ashare_data import local_daily, local_code_history

    setup_store(tmp_path).close()
    monkeypatch.setattr(
        local_daily, "PublicHTTP", lambda *a, **k: SimpleNamespace(history=SimpleNamespace())
    )
    monkeypatch.setattr(local_code_history, "refresh_code_history", lambda *a: 0)
    monkeypatch.setattr(
        local_daily,
        "fetch_job",
        lambda http, job: dict(source="tencent", rows=[bar(job["start"])], receipts=[]),
    )
    parser = argparse.ArgumentParser()
    local_daily.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "local-daily",
            "supplement",
            "--root",
            str(tmp_path),
            "--source",
            "tencent",
            "--symbol",
            "600001.XSHG",
            "--end",
            "2010-01-08",
        ]
    )
    local_daily.command(args)
    s = LocalDailyStore(tmp_path, readonly=True)
    assert "tencent" not in s.meta("supplement_sources", [])
    assert [r[0] for r in s.db.execute("SELECT symbol FROM jobs WHERE source='tencent'")] == [
        "sh600001"
    ]
    s.close()


def test_supplement_is_independent_idempotent_and_incremental(tmp_path):
    s = setup_store(tmp_path)
    s.set_meta("supplement_sources", ["sohu"])
    s.plan("2010-01-01", "2010-01-08")
    jobs = [dict(r) for r in s.db.execute("SELECT * FROM jobs WHERE source='sohu'")]
    assert len(jobs) == 2
    assert s.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 6
    s.ingest(jobs[0], dict(rows=[bar(jobs[0]["start"])], source="sohu", receipts=[]))
    s.plan("2010-01-01", "2010-01-08")
    assert s.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 6
    s.save_calendar(["2010-01-11"], [dict(url="fixture2", sha256="a")])
    s.plan("2010-01-01", "2010-01-11")
    assert [
        tuple(r)
        for r in s.db.execute("SELECT start,end FROM jobs WHERE source='sohu' AND end='2010-01-11'")
    ] == [("2010-01-11", "2010-01-11")]
    s.close()


def test_code_change_filters_backfilled_symbols_without_rewriting_raw(tmp_path):
    s = setup_store(tmp_path)
    event = dict(
        old_symbol="sh600009",
        new_symbol="sh600001",
        old_name="old code",
        issuer_ipo="2010-01-04",
        effective_date="2010-01-06",
        receipt="notice",
        halt_start="2010-01-05",
        halt_end="2010-01-05",
    )
    receipt = dict(
        url="https://issuer.test/notice.pdf",
        sha256="notice",
        received_at="2026-10-14T00:00:00+00:00",
    )
    s.save_code_events([event], [receipt])
    assert s.expected("sh600001", "2010-01-01", "2010-01-08", ["2010-01-04", "2010-01-06"]) == [
        "2010-01-06"
    ]
    assert s.expected("sh600009", "2010-01-01", "2010-01-08", ["2010-01-04", "2010-01-06"]) == [
        "2010-01-04"
    ]
    s.db.execute(
        "CREATE TABLE universe_membership (date TEXT,symbol TEXT,name TEXT,trade_status TEXT,capture_id TEXT)"
    )
    s.db.execute(
        "INSERT INTO universe_membership VALUES ('2010-01-04','sh600001','backfilled','1','capture')"
    )
    s.db.commit()
    pool = s.universe("2010-01-04")
    assert len(pool) == 1 and pool[0]["symbol"] == "sh600009"
    assert pool[0]["source_symbol"] == "sh600001" and pool[0]["source_trade_status"] is None
    job = dict(
        s.db.execute("SELECT * FROM jobs WHERE symbol='sh600001' AND kind='bars'").fetchone()
    )
    s.ingest(job, dict(rows=[bar("2010-01-04"), bar("2010-01-06")], source="sina", receipts=[]))
    assert len(s.get_price("600001.XSHG")) == 1
    assert s.db.execute("SELECT count(*) FROM bars").fetchone()[0] == 2
    s.report()
    assert (
        s.db.execute(
            "SELECT kind FROM gaps WHERE symbol='sh600009' AND start='2010-01-05'"
        ).fetchone()[0]
        == "announced_suspension_without_bar"
    )
    s.close()


def test_auto_source_chooses_whole_series_without_splicing(tmp_path):
    s = setup_store(tmp_path)
    job = dict(
        s.db.execute("SELECT * FROM jobs WHERE kind='bars' AND symbol='sh600001'").fetchone()
    )
    s.ingest(job, dict(rows=[bar("2010-01-04"), bar("2010-01-06")], source="sina", receipts=[]))
    s.ingest(job, dict(rows=[bar("2010-01-05", "10.2")], source="baostock", receipts=[]))
    series = s.get_price("600001.XSHG")
    assert series.attrs["source"] == "sina"
    assert series.date.tolist() == ["2010-01-04", "2010-01-06"]
    assert set(series.source) == {"sina"}
    fixed = s.get_price("600001.XSHG", source="baostock")
    assert fixed.date.tolist() == ["2010-01-05"]
    s.close()


def test_bao_supplement_only_plans_missing_source_segments(tmp_path):
    from ashare_data.local_baostock import daily_requests

    s = setup_store(tmp_path)
    job = dict(
        s.db.execute("SELECT * FROM jobs WHERE kind='bars' AND symbol='sh600001'").fetchone()
    )
    s.ingest(job, dict(rows=[bar("2010-01-04"), bar("2010-01-06")], source="baostock", receipts=[]))
    requests = daily_requests(tmp_path)
    segments = [
        (r["params"]["start_date"], r["params"]["end_date"])
        for r in requests
        if r["params"]["code"] == "sh.600001"
    ]
    assert segments == [("2010-01-05", "2010-01-05"), ("2010-01-07", "2010-01-07")]
    s.close()


def test_delisting_boundary_placeholders_are_preserved_but_not_returned_as_prices(tmp_path):
    s = setup_store(tmp_path)
    job = dict(
        s.db.execute("SELECT * FROM jobs WHERE kind='bars' AND symbol='sh600001'").fetchone()
    )
    s.ingest(job, dict(rows=[bar("2010-01-07"), bar("2010-01-08")], source="baostock", receipts=[]))
    series = s.get_price("600001.XSHG")
    assert series.date.tolist() == ["2010-01-07"]
    assert series.attrs["excluded_source_rows_outside_listing"] == 1
    assert s.summary()["bar_rows"] == 2
    report = s.report()
    assert report["outside_listing_source_rows"] == 1
    assert (
        s.db.execute("SELECT last_bar FROM coverage WHERE symbol='sh600001'").fetchone()[0]
        == "2010-01-07"
    )
    s.close()


def test_non_session_source_observations_are_retained_but_excluded_from_queries(tmp_path):
    s = setup_store(tmp_path)
    s.save_calendar(["2010-01-11"], [dict(url="calendar-next", sha256="next")])
    s.plan("2010-01-01", "2010-01-11")
    job = dict(
        s.db.execute(
            "SELECT * FROM jobs WHERE symbol='sz002001' AND kind='bars' LIMIT 1"
        ).fetchone()
    )
    s.ingest(job, dict(source="sohu", rows=[bar("2010-01-06"), bar("2010-01-10")], receipts=[]))
    for source in ("auto", "sohu"):
        frame = s.get_price("002001.XSHE", "2010-01-01", "2010-01-11", source=source)
        assert frame.date.tolist() == ["2010-01-06"]
        assert frame.attrs["excluded_source_rows_outside_calendar"] == 1
        assert frame.attrs["excluded_source_rows_outside_listing"] == 0
    empty = s.get_price("002001.XSHE", "2010-01-10", "2010-01-10")
    assert empty.empty and empty.attrs["excluded_source_rows_outside_calendar"] == 1
    report = s.report()
    assert report["outside_calendar_source_rows"] == 1
    assert report["outside_listing_source_rows"] == 0
    assert s.summary()["bar_rows"] == 2
    s.close()


def test_dated_membership_cannot_reintroduce_a_formally_delisted_stock(tmp_path):
    s = setup_store(tmp_path)
    s.db.execute("CREATE TABLE universe_membership(date,symbol,name,trade_status,capture_id)")
    s.db.executemany(
        "INSERT INTO universe_membership VALUES (?,?,?,?,?)",
        [
            ("2010-01-08", "sh600001", "backfilled name", "0", "capture"),
            ("2010-01-08", "sz002001", "source display name", "1", "capture"),
        ],
    )
    s.db.commit()
    pool = s.universe("2010-01-08", main_board_only=False)
    assert [r["symbol"] for r in pool] == ["sz002001"]
    assert pool[0]["name_is_historical"] is False
    assert s.report()["outside_listing_membership_rows"] == 1
    assert s.db.execute("SELECT count(*) FROM universe_membership").fetchone()[0] == 2
    s.close()


def test_listing_episodes_preserved(tmp_path):
    s = setup_store(tmp_path)
    s.save_universe(
        [
            dict(
                symbol="sh600001",
                name="relisted",
                ipo="2010-01-11",
                out=None,
                board="main",
                source="fixture",
                receipt="z",
            )
        ],
        [],
        "2010-01-01",
        "2010-01-11",
    )
    assert (
        len(
            s.db.execute(
                "SELECT DISTINCT ipo,out FROM listing_facts WHERE symbol='sh600001'"
            ).fetchall()
        )
        == 2
    )
    assert s.expected(
        "sh600001", "2010-01-01", "2010-01-11", ["2010-01-07", "2010-01-08", "2010-01-11"]
    ) == ["2010-01-07", "2010-01-11"]
    s.close()


def test_receipt_clock_and_older_observation_cannot_revert_latest(tmp_path):
    s = setup_store(tmp_path)
    job = dict(s.db.execute("SELECT * FROM jobs WHERE kind='bars' LIMIT 1").fetchone())
    new = dict(
        rows=[bar("2010-01-04", "10.5")],
        source="eastmoney",
        receipts=[dict(url="fixture", sha256="new", received_at="2026-10-14T11:00:00+00:00")],
    )
    old = dict(
        rows=[bar("2010-01-04", "10")],
        source="eastmoney",
        receipts=[dict(url="fixture", sha256="old", received_at="2026-10-13T11:00:00+00:00")],
    )
    s.ingest(job, new)
    s.ingest(job, old)
    row = s.db.execute("SELECT close,observed_at FROM bars").fetchone()
    assert tuple(row) == ("10.5", "2026-10-14T11:00:00+00:00")
    assert s.summary()["bar_revisions"] == 1
    s.close()


def test_legacy_code_includes_source_outdate_and_sme_classification(tmp_path):
    s = setup_store(tmp_path)
    s.save_universe(
        [
            dict(
                symbol="sh600099",
                name="old code",
                ipo="2000-01-01",
                out="2010-01-06",
                out_inclusive=True,
                board="main",
                source="baostock",
                receipt="legacy",
            )
        ],
        [],
        "2010-01-01",
        "2010-01-08",
    )
    assert s.expected(
        "sh600099", "2010-01-01", "2010-01-08", ["2010-01-05", "2010-01-06", "2010-01-07"]
    ) == ["2010-01-05", "2010-01-06"]
    assert "sz002001" in {r["symbol"] for r in s.universe("2010-01-06", main_board_only=False)}
    assert "sz002001" not in {r["symbol"] for r in s.universe("2010-01-06", main_board_only=True)}
    s.close()


def test_primary_failure_falls_back_but_denial_does_not(monkeypatch):
    import threading
    from ashare_data import local_sources as sources

    class HTTP:
        history = threading.local()

    http = HTTP()
    job = dict(symbol="sh600001", kind="bars", start="2010-01-04", end="2010-01-04")
    calls = []

    def fail(*args):
        error = sources.SourceUnavailable("connection closed")
        error.receipts = [dict(url="primary", error="closed")]
        raise error

    def backup(*args):
        calls.append("backup")
        return [bar("2010-01-04")], [dict(url="backup", sha256="z")]

    monkeypatch.setattr(sources, "eastmoney", fail)
    monkeypatch.setattr(sources, "sina_daily", backup)
    result = sources.fetch_job(http, job)
    assert result["source"] == "sina" and len(result["receipts"]) == 2

    def denied(*args):
        raise sources.SourceBlocked("HTTP 429")

    monkeypatch.setattr(sources, "eastmoney", denied)
    result = sources.fetch_job(http, job)
    assert result["blocked"] and calls == ["backup"]


def test_http_cached_by_request_epoch_and_rate_denial_stops_host(tmp_path, monkeypatch):
    import urllib.error
    from ashare_data.local_sources import PublicHTTP, SourceBlocked
    import ashare_data.local_sources as sources

    calls = []

    class Response:
        status = 200

        def read(self, n):
            return b"public fixture"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def opened(*args, **kwargs):
        calls.append("network")
        return Response()

    monkeypatch.setattr(sources.urllib.request, "urlopen", opened)
    http = PublicHTTP(tmp_path, interval=0)
    http.history.cache_scope = "2026-10-13"
    http.get("https://example.test/factor")
    http.get("https://example.test/factor")
    assert len(calls) == 1
    http.history.cache_scope = "2026-10-14"
    http.get("https://example.test/factor")
    assert len(calls) == 2

    def denied(*args, **kwargs):
        calls.append("denied")
        raise urllib.error.HTTPError("https://example.test", 429, "rate limited", {}, None)

    monkeypatch.setattr(sources.urllib.request, "urlopen", denied)
    with pytest.raises(SourceBlocked):
        http.get("https://example.test/other")
    with pytest.raises(SourceBlocked):
        http.get("https://example.test/another")
    assert calls.count("denied") == 1


def test_new_termination_supersedes_open_ended_fact_without_losing_history(tmp_path):
    s = setup_store(tmp_path)
    original = dict(
        symbol="sh600099",
        name="synthetic",
        ipo="2010-01-04",
        out=None,
        board="main",
        source="sse",
        receipt="first",
        observed_at="2020-01-01T00:00:00+00:00",
    )
    s.save_universe([original], [], "2010-01-01", "2010-01-08")
    terminated = dict(
        original, out="2010-01-07", receipt="second", observed_at="2020-02-01T00:00:00+00:00"
    )
    s.save_universe([terminated], [], "2010-01-01", "2010-01-08")
    assert s.expected("sh600099", "2010-01-01", "2010-01-08", ["2010-01-06", "2010-01-07"]) == [
        "2010-01-06"
    ]
    assert (
        s.db.execute("SELECT count(*) FROM listing_facts WHERE symbol='sh600099'").fetchone()[0]
        == 2
    )
    # Rebuilding from archives in filename order cannot revert a later observation.
    s.save_universe([original], [], "2010-01-01", "2010-01-08")
    assert (
        s.db.execute("SELECT out FROM instruments WHERE symbol='sh600099'").fetchone()[0]
        == "2010-01-07"
    )
    # Source can retract a previously stated termination; preserve both observations.
    s.save_universe(
        [dict(original, receipt="third", observed_at="2020-03-01T00:00:00+00:00")],
        [],
        "2010-01-01",
        "2010-01-08",
    )
    assert s.expected("sh600099", "2010-01-01", "2010-01-08", ["2010-01-06", "2010-01-07"]) == [
        "2010-01-06",
        "2010-01-07",
    ]
    s.close()


def test_same_day_horizon_advance_discovers_new_ipo(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ashare_data import local_daily as module
    from ashare_data import local_code_history

    monkeypatch.setattr(local_code_history, "refresh_code_history", lambda *a: 0)

    calls = []
    records = [
        dict(
            symbol="sh600001",
            name="old",
            ipo="2010-01-04",
            out=None,
            board="main",
            source="sse",
            receipt="a",
        ),
        dict(
            symbol="sh600002",
            name="new",
            ipo="2010-01-05",
            out=None,
            board="main",
            source="sse",
            receipt="b",
        ),
    ]

    def universe(http):
        calls.append(True)
        return records, []

    monkeypatch.setattr(
        module, "PublicHTTP", lambda *a, **k: SimpleNamespace(history=SimpleNamespace())
    )
    monkeypatch.setattr(module, "exchange_universe", universe)
    monkeypatch.setattr(
        module,
        "calendar",
        lambda http, end: (
            [d for d in ["2010-01-04", "2010-01-05"] if d <= end],
            [dict(url="fixture", sha256="x")],
        ),
    )
    monkeypatch.setattr(module.LocalDailyStore, "run", lambda *a, **k: None)
    args = SimpleNamespace(
        action="update",
        root=tmp_path,
        start="2010-01-01",
        end="2010-01-04",
        workers=1,
        interval=0,
        timeout=1,
        refresh_metadata=False,
        repair_gaps=False,
        retry_failed=False,
        symbol=None,
        limit_jobs=None,
    )
    module.command(args)
    s = LocalDailyStore(tmp_path)
    s.db.executemany(
        "INSERT INTO jobs(id,kind,symbol,start,end,source,state,nrows,response_ids) "
        "VALUES (?,'bars','sh600001','2010-01-04','2010-01-04',?,'empty_unknown',0,?)",
        [("bao-empty", "baostock", '["bao-receipt"]'), ("http-empty", "sina", '["http-receipt"]')],
    )
    s.db.commit()
    bao_before = dict(s.db.execute("SELECT * FROM jobs WHERE id='bao-empty'").fetchone())
    s.close()
    args.end = "2010-01-05"
    module.command(args)
    assert len(calls) == 2
    s = LocalDailyStore(tmp_path, readonly=True)
    assert dict(s.db.execute("SELECT * FROM jobs WHERE id='bao-empty'").fetchone()) == bao_before
    assert s.db.execute("SELECT state FROM jobs WHERE id='http-empty'").fetchone()[0] == "pending"
    assert [r["symbol"] for r in s.universe("2010-01-05")] == ["sh600001", "sh600002"]
    s.close()
