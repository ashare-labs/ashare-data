"""Synthetic calendar/state/visibility regressions, not a real-market certification."""
import copy
import json
from datetime import datetime as RealDatetime, timedelta

import pandas as pd
import pytest

from ashare_data import Client, CoverageContract, DataError, reconcile_turnover
from ashare_data import live, transport

SEC = "600000.XSHG"


def payload(labels, close="10"):
    return [{"day": t, "open": "10", "high": "11", "low": "9", "close": close,
             "volume": "100", "amount": "1000"} for t in labels]


def profile(frequency="1m", dates=("2026-09-30",), state="trading", sessions=None):
    cal, statuses = [], []
    for d in dates:
        spans = sessions or [("09:30", "09:35")]
        cal.append({"date": d, "is_open": True, "available_at": "2026-09-01T00:00:00+08:00",
                    "evidence": "synthetic schedule only", "sessions": [
                        {"id": f"s{i}", "start": f"{d}T{a}:00+08:00", "end": f"{d}T{b}:00+08:00",
                         "mapping": "end_labels"} for i, (a, b) in enumerate(spans)]})
        statuses.append({"security": SEC, "date": d, "state": state,
                         "available_at": "2026-09-01T00:00:00+08:00", "evidence": "synthetic full-day status"})
    return {"schema_version": 1, "id": "test-facts-v1", "provider": "sina_public", "frequency": frequency,
            "trading_scope": "synthetic_continuous", "evidence_kind": "synthetic", "evidence": "generated, not vendor evidence",
            "label_semantics": "verified", "calendar": cal, "statuses": statuses}


@pytest.fixture
def clock(monkeypatch):
    class Clock(RealDatetime):
        instant = RealDatetime(2026, 10, 8, 21, tzinfo=live.TZ)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)

    monkeypatch.setattr(live, "datetime", Clock)
    monkeypatch.setattr(transport, "datetime", Clock)
    return Clock


@pytest.fixture
def serve(monkeypatch):
    calls = []

    def install(rows):
        def read(*args):
            calls.append(args[0])
            return json.dumps(rows).encode()
        monkeypatch.setattr(transport, "public_read", read)
    return install, calls


@pytest.mark.parametrize("retained,kind,missing", [([2,3,4,5], "head", 1), ([1,2,4,5], "internal", 1),
                                                 ([1,2], "tail", 3), ([], "whole_day", 5)])
def test_grid_checks_head_internal_tail_empty(retained, kind, missing):
    c = CoverageContract(profile())
    labels = [f"2026-09-30 09:{30+x}:00" for x in retained]
    r = c.assess(SEC, labels, "2026-09-30 09:31", "2026-09-30 09:35")
    assert not r["complete"] and r["status"] == "incomplete"
    assert len(r["missing"]) == missing and {x["kind"] for x in r["missing"]} == {kind}


def test_legacy_strict_preserved_complete_gate_blocks_tail(clock, serve):
    serve[0](payload(["2026-09-30 09:31:00", "2026-09-30 09:32:00"]))
    legacy = Client().get_price(SEC, start_date="2026-09-30 09:31", end_date="2026-09-30 15:00", frequency="1m", strict=True)
    assert len(legacy) == 2 and legacy.attrs["provenance"][0]["coverage_report"]["status"] == "unknown"
    c = Client(coverage_contract=CoverageContract(profile(sessions=[("09:30", "11:30"), ("13:00", "15:00")])))
    with pytest.raises(DataError) as exc:
        c.get_price(SEC, start_date="2026-09-30 09:31", end_date="2026-09-30 15:00", frequency="1m", require_complete=True)
    assert exc.value.code == "COVERAGE_INCOMPLETE" and len(exc.value.details["missing"]) == 238
    assert all(x["kind"] == "tail" for x in exc.value.details["missing"])


