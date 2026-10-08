"""Regression evidence for independent review R1/R2 and paper admission.

Synthetic facts exercise mechanisms, never certify a real source or revised oracle.
"""

import copy
import hashlib
import json
import math
from datetime import datetime as RealDatetime

import pytest

from ashare_data import Client, CoverageContract, DataError
from ashare_data.cli import main
import test_data_contracts as helpers
from test_data_contracts import SEC, payload, profile

clock = helpers.clock
serve = helpers.serve


def at(clock, value):
    clock.instant = RealDatetime.fromisoformat(value)


def interval(a, b, state="trading", visible="2026-09-01T00:00:00+08:00"):
    return {
        "security": SEC,
        "effective_start": a,
        "effective_end": b,
        "state": state,
        "available_at": visible,
        "evidence": "synthetic interval, not a vendor status",
    }


def intervals_profile():
    data = profile(sessions=[("09:30", "11:30"), ("13:00", "15:00")])
    data.update(
        schema_version=2,
        statuses=[
            interval("2026-09-30T00:00:00+08:00", "2026-09-30T10:00:00+08:00"),
            interval("2026-09-30T10:00:00+08:00", "2026-09-30T13:02:00+08:00", "suspended"),
            interval("2026-09-30T13:02:00+08:00", "2026-10-01T00:00:00+08:00"),
        ],
    )
    return data


def cache_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("old_count,new_count", [(1, 2), (2, 1), (1, None), (None, 1)])
def test_received_available_window_wins_both_sizes_and_exact_url(
    clock, serve, tmp_path, old_count, new_count
):
    client = Client(cache=tmp_path)
    at(clock, "2026-09-30T09:32:00+08:00")
    serve[0](payload(["2026-09-30 09:31:00", "2026-09-30 09:32:00"]))
    older = client.get_price(
        SEC,
        frequency="1m",
        count=old_count or 1,
        **({"end_date": "2026-09-30"} if old_count is None else {}),
    )
    at(clock, "2026-09-30T09:34:00+08:00")
    serve[0](payload(["2026-09-30 09:31:00", "2026-09-30 09:32:00"], close="11"))
    client.get_price(
        SEC,
        frequency="1m",
        count=new_count or 1,
        **({"end_date": "2026-09-30"} if new_count is None else {}),
    )
    pinned = client.at("2026-09-30T09:32:30+08:00", visibility="received")
    before, calls = cache_hashes(tmp_path), len(serve[1])
    value = pinned.get_price(SEC, frequency="1m", count=1)
    assert len(value) == 1 and value.iloc[0]["close"] == 10
    assert value.attrs["provenance"][0]["sha256"] == older.attrs["provenance"][0]["sha256"]
    assert not value.attrs["point_in_time_verified"]
    assert len(serve[1]) == calls and cache_hashes(tmp_path) == before


def test_no_eligible_window_keeps_visibility_refusal(clock, serve, tmp_path):
    at(clock, "2026-09-30T09:34:00+08:00")
    serve[0](payload(["2026-09-30 09:31:00"]))
    client = Client(cache=tmp_path)
    client.get_price(SEC, count=1, frequency="1m")
    calls = len(serve[1])
    with pytest.raises(DataError) as ex:
        client.at("2026-09-30T09:32:00+08:00", visibility="received").get_price(
            SEC, count=1, frequency="1m"
        )
    assert ex.value.code == "VISIBILITY_UNKNOWN" and len(serve[1]) == calls


@pytest.mark.parametrize("price", ["1e-400", "1e-324", "4e-324", "1e-320", "2e-308"])
def test_positive_decimal_outside_normal_binary64_rejected_before_cache(
    clock, serve, tmp_path, price
):
    rows = payload(["2026-09-30"])
    rows[0].update({key: price for key in ("open", "high", "low", "close")})
    serve[0](rows)
    with pytest.raises(DataError) as ex:
        Client(cache=tmp_path).get_price(SEC, count=1)
    assert ex.value.code == "SOURCE_SCHEMA_ERROR"
    assert not list(tmp_path.rglob("*.json")) and not (tmp_path / "objects").exists()


