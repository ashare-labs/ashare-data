"""Offline Friday-to-Monday simulation; these fixtures are never live evidence."""

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from ashare_data.providers.free_updates import supplemental_batch as batch
from ashare_data.providers.free_updates import free_update_worker as worker
from ashare_data.providers.free_updates.free_update_manual import read_snapshot, write_snapshot
from ashare_data.providers.free_updates import digest
from ashare_data.providers.free_updates.supplemental_sources import CSI_CODES, parse_quotes
from ashare_data.providers.free_updates.supplemental_updates import ROWS
from test_supplemental_sources import wire


def test_all_nineteen_refresh_next_trading_day_retry_only_failed_quote_and_reuse_same_day(tmp_path, monkeypatch):
    clock = datetime(2026, 10, 30, 20, tzinfo=ZoneInfo("Asia/Shanghai"))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz)

    monkeypatch.setattr(batch, "datetime", Clock)
    monkeypatch.setattr(worker, "now", lambda: clock.isoformat())

    def no_network(*args, **kwargs):
        raise AssertionError("offline fixture attempted external HTTP")

    monkeypatch.setattr(worker.Capture, "get", no_network)
    monkeypatch.setattr(worker.Capture, "get_curl", no_network)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(dict(manual_snapshot_queries=[])))
    profile = tmp_path / "profile.json"
    daily_symbols = ["sh600000", "sh600028", "sh600519", "sh688002", "sz000001", "sz000009", "sz000012"]
    profile.write_text(json.dumps(dict(schema="ad.supplemental-19-batch.v1", root=str(tmp_path/"store"),
        source_index=str(seed), source_roots=[str(upstream)], csi_indices=sorted(CSI_CODES),
        topic_securities=["sh600000", "sz000001"], daily_securities=daily_symbols,
        max_jobs=40, attempts_per_day=2, interval_seconds=0)))

    def publish_first_batch_inputs():
        # Same stable scopes as the real dependency jobs, with fixture values
        # changed on Monday so stale-source reuse cannot accidentally pass.
        day = clock.date().isoformat()
        value = "1" if day == "2026-10-30" else "2"
        cases = [
            ("bond_zh_cov_info", ["SECUCODE"], [dict(SECUCODE="113052.SH", COUPON_IR=value)]),
            ("tdx_f10_valuation", ["security", "date"], [dict(security="sh600000", date=day, pe_ttm=value, pb_mrq=".5")]),
            ("tdx_f10_company", ["security"], [dict(security="sh600000", name="fixture-"+value)]),
            ("tdx_members", ["block_code", "security"], [dict(block_code="880583", security="sh600000", name="fixture-"+value)]),
            ("treasury_rates", ["date", "RATE_TYPE"], [dict(date=day, RATE_TYPE=k, RATE=value) for k in ["Over_10_Years", "Real_Rate"]]),
            ("sina_corporate_actions", ["security", "native_business_identity"], [dict(security="sh600000", native_business_identity=["dividend", day], announcement_date=day, cash_per_share=value)]),
        ]
        for function, keys, rows in cases:
            job = dict(function=function, parameters={"fixture_scope": "stable"})
            job["job_id"] = digest(job)
            check = dict(function=function, run_id=digest([job, day]), observed_at=clock.isoformat(),
                records=len(rows), execution_kind="offline_fixture_not_live_evidence", source_urls=[],
                semantics=dict(contract_reviewed=True, gaps=[], business_key=keys, units={}),
                coverage=dict(complete_history=False, source_count=len(rows)))
            write_snapshot(upstream, 0, job, rows, check)

    calls, fail_next_quote = [], False

    def collect(cap, function, params, as_of):
        nonlocal fail_next_quote
        calls.append((function, params, as_of))
        if function in {"index_stock_cons_csindex", "cni_current_members"}:
            symbol = "sz300750" if function == "cni_current_members" else "sh600000"
            if as_of == "2026-11-02" and params["symbol"] == "000300":
                symbol = "sh600519"  # A new constituent needs a new quote.
            rows = [dict(指数代码=params["symbol"], 日期=as_of,
                         交易所="上海证券交易所" if symbol.startswith("sh") else "深圳证券交易所",
                         成分券代码=symbol[2:], 成分券名称="fixture")]
        elif function == "tdx_f10_hot_topics":
            rows = [dict(security=params["symbol"], topic_id=as_of, source_native={"fixture": True})]
        elif function == "sina_raw_daily":
            assert params["end_date"] == as_of.replace("-", "")
            rows = [dict(security=params["symbol"], asset_type="stock", date=as_of,
                         open="10", high="11", low="9", close="10", volume="100", amount=None,
                         price_basis="unadjusted")]
        else:
            assert function == "sina_current_quotes"
            if fail_next_quote:
                fail_next_quote = False
                raise OSError("offline new-day quote failure")
            assert len(params["symbols"]) == len(set(params["symbols"]))
            rows, _ = parse_quotes(b"".join(wire(s, day=as_of) for s in params["symbols"]),
                                   params["symbols"], clock.isoformat())
        cap.record("https://fixture.invalid/offline", "fixture.invalid", 200,
                   json.dumps(rows).encode(), {})
        return rows, dict(source_count=len(rows), complete_returned_window=True,
                          fixture_only=True)

    monkeypatch.setattr(batch, "collect", collect)
    publish_first_batch_inputs()
    friday = batch.run(profile, execute=True)
    assert friday["all_requested_jobs_received"] and friday["attempted"] == 18
    assert friday["derived"]["usable_count"] == 19 and friday["unique_members"] == 2
    assert {i["excel_row"] for i in friday["derived"]["items"]} == ROWS
    repeat = batch.run(profile, execute=True)
    assert repeat["attempted"] == repeat["network_requests"] == 0

    clock = datetime(2026, 11, 2, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    publish_first_batch_inputs()
    fail_next_quote = True
    failed = batch.run(profile, execute=True)
    assert not failed["all_requested_jobs_received"] and failed["attempted"] == 18
    assert failed["state"] != friday["state"] and Path(friday["state"]).exists()
    # Old quotes can remain readable, but cannot complete the new-day queue.
    old_quote_count = sum(f == "sina_current_quotes" for f, _, _ in calls)
    monday = batch.run(profile, execute=True)
    assert monday["attempted"] == 1 and monday["all_requested_jobs_received"]
    assert sum(f == "sina_current_quotes" for f, _, _ in calls) == old_quote_count + 1
    assert monday["unique_members"] == 3 and monday["derived"]["usable_count"] == 19
    current = {i["excel_row"]: i for i in monday["derived"]["items"]}
    for row in range(125, 133):
        assert current[row]["scope"]["complete_current_member_quotes"]
        assert current[row]["scope"]["latest_quote_date"] == "2026-11-02"
    for row in ROWS:
        manifest, records = read_snapshot(current[row]["snapshot"])
        assert manifest["observed_at"] == clock.isoformat()
        assert any(m["observed_at"] == clock.isoformat() for m in manifest["derived_from"])
        assert records
    assert read_snapshot(current[30]["snapshot"])[1][0]["COUPON_IR"] == "2"
    assert current[50]["latest_period"] == current[64]["latest_period"] == "2026-11-02"
    assert current[201]["latest_period"] == "2026-11-02"
    again = batch.run(profile, execute=True)
    assert again["attempted"] == again["network_requests"] == 0
    assert all(i["new_records"] == i["revised_records"] == 0 for i in again["derived"]["items"])
