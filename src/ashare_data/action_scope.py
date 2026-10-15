"""Finite, reviewed issuer corpus plus fresh catalog/text checks for this pilot.

This is a deliberately bounded bootstrap, not an all-market action database or
an inference from an empty daily calendar. The review and original source bytes
are pinned together; every decision rechecks the catalog and every reviewed text
page. New notices, changed texts, missing pages and a date outside the pilot all
require a new review. The consumer still owns actual account entitlements.
"""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlencode

from .daily_observation import sha
from .model import canonical, require, timestamp

FIRST_DAY = "2026-10-13"
LAST_DAY = "2026-10-14"
COVERAGE_START = "2026-01-01"
EVIDENCE_ENV = "ASHARE_PILOT_EVIDENCE"
# Generated from the reviewed manifest, not supplied by the capture caller.
BASELINE_SHA256 = "781f8bf424794aebc046f285ff28445c033b5f2ee4d72eba6de49bb1451e0caa"
CODES = ("600000", "600036", "601318")
REPLAN_FIRST_DAY = "2026-10-14"
REPLAN_LAST_DAY = "2026-10-15"
REPLAN_SHA256 = "15d766cd4f638322886af02904cf4d8908e295dd1dbc40107535237c2f288a04"


def date_scope(replan=False, *, extension=True):
    if replan and extension:
        from .action_extension import selection, load

        if selection() is not None:
            manifest, _ = load()
            return manifest["from_day"], manifest["through_day"]
    return (REPLAN_FIRST_DAY, REPLAN_LAST_DAY) if replan else (FIRST_DAY, LAST_DAY)


def baseline_identity(*, replan=False):
    if replan:
        from .action_extension import selection

        chosen = selection()
        if chosen is not None:
            return chosen[1]
    return REPLAN_SHA256 if replan else BASELINE_SHA256


def catalog_url(code, day):
    return "https://np-anotice-stock.eastmoney.com/api/security/ann?" + urlencode(
        dict(
            sr=-1,
            page_size=100,
            page_index=1,
            ann_type="A",
            client_source="web",
            stock_list=code,
            begin_time=COVERAGE_START,
            end_time=day,
        )
    )


def content_url(article, page):
    return "https://np-cnotice-stock.eastmoney.com/api/content/ann?" + urlencode(
        dict(art_code=article, client_source="web", page_index=page)
    )


def catalog_projection(value, code, observed_at):
    data = value["data"]
    rows = data["list"]
    require(
        value["success"] == 1
        and not value.get("error")
        and data["page_index"] == 1
        and data["page_size"] == 100
        and len(rows) == data["total_hits"] <= 100,
        "ACTION_CATALOG_INCOMPLETE",
        "目录必须完整；超过单页须重新审阅采集方案",
    )
    result = {}
    for row in rows:
        require(
            any(c["stock_code"] == code for c in row["codes"])
            and COVERAGE_START <= row["notice_date"][:10] <= observed_at[:10]
            and row["art_code"] not in result,
            "ACTION_CATALOG_IDENTITY",
            "证券、日期或唯一性不符",
        )
        result[row["art_code"]] = {
            # display_time varied across all 218 unchanged records in two
            # actual captures; it is preserved in raw bytes, not a publication
            # or revision authority. Critical full text is rechecked below.
            k: row[k]
            for k in ("title", "notice_date", "eiTime", "codes", "columns")
        }
    return result


def content_projection(value, article, code, pages):
    d = value["data"]
    require(
        value["success"] == 1
        and d["art_code"] == article
        and d["page_size"] == pages
        and pages >= 1
        and d.get("is_ai_summary") == 0
        and any(s["stock"] == code and s["market_uni"] == "1" for s in d["security"])
        and isinstance(d["notice_content"], str)
        and d["notice_content"].strip(),
        "ACTION_TEXT_INCOMPLETE",
        "正文身份、完整分页或非摘要标识不符",
    )
    # Ignore transport/display counters only. Text, issuer identity, dates,
    # attachment location and declared page count must stay exactly unchanged.
    return {
        k: d[k]
        for k in (
            "art_code",
            "notice_content",
            "notice_title",
            "notice_date",
            "page_size",
            "security",
            "attach_url",
            "eitime",
            "is_ai_summary",
        )
    }


def compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def cash_from_text(text, code, notice_date, *, first_day=FIRST_DAY):
    text = compact(text)
    require(
        code in text[:180] and "实施公告" in text[:180] and "差异化分红送转:否" in text,
        "ACTION_CASH_SCHEMA",
        "必须为对应普通A股现金实施公告",
    )
    date_rows = re.findall(
        r"(?:普通股|A股)(2026/\d{1,2}/\d{1,2})[—－−-]+"
        r"(2026/\d{1,2}/\d{1,2})(2026/\d{1,2}/\d{1,2})",
        text,
    )
    require(
        len(date_rows) >= 2 and len(set(date_rows)) == 1,
        "ACTION_CASH_DATES",
        "提示与正文中的登记/除息/支付表须一致",
    )
    amounts = re.findall(r"每股(?:派发)?现金红利(?:人民币)?([\d.]+)元\(含税\)", text)
    require(
        len(amounts) >= 2 and Decimal(amounts[0]) == Decimal(amounts[1]),
        "ACTION_CASH_AMOUNT",
        "提示与分配方案的本次每股金额不一致",
    )
    record, ex, pay = (date(*map(int, d.split("/"))).isoformat() for d in date_rows[0])
    require(
        notice_date <= record < ex <= pay < first_day and Decimal(amounts[0]) > 0,
        "ACTION_CASH_SCOPE",
        "本批仅允许已实施且早于建账的现金事件",
    )
    return dict(
        type="cash_dividend",
        record_date=record,
        ex_date=ex,
        pay_date=pay,
        cash_per_share=str(Decimal(amounts[0])),
        notice_date=notice_date,
        record_before_pilot_genesis=True,
    )


