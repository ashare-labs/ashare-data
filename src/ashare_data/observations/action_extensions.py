"""Explicit, content-addressed extension of a reviewed issuer corpus.

The date interval is a review authorization, not a forecast of absent actions.
Every live admission still refetches the entire catalog and reviewed texts.
Existing disclosures must stay unchanged. New governance-only notices need a
complete, explicitly reviewed text and attachment; account actions are refused.
"""

import copy
import json
import os
from datetime import date, timedelta
from pathlib import Path

from ashare_data.observations.daily import sha
from ashare_data.core.model import TZ, require, timestamp

ROOT_ENV = "ASHARE_ACTION_EXTENSION"
SHA_ENV = "ASHARE_ACTION_EXTENSION_SHA256"
SCHEMA = "ad-reviewed-action-extension.v1"


def selection():
    root, selected = os.environ.get(ROOT_ENV), os.environ.get(SHA_ENV)
    require(
        bool(root) == bool(selected), "ACTION_EXTENSION_SELECTION", "路径和摘要必须同时显式选择"
    )
    if not root:
        return None
    require(
        len(selected) == 64 and all(c in "0123456789abcdef" for c in selected),
        "ACTION_EXTENSION_SELECTION",
        "非法摘要",
    )
    return Path(root).expanduser(), selected