@pytest.mark.parametrize("price", ["2.2250738585072014e-308", "1e-307", "0.000001", "9.70"])
def test_representable_positive_prices_remain_usable_with_raw_text(clock, serve, price):
    rows = payload(["2026-09-30"])
    rows[0].update({key: price for key in ("open", "high", "low", "close")})
    serve[0](rows)
    frame = Client().get_price(SEC, count=1)
    assert all(
        math.isfinite(frame.iloc[0][k]) and frame.iloc[0][k] > 0
        for k in ("open", "high", "low", "close")
    )
    assert frame.attrs["provenance"][0]["source_rows"][0]["close"] == price


@pytest.mark.parametrize(
    "time,state",
    [
        ("09:29:59", "closed_market"),
        ("09:30:00", "tradable"),
        ("09:59:59", "tradable"),
        ("10:00:00", "suspended"),
        ("13:01:59", "suspended"),
        ("13:02:00", "tradable"),
        ("14:59:59", "tradable"),
        ("15:00:00", "closed_market"),
    ],
)
def test_interval_status_half_open_boundaries(time, state):
    result = CoverageContract(intervals_profile()).trading_status(
        SEC, "2026-09-30T" + time + "+08:00"
    )
    assert result["state"] == state and result["tradable"] == (state == "tradable")
    assert result["evidence_kind"] == "synthetic"


@pytest.mark.parametrize(
    "time,state",
    [
        ("11:29:59", "tradable"),
        ("11:30:00", "session_break"),
        ("12:00:00", "session_break"),
        ("13:00:00", "tradable"),
    ],
)
def test_lunch_is_independent_of_latest_bar_watermark(time, state):
    data = profile(sessions=[("09:30", "11:30"), ("13:00", "15:00")])
    c = CoverageContract(data)
    d = c.trading_status(SEC, "2026-09-30T" + time + "+08:00")
    assert d["state"] == state
    if state == "session_break":
        r = c.freshness(SEC, ["2026-09-30 11:30:00"], "2026-09-30T" + time + "+08:00")
        assert (
            r["source_watermark_status"] == "fresh"
            and not r["admissible"]
            and r["status"] == "session_break"
        )


def test_resumption_without_inventing_morning_facts_has_stale_and_fresh_positive():
    data = intervals_profile()
    data["statuses"] = [interval("2026-09-30T13:00:00+08:00", "2026-10-01T00:00:00+08:00")]
    c = CoverageContract(data)
    stale = c.freshness(SEC, ["2026-09-30 11:30:00"], "2026-09-30T13:05:00+08:00")
    fresh = c.freshness(SEC, ["2026-09-30 13:05:00"], "2026-09-30T13:05:00+08:00")
    assert stale["status"] == "stale" and not stale["admissible"]
    assert fresh["status"] == "fresh" and fresh["admissible"]
    assert c.trading_status(SEC, "2026-09-30T10:00:00+08:00")["state"] == "unknown"
    all_day = c.assess(
        SEC,
        ["2026-09-30 13:05:00"],
        "2026-09-30",
        "2026-09-30 15:00",
        as_of="2026-09-30T16:00:00+08:00",
    )
    assert all_day["status"] == "unknown" and not all_day["complete"]


def test_halt_slot_coverage_removes_only_evidenced_intervals():
    c = CoverageContract(intervals_profile())
    r = c.assess(
        SEC,
        ["2026-09-30 10:00:00", "2026-09-30 13:03:00"],
        "2026-09-30 10:00",
        "2026-09-30 13:03",
        as_of="2026-09-30T13:04:00+08:00",
    )
    assert r["complete"] and r["expected_count"] == 2 and not r["trade_totals_verified"]


