"""Controlled clock/transport only: no claimed real-world publication times."""

from datetime import datetime
import json

import pytest

from ashare_data.observations import daily as obs
from ashare_data.observations import phases as phase
from ashare_data.providers import http as transport
from ashare_data.core.model import DataError, canonical
from test_daily_observation import SECURITIES, upstream  # noqa: F401


@pytest.fixture
def closing(upstream, monkeypatch):  # noqa: F811
    upstream["now"] = datetime.fromisoformat("2026-10-12T15:00:20+08:00")
    upstream["phase"] = "E110"
    upstream["date"] = 20261012
    upstream["close"] = 10.1

    def fetch(url, referer):
        code = url.split("/self/")[1].split("?")[0]
        return canonical(
            dict(
                date=upstream["date"],
                time=150000,
                total=1,
                begin=0,
                end=1,
                list=[
                    [code, "fixture", 10.1, 10.0, upstream["close"], 11.11, 9.09, upstream["phase"]]
                ],
            )
        )

    monkeypatch.setattr(phase, "fetch", fetch)
    return upstream


def capture(root):
    return obs.capture(root, securities=SECURITIES, signal_day="2026-10-12", confirmed_close=True)


def test_confirmed_close_has_no_1745_or_1505_delay(closing, tmp_path):
    a = capture(tmp_path)
    assert a["status"] == "observed", a
    p = obs.read(tmp_path, a["capture_id"])
    assert p["schema"] == obs.CLOSE_SCHEMA
    assert p["signal_ready"] and p["finality_verified"] is False
    assert p["source_publication_time_verified"] is False
    assert len([u for u in closing["calls"] if "getKLineData" in u]) == 6
    assert len(p["close_confirmation"]["securities"]) == 3
    assert obs.recover(tmp_path) == []
    assert (
        obs.capture(tmp_path / "legacy", securities=SECURITIES, signal_day="2026-10-12")["error"][
            "code"
        ]
        == "OBSERVATION_WAIT_PUBLICATION"
    )


@pytest.mark.parametrize(
    "key,value,code",
    [
        ("phase", "T111", "OBSERVATION_WAIT_CLOSE"),
        ("phase", "U111", "OBSERVATION_WAIT_CLOSE"),
        ("phase", "P111", "OBSERVATION_WAIT_CLOSE"),
        ("date", 20261009, "SSE_SNAPSHOT_CLOCK"),
        ("close", 10.2, "OBSERVATION_CLOSE_CONFLICT"),
    ],
)
def test_not_closed_stale_or_conflicting_quote_waits(closing, tmp_path, key, value, code):
    closing[key] = value
    a = capture(tmp_path)
    assert a["status"] == "blocked" and a["capture_id"] is None
    assert a["error"]["code"] == code
    assert "sse" in a["close_confirmation"]["securities"][SECURITIES[0]]
    assert not list((tmp_path / "packets").glob("*.json"))


def test_repeat_daily_revision_rejects(closing, tmp_path, monkeypatch):
    original = transport.public_read
    counts = {}

    def read(url, timeout):
        raw = original(url, timeout)
        if "getKLineData" in url:
            counts[url] = counts.get(url, 0) + 1
            if counts[url] == 2:
                rows = json.loads(raw)
                rows[-1]["volume"] = "99999"
                raw = canonical(rows)
        return raw

    monkeypatch.setattr(transport, "public_read", read)
    assert capture(tmp_path)["error"]["code"] == "OBSERVATION_WAIT_STABLE"


def test_early_and_weekend_cannot_be_observed(closing, tmp_path):
    closing["now"] = datetime.fromisoformat("2026-10-12T14:59:59+08:00")
    assert capture(tmp_path)["error"]["code"] == "OBSERVATION_WAIT_PUBLICATION"
    assert not closing["calls"]
    closing["now"] = datetime.fromisoformat("2026-10-17T15:00:20+08:00")
    a = obs.capture(tmp_path, securities=SECURITIES, signal_day="2026-10-17", confirmed_close=True)
    assert a["status"] == "blocked" and a["capture_id"] is None


def test_disconnect_retains_attempt_and_reconnect_requires_new_capture(
    closing, tmp_path, monkeypatch
):
    old = phase.fetch

    def offline(*args):
        raise ConnectionError("controlled offline")

    monkeypatch.setattr(phase, "fetch", offline)
    failed = capture(tmp_path)
    assert failed["status"] == "blocked" and failed["capture_id"] is None
    monkeypatch.setattr(phase, "fetch", old)
    success = capture(tmp_path)
    assert success["status"] == "observed" and success["attempt_id"] != failed["attempt_id"]
    assert (
        json.loads((tmp_path / "attempts" / (failed["attempt_id"] + ".json")).read_bytes())
        == failed
    )


def test_offline_read_checks_all_confirmation_blobs(closing, tmp_path):
    a = capture(tmp_path)
    p = obs.read(tmp_path, a["capture_id"])
    source = p["close_confirmation"]["securities"][SECURITIES[0]]["sse"]
    path = tmp_path / "blobs" / source["sha256"]
    path.unlink()
    path.write_bytes(b"tampered")
    with pytest.raises(DataError) as exc:
        obs.read(tmp_path, a["capture_id"])
    assert exc.value.code == "OBSERVATION_CORRUPT"
