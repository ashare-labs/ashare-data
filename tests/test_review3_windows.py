"""Public-query window and evidence-scope cross products; no network or private oracle."""

import hashlib
from datetime import datetime as RealDatetime

import pytest

from ashare_data import Client, CoverageContract, DataError
import test_data_contracts as helpers
from test_data_contracts import SEC, payload, profile
from test_review2_admission import interval

clock = helpers.clock
serve = helpers.serve

LAYOUTS = {
    "nested": ([31, 32, 33, 34], [33, 34]),
    "overlap": ([31, 32, 33], [33, 34, 35]),
    "disjoint": ([31, 32], [35, 36]),
    "same_labels": ([31, 32, 33], [31, 32, 33]),
}


def at(clock, t):
    clock.instant = RealDatetime.fromisoformat("2026-09-30T" + t + "+08:00")


def labels(minutes, hour="09"):
    return [f"2026-09-30 {hour}:{n:02}:00" for n in minutes]


def cache_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("url_mode", ["different", "exact_old", "same_url"])
@pytest.mark.parametrize("later_payload", [0, 1])
@pytest.mark.parametrize("query", ["count3", "end_count1", "range"])
@pytest.mark.parametrize("visibility", ["received", "assumed"])
def test_window_cross_product_keeps_latest_versions_and_required_history(
    clock, serve, tmp_path, layout, url_mode, later_payload, query, visibility
):
    chunks = LAYOUTS[layout]
    client = Client(cache=tmp_path, cache_mode="refresh")
    expected = {}
    for rank, chunk_index in enumerate((1 - later_payload, later_payload)):
        at(clock, "10:00:10" if rank == 0 else "10:01:10")
        close = "10" if rank == 0 else "11"
        serve[0](payload(labels(chunks[chunk_index]), close))
        kw = (
            {"end_date": "2026-09-30"}
            if url_mode == "same_url" or (url_mode == "exact_old" and rank == 0)
            else {}
        )
        client.get_price(SEC, frequency="1m", count=2 if rank == 0 else 1, **kw)
        expected.update({n: int(close) for n in chunks[chunk_index]})
    at(clock, "10:02:00")
    context = client.at("2026-09-30T10:01:30+08:00", visibility=visibility)
    before, calls = cache_hashes(tmp_path), len(serve[1])
    if query == "count3":
        kw = {"count": 3}
        wanted = sorted(expected)[-3:]
    elif query == "end_count1":
        kw = {"count": 1, "end_date": "2026-09-30 09:33:00"}
        wanted = [max(n for n in expected if n <= 33)]
    else:
        kw = {"start_date": "2026-09-30 09:31:00", "end_date": "2026-09-30 09:34:00"}
        wanted = [n for n in sorted(expected) if 31 <= n <= 34]
    frame = context.get_price(SEC, frequency="1m", **kw)
    assert list(frame.index.strftime("%H:%M")) == [f"09:{n}" for n in wanted]
    assert list(frame["close"]) == [expected[n] for n in wanted]
    p = frame.attrs["provenance"][0]
    assert len(p["row_observations"]) == len(frame)
    assert all(
        r["sha256"] in {o["sha256"] for o in p["response_observations"]}
        for r in p["row_observations"]
    )
    if len(p["response_observations"]) > 1:
        assert p["sha256"] is None and p["visibility"]["raw_hash"] is None
        assert p["completion_basis"] == "per_observation"
    assert not frame.attrs["point_in_time_verified"] and not frame.attrs["trade_totals_verified"]
    assert len(serve[1]) == calls and cache_hashes(tmp_path) == before


