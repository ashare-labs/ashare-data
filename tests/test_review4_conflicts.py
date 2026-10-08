"""Conflict scope through public API; receipts are captured, never edited in place."""

import itertools
import json

import pytest

from ashare_data import Client, CoverageContract, DataError, live, transport
import test_data_contracts as helpers
from test_data_contracts import SEC, payload, profile
from test_review3_windows import at, cache_hashes, labels

clock = helpers.clock


@pytest.fixture
def serve(monkeypatch):
    calls = []

    def install(data):
        def read(*args):
            calls.append(args[0])
            return data if isinstance(data, bytes) else json.dumps(data).encode()

        monkeypatch.setattr(transport, "public_read", read)

    return install, calls


def collect(client, clock, serve, time, rows, mode, rank, *, security=SEC, frequency="1m"):
    at(clock, time)
    serve[0](rows)
    kwargs = {"count": 1 if mode == "same" or rank % 2 == 0 else 2}
    if mode == "exact":
        kwargs = {"count": 1, **({"end_date": "2026-09-30"} if rank % 2 == 0 else {})}
    client.get_price(security, frequency=frequency, **kwargs)


def rows(close="9", *, last=32):
    data = payload(labels([31, last]), "9")
    data[0]["close"] = close
    for row in data:
        row["high"] = "13"
        row["low"] = "8"
    return data


def context(client, clock, cutoff="09:36:30", visibility="received"):
    at(clock, "16:00:00")
    return client.at("2026-09-30T" + cutoff + "+08:00", visibility=visibility)


def selected(q, **kwargs):
    return q.get_price(SEC, frequency="1m", count=1, end_date="2026-09-30 09:31", **kwargs)


def conflict(call):
    with pytest.raises(DataError) as err:
        call()
    assert err.value.code == "OBSERVATION_CONFLICT"
    details = err.value.details
    assert details["source_labels"] == labels([31])
    active = [x for x in details["observation_conflicts"] if x["state"] == "unresolved"]
    assert len(active) == 1 and active[0]["resolved_by"] == []
    assert len({r["sha256"] for r in active[0]["observations"]}) >= 2
    return details


def audit(frame, root, resolved):
    history = frame.attrs["provenance"][0]["observation_conflicts"]
    assert len(history) == int(resolved)
    if resolved:
        event = history[0]
        assert event["state"] == "resolved" and event["source_label"] == labels([31])[0]
        assert {x["source_row"]["close"] for x in event["observations"]} == {"10", "11"}
        assert {x["source_row"]["close"] for x in event["resolved_by"]} == {"12"}
        for obs in event["observations"] + event["resolved_by"]:
            saved = json.loads(
                (root / "observations" / (obs["observation_id"] + ".json")).read_text()
            )
            assert saved["sha256"] == obs["sha256"] and saved["url"] == obs["url"]
            assert obs["source_row"] in json.loads((root / "objects" / obs["sha256"]).read_bytes())


@pytest.mark.parametrize("mode", ["same", "different", "exact"])
@pytest.mark.parametrize("cutoff", ["09:34:30", "09:35:00", "09:36:00"])
@pytest.mark.parametrize("visibility", ["received", "assumed"])
@pytest.mark.parametrize("resolution", ["none", "selected_label", "other_label"])
@pytest.mark.parametrize("query", ["end_count", "exact_range", "latest"])
def test_conflict_scope_cross_product(
    clock, serve, tmp_path, mode, cutoff, visibility, resolution, query
):
    client = Client(cache=tmp_path, cache_mode="refresh")
    for rank, (t, value) in enumerate((("09:34:00", "9"), ("09:35:00", "10"), ("09:35:00", "11"))):
        collect(client, clock, serve, t, rows(value), mode, rank)
    if resolution != "none":
        data = rows("12")
        if resolution == "other_label":
            data = [dict(data[0], day=label) for label in labels([32, 33])]
        collect(client, clock, serve, "09:36:00", data, mode, 3)
    q = context(client, clock, cutoff, visibility)
    before, calls = cache_hashes(tmp_path), len(serve[1])
    if query == "latest":
        frame = q.get_price(SEC, frequency="1m", count=1)
        new_other = resolution == "other_label" and (
            visibility == "assumed" or cutoff == "09:36:00"
        )
        assert frame.index[-1].minute == (33 if new_other else 32)
        assert frame.iloc[-1]["close"] == (12 if new_other else 9)
        audit(frame, tmp_path, False)
    else:
        call = (
            (lambda: selected(q))
            if query == "end_count"
            else (
                lambda: q.get_price(
                    SEC, frequency="1m", start_date="2026-09-30 09:31", end_date="2026-09-30 09:31"
                )
            )
        )
        old = visibility == "received" and cutoff == "09:34:30"
        healed = resolution == "selected_label" and (
            visibility == "assumed" or cutoff == "09:36:00"
        )
        if old or healed:
            frame = call()
            assert frame.iloc[-1]["close"] == (9 if old else 12)
            audit(frame, tmp_path, healed)
        else:
            conflict(call)
    assert len(serve[1]) == calls and cache_hashes(tmp_path) == before


