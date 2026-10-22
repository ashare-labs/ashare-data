import json
import sqlite3
import zlib

import pytest

from ashare_data.cli.daily_increment import main
from ashare_data.market.increment import DEFAULT_MINUTE_WINDOW, minute_rows
from ashare_data.providers.baostock_daily import BULK, DailySession, decode_bulk
from ashare_data.providers.baostock_receipt.protocol import END, SEP
from ashare_data.providers.daily_increment import equity_security, assess_minutes, session_labels, universe
from ashare_data.storage.daily_increment import initialize, IncrementStore


def config():
    return dict(sdk_path="unused", seed_directory=None, base_release=None,
                minute_window=DEFAULT_MINUTE_WINDOW, interval=1, timeout=30,
                budget_bytes=10**8, free_reserve_bytes=0, ready_time="20:30",
                timezone="Asia/Shanghai", daily_sources=["baostock", "sohu"], retry_window_days=10)


@pytest.fixture
def store(tmp_path):
    root = initialize(tmp_path / "increment", config())
    value = IncrementStore(root)
    yield value
    value.close()


def member(security):
    return dict(security=security, name=security, source="test_current_membership")


def native(label, close="10"):
    return dict(day=label, open="10", high="11", low="9", close=close, volume="100", amount="1000.1234")


def test_universe_includes_all_a_boards():
    expected = {"sh600000": "600000.XSHG", "sh688001": "688001.XSHG",
                "sz300001": "300001.XSHE", "sz000001": "000001.XSHE", "bj920000": "BJ.920000"}
    assert {k: equity_security(k) for k in expected} == expected
    assert equity_security("sh510300") is None
    assert equity_security("sz399001") is None
    assert equity_security("600000") is None


def test_universe_page_completeness_and_names():
    class HTTP:
        def get(self, url, referer):
            data = "3" if "Count" in url else [
                dict(symbol="sh688001", name="STAR"), dict(symbol="sz300001", name="Growth"), dict(symbol="bj920000", name="BJ")]
            return json.dumps(data).encode(), [dict(sha256="source", received_at="2026-10-22T15:00:00+08:00")]
    result = universe(HTTP())
    assert len(result["rows"]) == result["source_count"] == 3
    assert not result["excluded"]


def test_new_listing_added_absence_not_delisting_and_retired_history_kept(store):
    first = [member("600000.XSHG"), member("300001.XSHE"), member("BJ.920000")]
    store.plan("2026-10-21", first)
    store.plan("2026-10-22", [member("600000.XSHG"), member("688001.XSHG")], basics=[
        dict(code="sz.300001", code_name="retired", type="1", ipoDate="2010-01-01", outDate="2026-10-21")])
    retired = store.db.execute("SELECT * FROM instruments WHERE security='300001.XSHE'").fetchone()
    assert retired["state"] == "retired"
    assert store.db.execute("SELECT count(*) FROM scopes WHERE day='2026-10-21'").fetchone()[0] == 3
    assert store.db.execute("SELECT expected_state FROM scopes WHERE day='2026-10-22' AND security='BJ.920000'").fetchone()[0] == "unknown"
    assert store.db.execute("SELECT count(*) FROM jobs WHERE security='688001.XSHG'").fetchone()[0] == 3


def test_interrupted_job_recovered_and_values_idempotent(store):
    store.plan("2026-10-22", [member("600000.XSHG")])
    store.start("2026-10-22", "600000.XSHG", "minute")
    with store.writer():
        assert store.pending("2026-10-22", "minute")[0]["state"] == "pending"
        from ashare_data.market.increment import normalize_values
        values = normalize_values(native("2026-10-22 09:31:00"))
        values["label_at"] = "2026-10-22 09:31:00+08:00"
        with store.db:
            first = store.observe("sina", "1m", "600000.XSHG", "2026-10-22", values, "asset", 0, "now")
            second = store.observe("sina", "1m", "600000.XSHG", "2026-10-22", values, "asset", 0, "now")
        assert first == dict(added=1, duplicate=0, conflict=0)
        assert second == dict(added=0, duplicate=1, conflict=0)
        values["close_price"] = "10.1"
        with store.db:
            revised = store.observe("sina", "1m", "600000.XSHG", "2026-10-22", values, "new_asset", 0, "later")
        assert revised["conflict"] == 1
        assert store.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2


def test_complete_grid_and_sparse_beijing_are_distinct():
    day = "2026-10-22"
    rows = [native(x) for x in sorted(session_labels(day))]
    _, whole = assess_minutes(json.dumps(rows).encode(), day, 1970)
    _, sparse = assess_minutes(json.dumps(rows[::4]).encode(), day, 1970)
    assert whole["state"] == "complete" and whole["observed_count"] == 240
    assert sparse["state"] == "partial" and sparse["missing_regular_labels"] == 180