@pytest.mark.parametrize("visibility", ["received", "assumed"])
def test_snapshot_and_same_url_revisions_remain_pinned(clock, serve, tmp_path, visibility):
    client = Client(cache=tmp_path, cache_mode="refresh")
    at(clock, "09:35:00")
    serve[0](payload(labels([31, 32, 33]), "10"))
    client.get_price(SEC, frequency="1m", count=1, end_date="2026-09-30")
    pinned = client.at("2026-09-30T09:35:00+08:00", visibility=visibility)
    pinned_hash = pinned.transport.snapshot_id
    at(clock, "09:36:00")
    serve[0](payload(labels([33, 34]), "11"))
    client.get_price(SEC, frequency="1m", count=1, end_date="2026-09-30")
    current = client.at("2026-09-30T09:36:00+08:00", visibility=visibility)
    calls = len(serve[1])
    older = pinned.get_price(SEC, frequency="1m", count=1)
    latest = current.get_price(SEC, frequency="1m", count=4)
    assert older.index[-1].minute == 33 and older.iloc[-1]["close"] == 10
    assert list(latest["close"]) == [10, 10, 11, 11]
    assert pinned.transport.snapshot_id == pinned_hash != current.transport.snapshot_id
    assert len(serve[1]) == calls


@pytest.mark.parametrize(
    "as_of,expected", [("09:34:59", None), ("09:35:00", 10), ("09:35:59", 10), ("09:36:00", 11)]
)
def test_received_cutoff_exactly_controls_all_window_versions(
    clock, serve, tmp_path, as_of, expected
):
    client = Client(cache=tmp_path, cache_mode="refresh")
    for t, close, count in [("09:35:00", "10", 2), ("09:36:00", "11", 1)]:
        at(clock, t)
        serve[0](payload(labels([31, 32]), close))
        client.get_price(SEC, frequency="1m", count=count)
    at(clock, "09:37:00")
    context = client.at("2026-09-30T" + as_of + "+08:00", visibility="received")
    calls = len(serve[1])
    if expected is None:
        with pytest.raises(DataError) as ex:
            context.get_price(SEC, frequency="1m", count=1)
        assert ex.value.code == "VISIBILITY_UNKNOWN"
    else:
        assert context.get_price(SEC, frequency="1m", count=1).iloc[-1]["close"] == expected
    assert len(serve[1]) == calls


def test_assumed_backfill_is_explicit_and_filters_future_labels(clock, serve, tmp_path):
    at(clock, "10:00:00")
    serve[0](payload(labels([31, 32, 33, 34]), "11"))
    c = Client(cache=tmp_path)
    c.get_price(SEC, frequency="1m", count=1)
    q = c.at("2026-09-30T09:32:00+08:00", visibility="assumed")
    f = q.get_price(SEC, frequency="1m", count=2)
    assert list(f.index.minute) == [31, 32] and list(f["close"]) == [11, 11]
    assert f.attrs["visibility_level"] == "assumed" and not f.attrs["point_in_time_verified"]
    with pytest.raises(DataError) as ex:
        q.get_price(SEC, frequency="1m", count=3)
    assert ex.value.code == "COVERAGE_INCOMPLETE"


def test_forming_row_never_matures_by_merging_later_unrelated_response(clock, serve, tmp_path):
    c = Client(cache=tmp_path, cache_mode="refresh")
    at(clock, "09:31:50")
    serve[0](payload(labels([31, 32]), "10"))
    c.get_price(SEC, frequency="1m", count=1)
    at(clock, "09:34:00")
    serve[0](payload(labels([33]), "11"))
    c.get_price(SEC, frequency="1m", count=1, end_date="2026-09-30")
    q = c.at("2026-09-30T09:34:00+08:00", visibility="received")
    f = q.get_price(SEC, frequency="1m", count=2)
    assert list(f.index.minute) == [31, 33]
    with pytest.raises(DataError) as ex:
        q.get_price(SEC, frequency="1m", count=3)
    assert ex.value.code == "COVERAGE_INCOMPLETE"


def test_disjoint_observations_do_not_fabricate_internal_gap(clock, serve, tmp_path):
    at(clock, "09:36:00")
    c = Client(cache=tmp_path, cache_mode="refresh", coverage_contract=CoverageContract(profile()))
    serve[0](payload(labels([31, 32])))
    c.get_price(SEC, frequency="1m", count=2)
    serve[0](payload(labels([34, 35])))
    c.get_price(SEC, frequency="1m", count=1)
    q = c.at("2026-09-30T09:36:00+08:00", visibility="received")
    with pytest.raises(DataError) as ex:
        q.get_price(
            SEC,
            frequency="1m",
            start_date="2026-09-30 09:31",
            end_date="2026-09-30 09:35",
            require_complete=True,
        )
    assert ex.value.code == "COVERAGE_INCOMPLETE" and ex.value.details["missing"] == [
        {"label": "2026-09-30T09:33:00+08:00", "kind": "internal"}
    ]