def test_missing_day_and_unknown_calendar():
    c = CoverageContract(profile(dates=("2026-09-29", "2026-09-30")))
    r = c.assess(SEC, [f"2026-09-29 09:{x}:00" for x in range(31,36)], "2026-09-29", "2026-09-30 15:00")
    assert len(r["missing"]) == 5 and all(x["kind"] == "whole_day" for x in r["missing"])
    r = c.assess(SEC, [], "2026-09-28", "2026-09-30 15:00")
    assert r["status"] == "unknown" and r["unknown"][0]["code"] == "CALENDAR_UNKNOWN"


@pytest.mark.parametrize("state", ["suspended", "unknown", "absent"])
def test_status_not_inferred_from_missing_data(state):
    data = profile(state="unknown" if state == "absent" else state)
    if state == "absent":
        data["statuses"] = []
    r = CoverageContract(data).assess(SEC, [], "2026-09-30", "2026-09-30 15:00")
    assert r["complete"] == (state == "suspended") and r["expected_count"] == 0
    if state != "suspended":
        assert r["unknown"][0]["code"] == "TRADING_STATUS_UNKNOWN"


def test_explicit_closed_day():
    data = profile()
    data["calendar"][0].update(is_open=False, sessions=[])
    assert CoverageContract(data).assess(SEC, [], "2026-09-30", "2026-09-30 15:00")["complete"]


def test_lunch_and_explicit_auction_not_filled():
    data = profile(sessions=[("09:30", "11:30"), ("13:00", "14:57")])
    data["calendar"][0]["sessions"].append({"id": "auction", "start": "2026-09-30T14:57:00+08:00",
        "end": "2026-09-30T15:00:00+08:00", "mapping": "explicit", "slots": [
            {"label": "2026-09-30T15:00:00+08:00", "start": "2026-09-30T14:57:00+08:00", "end": "2026-09-30T15:00:00+08:00"}]})
    c = CoverageContract(data)
    labels = [s["label"].isoformat() for s in c._slots["2026-09-30"]]
    r = c.assess(SEC, labels, "2026-09-30", "2026-09-30 15:00")
    assert len(labels) == 238 and r["complete"] and not r["trade_totals_verified"]
    assert all(t not in " ".join(labels) for t in ["T14:58", "T14:59", "T12:00"])


@pytest.mark.parametrize("change", ["duplicate_date", "duplicate_status", "overlap", "partial_explicit", "naive_time"])
def test_invalid_contracts_fail(change):
    data = profile()
    if change == "duplicate_date":
        data["calendar"].append(copy.deepcopy(data["calendar"][0]))
    if change == "duplicate_status":
        data["statuses"].append(copy.deepcopy(data["statuses"][0]))
    if change == "overlap":
        extra = copy.deepcopy(data["calendar"][0]["sessions"][0])
        extra["id"] = "other"
        data["calendar"][0]["sessions"].append(extra)
    if change == "partial_explicit":
        data["calendar"][0]["sessions"][0].update(mapping="explicit", slots=[])
    if change == "naive_time":
        data["statuses"][0]["available_at"] = "2026-09-01T00:00:00"
    with pytest.raises(DataError):
        CoverageContract(data)


def test_contract_input_and_results_detached():
    data = profile()
    c = CoverageContract(data)
    data["statuses"][0]["state"] = "suspended"
    r = c.assess(SEC, [], "2026-09-30", "2026-09-30 15:00")
    r["missing"].clear()
    assert len(c.assess(SEC, [], "2026-09-30", "2026-09-30 15:00")["missing"]) == 5


@pytest.mark.parametrize("where", ["calendar", "statuses"])
def test_future_fact_unavailable_at_clock(where):
    data = profile()
    data[where][0]["available_at"] = "2026-10-01T00:00:00+08:00"
    r = CoverageContract(data).assess(SEC, [], "2026-09-30", "2026-09-30 15:00", as_of="2026-09-30T16:00:00+08:00")
    assert r["status"] == "unknown"


def test_unknown_label_mapping_cannot_pass():
    data = profile()
    data["label_semantics"] = "unverified"
    assert not CoverageContract(data).assess(SEC, [], "2026-09-30", "2026-09-30 15:00")["complete"]


