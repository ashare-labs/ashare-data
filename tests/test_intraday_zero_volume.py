"""Synthetic compound failures through production capture, never market evidence."""

import json

import pytest


from ashare_data import intraday_observation as obs, phase_observation as phase
from ashare_data.intraday_refresh import CLOCK_BLOCKER
from ashare_data.model import canonical
from test_intraday_observation import DAY, sources

pytestmark = pytest.mark.usefixtures("synthetic_action_baseline")

ZERO = "INTRADAY_MINUTE_PRICE_OR_VOLUME:600000"


def capture(monkeypatch, root, mode, shape="selected_zero"):
    sources(monkeypatch, at=DAY + "T10:04:29.900000+08:00")
    original = phase.fetch
    calls, quote_calls = [], 0

    def fetch(url, referer):
        nonlocal quote_calls
        calls.append(url)
        raw = original(url, referer)
        if "getKLineData" in url:
            rows = json.loads(raw)
            selected = dict(rows[0], day=DAY + " 10:03:00")
            older = dict(rows[0], day=DAY + " 10:02:00")
            if shape == "earlier_zero":
                if "600000" in url:
                    older["volume"] = "0"
                rows = [older, selected]
            elif shape == "later_unshared_zero":
                rows = [older]
                if "600000" in url:
                    rows.append(dict(selected, volume="0"))
            else:
                if "600000" in url:
                    selected["volume"] = "0"
                rows = [older, selected] if shape == "selected_zero_with_history" else [selected]
            return canonical(rows)
        if "yunhq" in url and "/600000?" in url:
            quote_calls += 1
            if mode != "zero_only" and (quote_calls == 1 or mode == "persists"):
                value = json.loads(raw)
                value["time"] = 100430
                return canonical(value)
        return raw

    monkeypatch.setattr(phase, "fetch", fetch)
    attempt = obs.capture(root, "execution", DAY, DAY + "T10:00:31+08:00")
    packet = obs.read(root, attempt["capture_id"])
    (root / "CONTROLLED_TEST_ONLY.json").write_bytes(
        canonical(
            dict(
                market_result=False,
                classification="synthetic_compound_failure",
                live_network_requests=0,
                mode=mode,
                shape=shape,
                requests=len(calls),
                blockers=packet["assessment"]["blockers"],
                refresh=packet["refresh"],
                ready=packet["assessment"]["ready"],
                capture_id=attempt["capture_id"],
            )
        )
    )
    return attempt, packet, calls


@pytest.mark.parametrize("mode", ["zero_only", "recovers", "persists"])
def test_selected_zero_retains_both_causes_without_quote_refresh(tmp_path, monkeypatch, mode):
    attempt, packet, calls = capture(monkeypatch, tmp_path, mode)
    assert len(calls) == 33
    assert attempt["status"] == "blocked" and packet["assessment"]["ready"] is False
    assert packet["refresh"] == dict(rounds=0, retryable=False, status="blocked_other_evidence")
    assert packet["assessment"]["blockers"] == sorted(
        [ZERO] if mode == "zero_only" else [ZERO, CLOCK_BLOCKER]
    )


@pytest.mark.parametrize(
    "shape,minute",
    [
        ("earlier_zero", "10:03:00"),
        ("later_unshared_zero", "10:02:00"),
    ],
)
def test_zero_outside_selected_common_minute_keeps_existing_semantics(
    tmp_path, monkeypatch, shape, minute
):
    attempt, packet, calls = capture(monkeypatch, tmp_path, "recovers", shape)
    assert attempt["status"] == "observed" and len(calls) == 37
    assert packet["assessment"]["blockers"] == []
    assert packet["refresh"] == dict(rounds=1, retryable=False, status="ready")
    assert all(
        row["execution"]["minute_end"] == DAY + "T" + minute + "+08:00"
        and row["execution"]["volume"] > 0
        for row in packet["assessment"]["rows"]
    )


def test_selected_zero_does_not_fall_back_to_earlier_positive_volume(tmp_path, monkeypatch):
    attempt, packet, calls = capture(
        monkeypatch, tmp_path, "zero_only", "selected_zero_with_history"
    )
    assert attempt["status"] == "blocked" and len(calls) == 33
    assert packet["assessment"]["blockers"] == [ZERO]
    row = next(r for r in packet["assessment"]["rows"] if r["security"].startswith("600000"))
    assert row["execution"]["volume"] == 0
    assert row["execution"]["minute_end"] == DAY + "T10:03:00+08:00"
