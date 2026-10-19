"""Protocol and gating fixtures are synthetic, not issuer observations."""

import gzip
import json
from datetime import datetime

import pytest

from ashare_data.financials.official_expansion import (
    Client,
    Deferred,
    SourceExhausted,
    index_security,
    report_identity,
    replay,
    select_documents,
    statement_cells,
)
from ashare_data.financials.official_access import reviewed_ledger
from ashare_data.financials.expansion import digest, encoded
from ashare_data.financials.ledger import RESEARCH_MODE, VisibilityUnproven


@pytest.fixture
def reviewed_fixture(tmp_path):
    def source(key, raw, url=None):
        path = tmp_path / "raw" / (key + ".gz")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(gzip.compress(raw))
        receipt = {
            "state": "complete",
            "path": str(path.relative_to(tmp_path)),
            "sha256": digest(raw),
            "bytes": len(raw),
            "request": {"url": url},
        }
        folder = tmp_path / "receipts"
        folder.mkdir(exist_ok=True)
        (folder / (key + ".json")).write_bytes(encoded(receipt))
        return receipt

    source("pdf-notice", b"synthetic correction notice", "https://static.cninfo.com.cn/notice.pdf")
    review = {"schema": "ad.official-field-review.v1", "fields": []}
    indexed = []
    issuer = {"code": "600000", "category": "A股", "orgId": "synthetic"}
    source("identity-sh600000", encoded([issuer]))
    for aid, day, value, parent in [
        ("one", "2015-04-29", "100.00", None),
        ("two", "2015-06-06", "90.00", "one"),
    ]:
        url = "https://static.cninfo.com.cn/" + aid + ".pdf"
        pdf = source("pdf-" + aid, ("synthetic original " + aid).encode(), url)
        raw_row = dict(secCode="600000", orgId="synthetic", announcementId=aid,
                       announcementTitle="2015年第一季度报告" + ("（修订版）" if parent else ""),
                       announcementTime=int(datetime.fromisoformat(day + "T00:00:00+08:00").timestamp() * 1000),
                       adjunctUrl=aid + ".pdf")
        source("index-" + aid, encoded({"announcements": [raw_row]}))
        report = report_identity(raw_row) | {"index_receipt": "index-" + aid}
        indexed.append(report)
        cell = dict(
            field="net_operate_cash_flow",
            value=value,
            page=1,
            security="600000.XSHG",
            period_start="2015-01-01",
            period_end="2015-03-31",
            basis="YTD",
            scope="consolidated",
            currency="CNY",
            unit="CNY",
            review_status="native_text_candidate_requires_layout_review",
            provider_matches=[],
        )
        document = dict(
            report=report,
            cells=[cell],
            pdf_receipt="pdf-" + aid,
            document_sha256=pdf["sha256"],
            observed_at="2026-10-19T00:00:00+00:00",
        )
        blob = gzip.compress(encoded(document))
        (tmp_path / "documents").mkdir(exist_ok=True)
        (tmp_path / "documents" / ("sh600000-" + aid + ".json.gz")).write_bytes(blob)
        review["fields"].append(
            dict(
                symbol="sh600000",
                announcement_id=aid,
                record_id=aid,
                field=cell["field"],
                value=value,
                page=1,
                document_sha256=pdf["sha256"],
                document_derivative_sha256=digest(blob),
                layout_reviewed=True,
                replaces=parent,
                supporting_receipts=["pdf-notice"] if parent else [],
            )
        )
    raw_notice = dict(raw_row, announcementId="notice", adjunctUrl="notice.pdf",
                      announcementTitle="2015年第一季度报告更正公告")
    source("index-notice", encoded({"announcements": [raw_notice]}))
    source("index-empty", encoded({"announcements": None}))
    indexed.append(report_identity(raw_notice) | {"index_receipt": "index-notice"})
    (tmp_path / "indices").mkdir(exist_ok=True)
    (tmp_path / "indices/sh600000.json.gz").write_bytes(gzip.compress(encoded(dict(
        security="600000.XSHG", issuer=issuer, announcements=indexed,
        sources=["identity-sh600000", "index-one", "index-two", "index-notice", "index-empty"],
        queries=[dict(kind=k, pagination_complete_for_query=True)
                 for k in ("periodic", "correction", "revision")],
    ))))
    return tmp_path, review


