"""Self-authored synthetic issuer inputs and simulated market clock/quotes.

These tests are not actual future-day receipts. No network is touched.
"""

import copy
import json
from urllib.parse import parse_qs, urlparse

import pytest


from ashare_data import action_scope as action
from ashare_data import phase_observation as phase
from ashare_data.calendar_scope import cash_inventory
from ashare_data.model import DataError, canonical
from test_calendar_scope import market

from synthetic_pilot import dividends

pytestmark = pytest.mark.usefixtures("synthetic_action_baseline")


def issuer_sources(monkeypatch, *, day="2026-10-14", replan=True):
    market(monkeypatch, day=day)
    old = phase.fetch
    manifest, blobs = action.load_baseline(replan=replan)
    urls = {}
    for code, item in manifest["securities"].items():
        urls[action.catalog_url(code, day)] = blobs[item["catalog_source"]]
        for article, document in item["documents"].items():
            for page, name in enumerate(document["sources"], 1):
                urls[action.content_url(article, page)] = blobs[name]

    def fetch(url, referer):
        if url in urls:
            return urls[url]
        if "RPT_SHAREBONUS_DET" in url:
            code = parse_qs(urlparse(url).query)["filter"][0].split('"')[1]
            return canonical(dividends(code))
        return old(url, referer)

    monkeypatch.setattr(phase, "fetch", fetch)
    return urls, fetch


def test_complete_scoped_issuer_evidence_can_admit_not_constant_block(tmp_path, monkeypatch):
    issuer_sources(monkeypatch)
    attempt = phase.capture(tmp_path, "admission", "2026-10-14")
    packet = phase.read(tmp_path, attempt["capture_id"])
    assert attempt["status"] == "observed"
    assert packet["assessment"]["execution_permission"] is True
    assert len(packet["sources"]) == 27
    rows = packet["assessment"]["rows"]
    assert [len(r["cash_inventory"]["events"]) for r in rows] == [1, 1, 1]
    assert sum(r["cash_inventory"]["excluded_pre_2026_events"] for r in rows) == 3
    for row in rows:
        review = row["action_scope"]["price_share_code_review"]
        assert review["baseline_sha256"] == action.REPLAN_SHA256
        assert review["status"] == "proven" and row["actions_known_absent"] is False
        assert all(e["pay_date"] < "2026-10-13" for e in row["cash_inventory"]["events"])


@pytest.mark.parametrize(
    "damage",
    [
        "new_notice",
        "missing_notice",
        "pagination",
        "identity",
        "future_publication",
        "text_revision",
        "page_count",
        "offline",
        "cash_changed",
        "new_price_action",
    ],
)
def test_new_unreviewed_or_incomplete_sources_reject(tmp_path, monkeypatch, damage):
    _, original = issuer_sources(monkeypatch)

    def fetch(url, referer):
        raw = original(url, referer)
        if "security/ann" in url and "stock_list=600000" in url:
            v = json.loads(raw)
            if damage == "new_notice":
                r = copy.deepcopy(v["data"]["list"][0])
                r["art_code"] += "X"
                v["data"]["list"].append(r)
                v["data"]["total_hits"] += 1
            elif damage == "missing_notice":
                v["data"]["list"].pop()
                v["data"]["total_hits"] -= 1
            elif damage == "pagination":
                v["data"]["total_hits"] += 1
            elif damage == "identity":
                v["data"]["list"][0]["codes"] = [{"stock_code": "600036"}]
            elif damage == "future_publication":
                v["data"]["list"][0]["notice_date"] = "2026-10-15 00:00:00"
            raw = canonical(v)
        if "art_code=SYNTHETIC-600000-CASH" in url:
            v = json.loads(raw)
            if damage == "text_revision":
                v["data"]["notice_content"] += "更正"
            elif damage == "page_count":
                v["data"]["page_size"] = 2
            elif damage == "offline":
                raise ConnectionError("controlled offline fixture")
            raw = canonical(v)
        if "RPT_SHAREBONUS_DET" in url and "600000" in url:
            v = json.loads(raw)
            if damage == "cash_changed":
                v["result"]["data"][0]["PRETAX_BONUS_RMB"] = 9.9
            elif damage == "new_price_action":
                v["result"]["data"][0]["EX_DIVIDEND_DATE"] = "2026-10-14 00:00:00"
            raw = canonical(v)
        return raw

    monkeypatch.setattr(phase, "fetch", fetch)
    attempt = phase.capture(tmp_path, "admission", "2026-10-14")
    packet = phase.read(tmp_path, attempt["capture_id"])
    assert attempt["status"] == "blocked" and not packet["assessment"]["execution_permission"]
    assert any(
        "SCOPED_PRICE_SHARE_CODE_REVIEW_MISSING:600000" in s
        for s in packet["assessment"]["blockers"]
    )