@pytest.mark.parametrize("conflicting", [False, True])
def test_equal_observation_time_overlaps_do_not_use_window_size_as_tiebreak(
    clock, serve, tmp_path, conflicting
):
    at(clock, "09:36:00")
    c = Client(cache=tmp_path, cache_mode="refresh")
    serve[0](payload(labels([31, 32]), "10"))
    c.get_price(SEC, frequency="1m", count=2)
    serve[0](payload(labels([32, 33]), "11" if conflicting else "10"))
    c.get_price(SEC, frequency="1m", count=1)
    q = c.at("2026-09-30T09:36:00+08:00", visibility="received")
    assert q.get_price(SEC, frequency="1m", count=1).index[-1].minute == 33
    if conflicting:
        with pytest.raises(DataError) as ex:
            q.get_price(SEC, frequency="1m", count=2)
        assert ex.value.code == "OBSERVATION_CONFLICT"
    else:
        assert len(q.get_price(SEC, frequency="1m", count=3)) == 3


def resumed_profile():
    d = profile(sessions=[("09:30", "11:30"), ("13:00", "15:00")])
    d.update(
        schema_version=2,
        statuses=[interval("2026-09-30T13:00:00+08:00", "2026-10-01T00:00:00+08:00")],
    )
    return d


@pytest.mark.parametrize(
    "count,end,accepted",
    [(1, None, True), (5, None, True), (6, None, False), (3, "13:03", True), (4, "13:03", False)],
)
def test_count_tail_needs_only_decisive_statuses_but_full_required_warmup(
    clock, serve, count, end, accepted
):
    at(clock, "13:05:10")
    serve[0](payload(labels([1, 2, 3, 4, 5], "13")))
    c = Client(coverage_contract=CoverageContract(resumed_profile()))
    kw = {"count": count, "frequency": "1m", "require_complete": True}
    if end:
        kw["end_date"] = "2026-09-30 " + end
    if accepted:
        f = c.get_price(SEC, **kw)
        p = f.attrs["provenance"][0]["coverage_report"]
        assert len(f) == count and p["complete"] and p["unknown"] == []
        assert p["count_evidence_start"].startswith("2026-09-30T13:")
    else:
        with pytest.raises(DataError) as ex:
            c.get_price(SEC, **kw)
        assert (
            ex.value.code == "COVERAGE_UNKNOWN"
            and ex.value.details["unknown"][0]["code"] == "TRADING_STATUS_UNKNOWN"
        )


@pytest.mark.parametrize("change", ["hole", "late_state", "late_calendar", "partial_bar"])
def test_unknown_needed_tail_evidence_is_not_discarded(change):
    d = resumed_profile()
    if change == "hole":
        d["statuses"][0]["effective_start"] = "2026-09-30T13:05:00+08:00"
    if change == "late_state":
        d["statuses"][0]["available_at"] = "2026-09-30T13:06:00+08:00"
    if change == "late_calendar":
        d["calendar"][0]["available_at"] = "2026-09-30T13:06:00+08:00"
    if change == "partial_bar":
        d["statuses"] = [
            interval("2026-09-30T13:00:00+08:00", "2026-09-30T13:04:30+08:00", "suspended"),
            interval("2026-09-30T13:04:30+08:00", "2026-10-01T00:00:00+08:00"),
        ]
    r = CoverageContract(d).assess(
        SEC,
        labels([5], "13"),
        "2026-09-30",
        "2026-09-30 13:05",
        count=1,
        as_of="2026-09-30T13:05:10+08:00",
    )
    assert not r["complete"] and r["status"] == "unknown" and r["unknown"]


def test_count_missing_latest_bar_is_still_incomplete_not_previous_bar_success():
    c = CoverageContract(resumed_profile())
    r = c.assess(
        SEC,
        labels([4], "13"),
        "2026-09-30",
        "2026-09-30 13:05",
        count=1,
        as_of="2026-09-30T13:05:10+08:00",
    )
    assert (
        not r["complete"]
        and r["status"] == "incomplete"
        and r["missing"][0]["label"].endswith("13:05:00+08:00")
    )