def test_reviewed_versions_use_official_dates_but_remain_strictly_unproven(reviewed_fixture):
    root, review = reviewed_fixture
    ledger = reviewed_ledger(root, review)
    for cutoff, value in [
        ("2015-05-01T00:00:00+08:00", "100.00"),
        ("2015-06-07T00:00:00+08:00", "90.00"),
    ]:
        assert (
            ledger.select(
                "600000.XSHG",
                "net_operate_cash_flow",
                "2015-03-31",
                as_of=cutoff,
                mode=RESEARCH_MODE,
            )["value"]
            == value
        )
        with pytest.raises(VisibilityUnproven):
            ledger.select(
                "600000.XSHG", "net_operate_cash_flow", "2015-03-31", as_of=cutoff, mode="strict"
            )


def test_review_cannot_approve_changed_or_unreviewed_evidence(reviewed_fixture):
    root, review = reviewed_fixture
    review["fields"][0]["layout_reviewed"] = False
    with pytest.raises(ValueError, match="layout review"):
        reviewed_ledger(root, review)
    review["fields"][0]["layout_reviewed"] = True
    (root / "raw/pdf-one.gz").write_bytes(gzip.compress(b"changed source"))
    with pytest.raises(ValueError, match="checksum"):
        reviewed_ledger(root, review)


def daily_view(fixture, monkeypatch):
    from ashare_data.financials import official_daily

    sessions = {"XSHG": {d: {"source": "synthetic_session_evidence"}
                         for d in ("2015-04-30", "2015-06-08")}}
    monkeypatch.setattr(official_daily, "traded_sessions", lambda *a: sessions)
    root, review = fixture
    return official_daily.reader(root, review, reference_root=root, reference_tasks=["synthetic"])


def test_daily_versions_need_no_second_clock_and_change_only_after_later_session(reviewed_fixture, monkeypatch):
    from ashare_data.financials.ledger import DAILY_MODE

    view = daily_view(reviewed_fixture, monkeypatch)
    for day, value in [("2015-04-29", None), ("2015-04-30", "100.00"),
                       ("2015-06-06", "100.00"), ("2015-06-07", "100.00"),
                       ("2015-06-08", "90.00")]:
        row = view.query("600000.XSHG", "net_operate_cash_flow", period="2015-03-31",
                         as_of=day, mode=DAILY_MODE)["rows"][0]
        assert row["value"] == value
        if value is not None:
            assert row["daily_pit_usable"]
            assert row["terms"][0]["published_at"] is None
            assert row["terms"][0]["captured_at"].startswith("2026")


def test_daily_reader_checks_original_index_date_not_only_hashes(reviewed_fixture, monkeypatch):
    root, _ = reviewed_fixture
    path = root / "indices/sh600000.json.gz"
    index = json.loads(gzip.decompress(path.read_bytes()))
    index["announcements"][0]["published_date"] = "2015-04-01"
    path.write_bytes(gzip.compress(encoded(index)))
    with pytest.raises(ValueError, match="differs from captured index"):
        daily_view(reviewed_fixture, monkeypatch)


def test_known_unreviewed_version_blocks_only_after_its_official_date(reviewed_fixture, monkeypatch):
    from ashare_data.financials.ledger import DAILY_MODE

    root, review = reviewed_fixture
    review["fields"] = review["fields"][:1]
    view = daily_view((root, review), monkeypatch)
    args = ("600000.XSHG", "net_operate_cash_flow")
    assert view.query(*args, period="2015-03-31", as_of="2015-06-05", mode=DAILY_MODE)["rows"][0]["value"] == "100.00"
    row = view.query(*args, period="2015-03-31", as_of="2015-06-08", mode=DAILY_MODE)["rows"][0]
    assert row["status"] == "visibility_unproven" and "known revision" in row["reason"]


def test_derived_index_cannot_hide_known_revision(reviewed_fixture, monkeypatch):
    root, _ = reviewed_fixture
    path = root / "indices/sh600000.json.gz"
    index = json.loads(gzip.decompress(path.read_bytes()))
    index["announcements"] = index["announcements"][:1]
    path.write_bytes(gzip.compress(encoded(index)))
    with pytest.raises(ValueError, match="versions missing"):
        daily_view(reviewed_fixture, monkeypatch)


