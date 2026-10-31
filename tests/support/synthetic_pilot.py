"""Self-authored protocol fixtures, never captured issuer or market data.

All names, article IDs, cash amounts, text, PDF markers and receipts below are
invented. Three catalogs and nine text pages add 12 action requests to the
15 market requests (27 admission / 33 execution requests before retries).
Production pins reject these baselines. Tests substitute pins only
through pytest's monkeypatch fixture; no such override exists in the package.
"""

from decimal import Decimal
from urllib.parse import urlencode

from ashare_data.observations.action_baseline import CODES, catalog_url, content_url
from ashare_data.observations.daily import sha
from ashare_data.market.client import CALENDAR_URL
from ashare_data.core.model import canonical

CASH = dict(zip(CODES, ("1.23", "2.34", "3.45"), strict=True))


def dividends(code):
    rows = [
        dict(
            SECURITY_CODE=code,
            ASSIGN_PROGRESS="实施分配",
            EQUITY_RECORD_DATE="2026-06-03",
            EX_DIVIDEND_DATE="2026-06-04",
            NOTICE_DATE="2026-06-01",
            PRETAX_BONUS_RMB=str(Decimal(CASH[code]) * 10),
        ),
        # Deliberately invalid old notice ordering exercises bounded exclusion.
        dict(
            SECURITY_CODE=code,
            ASSIGN_PROGRESS="实施分配",
            EQUITY_RECORD_DATE="2014-02-03",
            EX_DIVIDEND_DATE="2014-02-04",
            NOTICE_DATE="2014-02-05",
            PRETAX_BONUS_RMB="1",
        ),
    ]
    return dict(
        classification="synthetic",
        success=True,
        code=0,
        result=dict(pages=1, count=len(rows), data=rows),
    )


def quote(code="600000", price=10):
    return dict(
        classification="synthetic",
        date=20261014,
        time=93333,
        total=1,
        begin=0,
        end=1,
        list=[[code, "合成证券", price, price, price, price * 1.1, price * 0.9, "T111"]],
    )