def test_window_is_configured_and_historical_target_can_reuse_later_capture():
    rows = [native("2026-10-21 09:31:00") for _ in range(1969)] + [native("2026-10-22 09:31:00")]
    with pytest.raises(ValueError, match="bounded"):
        minute_rows(json.dumps(rows).encode(), after_date="2026-10-20", end_date="2026-10-22")
    parsed, coverage = assess_minutes(json.dumps(rows).encode(), "2026-10-21", 1970)
    assert len(parsed) == 1970
    assert coverage["quarantined"] == 0
    assert coverage["resolved_unused"] == 1
    assert coverage["input_rows"] == sum(coverage[k] for k in ("adopted", "equal_duplicate", "resolved_unused", "quarantined", "format_rejected"))


def test_expired_window_not_reported_as_complete_or_suspension():
    body = json.dumps([native("2026-10-22 09:31:00")]).encode()
    _, coverage = assess_minutes(body, "2026-10-01", 1970)
    assert coverage["state"] == "window_expired"
    _, empty = assess_minutes(b"null", "2026-10-22", 1970)
    assert empty["state"] == "empty_unknown"
    _, suspended = assess_minutes(b"null", "2026-10-22", 1970, trade_status="0")
    assert suspended["state"] == "suspended"
    bad, malformed = assess_minutes(b'[{"close":"10"}]', "2026-10-22", 1970)
    assert malformed["state"] == "quality_gap" and malformed["format_rejected"] == 1
    assert bad[0]["reason"] == "invalid_native_minute_label"


def wire(kind, text):
    return ("00.9.40" + SEP + kind + SEP + f"{len(text):010d}").encode() + text


def test_bulk_protocol_does_not_page_at_exactly_2000():
    method = "query_daily_history_k_AStock"
    outgoing = SEP.join([method, "anonymous", "2026-10-22"]).encode()
    core = wire(BULK[method][0], outgoing)
    sent = core + SEP.encode() + str(zlib.crc32(core)).encode() + b"\n"
    records = [["2026-10-22", "sh.600000"]] * 2000
    body = SEP.join(["0", "success", method, "anonymous", json.dumps({"record": records}), "date,code"]).encode()
    raw = wire(BULK[method][1], zlib.compress(body)) + b"\x010\n" + END
    rows, fields = decode_bulk(raw, sent, dict(method=method, params={"date": "2026-10-22"}))
    assert len(rows) == 2000 and fields == ["date", "code"]
    with pytest.raises(ValueError, match="identity"):
        decode_bulk(raw, sent, dict(method=method, params={"date": "2026-10-21"}))


def test_quota_counts_sends_before_network(tmp_path):
    session = DailySession(tmp_path, tmp_path, request_limit=1)
    session.quota = sqlite3.connect(tmp_path / "quota.sqlite3")
    session.quota.execute("CREATE TABLE quota(day TEXT PRIMARY KEY,sends INTEGER)")
    try:
        session.reserve()
        with pytest.raises(RuntimeError, match="budget"):
            session.reserve()
    finally:
        session.quota.close()


def test_schedule_only_prints_configuration(store, capsys):
    before = set(store.root.iterdir())
    assert main(["schedule", "--root", str(store.root)]) == 0
    text = capsys.readouterr().out
    assert "StartInterval" in text and "--due" in text
    assert set(store.root.iterdir()) == before


def test_live_market_root_rejected(tmp_path):
    (tmp_path / "CURRENT.json").write_text("{}")
    with pytest.raises(ValueError, match="dedicated"):
        initialize(tmp_path, config())


def test_access_denial_persists_between_invocations(tmp_path, monkeypatch):
    from ashare_data.providers.daily_increment import IncrementHTTP
    from ashare_data.providers.daily_sources import PublicHTTP, SourceBlocked
    def deny(self, url, referer):
        self.blocked["example.test"] = "HTTP 403; blocked"
        raise SourceBlocked(self.blocked["example.test"])
    monkeypatch.setattr(PublicHTTP, "get", deny)
    with pytest.raises(SourceBlocked):
        IncrementHTTP(tmp_path / "capture1", tmp_path).get("https://example.test")
    second = IncrementHTTP(tmp_path / "capture2", tmp_path)
    assert second.blocked == {"example.test": "HTTP 403; blocked"}


def test_sdk_validation_failure_releases_single_connection_lock(tmp_path, monkeypatch):
    import fcntl
    from ashare_data.providers import baostock_daily
    monkeypatch.setattr(baostock_daily.tempfile, "gettempdir", lambda: str(tmp_path))
    def reject(path):
        raise ValueError("invalid pinned sdk")
    monkeypatch.setattr(baostock_daily, "sdk_identity", reject)
    session = DailySession(tmp_path, tmp_path)
    with pytest.raises(ValueError, match="invalid pinned"):
        session.__enter__()
    with (tmp_path / f"ashare-baostock-{baostock_daily.os.getuid()}.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