def test_daily_reader_does_not_borrow_another_exchanges_sessions(reviewed_fixture, monkeypatch):
    from ashare_data.financials import official_daily
    from ashare_data.financials.ledger import DAILY_MODE

    monkeypatch.setattr(official_daily, "traded_sessions", lambda *a: {"XSHE": {"2015-04-30": {}}})
    root, review = reviewed_fixture
    view = official_daily.reader(root, review, reference_root=root, reference_tasks=["synthetic"])
    row = view.query("600000.XSHG", "net_operate_cash_flow", period="2015-03-31",
                     as_of="2015-04-30", mode=DAILY_MODE)["rows"][0]
    assert row["status"] == "visibility_unproven"


def test_same_day_explicit_correction_orders_daily_versions_without_clock(reviewed_fixture, monkeypatch):
    from ashare_data.financials.ledger import DAILY_MODE, FinancialLedger

    view = daily_view(reviewed_fixture, monkeypatch)
    pack = view._pack
    pack["reports"]["two"]["published_date"] = "2015-04-29"
    pack["reports"]["two"]["daily_visibility"]["available_from"] = "2015-04-30T00:00:00+08:00"
    row = FinancialLedger(pack).select("600000.XSHG", "net_operate_cash_flow", "2015-03-31",
                                       as_of="2015-04-30T00:00:00+08:00", mode=DAILY_MODE)
    assert row["value"] == "90.00"


def test_offline_replay_preserves_exact_reviewed_documents(reviewed_fixture):
    root, review = reviewed_fixture
    (root / "field-reviews.json").write_bytes(encoded(review))
    (root / "manifest.json").write_bytes(encoded({"source_batch": str(root), "target_codes": 1}))
    (root / "indices").mkdir(exist_ok=True)
    (root / "indices/sh600000.json.gz").write_bytes(
        gzip.compress(
            encoded(
                {
                    "announcements": [
                        dict(
                            announcement_id=aid,
                            announcement_time_raw=epoch,
                            full_report_candidate=True,
                            period_end="2015-03-31",
                        )
                        for aid, epoch in [("one", 1430236800000), ("two", 1433520000000)]
                    ]
                }
            )
        )
    )
    before = {p.name: p.read_bytes() for p in (root / "documents").glob("*.gz")}
    # No extractor or network can be used for these already reviewed documents.
    assert replay(root, None)["selected_documents_captured"] == 2
    assert before == {p.name: p.read_bytes() for p in (root / "documents").glob("*.gz")}
    reviewed_ledger(root, review)


def announcement(title="2024年第一季度报告", aid="SYNTHETIC"):
    return dict(
        announcementTitle=title,
        announcementId=aid,
        announcementTime=1714435200000,
        secCode="600000",
        orgId="synthetic",
        adjunctUrl="synthetic.pdf",
    )


def test_title_dates_cannot_prove_historical_visibility():
    report = report_identity(announcement())
    assert report["period_end"] == "2024-03-31"
    assert report["published_at"] is None and not report["first_publication_proven"]
    assert not report_identity(announcement("2024年第一季度报告更正公告"))["full_report_candidate"]
    assert not report_identity(announcement("2024年第一季度报告摘要"))["full_report_candidate"]
    assert report_identity(announcement("2024年第一季度报告（修订版）"))["revision_label"]
    old = announcement()
    old["announcementTime"] = 1430236800000
    assert report_identity(old)["published_date"] == "2015-04-29"


def test_selection_keeps_all_versions_of_historical_cohort_periods():
    reports = [
        report_identity(announcement(f"{year}年第一季度报告", str(year)))
        for year in range(2010, 2027)
    ]
    reports.append(report_identity(announcement("2010年第一季度报告（修订版）", "correction")))
    selected = select_documents({"announcements": reports})
    assert len(selected) == 4
    assert {r["period_end"] for r in selected} == {"2010-03-31", "2018-03-31", "2026-03-31"}


