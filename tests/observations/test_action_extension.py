"""Self-authored disclosures and controlled receipt times; no live evidence."""

import copy
import json

import pytest

from ashare_data.observations import action_extensions as extension
from ashare_data.observations import action_baseline as action
from ashare_data.observations.daily import sha
from ashare_data.market.client import CALENDAR_URL
from ashare_data.core.model import DataError, canonical


@pytest.fixture
def review(tmp_path, synthetic_action_baseline, monkeypatch):
    base, bodies = action.load_baseline(replan=True)
    root = tmp_path / "extension"
    (root / "blobs").mkdir(parents=True)
    sources = {}

    def add(name, body, url, referer):
        h = sha(body)
        (root / "blobs" / h).write_bytes(body)
        sources[name] = dict(
            url=url,
            referer=referer,
            sha256=h,
            http_status=200,
            started_at="2026-10-16T01:00:00+08:00",
            finished_at="2026-10-16T01:00:01+08:00",
        )

    add(
        "calendar",
        bodies[base["scope_review"]["calendar_source"]],
        CALENDAR_URL,
        "https://www.sse.com.cn/",
    )
    for code, item in base["securities"].items():
        add(
            "catalog:" + code,
            bodies[item["catalog_source"]],
            action.catalog_url(code, "2026-10-16"),
            "https://data.eastmoney.com/notices/",
        )
        for article, doc in item["documents"].items():
            for page, name in enumerate(doc["sources"], 1):
                add(
                    f"text:{article}:{page}",
                    bodies[name],
                    action.content_url(article, page),
                    f"https://data.eastmoney.com/notices/detail/{code}/{article}.html",
                )
    value = dict(
        schema=extension.SCHEMA,
        previous_baseline_sha256=action.REPLAN_SHA256,
        from_day="2026-10-14",
        through_day="2026-10-19",
        completed_at="2026-10-16T01:10:00+08:00",
        sources=sources,
        reviewed_additions={},
    )

    def seal():
        raw = canonical(value)
        (root / "review.json").write_bytes(raw)
        monkeypatch.setenv(extension.ROOT_ENV, str(root))
        monkeypatch.setenv(extension.SHA_ENV, sha(raw))
        return sha(raw)

    seal()
    return root, value, seal, add, base, bodies


def test_explicit_extension_preserves_legacy_and_requires_fresh_daily_check(review):
    root, value, seal, _, base, old = review
    h = seal()
    m, bodies = action.load_baseline(replan=True)
    assert action.date_scope(True) == ("2026-10-14", "2026-10-19")
    assert action.baseline_identity(replan=True) == h
    assert action.date_scope(False) == ("2026-10-13", "2026-10-14")
    assert action.load_baseline(replan=True, extension=False)[0] == base
    assert m["baseline_lineage"] == [action.BASELINE_SHA256, action.REPLAN_SHA256, h]
    sources = {}
    raw = {}
    for code, item in m["securities"].items():
        pairs = [("action_catalog_" + code, item["catalog_source"])]
        for article, doc in item["documents"].items():
            pairs += [
                (f"action_text_{article}_{page}", name)
                for page, name in enumerate(doc["sources"], 1)
            ]
        for current, name in pairs:
            digest = sha(bodies[name])
            raw[digest] = bodies[name]
            sources[current] = dict(sha256=digest, observed_at="2026-10-16T10:00:00+08:00")
        inventory = dict(
            events=[
                d["cash_event"]
                for d in item["documents"].values()
                if d["classification"] == "ordinary_cash_implemented"
            ],
            cash_history_complete=True,
            unresolved=[],
            proposals=[],
        )
        scope, _ = action.assess_security(code, "2026-10-16", sources, raw, inventory, replan=True)
        assert scope["price_share_code_review"]["baseline_sha256"] == h
        sources["action_catalog_" + code]["observed_at"] = "2026-10-15T10:00:00+08:00"
        with pytest.raises(DataError, match="来源早于审阅基线"):
            action.assess_security(code, "2026-10-16", sources, raw, inventory, replan=True)
    assert action.load_baseline(replan=True, extension=False)[1] == old


