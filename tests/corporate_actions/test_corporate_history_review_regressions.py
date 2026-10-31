"""R1/R2/R3: build real isolated synthetic packages through public entry points."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from ashare_data import CorporateHistoryView, DataError

SEC = "000001.XSHE"
WINDOW = dict(start="2021-01-01", end="2021-12-31")
SPEC = importlib.util.spec_from_file_location(
    "review_fix_builder", Path(__file__).parents[2] / "scripts/build_corporate_history.py"
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def html_rows(rows):
    return "".join("<tr>" + "".join("<td>" + c + "</td>" for c in row) + "</tr>" for row in rows)


def dividend_row(announcement="2021-05-01"):
    return [announcement, "0", "0", "10", "实施", "2021-06-02", "2021-06-01", "--", "查看"]


def dividend_html(rows=None, security=SEC, rights=None):
    header = [["分红方案(每10股)", "派息(税前)(元)"]]
    rights_header = [["配股方案(每10股配股股数)", "基准股本(股)"]]
    return (
        '<script>var page_symbol="sz'
        + security[:6]
        + '";</script><table id="sharebonus_1">'
        + html_rows(header + (rows if rows is not None else [dividend_row()]))
        + '</table><table id="sharebonus_2">'
        + html_rows(rights_header + (rights if rights is not None else [["暂时没有数据！"]]))
        + "</table>"
    ).encode("gb18030")


def structure_html(security=SEC, broken=False):
    rows = [
        ["变动日期", "2021-01-01"],
        ["公告日期", "2021-01-02"],
        ["变动原因", "定期报告"],
        ["总股本", "100.000 万股"],
        ["流通A股", "80.000 万股"],
    ]
    if broken:
        rows = [r for r in rows if r[0] != "公告日期"]
    return (
        '<script>var page_symbol="sz'
        + security[:6]
        + '";</script><table id="StockStructureNewTable1">'
        + html_rows(rows)
        + "</table>"
    ).encode("gb18030")


def notice(source="notice_a", security=SEC, **fields):
    return dict(
        source_id=source,
        security=security,
        type="distribution",
        locator="synthetic/page1",
        document_text=source + ".txt",
        source_status="document_extracted",
        currency="CNY",
        cash_basis="gross_pre_tax_per_old_eligible_share",
        fields=dict(
            dict(
                announcement_date="2021-05-01",
                record_date="2021-06-01",
                ex_date="2021-06-02",
                payment_date="2021-06-04",
                cash_per_share="1",
                bonus_share_ratio="0",
                capitalization_share_ratio="0",
            ),
            **fields,
        ),
        citations=[dict(page=1, quote="Synthetic reviewed notice facts.")],
        native_fields={"notice_identity": "FY2020 annual dividend"},
    )


def build_package(
    tmp_path,
    *,
    rows=None,
    notices=None,
    broken=False,
    extra_security=False,
    rights=None,
    scope=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    captures = tmp_path / "captures"
    captures.mkdir()

    def capture(source, kind, security, body):
        name = source + ".body"
        (captures / name).write_bytes(body)
        receipt = dict(
            ok=True,
            request=dict(
                id=source, kind=kind, security=security, url="https://example.invalid/synthetic"
            ),
            body=name,
            bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            http_status="200",
            error="",
            started_at="2026-10-15T12:00:00+00:00",
            received_at="2026-10-15T12:00:01+00:00",
        )
        (captures / (source + ".receipt.json")).write_text(json.dumps(receipt))

    capture("actions", "sina_actions", SEC, dividend_html(rows, rights=rights))
    capture("structure", "sina_structure", SEC, structure_html(broken=broken))
    scopes = [dict(security=SEC, **(scope or WINDOW))]
    docs = deepcopy(notices or [])
    if extra_security:
        other = "000002.XSHE"
        capture("actions-other", "sina_actions", other, dividend_html(security=other))
        capture("structure-other", "sina_structure", other, structure_html(other))
        scopes.append(dict(security=other, **WINDOW))
        docs.append(notice("notice_other", other))
    for doc in docs:
        capture(doc["source_id"], "issuer_pdf", doc["security"], b"%PDF-synthetic-notice")
        (tmp_path / doc["document_text"]).write_text(
            "\n--- PAGE 1 ---\nSynthetic reviewed notice facts.\n"
        )
    (tmp_path / "scope.json").write_text(json.dumps(dict(securities=scopes)))
    (tmp_path / "curation.json").write_text(
        json.dumps(dict(schema="ad.corporate-history-curation.v1", actions=docs))
    )
    result = builder.build(
        captures, tmp_path / "curation.json", tmp_path / "scope.json", tmp_path / "package"
    )
    view = CorporateHistoryView(result["binding"], expected_binding_sha256=result["sha256"])
    return view, result


def export(view, security=SEC, **window):
    return view.export_aq(
        security,
        **(window or WINDOW),
        tax_model="gross_cash_no_tax",
        accept_source_scoped_coverage=True,
    )


@pytest.mark.parametrize("announcement", ["--", "", "1900-01-01", "not-a-date"])
def test_missing_or_invalid_announcement_preserves_business_row_and_payment_gap(
    tmp_path, announcement
):
    v, _ = build_package(tmp_path, rows=[dividend_row(announcement)])
    result = export(v)
    assert len(result["rows"]) == 1
    assert result["rows"][0]["announcement_date"] is None
    assert not result["ready_for_declared_date_model"]
    assert any(x.get("field") == "payment_date" for x in result["issues"])
    result = v.events(
        SEC, **WINDOW, visibility="announced_next_day", as_of="2021-06-04T16:00:00+08:00"
    )
    assert result["rows"] == [] and len(result["visibility_gaps"]) == 1
    assert result["visibility_gaps"][0]["reason"] == "announcement_visibility_unknown"
    assert result["absence_proved"] is False


def test_unknown_announcement_does_not_block_complete_posthoc_schedule(tmp_path):
    v, _ = build_package(
        tmp_path, rows=[dividend_row("--")], notices=[notice(announcement_date=None)]
    )
    result = export(v)
    assert len(result["rows"]) == 1 and result["ready_for_declared_date_model"]
    assert result["rows"][0]["announcement_date"] is None
    assert result["historical_pit"] is False


def test_missing_rights_announcement_keeps_subscription_observation(tmp_path):
    rights = [
        [
            "--",
            "3",
            "4.65",
            "2790800000",
            "2021-04-17",
            "2021-04-08",
            "2021-04-09",
            "2021-04-15",
            "2021-04-29",
            "",
            "查看",
        ]
    ]
    v, _ = build_package(tmp_path, rows=[], rights=rights)
    result = export(v)
    assert result["rows"][0]["type"] == "rights_issue"
    assert result["rows"][0]["rights_ratio"] == "0.3"
    assert result["rows"][0]["announcement_date"] is None
    assert not result["ready_for_declared_date_model"]
    assert any(x["reason"] == "unsupported_event_type" for x in result["issues"])


def test_entirely_undated_event_is_retained_as_unlocated(tmp_path):
    row = dividend_row("--")
    row[5:8] = ["--", "--", "--"]
    v, _ = build_package(tmp_path, rows=[row])
    result = export(v, start="2021-09-01", end="2021-09-30")
    assert len(result["rows"]) == 1
    assert result["rows"][0]["window_placement"] == "unlocated"
    assert not result["ready_for_declared_date_model"]
    assert result["coverage"]["rows"][0]["interval_state"] == "unlocated_events"
    assert any(x["reason"] == "unknown_event_placement" for x in result["issues"])


def test_blank_source_is_not_accepted_empty_interval(tmp_path):
    v, _ = build_package(tmp_path, rows=[])
    result = export(v)
    assert result["rows"] == [] and not result["ready_for_declared_date_model"]
    assert result["coverage"]["rows"][0]["interval_state"] == "source_unresolved"
    assert any(x["reason"] == "source_has_no_locatable_history" for x in result["issues"])


def test_known_history_allows_source_scoped_empty_subinterval(tmp_path):
    v, _ = build_package(tmp_path, notices=[notice()])
    result = export(v, start="2021-08-01", end="2021-08-31")
    assert result["rows"] == [] and result["ready_for_declared_date_model"]
    assert result["coverage"]["rows"][0]["interval_state"] == "empty_source_scoped_interval"
    assert result["coverage"]["rows"][0]["absence_proved"] is False


def test_partial_unknown_row_kept_in_diagnostics_without_erasing_valid_event(tmp_path):
    v, _ = build_package(
        tmp_path, rows=[dividend_row(), ["unexpected", "business", "row"]], notices=[notice()]
    )
    result = export(v)
    assert len(result["rows"]) == 1 and not result["ready_for_declared_date_model"]
    gap = next(x for x in result["issues"] if x["reason"] == "source_parse_incomplete")
    assert gap["status"] == "partial_provider_tables"
    assert gap["unparsed_records"][0]["native_fields"] == ["unexpected", "business", "row"]


def test_parse_failed_package_opens_and_other_security_stays_usable(tmp_path):
    v, meta = build_package(tmp_path, broken=True, notices=[notice()], extra_security=True)
    result = export(v)
    assert not result["ready_for_declared_date_model"]
    gap = next(x for x in result["issues"] if x["reason"] == "source_parse_incomplete")
    assert gap["source_id"] == "structure" and gap["status"] == "parse_failed"
    assert gap["parse_error"]
    assert v.lineage()["acquisition_failures"]
    assert export(v, "000002.XSHE")["ready_for_declared_date_model"]
    reopened = CorporateHistoryView(meta["binding"], expected_binding_sha256=meta["sha256"])
    assert (
        reopened.share_history(SEC, **WINDOW)["source_coverage"]["structure"]["absence_proved"]
        is False
    )


def revised():
    old = notice()
    new = notice("notice_b", announcement_date="2021-05-02", ex_date="2021-06-03")
    new["supersedes"] = ["notice_a"]
    new["resolves"] = {"ex_date": "Reviewed correction of the same annual entitlement."}
    return old, new


def test_ex_date_correction_is_one_entitlement_with_stable_identity_and_visibility(tmp_path):
    old, new = revised()
    v, _ = build_package(tmp_path, notices=[old, new])
    result = export(v)
    assert result["ready_for_declared_date_model"] and len(result["rows"]) == 1
    event = result["rows"][0]
    assert event["cash_per_share"] == "1" and event["ex_date"] == "2021-06-03"
    before = v.events(
        SEC, **WINDOW, visibility="announced_next_day", as_of="2021-05-02T00:00:00+08:00"
    )["rows"][0]
    after = v.events(
        SEC, **WINDOW, visibility="announced_next_day", as_of="2021-05-03T00:00:00+08:00"
    )["rows"][0]
    assert before["fields"]["ex_date"]["value"] == "2021-06-02"
    assert after["fields"]["ex_date"]["value"] == "2021-06-03"
    assert before["event_id"] == after["event_id"] == event["event_id"]
    assert v.events(SEC, start="2021-06-02", end="2021-06-02", date_role="ex_date")["rows"] == []
    assert (
        len(v.events(SEC, start="2021-06-03", end="2021-06-03", date_role="ex_date")["rows"]) == 1
    )
    # Old/new publication labels are both queryable without using an obsolete ex-date.
    assert (
        len(
            v.events(SEC, start="2021-05-01", end="2021-05-01", date_role="announcement_date")[
                "rows"
            ]
        )
        == 1
    )
    assert (
        len(
            v.events(SEC, start="2021-05-02", end="2021-05-02", date_role="announcement_date")[
                "rows"
            ]
        )
        == 1
    )
    original, _ = build_package(tmp_path / "original-only", notices=[old])
    assert export(original)["rows"][0]["event_id"] == event["event_id"]


def test_repeated_windows_use_same_phase_key_and_do_not_create_second_entitlement(tmp_path):
    v, _ = build_package(tmp_path, notices=list(revised()))
    first = export(v, start="2021-06-01", end="2021-06-02")["rows"][0]
    second = export(v, start="2021-06-03", end="2021-06-04")["rows"][0]
    assert first["entitlement_key"] == second["entitlement_key"]
    assert first["settlement_keys"] == second["settlement_keys"]
    assert len({first["settlement_keys"]["cash"], second["settlement_keys"]["cash"]}) == 1
    assert len({e["entitlement_key"] for e in [first, second]}) * 1000 == 1000


@pytest.mark.parametrize("style", ["native_corrects", "explicit_identity"])
def test_supported_reviewed_association_forms(tmp_path, style):
    old, new = revised()
    new.pop("supersedes")
    if style == "native_corrects":
        new["native_fields"]["corrects"] = "notice_a"
    else:
        old["event_identity"] = new["event_identity"] = "issuer-plan-FY2020-annual"
    v, _ = build_package(tmp_path, notices=[old, new])
    result = export(v)
    assert result["ready_for_declared_date_model"] and len(result["rows"]) == 1
    assert result["rows"][0]["ex_date"] == "2021-06-03"


def test_shared_record_date_is_ambiguity_not_permission_to_merge(tmp_path):
    old, new = revised()
    new.pop("supersedes")
    new.pop("resolves")
    v, _ = build_package(tmp_path, notices=[old, new])
    result = export(v)
    assert len(result["rows"]) == 2 and not result["ready_for_declared_date_model"]
    assert any(x["reason"] == "ambiguous_event_identity" for x in result["issues"])


def test_unlinked_date_resolution_is_blocked(tmp_path):
    old, new = revised()
    new.pop("supersedes")
    v, _ = build_package(tmp_path, notices=[old, new])
    result = export(v)
    assert not result["ready_for_declared_date_model"]
    assert any(x["reason"] == "unlinked_date_correction" for x in result["issues"])


def test_explicitly_distinct_entitlements_can_share_record_date(tmp_path):
    a, b = revised()
    b.pop("supersedes")
    b.pop("resolves")
    a["event_identity"] = "FY2020-annual"
    b["event_identity"] = "FY2021-special"
    stale = dividend_row("2020-05-01")
    stale[5:7] = ["2020-06-02", "2020-06-01"]
    v, _ = build_package(tmp_path, rows=[stale], notices=[a, b])
    result = export(v)
    assert len(result["rows"]) == 2 and result["ready_for_declared_date_model"]


def test_revision_chain_selects_latest_visible_explicit_resolution(tmp_path):
    a, b = revised()
    c = notice("notice_c", announcement_date="2021-05-03", ex_date="2021-06-04")
    c["supersedes"] = ["notice_b"]
    c["resolves"] = {"ex_date": "Second reviewed correction."}
    v, _ = build_package(tmp_path, notices=[a, b, c])
    assert export(v)["rows"][0]["ex_date"] == "2021-06-04"
    earlier = v.events(
        SEC, **WINDOW, visibility="announced_next_day", as_of="2021-05-03T12:00:00+08:00"
    )["rows"][0]
    assert earlier["fields"]["ex_date"]["value"] == "2021-06-03"


def test_builder_preserves_predecessor_outside_new_scope_for_revision_identity(tmp_path):
    a = notice(
        announcement_date="2020-12-20",
        record_date="2020-12-29",
        ex_date="2020-12-30",
        payment_date="2020-12-31",
    )
    b = notice(
        "notice_b",
        announcement_date="2020-12-25",
        record_date="2021-01-01",
        ex_date="2021-01-04",
        payment_date="2021-01-05",
    )
    b["supersedes"] = ["notice_a"]
    b["resolves"] = {
        k: "Reviewed revised schedule." for k in ("record_date", "ex_date", "payment_date")
    }
    v, _ = build_package(tmp_path, rows=[], notices=[a, b])
    result = export(v)
    assert len(result["rows"]) == 1 and result["ready_for_declared_date_model"]
    assert result["rows"][0]["ex_date"] == "2021-01-04"
    observations = v.events(SEC, **WINDOW)["rows"][0]["observations"]
    assert {r["source_id"] for r in observations} == {"notice_a", "notice_b"}


def test_future_source_does_not_hide_already_visible_identity_ambiguity(tmp_path):
    a, b = revised()
    b.pop("supersedes")
    b.pop("resolves")
    c = notice("notice_c", announcement_date="2021-05-30")
    v, _ = build_package(tmp_path, notices=[a, b, c])
    result = v.events(
        SEC, **WINDOW, visibility="announced_next_day", as_of="2021-05-03T12:00:00+08:00"
    )
    assert len(result["rows"]) == 2
    assert all(
        any(i["reason"] == "ambiguous_event_identity" for i in e["identity_issues"])
        for e in result["rows"]
    )
    earlier = v.events(
        SEC, **WINDOW, visibility="announced_next_day", as_of="2021-05-02T00:00:00+08:00"
    )
    assert len(earlier["rows"]) == 1 and earlier["rows"][0]["identity_issues"] == []


def test_cited_document_repairs_invalid_provider_amount_without_erasing_raw_diagnostic(tmp_path):
    row = dividend_row()
    row[3] = "not-an-amount"
    v, _ = build_package(tmp_path, rows=[row], notices=[notice()])
    result = export(v)
    assert result["ready_for_declared_date_model"] and result["rows"][0]["cash_per_share"] == "1"
    observations = v.events(SEC, **WINDOW)["rows"][0]["observations"]
    raw = next(o for o in observations if o["source_id"] == "actions")
    assert raw["fields"]["cash_per_share"] is None
    assert raw["source_issues"] == [
        dict(field="cash_per_share", reason="invalid_source_value", native_value="not-an-amount")
    ]


def test_cited_withdrawal_never_falls_back_to_superseded_date(tmp_path):
    a, b = revised()
    b["fields"]["ex_date"] = None
    v, _ = build_package(tmp_path, notices=[a, b])
    result = export(v)
    assert len(result["rows"]) == 1 and result["rows"][0]["ex_date"] is None
    assert not result["ready_for_declared_date_model"]
    assert any(
        i.get("field") == "ex_date" and i["reason"] == "document_withdrawn"
        for i in result["issues"]
    )


def test_cyclic_revision_links_remain_diagnostic_not_a_ready_schedule(tmp_path):
    a, b = revised()
    b["fields"]["announcement_date"] = a["fields"]["announcement_date"]
    a["supersedes"] = ["notice_b"]
    v, _ = build_package(tmp_path, notices=[a, b])
    result = export(v)
    assert not result["ready_for_declared_date_model"]
    assert any(i["reason"] == "cyclic_event_revision" for i in result["issues"])


def test_missing_payment_stays_unsettled_after_known_ex_date_but_not_before_record(tmp_path):
    v, _ = build_package(tmp_path, rows=[dividend_row("--")])
    later = export(v, start="2021-08-01", end="2021-08-31")
    assert len(later["rows"]) == 1 and not later["ready_for_declared_date_model"]
    assert later["rows"][0]["window_placement"] == "unknown_required_date"
    earlier = export(v, start="2021-04-01", end="2021-04-30")
    assert earlier["rows"] == [] and earlier["ready_for_declared_date_model"]
    query = v.events(SEC, start="2021-08-01", end="2021-08-31", date_role="payment_date")
    assert len(query["rows"]) == 1 and query["rows"][0]["fields"]["payment_date"]["value"] is None


@pytest.mark.parametrize("day", ["2021-05-31", *[f"2021-06-{n:02}" for n in range(1, 10)]])
def test_payment_conflict_preserves_every_possible_unsettled_day(tmp_path, day):
    a = notice("payment_a", payment_date="2021-06-04")
    b = notice("payment_b", payment_date="2021-06-08")
    v, _ = build_package(tmp_path, rows=[], notices=[a, b])
    result = export(v, start=day, end=day)
    unsettled = "2021-06-01" <= day <= "2021-06-08"
    assert len(result["rows"]) == int(unsettled)
    assert result["ready_for_declared_date_model"] is not unsettled
    if unsettled:
        assert any(
            i.get("field") == "payment_date" and i["reason"] == "conflict" for i in result["issues"]
        )
    else:
        assert result["issues"] == []
        assert result["coverage"]["rows"][0]["interval_state"] == "empty_source_scoped_interval"


def test_payment_conflict_middle_window_and_visibility(tmp_path):
    a = notice("payment_a", payment_date="2021-06-04")
    b = notice("payment_b", announcement_date="2021-05-03", payment_date="2021-06-08")
    v, _ = build_package(tmp_path, rows=[], notices=[a, b])
    window = dict(start="2021-06-05", end="2021-06-07")
    result = export(v, **window)
    assert len(result["rows"]) == 1 and not result["ready_for_declared_date_model"]
    assert result["rows"][0]["window_placement"] == "ambiguous_date"
    whole = export(v)["rows"][0]
    assert result["rows"][0]["entitlement_key"] == whole["entitlement_key"]
    assert result["rows"][0]["settlement_keys"] == whole["settlement_keys"]
    before = v.events(
        SEC,
        **window,
        date_role="economic",
        visibility="announced_next_day",
        as_of="2021-05-03T23:59:59+08:00",
    )
    after = v.events(
        SEC,
        **window,
        date_role="economic",
        visibility="announced_next_day",
        as_of="2021-05-04T00:00:00+08:00",
    )
    assert before["rows"] == [] and len(after["rows"]) == 1
    assert after["rows"][0]["fields"]["payment_date"]["status"] == "conflict"
    # A date-label query matches candidate labels, while economic queries include
    # the interval during which an entitlement may still be unsettled.
    assert v.events(SEC, **window, date_role="payment_date")["rows"] == []


def test_resolved_payment_conflict_does_not_extend_to_superseded_late_date(tmp_path):
    a = notice("payment_a", payment_date="2021-06-08")
    b = notice("payment_b", announcement_date="2021-05-03", payment_date="2021-06-04")
    b.update(supersedes=["payment_a"], resolves={"payment_date": "Cited revised cash schedule."})
    v, _ = build_package(tmp_path, rows=[], notices=[a, b])
    whole = export(v)
    assert (
        whole["ready_for_declared_date_model"] and whole["rows"][0]["payment_date"] == "2021-06-04"
    )
    later = export(v, start="2021-06-05", end="2021-06-07")
    assert later["rows"] == [] and later["ready_for_declared_date_model"]


def test_all_economic_dates_conflicting_have_finite_window_not_unlocated_forever(tmp_path):
    a = notice("plan_a", record_date="2021-06-01", ex_date="2021-06-02", payment_date="2021-06-04")
    b = notice("plan_b", record_date="2021-06-03", ex_date="2021-06-04", payment_date="2021-06-08")
    a["event_identity"] = b["event_identity"] = "same-synthetic-plan"
    v, _ = build_package(tmp_path, rows=[], notices=[a, b])
    middle = export(v, start="2021-06-05", end="2021-06-07")
    assert len(middle["rows"]) == 1 and not middle["ready_for_declared_date_model"]
    assert middle["rows"][0]["window_placement"] == "ambiguous_date"
    after = export(v, start="2021-06-09", end="2021-06-30")
    assert after["rows"] == [] and after["ready_for_declared_date_model"]


def test_conflicting_stock_credit_and_listing_dates_keep_intermediate_window(tmp_path):
    a = notice(
        "stock_a",
        cash_per_share="0",
        payment_date=None,
        capitalization_share_ratio="0.4",
        share_credit_date="2021-06-04",
        new_shares_tradable_date="2021-06-04",
    )
    b = notice(
        "stock_b",
        cash_per_share="0",
        payment_date=None,
        capitalization_share_ratio="0.4",
        share_credit_date="2021-06-08",
        new_shares_tradable_date="2021-06-08",
    )
    v, _ = build_package(tmp_path, rows=[], notices=[a, b])
    result = export(v, start="2021-06-05", end="2021-06-07")
    assert len(result["rows"]) == 1 and not result["ready_for_declared_date_model"]
    assert {i.get("field") for i in result["issues"]} >= {
        "share_credit_date",
        "new_shares_tradable_date",
    }
    after = export(v, start="2021-06-09", end="2021-06-30")
    assert after["rows"] == [] and after["ready_for_declared_date_model"]


@pytest.mark.parametrize("credit,blocked", [("2021-06-02", True), ("2021-06-04", False)])
def test_merged_credit_chronology_blocks_impossible_date_but_keeps_separate_listing(
    tmp_path, credit, blocked
):
    a = notice(
        "record_source",
        record_date="2021-06-03",
        ex_date="2021-06-04",
        payment_date="2021-06-04",
        capitalization_share_ratio="0.4",
        share_credit_date=None,
        new_shares_tradable_date="2021-06-05",
    )
    b = notice(
        "credit_source",
        record_date=None,
        ex_date=None,
        payment_date=None,
        capitalization_share_ratio="0.4",
        share_credit_date=credit,
        new_shares_tradable_date=None,
    )
    a["event_identity"] = b["event_identity"] = "same-synthetic-plan"
    v, _ = build_package(tmp_path, rows=[], notices=[a, b])
    result = export(v)
    assert len(result["rows"]) == 1 and result["ready_for_declared_date_model"] is not blocked
    if blocked:
        assert any(
            i.get("field") == "share_credit_date"
            and i.get("before") == "record_date"
            and i["reason"] == "conflicting_consolidated_chronology"
            for i in result["issues"]
        )
    else:
        assert result["rows"][0]["share_credit_date"] == "2021-06-04"
        assert result["rows"][0]["new_shares_tradable_date"] == "2021-06-05"


@pytest.mark.parametrize(
    "first,last",
    [
        ("record_date", "ex_date"),
        ("record_date", "payment_date"),
        ("record_date", "share_credit_date"),
        ("record_date", "new_shares_tradable_date"),
        ("ex_date", "payment_date"),
        ("ex_date", "new_shares_tradable_date"),
        ("subscription_start", "subscription_end"),
    ],
)
def test_same_chronology_rules_for_single_and_merged_observations(tmp_path, first, last):
    a = notice("first_source", record_date=None, ex_date=None, payment_date=None)
    b = notice("last_source", record_date=None, ex_date=None, payment_date=None)
    a["fields"][first] = "2021-06-04"
    b["fields"][last] = "2021-06-02"
    a["event_identity"] = b["event_identity"] = "same-synthetic-plan"
    v, _ = build_package(tmp_path / "split", rows=[], notices=[a, b])
    result = export(v)
    assert not result["ready_for_declared_date_model"]
    assert any(
        i.get("before") == first
        and i.get("field") == last
        and i["reason"] == "conflicting_consolidated_chronology"
        for i in result["issues"]
    )
    single = deepcopy(a)
    single["fields"][last] = b["fields"][last]
    with pytest.raises(DataError) as error:
        build_package(tmp_path / "single", rows=[], notices=[single])
    assert error.value.code == "CORPORATE_DATE"
