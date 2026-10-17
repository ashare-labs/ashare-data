"""Local-clock fixtures; never counted as real pre-open or opening observations."""

from datetime import datetime, timedelta

import pytest


from ashare_data.observations import phases as phase
from ashare_data.core.model import DataError, canonical

pytestmark = pytest.mark.usefixtures("synthetic_action_baseline")


def install(monkeypatch, clock, quote_day=None, state="00"):
    at = datetime.fromisoformat(clock)
    monkeypatch.setattr(phase, "now", lambda: at)

    def fetch(url, referer):
        if "hq.sinajs.cn" in url:
            lines = []
            for sid, price in zip(phase.UNIVERSE, ("10", "20", "30"), strict=True):
                parts = ["0"] * 33
                parts[0:3] = ["测试证券", price, price]
                parts[30:33] = [
                    quote_day or str(at.date()),
                    str((at - timedelta(seconds=2)).time()),
                    state,
                ]
                lines.append("var hq_str_sh" + sid[:6] + '="' + ",".join(parts) + '";')
            return "\n".join(lines).encode("gb18030")
        if "query.sse.com.cn" in url:
            return canonical(dict(result=[], pageHelp=dict(total=0)))
        if "push2.eastmoney.com" in url:
            from urllib.parse import parse_qs, urlparse

            code = parse_qs(urlparse(url).query)["secid"][0][2:]
            return canonical(
                dict(rc=0, data=dict(f57=code, f86=int(at.timestamp()), f51=11, f52=9))
            )
        return canonical(dict(success=True, code=0, result=dict(pages=1, count=0, data=[])))

    monkeypatch.setattr(phase, "fetch", fetch)


def test_preopen_keeps_source_fields_but_never_invents_coverage(tmp_path, monkeypatch):
    install(monkeypatch, "2026-10-13T08:55:00+08:00", quote_day="2026-10-12")
    attempt = phase.capture(tmp_path, "admission", "2026-10-13")
    assert attempt["status"] == "blocked" and attempt["capture_id"]
    packet = phase.read(tmp_path, attempt["capture_id"])
    assessment = packet["assessment"]
    assert assessment["execution_permission"] is False
    assert all(
        r["suspended"] is None and r["is_st"] is False and not r["actions_known_absent"]
        for r in assessment["rows"]
    )
    assert any(
        x.startswith("SCOPED_PRICE_SHARE_CODE_REVIEW_MISSING") for x in assessment["blockers"]
    )
    assert len(packet["sources"]) == 27
    assert assessment["diagnostics"]["em_quote_600000"]["fields"]["f51"] == 11


def test_separate_opening_packet_is_observed_but_not_permission(tmp_path, monkeypatch):
    install(monkeypatch, "2026-10-13T09:31:00+08:00")
    attempt = phase.capture(tmp_path, "opening", "2026-10-13")
    packet = phase.read(tmp_path, attempt["capture_id"])
    assert attempt["status"] == "observed"
    assert packet["assessment"]["opening_ready"] is True
    assert packet["assessment"]["execution_permission"] is False
    assert all("close" not in r and "high" not in r for r in packet["assessment"]["rows"])


@pytest.mark.parametrize(
    "at,day",
    [
        ("2026-10-12T23:00:00+08:00", "2026-10-12"),
        ("2026-10-13T09:31:00+08:00", "2026-10-12"),
        ("2026-10-13T09:36:00+08:00", "2026-10-13"),
    ],
)
def test_no_backfill_or_late_opening(tmp_path, monkeypatch, at, day):
    install(monkeypatch, at)
    monkeypatch.setattr(
        phase, "fetch", lambda *args: pytest.fail("wrong-window request touched network")
    )
    attempt = phase.capture(tmp_path, "opening", day)
    assert attempt["error"]["code"] == "PHASE_OUTSIDE_REAL_WINDOW"
    assert attempt["capture_id"] is None


@pytest.mark.parametrize("state", ["02", "03", "UNKNOWN"])
def test_suspended_or_unknown_open_is_not_usable(tmp_path, monkeypatch, state):
    install(monkeypatch, "2026-10-13T09:31:00+08:00", state=state)
    attempt = phase.capture(tmp_path, "opening", "2026-10-13")
    assert attempt["status"] == "blocked"
    assert not phase.read(tmp_path, attempt["capture_id"])["assessment"]["opening_ready"]


def test_offline_source_error_and_reconnect_are_distinct_receipts(tmp_path, monkeypatch):
    install(monkeypatch, "2026-10-13T09:31:00+08:00")

    def offline(*args):
        raise ConnectionRefusedError("controlled transport failure; no quota claim")

    monkeypatch.setattr(phase, "fetch", offline)
    attempt = phase.capture(tmp_path, "opening", "2026-10-13")
    packet = phase.read(tmp_path, attempt["capture_id"])
    assert packet["sources"]["sina_quotes"]["error"]["type"] == "ConnectionRefusedError"
    install(monkeypatch, "2026-10-13T09:32:00+08:00")
    connected = phase.capture(tmp_path, "opening", "2026-10-13")
    assert connected["status"] == "observed" and connected["capture_id"] != attempt["capture_id"]


def test_rehashed_permission_tamper_and_raw_tamper_rejected(tmp_path, monkeypatch):
    install(monkeypatch, "2026-10-13T08:55:00+08:00")
    attempt = phase.capture(tmp_path, "admission", "2026-10-13")
    packet = phase.read(tmp_path, attempt["capture_id"])
    packet["assessment"]["execution_permission"] = True
    raw = canonical(packet)
    cap = phase.sha(raw)
    (tmp_path / "packets" / (cap + ".json")).write_bytes(raw)
    with pytest.raises(DataError, match="不能手填"):
        phase.read(tmp_path, cap)
    source = packet["sources"]["sina_quotes"]
    raw_path = tmp_path / "blobs" / source["sha256"]
    raw_path.unlink()  # immutable storage is read-only; simulate replacing the artifact
    raw_path.write_bytes(b"changed")
    with pytest.raises(DataError, match="原文不符"):
        phase.read(tmp_path, attempt["capture_id"])


def test_recovery_publishes_saved_packet_or_marks_interrupted(tmp_path, monkeypatch):
    install(monkeypatch, "2026-10-13T09:31:00+08:00")
    attempt = phase.capture(tmp_path, "opening", "2026-10-13")
    path = tmp_path / "attempts" / (attempt["attempt_id"] + ".json")
    attempt.update(status="started", capture_id=None)
    path.write_bytes(canonical(attempt))
    recovered = phase.recover(tmp_path)
    assert recovered[0]["status"] == "observed" and recovered[0]["capture_id"]
    (tmp_path / "attempts" / "interrupted.json").write_bytes(
        canonical({"attempt_id": "interrupted", "status": "started"})
    )
    assert phase.recover(tmp_path)[0]["status"] == "interrupted"
