"""Synthetic transport/clock fixtures, never counted as real calendar days."""

from datetime import date, datetime, timedelta, timezone
import json
import multiprocessing
import os
import signal

import pytest

from ashare_data import DataError, live, transport
from ashare_data import daily_observation as observation

SECURITIES = ["600000.XSHG", "600036.XSHG", "601318.XSHG"]


@pytest.fixture
def upstream(monkeypatch):
    state = {"now": datetime(2026, 10, 12, 14, tzinfo=timezone.utc), "calls": [], "fail": False}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"].astimezone(tz) if tz else state["now"].replace(tzinfo=None)

    monkeypatch.setattr(live, "datetime", Clock)
    monkeypatch.setattr(transport, "datetime", Clock)
    monkeypatch.setattr(observation, "now", lambda: state["now"])
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(285)]
    days = [d for d in days if d.weekday() < 5][-121:]
    state["rows"] = [
        {
            "day": d.isoformat(),
            "open": "10.00",
            "high": "10.30",
            "low": "9.80",
            "close": "10.10",
            "volume": "100000",
        }
        for d in days
    ]
    state["calendar"] = (
        "2026年休市安排 "
        + " ".join(f"1月{i}日（星期一）至1月{i}日（星期一）休市" for i in range(1, 8))
    ).encode()

    def read(url, timeout):
        state["calls"].append(url)
        if state["fail"]:
            raise DataError("NETWORK_ERROR", "synthetic disconnected transport")
        return state["calendar"] if url == live.CALENDAR_URL else json.dumps(state["rows"]).encode()

    monkeypatch.setattr(transport, "public_read", read)
    return state


def capture(root):
    return observation.capture(root, securities=SECURITIES, signal_day="2026-10-12")


def test_current_observation_offline_reopen_and_duplicate(upstream, tmp_path):
    first = capture(tmp_path)
    assert first["status"] == "observed", first
    cap = first["capture_id"]
    original = (tmp_path / "packets" / (cap + ".json")).read_bytes()
    packet = observation.read(tmp_path, cap)
    assert packet["calendar"]["next_open"] == "2026-10-13"
    assert packet["execution_permission"] is False and packet["finality_verified"] is False
    assert packet["securities"][SECURITIES[0]]["trading_state"] is None
    second = capture(tmp_path)
    assert second["status"] == "observed" and second["capture_id"] != cap
    assert observation.read(tmp_path, second["capture_id"])["revisions"] == []
    upstream["fail"] = True
    assert observation.read(tmp_path, cap) == packet
    assert (tmp_path / "packets" / (cap + ".json")).read_bytes() == original


@pytest.mark.parametrize(
    "at,day,code",
    [
        (
            datetime(2026, 10, 12, 9, tzinfo=timezone.utc),
            "2026-10-12",
            "OBSERVATION_WAIT_PUBLICATION",
        ),
        (datetime(2026, 10, 12, 14, tzinfo=timezone.utc), "2026-10-13", "OBSERVATION_NOT_TODAY"),
        (datetime(2026, 10, 12, 14, tzinfo=timezone.utc), "2026-10-09", "OBSERVATION_NOT_TODAY"),
    ],
)
def test_time_gate_precedes_network(upstream, tmp_path, at, day, code):
    upstream["now"] = at
    result = observation.capture(tmp_path, securities=SECURITIES, signal_day=day)
    assert result["error"]["code"] == code and not upstream["calls"]
    assert not list((tmp_path / "packets").glob("*.json"))


def test_network_failure_reconnect_is_explicit(upstream, tmp_path):
    upstream["fail"] = True
    failed = capture(tmp_path)
    assert failed["status"] == "blocked" and failed["error"]["code"] == "NETWORK_ERROR"
    assert len(upstream["calls"]) == 1
    upstream["fail"] = False
    assert capture(tmp_path)["status"] == "observed"
    assert (
        json.loads((tmp_path / "attempts" / (failed["attempt_id"] + ".json")).read_bytes())
        == failed
    )


def test_missing_late_and_internal_gap_do_not_fill(upstream, tmp_path):
    last = upstream["rows"].pop()
    assert capture(tmp_path)["error"]["code"] == "OBSERVATION_WAIT_LATEST"
    upstream["rows"].append(last)
    upstream["rows"].pop(-20)
    result = capture(tmp_path)
    assert result["error"]["code"] == "OBSERVATION_HISTORY_GAP", result
    assert not list((tmp_path / "packets").glob("*.json"))


def test_revision_keeps_original_and_blocks_new_signal(upstream, tmp_path):
    first = capture(tmp_path)
    original = observation.read(tmp_path, first["capture_id"])
    upstream["rows"][-1]["close"] = "10.20"
    second = capture(tmp_path)
    assert second["status"] == "revision_blocked"
    changed = observation.read(tmp_path, second["capture_id"])
    assert not changed["signal_ready"] and len(changed["revisions"][0]["changes"]) == 3
    assert observation.read(tmp_path, first["capture_id"]) == original


@pytest.mark.parametrize("stage", ["blobs", "packets"])
def test_sigkill_recovery_is_offline_and_idempotent(upstream, tmp_path, stage):
    def worker():
        original = observation.immutable_write

        def killed(path, content):
            original(path, content)
            if path.parent.name == stage:
                os.kill(os.getpid(), signal.SIGKILL)

        observation.immutable_write = killed
        capture(tmp_path)

    process = multiprocessing.get_context("fork").Process(target=worker)
    process.start()
    process.join(10)
    assert process.exitcode == -signal.SIGKILL
    upstream["fail"] = True
    result = observation.recover(tmp_path)
    assert len(result) == 1
    assert result[0]["status"] == ("observed" if stage == "packets" else "interrupted")
    assert observation.recover(tmp_path) == []


def test_corrupt_source_cannot_be_reopened_or_implicitly_repaired(upstream, tmp_path):
    cap = capture(tmp_path)["capture_id"]
    packet = observation.read(tmp_path, cap)
    blob = tmp_path / "blobs" / packet["calendar"]["source"]["sha256"]
    blob.chmod(0o644)
    blob.write_bytes(b"different")
    with pytest.raises(DataError) as error:
        observation.read(tmp_path, cap)
    assert error.value.code == "OBSERVATION_CORRUPT"


def test_second_collector_cannot_pass_writer_lock(upstream, tmp_path):
    with observation.writer(tmp_path):
        with pytest.raises(DataError) as error:
            capture(tmp_path)
    assert error.value.code == "OBSERVATION_BUSY"
    assert upstream["calls"] == []