@pytest.mark.parametrize(
    "change,code",
    [
        ("unknown", "TRADING_STATUS_UNKNOWN"),
        ("late_fact", "TRADING_STATUS_UNKNOWN"),
        ("within_bar", "PARTIAL_BAR_STATUS"),
    ],
)
def test_status_holes_late_facts_and_partial_bars_never_certify(change, code):
    data = intervals_profile()
    if change == "unknown":
        data["statuses"][1]["state"] = "unknown"
    if change == "late_fact":
        data["statuses"][1]["available_at"] = "2026-09-30T16:00:00+08:00"
    if change == "within_bar":
        data["statuses"][0]["effective_end"] = "2026-09-30T10:00:30+08:00"
        data["statuses"][1]["effective_start"] = "2026-09-30T10:00:30+08:00"
    r = CoverageContract(data).assess(
        SEC, [], "2026-09-30 10:01", "2026-09-30 10:01", as_of="2026-09-30T10:02:00+08:00"
    )
    assert r["status"] == "unknown" and not r["complete"] and r["unknown"][0]["code"] == code


@pytest.mark.parametrize("change", ["overlap", "empty", "naive", "ambiguous_date"])
def test_invalid_interval_facts_reject(change):
    data = intervals_profile()
    if change == "overlap":
        data["statuses"][1]["effective_start"] = "2026-09-30T09:59:00+08:00"
    if change == "empty":
        data["statuses"][1]["effective_end"] = data["statuses"][1]["effective_start"]
    if change == "naive":
        data["statuses"][1]["effective_start"] = "2026-09-30T10:00:00"
    if change == "ambiguous_date":
        data["statuses"][1]["date"] = "2026-09-30"
    with pytest.raises(DataError):
        CoverageContract(data)


def test_same_day_late_resumption_does_not_leak_status_evidence():
    data = intervals_profile()
    data["statuses"][2]["available_at"] = "2026-09-30T13:04:00+08:00"
    c = CoverageContract(data)
    r = c.trading_status(SEC, "2026-09-30T13:03:00+08:00")
    assert r["state"] == "unknown" and r["status_evidence"] is None
    assert c.trading_status(SEC, "2026-09-30T13:04:00+08:00")["tradable"]


def test_future_only_labels_are_unknown_not_stale():
    r = CoverageContract(profile()).freshness(
        SEC, ["2026-09-30 09:34:00"], "2026-09-30T09:33:00+08:00"
    )
    assert (
        r["status"] == "unknown" and not r["admissible"] and "FUTURE_SOURCE_LABEL" in r["reasons"]
    )


def test_future_extra_source_label_does_not_remove_valid_current_bar():
    r = CoverageContract(profile()).freshness(
        SEC, ["2026-09-30 09:33:00", "2026-09-30 09:34:00"], "2026-09-30T09:33:00+08:00"
    )
    assert r["status"] == "fresh" and r["admissible"] and r["future_source_labels"]


def test_public_paper_gate_post_resumption_positive_and_old_window_negative(clock, serve):
    data = intervals_profile()
    c = Client(coverage_contract=CoverageContract(data))
    at(clock, "2026-09-30T13:05:00+08:00")
    serve[0](payload(["2026-09-30 09:59:00", "2026-09-30 13:05:00"]))
    f = c.get_price(SEC, count=1, frequency="1m", require_fresh=True, require_tradable=True)
    fresh = f.attrs["provenance"][0]["freshness"]
    assert (
        fresh["admissible"]
        and fresh["source_watermark_status"] == fresh["selected_window_status"] == "fresh"
    )
    with pytest.raises(DataError) as ex:
        c.get_price(SEC, end_date="2026-09-30 09:59", count=1, frequency="1m", require_fresh=True)
    assert ex.value.code == "STALE_SELECTED_WINDOW"
    assert (
        ex.value.details["source_watermark_status"] == "fresh"
        and not ex.value.details["admissible"]
    )
    research = c.get_price(SEC, end_date="2026-09-30 09:59", count=1, frequency="1m")
    assert len(research) == 1 and not research.attrs["provenance"][0]["freshness"]["admissible"]


@pytest.mark.parametrize("time,code", [("10:00:00", "SUSPENDED"), ("15:00:00", "MARKET_CLOSED")])
def test_tradable_gate_fails_before_io(clock, serve, time, code):
    at(clock, "2026-09-30T" + time + "+08:00")
    with pytest.raises(DataError) as ex:
        Client(coverage_contract=CoverageContract(intervals_profile())).get_price(
            SEC, count=1, frequency="1m", require_tradable=True
        )
    assert ex.value.code == code and not serve[1]