@pytest.mark.parametrize("mode", ["same", "different"])
@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("healed", [False, True])
def test_observation_insertion_order_cannot_choose_ambiguous_value(
    clock, serve, tmp_path, mode, order, healed
):
    client = Client(cache=tmp_path, cache_mode="refresh")
    receipts = [
        ("09:35:00", "10"),
        ("09:35:00", "11"),
        ("09:36:00" if healed else "09:35:00", "12" if healed else "10"),
    ]
    for rank in order:
        time, close = receipts[rank]
        collect(client, clock, serve, time, rows(close), mode, rank)
    q = context(client, clock)
    before = cache_hashes(tmp_path)
    if healed:
        frame = selected(q)
        assert frame.iloc[0]["close"] == 12
        audit(frame, tmp_path, True)
    else:
        conflict(lambda: selected(q))
    assert cache_hashes(tmp_path) == before


@pytest.mark.parametrize("dimension", ["security", "frequency", "endpoint"])
@pytest.mark.parametrize("visibility", ["received", "assumed"])
def test_unrelated_stream_conflicts_do_not_block_or_leak(
    clock, serve, tmp_path, monkeypatch, dimension, visibility
):
    c = Client(cache=tmp_path, cache_mode="refresh")
    collect(c, clock, serve, "09:34:00", rows(), "same", 0)
    original = live.KLINE_URL
    if dimension == "endpoint":
        monkeypatch.setattr(live, "KLINE_URL", original + "/isolated-test")
    for value in ("10", "11"):
        collect(
            c,
            clock,
            serve,
            "09:35:00",
            rows(value),
            "same",
            0,
            security="000001.XSHE" if dimension == "security" else SEC,
            frequency="5m" if dimension == "frequency" else "1m",
        )
    monkeypatch.setattr(live, "KLINE_URL", original)
    q = context(c, clock, visibility=visibility)
    before, calls = cache_hashes(tmp_path), len(serve[1])
    f = selected(q)
    assert f.iloc[0]["close"] == 9
    audit(f, tmp_path, False)
    assert not f.attrs["point_in_time_verified"] and len(serve[1]) == calls
    assert cache_hashes(tmp_path) == before


@pytest.mark.parametrize("mode", ["same", "different"])
@pytest.mark.parametrize("visibility", ["received", "assumed"])
def test_fixed_healthy_conflicting_and_resolved_views_remain_immutable(
    clock, serve, tmp_path, mode, visibility
):
    c = Client(cache=tmp_path, cache_mode="refresh")
    collect(c, clock, serve, "09:34:00", rows(), mode, 0)
    old = context(c, clock, "09:36:30", visibility)
    for rank, value in enumerate(("10", "11")):
        collect(c, clock, serve, "09:35:00", rows(value), mode, rank)
    ambiguous = context(c, clock, "09:36:30", visibility)
    collect(c, clock, serve, "09:36:00", rows("12"), mode, 0)
    healed = context(c, clock, "09:36:30", visibility)
    before, calls = cache_hashes(tmp_path), len(serve[1])
    for _ in range(2):
        assert selected(old).iloc[0]["close"] == 9
        conflict(lambda: selected(ambiguous))
        assert selected(healed).iloc[0]["close"] == 12
    assert len({q.transport.snapshot_id for q in (old, ambiguous, healed)}) == 3
    assert len(serve[1]) == calls and cache_hashes(tmp_path) == before