def load_baseline(root=None, *, replan=False, extension=True):
    """Read separately held evidence; the public package contains no corpus.

    An explicit root selects one baseline directory. Otherwise the environment
    selects the family root, containing the original and replan directories.
    Location is configurable; the reviewed content hashes are not.
    """
    if replan and extension and root is None:
        from .action_extension import selection, load

        if selection() is not None:
            return load()
    if root is None:
        configured = os.environ.get(EVIDENCE_ENV)
        require(configured, "ACTION_BASELINE_REQUIRED", f"须显式设置 {EVIDENCE_ENV} 为本地证据目录")
        root = Path(configured).expanduser()
        if replan:
            root = root / "replan-20261014-15"
    root = Path(root).expanduser()
    first_day, last_day = date_scope(replan, extension=False)
    expected_sha = REPLAN_SHA256 if replan else BASELINE_SHA256
    require((root / "manifest.json").is_file(), "ACTION_BASELINE_MISSING", "缺少本地审阅基线清单")
    raw = (root / "manifest.json").read_bytes()
    require(sha(raw) == expected_sha, "ACTION_BASELINE_CHANGED", "审阅基线摘要不符")
    manifest = json.loads(raw)
    require(
        manifest["schema"] == "ad-three-security-action-review.v1"
        and manifest["from_day"] == first_day
        and manifest["through_day"] == last_day
        and tuple(manifest["securities"]) == CODES,
        "ACTION_BASELINE_SCOPE",
        "有限审阅范围不同",
    )
    blobs = {}
    for name, source in manifest["sources"].items():
        path = root / "blobs" / source["sha256"]
        require(path.is_file(), "ACTION_BASELINE_MISSING", name)
        body = path.read_bytes()
        require(
            sha(body) == source["sha256"]
            and source["http_status"] == 200
            and timestamp(source["started_at"])
            <= timestamp(source["finished_at"])
            <= timestamp(manifest["completed_at"]),
            "ACTION_BASELINE_SOURCE",
            name,
        )
        blobs[name] = body
    for code, security in manifest["securities"].items():
        identity = [
            r for r in json.loads(blobs[security["cninfo_identity_source"]]) if r["code"] == code
        ]
        require(
            len(identity) == 1
            and identity[0]["category"] == "A股"
            and identity[0]["type"] == "shj"
            and identity[0]["delisted"] == "false",
            "ACTION_ISSUER_IDENTITY",
            "巨潮主体标识须精确匹配A股",
        )
        cn_rows = []
        for page in (1, 2, 3):
            source_name = f"cninfo-catalog-{code}-dated-p{page}.json"
            source = manifest["sources"][source_name]
            params = parse_qs(source["request_body"])
            v = json.loads(blobs[source_name])
            require(
                source["url"] == "https://www.cninfo.com.cn/new/hisAnnouncement/query"
                and params["stock"] == [code + "," + identity[0]["orgId"]]
                and params["pageNum"] == [str(page)]
                and params["seDate"] == ["2026-01-01~2026-10-13"]
                and v["hasMore"] is (page != 3)
                and v["totalAnnouncement"] == security["cninfo_count"],
                "ACTION_ISSUER_CATALOG",
                "主体、查询范围或完整分页不符",
            )
            cn_rows.extend(v["announcements"])
        require(
            len(cn_rows) == len({r["announcementId"] for r in cn_rows}) == security["cninfo_count"]
            and all(
                r["secCode"] == code
                and r["orgId"] == identity[0]["orgId"]
                and datetime.fromtimestamp(r["announcementTime"] / 1000, timezone.utc)
                <= timestamp(manifest["completed_at"])
                for r in cn_rows
            ),
            "ACTION_ISSUER_CATALOG",
            "主体目录漏页、重复或未来公告",
        )
        name = security["catalog_source"]
        projection = catalog_projection(
            json.loads(blobs[name]), code, manifest["sources"][name]["finished_at"]
        )
        require(
            set(projection) == set(security["catalog_review"]),
            "ACTION_BASELINE_REVIEW",
            "每条目录均须有固定分类记录",
        )
        for article, document in security["documents"].items():
            require(
                article in projection and document["review_note"],
                "ACTION_BASELINE_REVIEW",
                "关键正文必须属于该证券目录",
            )
            texts = []
            require(
                len(document["sources"]) == document["pages"],
                "ACTION_TEXT_INCOMPLETE",
                "审阅页数不同",
            )
            for page, source_name in enumerate(document["sources"], 1):
                require(
                    manifest["sources"][source_name]["url"] == content_url(article, page),
                    "ACTION_BASELINE_SOURCE",
                    "正文分页URL不符",
                )
                p = content_projection(
                    json.loads(blobs[source_name]), article, code, document["pages"]
                )
                require(
                    p["notice_title"] == projection[article]["title"]
                    and p["notice_date"] == projection[article]["notice_date"],
                    "ACTION_TEXT_IDENTITY",
                    "正文与目录不一致",
                )
                texts.append(p["notice_content"])
            if document["classification"] == "ordinary_cash_implemented":
                cash = cash_from_text(
                    "".join(texts),
                    code,
                    projection[article]["notice_date"][:10],
                    first_day=first_day,
                )
                require(
                    cash == document["cash_event"], "ACTION_CASH_CHANGED", "现金事件须由正文重算"
                )
                require(
                    blobs[document["issuer_pdf_source"]].startswith(b"%PDF-"),
                    "ACTION_ISSUER_PDF",
                    "原始发行人PDF缺失",
                )
                pdf_url = manifest["sources"][document["issuer_pdf_source"]]["url"]
                require(
                    sum(
                        "https://static.cninfo.com.cn/" + r["adjunctUrl"] == pdf_url
                        for r in cn_rows
                    )
                    == 1,
                    "ACTION_ISSUER_PDF",
                    "PDF必须绑定到该主体完整目录中唯一公告",
                )
    if replan:
        from .live import CALENDAR_URL, _calendar

        previous, old_blobs = load_baseline(root.parent)
        review = manifest["scope_review"]
        calendar_source = manifest["sources"][review["calendar_source"]]
        year, closed = _calendar(blobs[review["calendar_source"]])
        dates = (date.fromisoformat(first_day), date.fromisoformat(last_day))
        require(
            review["previous_baseline_sha256"] == BASELINE_SHA256
            and review["reviewed_at"] == manifest["completed_at"]
            and calendar_source["url"] == CALENDAR_URL
            and (dates[1] - dates[0]).days == 1
            and all(d.year == year and d.weekday() < 5 and d not in closed for d in dates),
            "ACTION_REPLAN_CALENDAR",
            "顺延必须绑定旧基线及实际交易所日历",
        )
        # This finite extension is justified only by a complete, freshly reread,
        # unchanged corpus; a changed disclosure requires a different review.
        for code, item in manifest["securities"].items():
            old_item = previous["securities"][code]
            name = item["catalog_source"]
            require(
                catalog_projection(json.loads(blobs[name]), code, manifest["completed_at"])
                == catalog_projection(
                    json.loads(old_blobs[old_item["catalog_source"]]),
                    code,
                    previous["completed_at"],
                )
                and set(item["documents"]) == set(old_item["documents"]),
                "ACTION_REPLAN_CHANGED",
                "目录或已审阅文档集合改变",
            )
            names = [name]
            for article, doc in item["documents"].items():
                old_doc = old_item["documents"][article]
                require(doc["pages"] == old_doc["pages"], "ACTION_REPLAN_CHANGED", article)
                for new_name, old_name in zip(doc["sources"], old_doc["sources"], strict=True):
                    require(
                        content_projection(json.loads(blobs[new_name]), article, code, doc["pages"])
                        == content_projection(
                            json.loads(old_blobs[old_name]), article, code, doc["pages"]
                        ),
                        "ACTION_REPLAN_CHANGED",
                        article,
                    )
                names.extend(doc["sources"])
            require(
                all(
                    timestamp(manifest["sources"][n]["started_at"])
                    > timestamp(previous["completed_at"])
                    for n in names
                ),
                "ACTION_REPLAN_STALE",
                "旧来源不能改写为新范围的本次复读",
            )
    return manifest, blobs