def test_missing_contract_status_is_explicit_and_offline(clock, serve):
    result = Client().trading_status(SEC, as_of="2026-09-30T10:00:00+08:00")
    assert result["state"] == "unknown" and not result["tradable"] and not serve[1]


def test_fixed_context_trading_status_cannot_override_clock(clock, serve, tmp_path):
    c = Client(cache=tmp_path, coverage_contract=CoverageContract(intervals_profile())).at(
        "2026-09-30T13:03:00+08:00", visibility="assumed"
    )
    assert c.trading_status(SEC)["tradable"] and not serve[1]
    with pytest.raises(DataError) as ex:
        c.trading_status(SEC, as_of="2026-09-30T13:04:00+08:00")
    assert ex.value.code == "INVALID_REQUEST"


def test_v2_input_and_outputs_detached():
    data = intervals_profile()
    c = CoverageContract(data)
    data["statuses"][0]["state"] = "suspended"
    r = c.trading_status(SEC, "2026-09-30T09:35:00+08:00")
    r["status_evidence"]["evidence"] = "changed"
    assert c.trading_status(SEC, "2026-09-30T09:35:00+08:00")["tradable"]
    assert (
        c.trading_status(SEC, "2026-09-30T09:35:00+08:00")["status_evidence"]["evidence"]
        != "changed"
    )


def test_coverage_empty_states_and_query_scope_are_explicit():
    data = profile(state="suspended")
    c = CoverageContract(data)
    r = c.assess(SEC, [], "2026-09-30", "2026-09-30 15:00")
    assert r["status"] == "suspended" and r["complete"] and not r["tradable"]
    data["calendar"][0].update(is_open=False, sessions=[])
    r = CoverageContract(data).assess(SEC, [], "2026-09-30", "2026-09-30 15:00")
    assert r["status"] == "closed_market" and r["complete"] and not r["tradable"]
    c = CoverageContract(profile())
    labels = [f"2026-09-30 09:{m}:00" for m in (31, 32, 33)] + ["2026-09-30 12:00:00"]
    r = c.assess(SEC, labels, "2026-09-30 09:30", "2026-09-30 09:33")
    assert r["complete"] and r["out_of_request_labels"] == ["2026-09-30T12:00:00+08:00"]
    assert not c.assess(SEC, labels, "2026-09-30 09:30", "2026-09-30 12:01")["complete"]


def test_postmarket_status_gap_is_unknown_without_silent_extension():
    data = intervals_profile()
    data["calendar"][0]["sessions"].append(
        {
            "id": "post",
            "start": "2026-09-30T15:05:00+08:00",
            "end": "2026-09-30T15:06:00+08:00",
            "mapping": "end_labels",
        }
    )
    data["statuses"][-1]["effective_end"] = "2026-09-30T15:00:00+08:00"
    r = CoverageContract(data).assess(
        SEC, [], "2026-09-30 15:05", "2026-09-30 15:06", as_of="2026-09-30T16:00:00+08:00"
    )
    assert r["status"] == "unknown" and r["expected_count"] == 0 and not r["complete"]
    valid = copy.deepcopy(data)
    valid["statuses"].append(interval("2026-09-30T15:05:00+08:00", "2026-09-30T15:06:00+08:00"))
    r = CoverageContract(valid).assess(
        SEC,
        ["2026-09-30 15:06:00"],
        "2026-09-30 15:05",
        "2026-09-30 15:06",
        as_of="2026-09-30T16:00:00+08:00",
    )
    assert r["complete"] and r["expected_count"] == 1


def test_trading_status_cli_and_require_flag(clock, serve, tmp_path, capsys):
    p = tmp_path / "contract.json"
    p.write_text(json.dumps(intervals_profile()))
    for time, code, state in [
        ("09:35", 0, "tradable"),
        ("10:00", 2, "suspended"),
        ("15:00", 2, "closed_market"),
    ]:
        assert (
            main(
                [
                    "trading-status",
                    SEC,
                    "--as-of",
                    "2026-09-30T" + time + ":00+08:00",
                    "--coverage-contract",
                    str(p),
                ]
            )
            == code
        )
        assert json.loads(capsys.readouterr().out)["state"] == state
    assert not serve[1]