def test_explicit_range_keeps_unknown_morning_and_count_start_is_invalid(clock, serve):
    at(clock, "13:05:10")
    serve[0](payload(labels([5], "13")))
    c = Client(coverage_contract=CoverageContract(resumed_profile()))
    with pytest.raises(DataError) as ex:
        c.get_price(
            SEC,
            frequency="1m",
            start_date="2026-09-30 09:30",
            end_date="2026-09-30 13:05",
            require_complete=True,
        )
    assert ex.value.code == "COVERAGE_UNKNOWN"
    calls = len(serve[1])
    with pytest.raises(DataError) as ex:
        c.get_price(SEC, frequency="1m", start_date="2026-09-30 13:05", count=1)
    assert ex.value.code == "INVALID_REQUEST" and len(serve[1]) == calls


def test_count_cross_day_requires_unknown_calendar_gap_when_needed():
    d = profile(frequency="daily", dates=("2026-09-28", "2026-09-30"))
    c = CoverageContract(d)
    kw = {
        "security": SEC,
        "labels": ["2026-09-28", "2026-09-30"],
        "start": "2026-09-28",
        "end": "2026-09-30",
        "as_of": "2026-09-30T16:00:00+08:00",
    }
    assert c.assess(count=1, **kw)["complete"]
    r = c.assess(count=2, **kw)
    assert not r["complete"] and r["unknown"][0] == {
        "date": "2026-09-29",
        "code": "CALENDAR_UNKNOWN",
    }
    d["calendar"].append(
        {
            "date": "2026-09-29",
            "is_open": False,
            "sessions": [],
            "available_at": "2026-09-01T00:00:00+08:00",
            "evidence": "synthetic closed date",
        }
    )
    assert CoverageContract(d).assess(count=2, **kw)["complete"]


def test_count_tail_cannot_escape_explicit_lower_bound_or_prove_unobserved_rows():
    c = CoverageContract(resumed_profile())
    r = c.assess(
        SEC,
        labels([1, 2, 3, 4, 5], "13"),
        "2026-09-30 13:05",
        "2026-09-30 13:05",
        count=2,
        as_of="2026-09-30T13:05:10+08:00",
    )
    assert not r["complete"] and r["unknown"][0]["code"] == "CALENDAR_COVERAGE"


@pytest.mark.parametrize("entry", ["get_price", "coverage"])
def test_direct_queries_do_not_use_facts_from_after_wall_clock(clock, serve, entry):
    at(clock, "13:05:10")
    data = resumed_profile()
    data["statuses"][0]["available_at"] = "2026-09-30T13:06:00+08:00"
    serve[0](payload(labels([5], "13")))
    client = Client(coverage_contract=CoverageContract(data))
    if entry == "get_price":
        with pytest.raises(DataError) as ex:
            client.get_price(SEC, frequency="1m", count=1, require_complete=True)
        assert ex.value.code == "COVERAGE_UNKNOWN"
    else:
        r = client.coverage(SEC, "2026-09-30 13:05", "2026-09-30 13:05")
        assert r["status"] == "unknown" and not r["complete"]


def test_multiwindow_selection_stays_with_exact_security_frequency_and_provider(
    clock, serve, tmp_path
):
    at(clock, "09:36:00")
    client = Client(cache=tmp_path, cache_mode="refresh")
    serve[0](payload(labels([31, 32]), "10"))
    client.get_price(SEC, frequency="1m", count=1)
    at(clock, "09:37:00")
    serve[0](payload(labels([35]), "11"))
    client.get_price("000001.XSHE", frequency="1m", count=1)
    client.get_price(SEC, frequency="5m", count=1)
    q = client.at("2026-09-30T09:37:00+08:00", visibility="received")
    calls = len(serve[1])
    f = q.get_price(SEC, frequency="1m", count=1)
    assert f.index[-1].minute == 32 and f.iloc[-1]["close"] == 10
    assert len(serve[1]) == calls