def specs(day, *, replan=False):
    manifest, _ = load_baseline(replan=replan)
    result = {}
    for code, item in manifest["securities"].items():
        result["action_catalog_" + code] = (
            catalog_url(code, day),
            "https://data.eastmoney.com/notices/",
        )
        for article, document in item["documents"].items():
            for page in range(1, document["pages"] + 1):
                result[f"action_text_{article}_{page}"] = (
                    content_url(article, page),
                    f"https://data.eastmoney.com/notices/detail/{code}/{article}.html",
                )
    return result


def assess_security(code, day, sources, bodies, inventory, *, replan=False):
    first_day, last_day = date_scope(replan)
    require(first_day <= day <= last_day, "ACTION_DATE_SCOPE", "超过本次真实观测范围")
    manifest, old_bodies = load_baseline(replan=replan)
    review = manifest["securities"][code]
    hashes, receipts = {}, []

    def current(name):
        source = sources[name]
        require(source.get("sha256") in bodies, "ACTION_SOURCE_MISSING", name)
        require(
            timestamp(source["observed_at"]) >= timestamp(manifest["completed_at"]),
            "ACTION_REVIEW_NOT_VISIBLE",
            "来源早于审阅基线",
        )
        hashes[name] = source["sha256"]
        receipts.append(source["observed_at"])
        return json.loads(bodies[source["sha256"]])

    name = "action_catalog_" + code
    current_catalog = catalog_projection(current(name), code, sources[name]["observed_at"])
    old_catalog = catalog_projection(
        json.loads(old_bodies[review["catalog_source"]]), code, manifest["completed_at"]
    )
    require(
        current_catalog == old_catalog,
        "ACTION_CATALOG_CHANGED",
        "新增、消失或修订公告须重新审阅，不能自动当作无行动",
    )
    events = []
    for article, document in review["documents"].items():
        for page, source_name in enumerate(document["sources"], 1):
            latest = content_projection(
                current(f"action_text_{article}_{page}"), article, code, document["pages"]
            )
            previous = content_projection(
                json.loads(old_bodies[source_name]), article, code, document["pages"]
            )
            require(latest == previous, "ACTION_TEXT_CHANGED", article)
        if document["classification"] == "ordinary_cash_implemented":
            events.append(
                {
                    **document["cash_event"],
                    "article": article,
                    "issuer_pdf_sha256": manifest["sources"][document["issuer_pdf_source"]][
                        "sha256"
                    ],
                }
            )

    # The independent dividend table cannot introduce or silently lose a 2026
    # implementation compared with the issuer texts. Older records have no
    # entitlement in this zero-genesis pilot and need no invented payment date.
    def key(event):
        return event["record_date"], event["ex_date"], Decimal(event["cash_per_share"])

    relevant = [e for e in inventory["events"] if e["ex_date"] >= COVERAGE_START]
    require(
        inventory["cash_history_complete"]
        and not inventory["unresolved"]
        and sorted(map(key, relevant)) == sorted(map(key, events)),
        "ACTION_CASH_INVENTORY_CHANGED",
        "实施公告与独立分红表不一致",
    )
    proof = dict(
        baseline_sha256=baseline_identity(replan=replan),
        security=code + ".XSHG",
        day=day,
        sources=hashes,
        catalog_count=len(current_catalog),
        reviewed_text_count=len(review["documents"]),
        reviewed_at=manifest["completed_at"],
        fresh_check_completed_at=max(receipts),
        basis="bounded_reviewed_issuer_corpus_and_unchanged_current_sources",
    )
    scope = dict(
        from_day=first_day,
        through_day=last_day,
        security=code + ".XSHG",
        relevant_types=[
            "cash_entitlement",
            "price_adjustment",
            "share_transformation",
            "code_change",
        ],
        actions_known_absent=False,
        price_share_code_review=dict(status="proven", source_sha256=sha(canonical(proof)), **proof),
    )
    cash = dict(
        events=events,
        proposals=inventory["proposals"],
        unresolved=[],
        cash_history_complete=True,
        coverage_from=COVERAGE_START,
        excluded_pre_2026_events=(
            len(inventory["events"])
            - len(relevant)
            + len(inventory.get("excluded_earlier_events", []))
        ),
        entitlement_owner="AQ native account genesis and record checkpoints",
    )
    return scope, cash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="所选基线目录；省略时读取 ASHARE_PILOT_EVIDENCE")
    parser.add_argument(
        "--replan", action="store_true", help="校验后续 14/15 范围及同目录族的原基线"
    )
    args = parser.parse_args()
    manifest, _ = load_baseline(args.root, replan=args.replan)
    print(
        json.dumps(
            dict(
                baseline_sha256=baseline_identity(replan=args.replan),
                completed_at=manifest["completed_at"],
                from_day=manifest["from_day"],
                through_day=manifest["through_day"],
                securities={c: len(s["documents"]) for c, s in manifest["securities"].items()},
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
