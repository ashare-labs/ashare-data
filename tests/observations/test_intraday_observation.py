"""Synthetic clock/transport fixtures, not real intraday observation evidence."""

from datetime import datetime, timedelta
import json
import pytest

from ashare_data.observations import intraday as obs
from ashare_data.observations import phases as phase
from ashare_data.core.model import canonical, DataError
from test_phase_timing import sources as morning_sources

pytestmark = pytest.mark.usefixtures("synthetic_action_baseline")

DAY = "2026-10-14"


def sources(mp, at="2026-10-14T10:03:30+08:00", damage=None):
    morning_sources(mp, opening=True)
    original = phase.fetch
    clock = datetime.fromisoformat(at)
    mp.setattr(obs, "now", lambda: clock)

    def fetch(url, referer):
        if damage == "offline":
            raise ConnectionError("controlled disconnect")
        if "getKLineData" in url:
            price = 10 if "600000" in url else 20 if "600036" in url else 30
            label = "2026-10-14 10:02:00"
            if damage == "old_minute":
                label = "2026-10-14 10:01:00"
            if damage == "forming_minute":
                label = "2026-10-14 10:04:00"
            return canonical(
                [
                    dict(
                        day=label,
                        open=str(price),
                        high=str(price),
                        low=str(price),
                        close=str(price),
                        volume="1000000",
                    )
                ]
            )
        raw = original(url, referer)
        if "yunhq" in url:
            v = json.loads(raw)
            v["time"] = int((clock - timedelta(seconds=2)).strftime("%H%M%S"))
            if damage == "halt":
                v["list"][0][7] = "P111"
            if damage == "stale":
                v["time"] = 93000
            return canonical(v)
        if "hq.sinajs.cn" in url:
            return raw.replace(
                b"09:30:58", (clock - timedelta(seconds=2)).strftime("%H:%M:%S").encode()
            )
        return raw

    mp.setattr(phase, "fetch", fetch)


def test_complete_minute_must_start_after_intent_and_never_use_past_open(tmp_path, monkeypatch):
    sources(monkeypatch)
    a = obs.capture(tmp_path, "execution", DAY, DAY + "T10:00:31+08:00")
    p = obs.read(tmp_path, a["capture_id"])
    assert p["assessment"]["ready"] and not p["assessment"]["execution_permission"]
    assert [r["execution"]["match_price"] for r in p["assessment"]["rows"]] == [
        "10.01",
        "20.01",
        "30.01",
    ]
    assert all(
        r["execution"]["minute_end"] == DAY + "T10:02:00+08:00" for r in p["assessment"]["rows"]
    )


@pytest.mark.parametrize("damage", ["old_minute", "forming_minute", "halt", "stale", "offline"])
def test_bad_real_source_shapes_refuse_without_fill(tmp_path, monkeypatch, damage):
    sources(monkeypatch, damage=damage)
    a = obs.capture(tmp_path, "execution", DAY, DAY + "T10:00:31+08:00")
    assert a["status"] == "blocked"
    if a["capture_id"]:
        assert not obs.read(tmp_path, a["capture_id"])["assessment"]["ready"]


@pytest.mark.parametrize(
    "at", ["2026-10-14T11:45:00+08:00", "2026-10-14T15:01:00+08:00", "2026-10-13T10:03:00+08:00"]
)
def test_lunch_closed_or_old_day_cannot_capture(tmp_path, monkeypatch, at):
    sources(monkeypatch, at=at)
    a = obs.capture(tmp_path, "admission", DAY, DAY + "T10:00:00+08:00")
    assert a["status"] == "blocked" and a["capture_id"] is None


def test_forged_readiness_and_price_is_detected(tmp_path, monkeypatch):
    sources(monkeypatch)
    a = obs.capture(tmp_path, "execution", DAY, DAY + "T10:00:31+08:00")
    p = obs.read(tmp_path, a["capture_id"])
    p["assessment"]["rows"][0]["execution"]["match_price"] = "9.60"
    raw = canonical(p)
    cap = obs.sha(raw)
    obs.immutable_write(tmp_path / "packets" / (cap + ".json"), raw)
    with pytest.raises(DataError):
        obs.read(tmp_path, cap)


def test_changed_completed_minute_between_two_receipts_rejects(tmp_path, monkeypatch):
    sources(monkeypatch)
    original = phase.fetch
    counts = {}

    def revised(url, referer):
        raw = original(url, referer)
        if "getKLineData" in url:
            counts[url] = counts.get(url, 0) + 1
            if counts[url] == 2:
                value = json.loads(raw)
                value[0]["volume"] = "999999"
                raw = canonical(value)
        return raw

    monkeypatch.setattr(phase, "fetch", revised)
    a = obs.capture(tmp_path, "execution", DAY, DAY + "T10:00:31+08:00")
    packet = obs.read(tmp_path, a["capture_id"])
    assert not packet["assessment"]["ready"]
    assert any("INTRADAY_MINUTE_REVISION" in x for x in packet["assessment"]["blockers"])