@pytest.mark.parametrize("mode", ["same", "different"])
@pytest.mark.parametrize("field", ["volume", "amount"])
def test_unrequested_field_conflict_still_makes_source_row_ambiguous(
    clock, serve, tmp_path, mode, field
):
    c = Client(cache=tmp_path, cache_mode="refresh")
    for rank, value in enumerate(("100", "200")):
        data = rows()
        data[0][field] = value
        collect(c, clock, serve, "09:35:00", json.dumps(data).encode(), mode, rank)
    q = context(c, clock)
    conflict(lambda: selected(q, fields=["close"]))


@pytest.mark.parametrize("mode", ["same", "different"])
def test_different_raw_bytes_with_equal_decoded_rows_are_not_value_conflict(
    clock, serve, tmp_path, mode
):
    c = Client(cache=tmp_path, cache_mode="refresh")
    for rank, indent in enumerate((None, 2)):
        data = json.dumps(rows(), indent=indent).encode()
        collect(c, clock, serve, "09:35:00", data, mode, rank)
    q = context(c, clock)
    f = selected(q)
    assert f.iloc[0]["close"] == 9
    audit(f, tmp_path, False)
    assert len(list((tmp_path / "objects").iterdir())) == 2


@pytest.mark.parametrize("mode", ["same", "different"])
def test_new_conflict_after_a_resolution_is_not_silently_healed(clock, serve, tmp_path, mode):
    c = Client(cache=tmp_path, cache_mode="refresh")
    for rank, (time, value) in enumerate(
        (("09:35:00", "10"), ("09:35:00", "11"), ("09:36:00", "12"), ("09:36:00", "13"))
    ):
        collect(c, clock, serve, time, rows(value), mode, rank)
    q = context(c, clock)
    d = conflict(lambda: selected(q))
    assert [x["state"] for x in d["observation_conflicts"]] == [
        "superseded_by_conflict",
        "unresolved",
    ]
    assert all(x["resolved_by"] == [] for x in d["observation_conflicts"])


@pytest.mark.parametrize("mode", ["same", "different"])
@pytest.mark.parametrize("scope", ["selected", "outside", "resolved"])
def test_coverage_query_scopes_value_conflicts_too(clock, serve, tmp_path, mode, scope):
    c = Client(cache=tmp_path, cache_mode="refresh", coverage_contract=CoverageContract(profile()))
    for rank, value in enumerate(("10", "11")):
        collect(c, clock, serve, "09:35:00", rows(value), mode, rank)
    if scope == "resolved":
        collect(c, clock, serve, "09:36:00", rows("12"), mode, 0)
    q = context(c, clock)
    date = "2026-09-30 09:32" if scope == "outside" else "2026-09-30 09:31"

    def call():
        return q.coverage(SEC, date, date)

    if scope == "selected":
        conflict(call)
    else:
        report = call()
        assert report["complete"] and len(report["observation_conflicts"]) == int(
            scope == "resolved"
        )


@pytest.mark.parametrize("mode", ["same", "different"])
@pytest.mark.parametrize("security_order", [(SEC, "000001.XSHE"), ("000001.XSHE", SEC)])
def test_multi_security_query_refuses_selected_conflict_in_any_position(
    clock, serve, tmp_path, mode, security_order
):
    c = Client(cache=tmp_path, cache_mode="refresh")
    for rank, value in enumerate(("10", "11")):
        collect(c, clock, serve, "09:35:00", rows(value), mode, rank)
    collect(c, clock, serve, "09:35:00", rows(), mode, 0, security="000001.XSHE")
    q = context(c, clock)
    conflict(
        lambda: q.get_price(security_order, frequency="1m", count=1, end_date="2026-09-30 09:31")
    )