@pytest.mark.parametrize(
    "damage",
    [
        "hash",
        "missing_text",
        "stale",
        "calendar_weekend",
        "changed_old",
        "unreviewed_new",
        "unpaired_selection",
        "too_long",
        "foreign_baseline",
    ],
)
def test_extension_does_not_turn_changed_or_missing_sources_into_permission(
    review, monkeypatch, damage
):
    root, value, seal, add, base, bodies = review
    if damage == "hash":
        seal()
        monkeypatch.setenv(extension.SHA_ENV, "0" * 64)
    elif damage == "unpaired_selection":
        monkeypatch.delenv(extension.SHA_ENV)
    else:
        if damage == "missing_text":
            value["sources"].pop(next(n for n in value["sources"] if n.startswith("text:")))
        if damage == "stale":
            value["sources"]["calendar"]["started_at"] = base["completed_at"]
        if damage == "calendar_weekend":
            value["through_day"] = "2026-10-18"
        if damage == "too_long":
            value["through_day"] = "2026-12-01"
        if damage == "foreign_baseline":
            value["previous_baseline_sha256"] = "0" * 64
        if damage in ("changed_old", "unreviewed_new"):
            name = "catalog:600000"
            s = value["sources"][name]
            v = json.loads((root / "blobs" / s["sha256"]).read_bytes())
            if damage == "changed_old":
                v["data"]["list"][0]["title"] += " revision"
            else:
                row = copy.deepcopy(v["data"]["list"][0])
                row["art_code"] = "SYNTHETIC-UNREVIEWED"
                v["data"]["list"].append(row)
                v["data"]["total_hits"] += 1
            add(name, canonical(v), s["url"], s["referer"])
        seal()
    with pytest.raises((DataError, FileNotFoundError)):
        action.load_baseline(replan=True)


def test_new_governance_notice_needs_complete_explicit_review_and_live_text_check(review):
    root, value, seal, add, base, bodies = review
    code = "600000"
    article = "SYNTHETIC-NEW-GOVERNANCE"
    cat = json.loads(bodies[base["securities"][code]["catalog_source"]])
    row = copy.deepcopy(cat["data"]["list"][0])
    row.update(
        art_code=article, title="SYNTHETIC board role change", notice_date="2026-10-16 00:00:00"
    )
    cat["data"]["list"].append(row)
    cat["data"]["total_hits"] += 1
    add(
        "catalog:" + code,
        canonical(cat),
        action.catalog_url(code, "2026-10-16"),
        "https://data.eastmoney.com/notices/",
    )
    existing = next(iter(base["securities"][code]["documents"].values()))
    body = json.loads(bodies[existing["sources"][0]])
    body["data"].update(
        art_code=article,
        notice_title=row["title"],
        notice_date=row["notice_date"],
        notice_content="SYNTHETIC board role change only.",
        page_size=1,
        attach_url="https://example.invalid/synthetic-governance.pdf",
    )
    referer = f"https://data.eastmoney.com/notices/detail/{code}/{article}.html"
    add("text:" + article + ":1", canonical(body), action.content_url(article, 1), referer)
    add("pdf:" + article, b"%PDF-SYNTHETIC-NON-MARKET", body["data"]["attach_url"], referer)
    value["reviewed_additions"] = {
        code: {
            article: dict(
                classification="governance_no_account_effect",
                pages=1,
                attachment_url=body["data"]["attach_url"],
                review_note="Controlled synthetic reviewer examined full fixture; no cash, price, share or code action.",
            )
        }
    }
    seal()
    m, _ = action.load_baseline(replan=True)
    assert article in m["securities"][code]["documents"]
    assert "action_text_" + article + "_1" in action.specs("2026-10-16", replan=True)
    value["reviewed_additions"][code][article]["classification"] = "cash_dividend"
    seal()
    with pytest.raises(DataError):
        action.load_baseline(replan=True)