def test_baseline_manifest_and_pdf_tampering_rejected(tmp_path, synthetic_action_baseline):
    import shutil

    shutil.copytree(synthetic_action_baseline, tmp_path / "review")
    root = tmp_path / "review"
    m, _ = action.load_baseline(root)
    pdf = next(s for n, s in m["sources"].items() if n.endswith(".pdf"))
    target = root / "blobs" / pdf["sha256"]
    target.write_bytes(b"%PDF-fake")
    with pytest.raises(DataError, match="cninfo-cash"):
        action.load_baseline(root)
    (root / "manifest.json").write_bytes(canonical({**m, "through_day": "2027-01-01"}))
    with pytest.raises(DataError, match="审阅基线摘要"):
        action.load_baseline(root)


def test_old_unentitled_record_does_not_demand_unbounded_history():
    raw = dividends("601318")
    with pytest.raises(DataError, match="实施日期不符"):
        cash_inventory(raw, "601318", "2026-10-13")
    result = cash_inventory(raw, "601318", "2026-10-13", coverage_from="2026-01-01")
    assert len(result["events"]) == 1
    assert {"record_date": "2014-02-03", "ex_date": "2014-02-04"} in result[
        "excluded_earlier_events"
    ]


def test_rehashing_derived_proof_cannot_create_or_remove_scope(tmp_path, monkeypatch):
    issuer_sources(monkeypatch)
    attempt = phase.capture(tmp_path, "admission", "2026-10-14")
    p = phase.read(tmp_path, attempt["capture_id"])
    p["assessment"]["rows"][0]["cash_inventory"]["events"][0]["pay_date"] = "2026-10-13"
    raw = canonical(p)
    cap = phase.sha(raw)
    (tmp_path / "packets" / (cap + ".json")).write_bytes(raw)
    with pytest.raises(DataError, match="不能手填"):
        phase.read(tmp_path, cap)


def test_v2_bytes_do_not_inherit_v3_action_permission(tmp_path, monkeypatch):
    market(monkeypatch)
    sources, bodies = {}, {}
    for name, (url, referer) in phase.specs(
        "admission", "2026-10-13", "ad-calendar-phase-observation.v2"
    ).items():
        body = phase.fetch(url, referer)
        bodies[phase.sha(body)] = body
        sources[name] = dict(sha256=phase.sha(body), observed_at="2026-10-13T08:55:00+08:00")
    assessment = phase.assess(
        "admission", "2026-10-13", sources, bodies, "ad-calendar-phase-observation.v2"
    )
    assert not assessment["execution_permission"]
    assert all(r["action_scope"]["price_share_code_review"] is None for r in assessment["rows"])


def test_display_time_is_not_a_version_but_body_and_ingestion_time_are():
    manifest, blobs = action.load_baseline()
    v = json.loads(blobs[manifest["securities"]["600000"]["catalog_source"]])
    old = action.catalog_projection(v, "600000", "2026-10-13T08:55:00+08:00")
    v["data"]["list"][0]["display_time"] = "irrelevant observed display field drift"
    assert action.catalog_projection(v, "600000", "2026-10-13T08:55:00+08:00") == old
    v["data"]["list"][0]["eiTime"] = "changed ingestion identity"
    assert action.catalog_projection(v, "600000", "2026-10-13T08:55:00+08:00") != old
