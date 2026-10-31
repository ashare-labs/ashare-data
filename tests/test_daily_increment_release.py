"""Free-source continuation preserves baseline release values and isolates conflicts."""

import csv
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_data.market.increment import normalize_values
from ashare_data.market.increment_schedule import due_plan, historical_members
from ashare_data.product.market_release import ReleasedDailyProduct, minute_prices
from ashare_data.storage.cmes_release import import_cmes
from ashare_data.storage.daily_increment import IncrementStore, initialize
from ashare_data.storage.increment_release import build_candidate
from ashare_data.storage.market_release import MarketRelease, sha_file

from test_daily_increment import config, member, native


def baseline(tmp_path):
    source = tmp_path / "SH.600000.csv"
    with source.open("w") as out:
        csv.writer(out).writerows([
            ["股票代码", "时间", "开盘价", "最高价", "最低价", "收盘价", "昨收价", "成交量", "成交额"],
            ["SH.600000", "2024-01-02", "10", "11", "9", "10", "9.8", "1.25", "1.25"]])
    return import_cmes(tmp_path / "base", daily=[source], free_reserve_bytes=0)


def test_candidate_standard_reads_old_release_dedup_and_conflicts(tmp_path):
    original = baseline(tmp_path)
    binding = dict(root=str(tmp_path / "base"), release_id=1, manifest_sha256=original["manifest_sha256"])
    cfg = dict(config(), base_release=binding)
    root = initialize(tmp_path / "increment", cfg)
    store = IncrementStore(root)
    store.seed([])
    store.plan("2024-01-03", [member("600000.XSHG"), member("BJ.920000")])
    with store.db:
        store.capture(dict(sha256="asset", path="original", source="test"))
        for security in ("600000.XSHG", "BJ.920000"):
            values = normalize_values(native("2024-01-03 09:31:00"))
            store.observe("sohu", "1d", security, "2024-01-03", values, "asset", 0, "2024-01-03T13:00:00Z")
            values["label_at"] = "2024-01-03T09:31:00+08:00"
            store.observe("sina", "1m", security, "2024-01-03", values, "asset", 0, "2024-01-03T13:00:00Z")
            store.cover("2024-01-03", security, "minute", dict(state="partial", observed_count=1))
    store.close()
    before = {p.name: sha_file(p) for p in (tmp_path / "base" / "objects").iterdir()}
    output = root / "candidates" / "market"
    first = build_candidate(root, "2024-01-03", output)
    assert first["input_dispositions"]["adopted"] == 4
    release = MarketRelease(output)
    product = ReleasedDailyProduct({"market_release": dict(root=str(output), release_id=2, manifest_sha256=release.sha256)})
    all_prices = product.prices(["600000.XSHG"], "2024-01-02", "2024-01-03")
    assert [r["source"] for r in all_prices["rows"]] == ["cmes", "sohu"]
    assert len(product.prices(["600000.XSHG"], "2024-01-02", "2024-01-03", "cmes")["rows"]) == 1
    assert len(product.prices(["600000.XSHG"], "2024-01-02", "2024-01-03", "sohu")["rows"]) == 1
    assert len(minute_prices({"market_release": dict(root=str(output), release_id=2, manifest_sha256=release.sha256)}, "BJ.920000", "2024-01-03T09:31:00+08:00", "2024-01-03T09:31:00+08:00")["rows"]) == 1
    assert MarketRelease(output, 1).read("bars_1d", "600000.XSHG", "2024-01-02", "2024-01-03")[0]["volume_shares"] == 125
    again = build_candidate(root, "2024-01-03", output)
    assert again["unchanged"] and again["manifest_sha256"] == first["manifest_sha256"]
    recipe = Path(__file__).resolve().parents[1] / "scripts" / "accept_daily_update.py"
    spec = importlib.util.spec_from_file_location("increment_candidate_acceptance", recipe)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    with closing(IncrementStore(root)) as check:
        assert verifier.verify_candidate(check, "2024-01-03", release) == 4
        check.db.execute("UPDATE observations SET close_price='10.0000000000001' WHERE frequency='1m'")
        with pytest.raises(AssertionError, match="candidate key/value differs"):
            verifier.verify_candidate(check, "2024-01-03", release)
        check.db.rollback()
    assert before == {p.name: sha_file(p) for p in (tmp_path / "base" / "objects").iterdir()}
    store = IncrementStore(root)
    with store.db:
        store.capture(dict(sha256="revised", path="original revision", source="test"))
        values = normalize_values(native("2024-01-03 09:31:00", close="10.1"))
        store.observe("sohu", "1d", "600000.XSHG", "2024-01-03", values, "revised", 0, "later")
    store.close()
    revised = build_candidate(root, "2024-01-03", output)
    assert revised["input_dispositions"]["quarantined"] == 2
    assert MarketRelease(output).read("bars_1d", "600000.XSHG", "2024-01-03", "2024-01-03")[0]["close_price"] == Decimal(10)
    with MarketRelease(output).metadata() as db:
        contract = json.loads(db.execute("SELECT body FROM series_contracts WHERE series_id=1").fetchone()[0])
        assert contract["sources"] == ["cmes", "baostock", "sohu"]


def test_bounded_outage_plan_does_not_skip_gap_or_invent_calendar(tmp_path):
    store = IncrementStore(initialize(tmp_path / "increment", config()))
    store.plan("2026-10-19", [member("600000.XSHG")])
    with store.db:
        store.db.executemany("INSERT INTO calendar VALUES(?,1,'test')", [(f"2026-10-{n}",) for n in (19, 20, 21, 22)])
        for kind in ("daily", "minute", "factor"):
            store.cover("2026-10-19", "600000.XSHG", kind, dict(state="complete", observed_count=1))
            store.finish("2026-10-19", "600000.XSHG", kind)
    now = datetime(2026, 10, 22, 21, tzinfo=ZoneInfo("Asia/Shanghai"))
    plan = due_plan(store, now, fetch_calendar=False)
    assert plan["days"] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert store.cursors()["minute"] == dict(complete_through="2026-10-19", first_incomplete_day="2026-10-20")
    morning = due_plan(store, now.replace(hour=9), fetch_calendar=False)
    assert morning["days"] == ["2026-10-20", "2026-10-21"]
    store.close()


def test_historical_membership_is_not_invented_from_current_list(tmp_path):
    store = IncrementStore(initialize(tmp_path / "increment", config()))
    store.plan("2026-10-22", [member("600000.XSHG"), member("BJ.920000")])
    historical_members(store, "2026-10-21", [member("600000.XSHG"), member("BJ.920000")], {
        "query_all_stock": dict(status="ok", rows=[dict(code="sh.600000", code_name="test")])})
    states = dict(store.db.execute("SELECT security,expected_state FROM scopes WHERE day='2026-10-21'"))
    assert states == {"600000.XSHG": "expected", "BJ.920000": "unknown"}
    assert store.report("2026-10-21")["all_requested_data_complete"] is False
    store.close()