def write_baselines(root):
    def build(path, *, replan=False, previous_sha=None):
        received = "2026-10-13T01:00:01+00:00" if replan else "2026-10-12T10:00:01+00:00"
        started = received.replace("00:01", "00:00")
        completed = "2026-10-13T01:10:00+00:00" if replan else "2026-10-12T10:10:00+00:00"
        manifest = dict(
            schema="ad-three-security-action-review.v1",
            classification="synthetic",
            from_day="2026-10-14" if replan else "2026-10-13",
            through_day="2026-10-15" if replan else "2026-10-14",
            coverage_from="2026-01-01",
            completed_at=completed,
            securities={},
            sources={},
        )
        (path / "blobs").mkdir(parents=True)

        def add(name, value, url, **extra):
            body = value if isinstance(value, bytes) else canonical(value)
            digest = sha(body)
            (path / "blobs" / digest).write_bytes(body)
            manifest["sources"][name] = dict(
                url=url,
                sha256=digest,
                started_at=started,
                finished_at=received,
                http_status=200,
                **extra,
            )
            return name

        for code in CODES:
            org = "synthetic-org-" + code
            identity = add(
                "identity-" + code,
                [
                    dict(
                        code=code,
                        orgId=org,
                        category="A股",
                        type="shj",
                        delisted="false",
                        name="合成主体",
                    )
                ],
                "https://example.invalid/identity",
            )
            for page in (1, 2, 3):
                add(
                    f"cninfo-catalog-{code}-dated-p{page}.json",
                    dict(
                        classification="synthetic",
                        hasMore=page != 3,
                        totalAnnouncement=3,
                        announcements=[
                            dict(
                                announcementId=f"SYNTHETIC-{code}-{page}",
                                secCode=code,
                                orgId=org,
                                announcementTime=1780272000000,
                                adjunctUrl=f"synthetic/{code}-{page}.pdf",
                            )
                        ],
                    ),
                    "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                    request_body=urlencode(
                        dict(stock=code + "," + org, pageNum=page, seDate="2026-01-01~2026-10-13")
                    ),
                )
            pdf = add(
                f"cninfo-cash-{code}.pdf",
                b"%PDF-1.4\nSynthetic test marker, not a real PDF.\n",
                f"https://static.cninfo.com.cn/synthetic/{code}-1.pdf",
            )
            records, documents = [], {}
            for kind, pages in [("CASH", 1), ("GENERAL", 2)]:
                article = f"SYNTHETIC-{code}-{kind}"
                title = "合成测试实施公告" if kind == "CASH" else "合成测试普通事项"
                notice_date = "2026-06-01 00:00:00"
                records.append(
                    dict(
                        art_code=article,
                        codes=[dict(stock_code=code)],
                        title=title,
                        notice_date=notice_date,
                        eiTime="synthetic-ingestion-v1",
                        columns=[dict(column_name="合成测试")],
                        display_time="synthetic",
                    )
                )
                names = []
                for page in range(1, pages + 1):
                    text = f"{code} 合成测试普通事项第 {page} 页。无真实公司事实。"
                    if kind == "CASH":
                        table = "普通股2026/6/3-2026/6/42026/6/4"
                        amount = f"每股现金红利人民币{CASH[code]}元(含税)"
                        text = f"{code} 合成测试实施公告 差异化分红送转:否 {table} {amount} {table} {amount}"
                    names.append(
                        add(
                            f"text-{article}-{page}",
                            dict(
                                classification="synthetic",
                                success=1,
                                data=dict(
                                    art_code=article,
                                    page_size=pages,
                                    is_ai_summary=0,
                                    security=[dict(stock=code, market_uni="1")],
                                    notice_content=text,
                                    notice_title=title,
                                    notice_date=notice_date,
                                    attach_url="https://example.invalid/synthetic",
                                    eitime="synthetic",
                                ),
                            ),
                            content_url(article, page),
                        )
                    )
                doc = dict(
                    classification="ordinary_cash_implemented"
                    if kind == "CASH"
                    else "synthetic_general",
                    review_note="Self-authored test input only.",
                    sources=names,
                    pages=pages,
                )
                if kind == "CASH":
                    doc.update(
                        issuer_pdf_source=pdf,
                        cash_event=dict(
                            type="cash_dividend",
                            record_date="2026-06-03",
                            ex_date="2026-06-04",
                            pay_date="2026-06-04",
                            cash_per_share=CASH[code],
                            notice_date="2026-06-01",
                            record_before_pilot_genesis=True,
                        ),
                    )
                documents[article] = doc
            catalog = add(
                "catalog-" + code,
                dict(
                    classification="synthetic",
                    success=1,
                    data=dict(list=records, page_index=1, page_size=100, total_hits=len(records)),
                ),
                catalog_url(code, "2026-10-13"),
            )
            manifest["securities"][code] = dict(
                cninfo_identity_source=identity,
                cninfo_count=3,
                catalog_source=catalog,
                catalog_review={
                    r["art_code"]: dict(classification="synthetic_review") for r in records
                },
                documents=documents,
            )
        if replan:
            html = "<p>合成测试 2026年休市安排；非交易所实际安排。</p>" + "".join(
                f"{m}月1日（星期一）至{m}月2日（星期二）休市" for m in range(1, 8)
            )
            calendar = add("synthetic-calendar", html.encode(), CALENDAR_URL)
            manifest["scope_review"] = dict(
                previous_baseline_sha256=previous_sha,
                reviewed_at=completed,
                calendar_source=calendar,
            )
        raw = canonical(manifest)
        (path / "manifest.json").write_bytes(raw)
        return sha(raw)

    original = build(root)
    replan = build(root / "replan-20261014-15", replan=True, previous_sha=original)
    return dict(original=original, replan=replan)
