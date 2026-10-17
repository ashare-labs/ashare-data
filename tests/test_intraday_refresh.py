"""Directed precision, retry and journal counterexamples, not market evidence."""

from datetime import datetime, timedelta
import json

import pytest


from ashare_data.observations import intraday as obs
from ashare_data.observations import phases as phase
from ashare_data.core.model import DataError, canonical
from ashare_data.providers.sse_snapshot import snapshot
from test_intraday_observation import DAY, sources

pytestmark = pytest.mark.usefixtures("synthetic_action_baseline")


def value():
    return dict(
        total=1,
        begin=0,
        end=1,
        date=20261014,
        time=100330,
        list=[["600000", "浦发银行", 10, 10, 10, 11, 9, "T111"]],
    )


@pytest.mark.parametrize("delta_us", [-1000000, -108526, -74989, -64712, -1, 0, 1, 999999])
def test_encoded_second_has_no_implicit_rounding_tolerance(delta_us):
    at = datetime.fromisoformat(DAY + "T10:03:30+08:00")
    receipt = at + timedelta(microseconds=delta_us)
    # This algebraic equivalence explains why simply truncating local precision
    # cannot fix the actual three ahead-of-receipt observations.
    assert (at <= receipt) == (at <= receipt.replace(microsecond=0))
    if delta_us < 0:
        with pytest.raises(DataError, match="官方快照不是当日或晚于收讫"):
            snapshot(value(), "600000", DAY, receipt.isoformat())
    else:
        assert (
            snapshot(value(), "600000", DAY, receipt.isoformat())["source_as_of"] == at.isoformat()
        )


def test_equal_utc_instant_is_valid_but_old_day_and_actual_future_remain_invalid():
    assert snapshot(value(), "600000", DAY, DAY + "T02:03:30+00:00")
    for change in [dict(date=20261013), dict(date=20261015), dict(time=100331)]:
        with pytest.raises(DataError):
            snapshot(dict(value(), **change), "600000", DAY, DAY + "T10:03:30+08:00")


def capture(mp, root, *, recover=True, damage=None, kind="admission"):
    sources(mp, at=DAY + "T10:03:29.900000+08:00", damage=damage)
    original = phase.fetch
    calls = []
    quote_calls = 0

    def fetch(url, referer):
        nonlocal quote_calls
        calls.append(url)
        raw = original(url, referer)
        if "yunhq" in url and "/600000?" in url:
            quote_calls += 1
            v = json.loads(raw)
            if quote_calls == 1 or not recover:
                v["time"] = 100330  # 100 ms ahead; never admitted
            return canonical(v)
        return raw

    mp.setattr(phase, "fetch", fetch)
    attempt = obs.capture(root, kind, DAY, DAY + "T10:00:00+08:00")
    return attempt, obs.read(root, attempt["capture_id"]), calls


@pytest.mark.parametrize("kind,initial", [("admission", 27), ("execution", 33)])
def test_fresh_responses_recover_through_real_collector_and_parser(
    tmp_path, monkeypatch, kind, initial
):
    attempt, packet, calls = capture(monkeypatch, tmp_path, kind=kind)
    assert attempt["status"] == "observed"
    assert len(calls) == initial + 4
    assert packet["refresh"] == dict(rounds=1, retryable=False, status="ready")
    assert not packet["assessment"]["source_publication_time_verified"]
    history = [r for r in packet["responses"] if r["name"] == "sse_quote_600000"]
    assert len(history) == 2 and history[0]["sha256"] != history[1]["sha256"]
    assert packet["sources"]["sse_quote_600000"]["sha256"] == history[1]["sha256"]
    # The invalid first response still exists and still fails the actual parser.
    old = json.loads((tmp_path / "blobs" / history[0]["sha256"]).read_bytes())
    with pytest.raises(DataError):
        snapshot(old, "600000", DAY, history[0]["observed_at"])


def test_unchanged_forward_labels_exhaust_finite_retry_without_becoming_valid(
    tmp_path, monkeypatch
):
    attempt, packet, calls = capture(monkeypatch, tmp_path, recover=False)
    assert len(calls) == 35 and attempt["status"] == "blocked"
    assert packet["refresh"] == dict(rounds=2, retryable=True, status="retry_limit_reached")
    assert packet["assessment"]["ready"] is False


def test_positive_halt_is_not_masked_by_clock_retry(tmp_path, monkeypatch):
    attempt, packet, calls = capture(monkeypatch, tmp_path, damage="halt")
    assert len(calls) == 27 and attempt["status"] == "blocked"
    assert packet["refresh"]["status"] == "blocked_other_evidence"


@pytest.mark.parametrize("damage", ["old_minute", "forming_minute"])
def test_invalid_minute_is_checked_even_when_quote_clock_also_fails(tmp_path, monkeypatch, damage):
    attempt, packet, calls = capture(monkeypatch, tmp_path, damage=damage, kind="execution")
    assert len(calls) == 33 and attempt["status"] == "blocked"
    assert packet["refresh"]["status"] == "blocked_other_evidence"
    assert "INTRADAY_NO_COMMON_POST_INTENT_MINUTE" in packet["assessment"]["blockers"]


@pytest.mark.parametrize("damage", ["delete_first_response", "replace_trigger_with_old_day"])
def test_reader_rejects_hidden_or_ineligible_superseded_receipts(tmp_path, monkeypatch, damage):
    _, packet, _ = capture(monkeypatch, tmp_path)
    first = next(r for r in packet["responses"] if r["name"] == "sse_quote_600000")
    if damage == "delete_first_response":
        packet["responses"].remove(first)
    else:
        value = json.loads((tmp_path / "blobs" / first["sha256"]).read_bytes())
        value["date"] = 20261013
        raw = canonical(value)
        first["sha256"] = obs.sha(raw)
        obs.immutable_write(tmp_path / "blobs" / first["sha256"], raw)
    raw = canonical(packet)
    ident = obs.sha(raw)
    obs.immutable_write(tmp_path / "packets" / (ident + ".json"), raw)
    with pytest.raises(DataError) as error:
        obs.read(tmp_path, ident)
    assert error.value.code == (
        "INTRADAY_REFRESH_SEQUENCE"
        if damage == "delete_first_response"
        else "INTRADAY_REFRESH_TRIGGER"
    )


def test_retry_cannot_extend_the_existing_request_budget(tmp_path, monkeypatch):
    sources(monkeypatch, at=DAY + "T10:03:29.900000+08:00")
    original = phase.fetch
    clock = obs.now()
    calls = 0

    def fetch(url, referer):
        nonlocal calls
        calls += 1
        raw = original(url, referer)
        if "yunhq" in url and "/600000?" in url:
            v = json.loads(raw)
            v["time"] = 100330
            return canonical(v)
        return raw

    # Keep every first-pass receipt unchanged; the next budget check reaches
    # the original 225 s cutoff, before any refresh HTTP request is issued.
    times = iter([clock] * (1 + 27 * 3) + [clock + timedelta(seconds=225)] * 2)
    monkeypatch.setattr(obs, "now", lambda: next(times))
    monkeypatch.setattr(phase, "fetch", fetch)
    attempt = obs.capture(tmp_path, "admission", DAY, DAY + "T10:00:00+08:00")
    assert attempt["status"] == "blocked" and attempt["capture_id"] is None
    assert attempt["error"]["code"] == "INTRADAY_BUDGET"
    assert calls == len(attempt["responses"]) == 27
    assert attempt["refresh"] == dict(rounds=0, retryable=False, status="budget_exhausted")