def test_statement_scope_unit_and_column_gates():
    report = report_identity(announcement())
    source = "合并利润表\n单位：元 币种：人民币\n项目 2024年第一季度 2023年第一季度\n一、营业总收入 1,234.00 567.00\n母公司利润表\n单位：元\n项目 本期发生额 上期发生额\n一、营业总收入 999.00 666.00"

    def parse(text):
        return statement_cells({"pages": [{"page": 1, "text": text}]}, report)

    rows = parse(source)
    assert len(rows) == 1 and rows[0]["value"] == "1234.00"
    assert rows[0]["review_status"] == "native_text_candidate_requires_layout_review"
    assert not parse(source.replace("单位：元", "单位：万元"))
    assert not parse(
        source.replace("2024年第一季度 2023年第一季度", "2023年第一季度 2024年第一季度")
    )
    assert not statement_cells(
        {"pages": [{"page": 1, "text": source}]}, {"period_end": "2024-09-30"}
    )
    # Older parent-only statements may omit the explicit parent-company prefix.
    assert parse(source.replace("母公司利润表", "利 润 表")) == rows


def test_cache_integrity_and_no_duplicate_network(tmp_path):
    calls = []

    def transport(url, data):
        calls.append(url)
        return 200, b"public synthetic", {}

    client = Client(tmp_path, transport=transport)
    url = "https://www.cninfo.com.cn/synthetic"
    assert client.fetch("one", url)[0] == client.fetch("one", url)[0]
    assert len(calls) == 1
    (tmp_path / "raw/one.gz").write_bytes(gzip.compress(b"changed"))
    with pytest.raises(ValueError, match="checksum"):
        client.fetch("one", url)


def test_429_applies_to_all_requests_and_is_persisted(tmp_path):
    client = Client(tmp_path, transport=lambda u, d: (429, b"slow down", {"Retry-After": "7200"}))
    with pytest.raises(Deferred):
        client.fetch("one", "https://www.cninfo.com.cn/synthetic")
    restored = Client(tmp_path, transport=lambda *_: pytest.fail("cooldown bypassed"))
    with pytest.raises(Deferred, match="cooldown"):
        restored.fetch("two", "https://www.cninfo.com.cn/other")
    assert restored.state["www.cninfo.com.cn"]["interval"] >= 10
    assert not (tmp_path / "raw/one.gz").exists()


def test_exhausted_requests_are_terminal(tmp_path):
    (tmp_path / "receipts").mkdir()
    url = "https://www.cninfo.com.cn/synthetic"
    (tmp_path / "receipts/one.json").write_text(
        json.dumps(
            {
                "request": {"url": url, "form": None, "authenticated": False},
                "attempts": [{}, {}, {}],
            }
        )
    )
    with pytest.raises(SourceExhausted):
        Client(tmp_path).fetch("one", url)


def test_cross_issuer_and_pagination_counts_fail_closed(tmp_path):
    class Fake:
        root = tmp_path

        def fetch(self, key, url, data):
            if key.startswith("identity"):
                obj = [dict(code="600000", category="A股", orgId="synthetic")]
            else:
                obj = dict(totalAnnouncement=2, hasMore=False, announcements=[announcement()])
            return json.dumps(obj).encode(), {"observed_at": "2026-10-19T00:00:00+00:00"}

    with pytest.raises(ValueError, match="count disagrees"):
        index_security(Fake(), "sh600000", dict(target_start="2010-01-01", target_end="2026-10-19"))
    assert not (tmp_path / "indices/sh600000.json.gz").exists()


def test_same_day_pagination_tie_uses_bounded_repair(tmp_path):
    calls = []

    class Fake:
        root = tmp_path

        def fetch(self, key, url, data):
            calls.append(data)
            if key.startswith("identity"):
                obj = [dict(code="600000", category="A股", orgId="synthetic")]
            elif "-tie-" in key:
                assert data["seDate"] == "2024-04-30~2024-04-30"
                obj = dict(
                    totalAnnouncement=2,
                    hasMore=False,
                    announcements=[announcement(aid="a"), announcement(aid="b")],
                )
            elif "-periodic-" in key:
                obj = dict(
                    totalAnnouncement=2,
                    hasMore=False,
                    announcements=[announcement(aid="a"), announcement(aid="a")],
                )
            else:
                obj = dict(totalAnnouncement=0, hasMore=False, announcements=[])
            return json.dumps(obj).encode(), {"observed_at": "2026-10-19T00:00:00+00:00"}

    result = index_security(
        Fake(), "sh600000", dict(target_start="2010-01-01", target_end="2026-10-19")
    )
    assert {row["announcement_id"] for row in result["announcements"]} == {"a", "b"}
    assert len(result["queries"][0]["tie_date_repairs"]) == 1
    assert len(calls) == 5