def test_new_http_old_data_age_and_stale_gate(clock, serve):
    serve[0](payload(["2026-09-30"]))
    c = Client(coverage_contract=CoverageContract(profile(frequency="daily", dates=("2026-10-08",))))
    fresh = c.get_price(SEC, count=1).attrs["provenance"][0]["freshness"]
    assert fresh["http_observation_age_seconds"] == 0 and fresh["source_label_age_seconds"] > 8*86400
    assert fresh["status"] == "stale" and fresh["expected_latest_label"].startswith("2026-10-08")
    with pytest.raises(DataError) as exc:
        c.get_price(SEC, count=1, require_fresh=True)
    assert exc.value.code == "STALE_SOURCE"


def test_count_uses_expected_tail(clock, serve):
    serve[0](payload(["2026-09-30"]))
    c = Client(coverage_contract=CoverageContract(profile(frequency="daily", dates=("2026-09-30", "2026-10-08"))))
    with pytest.raises(DataError) as exc:
        c.get_price(SEC, count=1, require_complete=True)
    assert not exc.value.details["complete"] and exc.value.details["missing"][0]["label"].startswith("2026-10-08")


@pytest.mark.parametrize("kw,code", [({"require_complete":True},"COVERAGE_UNKNOWN"), ({"require_fresh":True},"FRESHNESS_UNKNOWN"),
                                    ({"require_final":True},"BAR_NOT_FINAL"), ({"as_of":"2026-09-30T14:55:00+08:00"},"VISIBILITY_UNKNOWN")])
def test_missing_guarantees_fail_before_network(clock, serve, kw, code):
    serve[0](payload(["2026-09-30"]))
    with pytest.raises(DataError) as exc:
        Client().get_price(SEC, count=1, **kw)
    assert exc.value.code == code and not serve[1]


def test_historical_daily_assumed_is_usable_but_not_pit(clock, serve, tmp_path):
    serve[0](payload(["2026-09-29", "2026-09-30"]))
    c = Client(cache=tmp_path)
    assert c.get_price(SEC, end_date="2026-09-30 14:55", count=1).index[-1] == pd.Timestamp("2026-09-30")
    context = c.at("2026-09-30T14:55:00+08:00", visibility="assumed")
    frame = context.get_price(SEC, end_date="2026-09-30 14:55", count=1)
    assert frame.index[-1] == pd.Timestamp("2026-09-29") and len(serve[1]) == 1
    assert not frame.attrs["point_in_time_verified"] and frame.attrs["available_at"] is None
    v = frame.attrs["provenance"][0]["visibility"]
    assert v["level"] == "assumed" and v["revision_history"] == "unknown" and v["assumptions"]
    with pytest.raises(DataError) as exc:
        c.at("2026-09-30T14:55:00+08:00", visibility="received").get_price(SEC, count=1)
    assert exc.value.code == "VISIBILITY_UNKNOWN"


@pytest.mark.parametrize("time,expected", [("14:54:59", "14:54:00"), ("14:55:00", "14:55:00")])
def test_assumed_minute_boundary(clock, serve, tmp_path, time, expected):
    serve[0](payload(["2026-09-30 14:54:00", "2026-09-30 14:55:00"]))
    c = Client(cache=tmp_path)
    c.get_price(SEC, end_date="2026-09-30", count=1, frequency="1m")
    f = c.at("2026-09-30T"+time+"+08:00", visibility="assumed").get_price(SEC, count=1, frequency="1m")
    assert f.index[-1] == pd.Timestamp("2026-09-30 "+expected) and not f.attrs["point_in_time_verified"]