def load(root=None, selected=None):
    from ashare_data.observations import action_baseline as action
    from ashare_data.market.client import CALENDAR_URL, _calendar

    if root is None:
        chosen = selection()
        require(chosen is not None, "ACTION_EXTENSION_SELECTION", "缺少显式选择")
        root, selected = chosen
    root = Path(root)
    raw = (root / "review.json").read_bytes()
    require(sha(raw) == selected, "ACTION_EXTENSION_CHANGED", "复审摘要不同")
    review = json.loads(raw)
    previous, old_bodies = action.load_baseline(replan=True, extension=False)
    first, last = date.fromisoformat(review["from_day"]), date.fromisoformat(review["through_day"])
    completed = timestamp(review["completed_at"])
    require(
        review["schema"] == SCHEMA
        and review["previous_baseline_sha256"] == action.REPLAN_SHA256
        and review["from_day"] == previous["from_day"]
        and last > date.fromisoformat(previous["through_day"])
        and timedelta(0) < last - first <= timedelta(days=31)
        and first.year == last.year
        and timestamp(previous["completed_at"]) < completed
        and first <= completed.astimezone(TZ).date() <= last,
        "ACTION_EXTENSION_SCOPE",
        "须为固定旧基线的一次有限、已实际复读范围扩展",
    )
    expected = {"calendar": (CALENDAR_URL, "https://www.sse.com.cn/")}
    mapping = {}
    additions = review.get("reviewed_additions", {})
    require(set(additions) <= set(previous["securities"]), "ACTION_EXTENSION_REVIEW", "未知证券")
    observed_day = completed.astimezone(TZ).date().isoformat()
    for code, item in previous["securities"].items():
        name = "catalog:" + code
        expected[name] = (
            action.catalog_url(code, observed_day),
            "https://data.eastmoney.com/notices/",
        )
        mapping[name] = item["catalog_source"]
        for article, document in item["documents"].items():
            for page, old_name in enumerate(document["sources"], 1):
                name = f"text:{article}:{page}"
                expected[name] = (
                    action.content_url(article, page),
                    f"https://data.eastmoney.com/notices/detail/{code}/{article}.html",
                )
                mapping[name] = old_name
        for article, doc in additions.get(code, {}).items():
            require(
                doc["classification"] == "governance_no_account_effect"
                and isinstance(doc["review_note"], str)
                and len(doc["review_note"].strip()) >= 30
                and type(doc["pages"]) is int
                and 1 <= doc["pages"] <= 20
                and article not in item["documents"],
                "ACTION_EXTENSION_REVIEW",
                article,
            )
            for page in range(1, doc["pages"] + 1):
                name = f"text:{article}:{page}"
                expected[name] = (
                    action.content_url(article, page),
                    f"https://data.eastmoney.com/notices/detail/{code}/{article}.html",
                )
                mapping[name] = f"extension-text-{article}-{page}"
            expected["pdf:" + article] = (
                doc["attachment_url"],
                f"https://data.eastmoney.com/notices/detail/{code}/{article}.html",
            )
    require(set(review["sources"]) == set(expected), "ACTION_EXTENSION_SOURCES", "完整来源集不同")
    bodies = dict(old_bodies)
    refreshed = {}
    for name, (url, referer) in expected.items():
        source = review["sources"][name]
        require(
            source["url"] == url
            and source["referer"] == referer
            and source.get("http_status") == 200
            and not source.get("error")
            and timestamp(previous["completed_at"])
            < timestamp(source["started_at"])
            <= timestamp(source["finished_at"])
            <= completed
            and timestamp(source["started_at"]).astimezone(TZ).date().isoformat() == observed_day,
            "ACTION_EXTENSION_SOURCE",
            name,
        )
        body = (root / "blobs" / source["sha256"]).read_bytes()
        require(sha(body) == source["sha256"], "ACTION_EXTENSION_CHANGED", name)
        refreshed[name] = body
    year, closed = _calendar(refreshed["calendar"])
    require(
        first.year == year and all(d.weekday() < 5 and d not in closed for d in (first, last)),
        "ACTION_EXTENSION_CALENDAR",
        "范围端点须由交易所日历确认",
    )
    for code, item in previous["securities"].items():
        catalog = action.catalog_projection(
            json.loads(refreshed["catalog:" + code]), code, completed.astimezone(TZ).isoformat()
        )
        old_catalog = action.catalog_projection(
            json.loads(old_bodies[item["catalog_source"]]), code, previous["completed_at"]
        )
        new_articles = set(catalog) - set(old_catalog)
        require(
            set(old_catalog) <= set(catalog)
            and all(catalog[k] == v for k, v in old_catalog.items())
            and new_articles == set(additions.get(code, {})),
            "ACTION_EXTENSION_DISCLOSURE_CHANGED",
            code,
        )
        for article, doc in item["documents"].items():
            for page, old_name in enumerate(doc["sources"], 1):
                current = action.content_projection(
                    json.loads(refreshed[f"text:{article}:{page}"]), article, code, doc["pages"]
                )
                old = action.content_projection(
                    json.loads(old_bodies[old_name]), article, code, doc["pages"]
                )
                require(current == old, "ACTION_EXTENSION_DISCLOSURE_CHANGED", article)
        for article, doc in additions.get(code, {}).items():
            for page in range(1, doc["pages"] + 1):
                current = action.content_projection(
                    json.loads(refreshed[f"text:{article}:{page}"]), article, code, doc["pages"]
                )
                require(
                    current["notice_title"] == catalog[article]["title"]
                    and current["notice_date"] == catalog[article]["notice_date"]
                    and current["attach_url"] == doc["attachment_url"],
                    "ACTION_EXTENSION_REVIEW",
                    article,
                )
            require(
                refreshed["pdf:" + article].startswith(b"%PDF-"),
                "ACTION_EXTENSION_ATTACHMENT",
                article,
            )
    result = copy.deepcopy(previous)
    result.update(
        from_day=review["from_day"],
        through_day=review["through_day"],
        completed_at=review["completed_at"],
    )
    for name, old_name in mapping.items():
        result["sources"][old_name] = review["sources"][name]
        bodies[old_name] = refreshed[name]
    for code, documents in additions.items():
        for article, doc in documents.items():
            result["securities"][code]["catalog_review"][article] = (
                "reviewed_governance_no_account_effect"
            )
            result["securities"][code]["documents"][article] = {
                "classification": doc["classification"],
                "review_note": doc["review_note"],
                "pages": doc["pages"],
                "sources": [
                    mapping[f"text:{article}:{page}"] for page in range(1, doc["pages"] + 1)
                ],
            }
    calendar_name = "extension-official-calendar"
    result["sources"][calendar_name] = review["sources"]["calendar"]
    bodies[calendar_name] = refreshed["calendar"]
    result["scope_review"] = dict(
        calendar_source=calendar_name,
        previous_baseline_sha256=action.REPLAN_SHA256,
        reviewed_at=review["completed_at"],
    )
    result["baseline_lineage"] = [action.BASELINE_SHA256, action.REPLAN_SHA256, selected]
    return result, bodies
