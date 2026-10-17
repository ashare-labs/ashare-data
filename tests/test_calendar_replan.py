"""14/15 date gates and unchanged historical interpretation; all clocks are fixtures."""

import copy
import json

import pytest


from ashare_data.observations import action_baseline as action
from ashare_data.observations import phases as phase
from ashare_data.core.model import DataError, canonical
from test_action_scope import issuer_sources

pytestmark = pytest.mark.usefixtures("synthetic_action_baseline")


@pytest.mark.parametrize(
    "day,ready",
    [("2026-10-13", False), ("2026-10-14", True), ("2026-10-15", True), ("2026-10-16", False)],
)
def test_new_schema_is_bounded_to_exact_shifted_days(tmp_path, monkeypatch, day, ready):
    issuer_sources(monkeypatch, day=day)
    attempt = phase.capture(tmp_path, "admission", day)
    packet = phase.read(tmp_path, attempt["capture_id"])
    assert packet["schema"] == "ad-calendar-phase-observation.v4"
    assert packet["assessment"]["execution_permission"] is ready
    if ready:
        for row in packet["assessment"]["rows"]:
            scope = row["action_scope"]
            assert (scope["from_day"], scope["through_day"]) == ("2026-10-14", "2026-10-15")
            assert scope["price_share_code_review"]["baseline_sha256"] == action.REPLAN_SHA256
    else:
        assert "PILOT_DATE_SCOPE_EXCEEDED" in packet["assessment"]["blockers"]


@pytest.mark.parametrize("day", ["2026-10-13", "2026-10-14"])
def test_v3_packet_keeps_old_scope_and_cannot_be_relabelled(tmp_path, monkeypatch, day):
    (tmp_path / "blobs").mkdir()
    (tmp_path / "packets").mkdir()
    issuer_sources(monkeypatch, day=day, replan=False)
    schema = "ad-calendar-phase-observation.v3"
    at = phase.now().isoformat()
    sources = {}
    bodies = {}
    for name, (url, referer) in phase.specs("admission", day, schema).items():
        raw = phase.fetch(url, referer)
        h = phase.sha(raw)
        bodies[h] = raw
        sources[name] = dict(
            url=url, referer=referer, started_at=at, observed_at=at, sha256=h, http_status=200
        )
        phase.immutable_write(tmp_path / "blobs" / h, raw)
    packet = dict(
        schema=schema,
        kind="admission",
        day=day,
        attempt_id="fixture-legacy",
        evidence_kind="live_local_capture",
        started_at=at,
        completed_at=at,
        sources=sources,
        assessment=phase.assess("admission", day, sources, bodies, schema),
    )
    assert packet["assessment"]["execution_permission"]
    raw = canonical(packet)
    cap = phase.sha(raw)
    phase.immutable_write(tmp_path / "packets" / (cap + ".json"), raw)
    assert phase.read(tmp_path, cap) == packet
    assert packet["assessment"]["rows"][0]["action_scope"]["through_day"] == "2026-10-14"
    packet["schema"] = phase.SCHEMA
    altered = canonical(packet)
    newcap = phase.sha(altered)
    phase.immutable_write(tmp_path / "packets" / (newcap + ".json"), altered)
    with pytest.raises(DataError):
        phase.read(tmp_path, newcap)
    assert (tmp_path / "packets" / (cap + ".json")).read_bytes() == raw


@pytest.mark.parametrize(
    "damage", ["new_notice_on_15", "effective_15", "old_receipts", "extend_through_16"]
)
def test_day_15_cannot_gain_permission_by_relabeling_old_evidence(tmp_path, monkeypatch, damage):
    _, original = issuer_sources(monkeypatch, day="2026-10-15")

    def fetch(url, referer):
        raw = original(url, referer)
        if damage == "new_notice_on_15" and "security/ann" in url and "stock_list=600000" in url:
            v = json.loads(raw)
            r = copy.deepcopy(v["data"]["list"][0])
            r.update(art_code="new-15", notice_date="2026-10-15 00:00:00")
            v["data"]["list"].append(r)
            v["data"]["total_hits"] += 1
            return canonical(v)
        if damage == "effective_15" and "RPT_SHAREBONUS_DET" in url and "600000" in url:
            v = json.loads(raw)
            v["result"]["data"][0]["EX_DIVIDEND_DATE"] = "2026-10-15 00:00:00"
            return canonical(v)
        return raw

    monkeypatch.setattr(phase, "fetch", fetch)
    attempt = phase.capture(tmp_path, "admission", "2026-10-15")
    packet = phase.read(tmp_path, attempt["capture_id"])
    if damage in ("new_notice_on_15", "effective_15"):
        assert not packet["assessment"]["execution_permission"]
        return
    if damage == "extend_through_16":
        packet["assessment"]["rows"][0]["action_scope"]["through_day"] = "2026-10-16"
    else:
        for source in packet["sources"].values():
            source["observed_at"] = "2026-10-13T01:56:00+00:00"
    raw = canonical(packet)
    cap = phase.sha(raw)
    phase.immutable_write(tmp_path / "packets" / (cap + ".json"), raw)
    with pytest.raises(DataError):
        phase.read(tmp_path, cap)