def test_received_old_revision_and_context_pinning(clock, serve, tmp_path):
    clock.instant = RealDatetime(2026,9,30,16,tzinfo=live.TZ)
    serve[0](payload(["2026-09-30 14:55:00"], "10"))
    c = Client(cache=tmp_path)
    c.get_price(SEC, end_date="2026-09-30", count=1, frequency="1m")
    frozen = c.at(clock.instant, visibility="received")
    old = {str(p.relative_to(tmp_path)):p.read_bytes() for p in (tmp_path/"observations").iterdir()}
    clock.instant += timedelta(hours=1)
    serve[0](payload(["2026-09-30 14:55:00"], "10.5"))
    Client(cache=tmp_path, cache_mode="refresh").get_price(SEC, end_date="2026-09-30", count=1, frequency="1m")
    replay = c.at("2026-09-30T16:00:00+08:00", visibility="received")
    for context in (frozen, replay):
        f = context.get_price(SEC, count=1, frequency="1m")
        assert f.iloc[-1].close == 10 and not f.attrs["point_in_time_verified"]
    assert all((tmp_path/k).read_bytes()==v for k,v in old.items())
    assert len(list((tmp_path/"observations").iterdir())) == 2 and len(serve[1]) == 2
    with pytest.raises(DataError) as exc:
        frozen.acquire_price(SEC,"2026-09-30","2026-09-30")
    assert exc.value.code == "OFFLINE_QUERY"


def test_legacy_cache_read_only_compatibility(clock, serve, tmp_path):
    serve[0](payload(["2026-09-30 14:55:00"]))
    c = Client(cache=tmp_path)
    c.get_price(SEC,end_date="2026-09-30",count=1,frequency="1m")
    path=next((tmp_path/"requests").iterdir())
    entry=json.loads(path.read_text())
    entry.pop("observation_id")
    entry.pop("supersedes_observation")
    path.write_text(json.dumps(entry))
    before={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    c.at(clock.instant,visibility="received").get_price(SEC,count=1,frequency="1m")
    assert before=={str(p.relative_to(tmp_path)):p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_explicit_acquisition_preserves_unknowns_and_history_failure(clock, serve, tmp_path):
    serve[0](payload(["2026-09-30 09:31:00", "2026-09-30 09:32:00"]))
    c=Client(cache=tmp_path)
    r=c.acquire_price(SEC,"2026-09-30 09:31","2026-09-30 15:00")
    assert not r["requirements_met"] and r["fabricated_rows"]==0 and len(serve[1])==1
    with pytest.raises(DataError) as exc:
        c.acquire_price(SEC,"2026-04-09","2026-09-30")
    assert exc.value.details["acquisition"]["historical_paging_supported"] is False and len(serve[1])==2


def reference(**kw):
    return {"security":SEC,"trade_date":"2026-09-30","volume":"100","amount":"1000",
            "volume_unit":"share","amount_unit":"CNY","trading_scope":"unknown",**kw}


def test_equal_totals_unknown_scope_not_accepted():
    r=reconcile_turnover(payload(["2026-09-30 09:31:00"]),reference())
    assert r["comparison"]=="equal_within_declared_tolerance" and not r["accepted"] and r["status"]=="unknown"


def test_reconciliation_decimal_mismatch_and_missing_amount():
    rows=payload(["2026-09-30 09:31:00"])
    ref=reference(trading_scope="synthetic",scope_evidence="synthetic reference scope")
    coverage={"complete":True,"contract_id":"synthetic"}
    assert reconcile_turnover(rows,ref,bars_scope="synthetic",coverage=coverage)["accepted"]
    ref["volume"]="101"
    ref["amount"]="1000.0003"
    r=reconcile_turnover(rows,ref,bars_scope="synthetic",coverage=coverage)
    assert r["status"]=="failed" and r["reference_minus_bars_amount"]=="0.0003" and r["reference_minus_bars_volume"]=="1"
    with pytest.raises(DataError):
        reconcile_turnover(rows,ref,amount_tolerance="1000000")
    del rows[0]["amount"]
    with pytest.raises(DataError) as exc:
        reconcile_turnover(rows,ref)
    assert exc.value.code=="SOURCE_FIELD_MISSING"


def test_frozen_context_miss_never_fetches(clock, serve, tmp_path):
    serve[0](payload(["2026-09-30 09:31:00"]))
    with pytest.raises(DataError) as exc:
        Client(cache=tmp_path).at("2026-09-30T15:00:00+08:00",visibility="assumed").get_price(SEC,frequency="1m",count=1)
    assert exc.value.code=="CACHE_MISS" and not serve[1]


def test_context_cannot_leak_current_identity_or_calendar(clock, serve, tmp_path):
    serve[0](payload(["2026-09-30"]))
    c = Client(cache=tmp_path).at("2026-09-30T14:55:00+08:00", visibility="assumed")
    for query in [lambda: c.get_security_info(SEC), c.calendar]:
        with pytest.raises(DataError):
            query()
    assert not serve[1]


def test_future_calendar_mapping_not_used_for_earlier_clock():
    data = profile(frequency="daily")
    data["calendar"][0]["available_at"] = "2026-10-01T00:00:00+08:00"
    assert CoverageContract(data).bounds("2026-09-30", as_of="2026-09-30T14:55:00+08:00") is None


def test_complete_grid_does_not_claim_trade_totals(clock, serve):
    serve[0](payload([f"2026-09-30 09:{n}:00" for n in range(31,36)]))
    c = Client(coverage_contract=CoverageContract(profile()))
    f = c.get_price(SEC,start_date="2026-09-30 09:31",end_date="2026-09-30 09:35",frequency="1m",require_complete=True)
    assert f.attrs["provenance"][0]["coverage_report"]["grid_complete"]
    assert f.attrs["trade_totals_verified"] is False


def test_all_guarantees_report_unknown_without_fake_contract(clock, serve):
    serve[0](payload(["2026-09-30 09:31:00"]))
    assert Client().coverage(SEC,"2026-09-30","2026-09-30")["status"] == "unknown"
    assert not serve[1]


def test_query_snapshot_rejects_changed_body(clock, serve, tmp_path):
    serve[0](payload(["2026-09-30 14:55:00"]))
    c = Client(cache=tmp_path)
    c.get_price(SEC,end_date="2026-09-30",frequency="1m",count=1)
    q = c.at("2026-09-30T15:00:00+08:00",visibility="assumed")
    next((tmp_path/"objects").iterdir()).write_bytes(b"tampered")
    with pytest.raises(DataError) as exc:
        q.get_price(SEC,frequency="1m",count=1)
    assert exc.value.code == "CACHE_CORRUPT"


def test_context_reuses_existing_same_source_window_without_fetch(clock, serve, tmp_path):
    serve[0](payload(["2026-09-29", "2026-09-30"]))
    c = Client(cache=tmp_path)
    c.get_price(SEC,count=1)
    q = c.at("2026-09-30T14:55:00+08:00",visibility="assumed")
    f = q.get_price(SEC,count=1)
    assert f.index[-1] == pd.Timestamp("2026-09-29") and len(serve[1]) == 1
    assert f.attrs["provenance"][0]["cache_window_reused"]
    with pytest.raises(DataError) as exc:
        q.get_price("000001.XSHE",count=1)
    assert exc.value.code == "CACHE_MISS" and len(serve[1]) == 1


def test_coverage_does_not_promote_old_partial_rows(clock, serve, tmp_path):
    clock.instant = RealDatetime(2026,9,30,9,34,59,tzinfo=live.TZ)
    serve[0](payload([f"2026-09-30 09:{n}:00" for n in range(31,36)]))
    c = Client(cache=tmp_path,coverage_contract=CoverageContract(profile()))
    c.get_price(SEC,start_date="2026-09-30 09:31",end_date="2026-09-30 09:35",frequency="1m")
    clock.instant = RealDatetime(2026,9,30,16,tzinfo=live.TZ)
    r = c.at(clock.instant,visibility="assumed").coverage(SEC,"2026-09-30 09:31","2026-09-30 09:35")
    assert not r["complete"] and r["missing"] == [{"label":"2026-09-30T09:35:00+08:00","kind":"tail"}]
    assert len(serve[1]) == 1


def test_closed_day_coverage_needs_no_network(clock, serve):
    serve[0]([])
    data = profile()
    data["calendar"][0].update(is_open=False,sessions=[])
    assert Client(coverage_contract=CoverageContract(data)).coverage(SEC,"2026-09-30","2026-09-30")["complete"]
    assert not serve[1]
