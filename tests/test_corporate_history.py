"""Immutable evidence, cash-rights distinction and event-clock regression cases."""

from copy import deepcopy
import hashlib
import json

import pytest

from ashare_data import CorporateHistoryView, DataError
from ashare_data.corporate_actions.history import DATA_SCHEMA, SCHEMA

SEC = "000001.XSHE"
CLOCK = "2026-10-15T12:00:00+00:00"
WINDOW = dict(start="2020-01-01", end="2025-12-31")


def raw(value):
    return json.dumps(value, sort_keys=True).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def action(name="one", **fields):
    return dict(
        observation_id=name,
        security=SEC,
        source_id="ledger",
        type="distribution",
        observed_at=CLOCK,
        historical_available_at=None,
        locator="row/1",
        currency="CNY",
        cash_basis="gross_pre_tax_per_old_eligible_share",
        fields=dict(
            announcement_date="2021-05-25",
            record_date="2021-06-01",
            ex_date="2021-06-02",
            cash_per_share="1.60058",
            bonus_share_ratio="0",
            capitalization_share_ratio="0",
            **fields,
        ),
    )


def fixture_data():
    return dict(
        schema=DATA_SCHEMA,
        historical_pit=False,
        market_complete=False,
        scope={"securities": [dict(security=SEC, **WINDOW)]},
        actions=[action()],
        shares=[],
        sources={},
        source_coverage={},
        acquisition_failures=[],
        limitations=[],
        security_boundaries=[],
    )


def seal(tmp_path, data=None):
    data = deepcopy(data or fixture_data())
    tmp_path.mkdir(parents=True, exist_ok=True)
    files = []

    def put(name, content):
        (tmp_path / name).write_bytes(content)
        files.append(dict(path=name, bytes=len(content), sha256=sha(content)))
        return name

    for source, kind in (
        ("ledger", "sina_actions"),
        ("shares", "sina_structure"),
        ("document", "issuer_pdf"),
        ("float", "sina_float"),
    ):
        body = b"%PDF-synthetic-fixture" if kind == "issuer_pdf" else source.encode()
        receipt = dict(
            ok=True,
            request=dict(kind=kind, security=SEC),
            started_at=CLOCK,
            received_at=CLOCK,
            bytes=len(body),
            sha256=sha(body),
        )
        data["sources"][source] = dict(
            kind=kind,
            security=SEC,
            body=put(source + ".body", body),
            receipt=put(source + ".receipt", raw(receipt)),
            observed_at=CLOCK,
            sha256=sha(body),
        )
        if kind != "issuer_pdf":
            data["source_coverage"][source] = dict(
                kind=kind, security=SEC, status="parsed_provider_tables", absence_proved=False
            )
    put(
        "document.txt",
        b"\n--- PAGE 1 ---\nCorrected cash 1.6005847; payment 2021-06-02.\n--- PAGE 2 ---\nOther content.\n",
    )
    put("data.json", raw(data))
    binding = dict(
        schema=SCHEMA,
        historical_pit=False,
        execution_permission=False,
        data="data.json",
        files=files,
    )
    put_raw = raw(binding)
    (tmp_path / "binding.json").write_bytes(put_raw)
    return tmp_path / "binding.json", sha(put_raw)


def view(tmp_path, data=None):
    path, digest = seal(tmp_path, data)
    return CorporateHistoryView(path, expected_binding_sha256=digest)


def document():
    row = action("correction")
    row.update(
        source_id="document",
        source_status="document_extracted",
        document_text="document.txt",
        citations=[dict(page=1, quote="Corrected cash 1.6005847; payment 2021-06-02.")],
        resolves={"cash_per_share": "Document precision, not provider rounded display."},
    )
    row["fields"].update(
        announcement_date="2021-05-26", cash_per_share="1.6005847", payment_date="2021-06-02"
    )
    return row


def test_missing_payment_blocks_export_and_explicit_scope_required(tmp_path):
    v = view(tmp_path)
    result = v.export_aq(SEC, **WINDOW, tax_model="gross_cash_no_tax")
    assert not result["ready_for_declared_date_model"]
    assert {x.get("field") for x in result["issues"]} >= {"payment_date"}
    assert "source_scoped_coverage_not_accepted" in {x.get("reason") for x in result["issues"]}
    assert result["economic_result_certified"] is result["historical_pit"] is False


def test_corrected_precision_and_paydate_query_keep_ledger_stock_fields(tmp_path):
    data = fixture_data()
    data["actions"].append(document())
    v = view(tmp_path, data)
    result = v.events(SEC, start="2021-06-02", end="2021-06-02", date_role="payment_date")
    f = result["rows"][0]["fields"]
    assert f["cash_per_share"]["value"] == "1.6005847"
    assert f["cash_per_share"]["status"] == "document_resolved_conflict"
    assert f["bonus_share_ratio"]["value"] == "0"
    assert len(f["cash_per_share"]["observations"]) == 2
    assert v.export_aq(
        SEC, **WINDOW, tax_model="gross_cash_no_tax", accept_source_scoped_coverage=True
    )["ready_for_declared_date_model"]
    result["rows"][0]["fields"]["cash_per_share"]["value"] = "999"
    assert v.events(SEC, **WINDOW)["rows"][0]["fields"]["cash_per_share"]["value"] == "1.6005847"


def test_unresolved_economic_conflict_blocks(tmp_path):
    data = fixture_data()
    doc = document()
    doc["resolves"] = {}
    data["actions"].append(doc)
    v = view(tmp_path, data)
    assert v.events(SEC, **WINDOW)["rows"][0]["fields"]["cash_per_share"]["value"] is None
    assert not v.export_aq(
        SEC, **WINDOW, tax_model="gross_cash_no_tax", accept_source_scoped_coverage=True
    )["ready_for_declared_date_model"]


def test_correction_wrong_year_is_rejected_not_coerced(tmp_path):
    data = fixture_data()
    data["actions"][0]["fields"]["payment_date"] = "2020-06-02"
    with pytest.raises(DataError, match="precedes record"):
        view(tmp_path, data)


@pytest.mark.parametrize(
    "change", ["quote", "page", "currency", "receipt_clock", "unknown_field", "duplicate", "value"]
)
def test_invalid_evidence_rejected(tmp_path, change):
    data = fixture_data()
    doc = document()
    data["actions"].append(doc)
    if change == "quote":
        doc["citations"][0]["quote"] = "not in document"
    elif change == "page":
        doc["citations"][0]["page"] = 2
    elif change == "currency":
        doc["currency"] = "HKD"
    elif change == "receipt_clock":
        doc["observed_at"] = "2020-01-01T00:00:00Z"
    elif change == "unknown_field":
        doc["fields"]["some_factor"] = "0.95"
    elif change == "duplicate":
        data["actions"].append(deepcopy(doc))
    else:
        doc["fields"]["cash_per_share"] = "NaN"
    with pytest.raises(DataError):
        view(tmp_path, data)


def test_all_bound_bytes_and_external_digest_enforced(tmp_path):
    path, digest = seal(tmp_path)
    with pytest.raises(DataError):
        CorporateHistoryView(path, expected_binding_sha256="0" * 64)
    (tmp_path / "ledger.body").write_bytes(b"tamper")
    with pytest.raises(DataError) as exc:
        CorporateHistoryView(path, expected_binding_sha256=digest)
    assert exc.value.code == "CORPORATE_INTEGRITY"


def test_evidence_paths_cannot_escape(tmp_path):
    path, _ = seal(tmp_path)
    binding = json.loads(path.read_bytes())
    binding["files"][0]["path"] = "../outside"
    content = raw(binding)
    path.write_bytes(content)
    with pytest.raises(DataError) as exc:
        CorporateHistoryView(path, expected_binding_sha256=sha(content))
    assert exc.value.code == "CORPORATE_FILE"


def test_announcement_is_not_receipt_clock_or_pit(tmp_path):
    data = fixture_data()
    data["actions"].append(document())
    v = view(tmp_path, data)
    assert (
        v.events(SEC, **WINDOW, visibility="received", as_of="2021-06-02T16:00:00+08:00")["rows"]
        == []
    )
    assert (
        v.events(SEC, **WINDOW, visibility="announced_next_day", as_of="2021-05-25T23:59:59+08:00")[
            "rows"
        ]
        == []
    )
    before = v.events(
        SEC, **WINDOW, visibility="announced_next_day", as_of="2021-05-26T23:59:59+08:00"
    )
    assert before["rows"][0]["fields"]["cash_per_share"]["value"] == "1.60058"
    after = v.events(
        SEC, **WINDOW, visibility="announced_next_day", as_of="2021-05-27T00:00:00+08:00"
    )
    assert after["rows"][0]["fields"]["cash_per_share"]["value"] == "1.6005847"
    with pytest.raises(DataError) as exc:
        v.events(SEC, **WINDOW, visibility="verified", as_of=CLOCK)
    assert exc.value.code == "PIT_UNAVAILABLE"


def test_future_implementation_is_not_current_year_economic_event(tmp_path):
    data = fixture_data()
    data["actions"][0]["fields"].update(
        announcement_date="2025-12-01",
        plan_announcement_date="2025-11-01",
        record_date="2026-01-15",
        ex_date="2026-01-16",
    )
    v = view(tmp_path, data)
    assert len(v.events(SEC, **WINDOW)["rows"]) == 1
    assert v.events(SEC, **WINDOW, date_role="economic")["rows"] == []
    assert v.coverage(SEC, **WINDOW)["rows"][0]["events"] == 0


def test_unknown_economic_date_blocks_not_silent_empty(tmp_path):
    data = fixture_data()
    data["actions"][0]["fields"].update(record_date=None, ex_date=None)
    v = view(tmp_path, data)
    assert (
        v.coverage(SEC, **WINDOW)["rows"][0]["event_field_gaps"][0]["reason"]
        == "unknown_event_placement"
    )


def test_stock_listing_does_not_manufacture_credit_date(tmp_path):
    data = fixture_data()
    data["actions"][0]["fields"].update(
        capitalization_share_ratio="0.4",
        payment_date="2021-06-02",
        new_shares_tradable_date="2021-06-03",
    )
    v = view(tmp_path, data)
    rows = v.events(
        SEC, start="2021-06-03", end="2021-06-03", date_role="new_shares_tradable_date"
    )["rows"]
    assert rows[0]["fields"]["ex_date"]["value"] == "2021-06-02"
    assert v.coverage(SEC, **WINDOW)["rows"][0]["event_field_gaps"] == [
        dict(event_id=rows[0]["event_id"], field="share_credit_date", reason="missing")
    ]


@pytest.mark.parametrize("kind", ["rights_issue", "reorganization", "security_change"])
def test_special_treatments_block_account_export(tmp_path, kind):
    data = fixture_data()
    data["actions"][0]["type"] = kind
    v = view(tmp_path, data)
    result = v.export_aq(
        SEC, **WINDOW, tax_model="external_tax_ledger", accept_source_scoped_coverage=True
    )
    assert not result["ready_for_declared_date_model"]
    assert "unsupported_event_type" in {x["reason"] for x in result["issues"]}


def share(name="s1", **extra):
    return dict(
        observation_id=name,
        security=SEC,
        source_id="shares",
        observed_at=CLOCK,
        historical_available_at=None,
        locator="table/column/1",
        date="2019-12-31",
        announcement_date="2020-03-31",
        fields={"total_shares": "1000000", "float_a_shares": "800000"},
        source_quantum_shares={"total_shares": "100"},
        **extra,
    )


def test_share_observation_not_daily_register_and_never_automatic_carry(tmp_path):
    data = fixture_data()
    data["shares"] = [share()]
    v = view(tmp_path, data)
    assert v.shares_on(SEC, "2020-01-02")["status"] == "missing"
    result = v.shares_on(SEC, "2020-01-02", carry="last_observation")
    assert result["status"] == "carried_observation_model"
    assert result["value"]["total_shares"] == "1000000"
    assert not result["effective_daily_state_verified"]
    assert (
        v.shares_on(
            SEC,
            "2020-01-02",
            carry="last_observation",
            visibility="announced_next_day",
            as_of="2020-01-03T00:00:00+08:00",
        )["status"]
        == "missing"
    )


def test_conflicting_same_date_share_observation_has_no_value(tmp_path):
    data = fixture_data()
    data["shares"] = [share(), share("s2")]
    data["shares"][1]["fields"]["total_shares"] = "1000001"
    assert (
        view(tmp_path, data).shares_on(SEC, "2020-01-02", carry="last_observation")["status"]
        == "conflict"
    )


def test_old_code_share_carry_stops_on_account_conversion_not_quote_start(tmp_path):
    data = fixture_data()
    data["shares"] = [share()]
    data["security_boundaries"] = [
        dict(
            security=SEC,
            successor_security="000002.XSHE",
            source_id="document",
            valid_through="2020-02-22",
            quote_trading_change_date="2020-02-28",
        )
    ]
    v = view(tmp_path, data)
    assert v.shares_on(SEC, "2020-02-22", carry="last_observation")["value"]
    with pytest.raises(DataError) as exc:
        v.shares_on(SEC, "2020-02-23", carry="last_observation")
    assert exc.value.code == "SECURITY_IDENTITY_ENDED"


def test_straddling_record_payment_window_and_out_of_scope(tmp_path):
    data = fixture_data()
    data["actions"][0]["fields"]["payment_date"] = "2021-06-10"
    v = view(tmp_path, data)
    assert (
        len(v.events(SEC, start="2021-06-05", end="2021-06-06", date_role="economic")["rows"]) == 1
    )
    with pytest.raises(DataError):
        v.events(SEC, start="2019-01-01", end="2021-01-01")


def test_cli_reads_real_installed_interface_contract(tmp_path, capsys):
    from ashare_data.cli import main

    path, digest = seal(tmp_path)
    base = ["corporate-history", "--binding", str(path), "--sha256", digest]
    assert (
        main(
            base
            + ["coverage", "--security", SEC, "--start", WINDOW["start"], "--end", WINDOW["end"]]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["rows"][0]["events"] == 1
    assert (
        main(
            base
            + [
                "export-aq",
                "--security",
                SEC,
                "--start",
                WINDOW["start"],
                "--end",
                WINDOW["end"],
                "--tax-model",
                "gross_cash_no_tax",
            ]
        )
        == 2
    )
    assert not json.loads(capsys.readouterr().out)["ready_for_declared_date_model"]


def test_late_reorganization_classification_cannot_leak_into_earlier_clock(tmp_path):
    data = fixture_data()
    r = data["actions"][0]
    r["type"] = "reorganization"
    r["classification_evidence"] = dict(
        type="reorganization", source_id="document", announcement_date="2022-05-23"
    )
    v = view(tmp_path, data)
    assert not v.events(
        SEC, **WINDOW, visibility="announced_next_day", as_of="2021-06-03T00:00:00+08:00"
    )["rows"]
    assert (
        len(
            v.events(
                SEC, **WINDOW, visibility="announced_next_day", as_of="2022-05-24T00:00:00+08:00"
            )["rows"]
        )
        == 1
    )


@pytest.mark.parametrize(
    "fields",
    [
        dict(ex_date="2021-05-31"),
        dict(subscription_start="2021-06-03", subscription_end="2021-06-02"),
        dict(new_shares_tradable_date="2021-06-01"),
    ],
)
def test_reversed_event_dates_rejected(tmp_path, fields):
    data = fixture_data()
    data["actions"][0]["fields"].update(fields)
    with pytest.raises(DataError):
        view(tmp_path, data)


def test_float_sources_disagree_no_silent_fallback_or_raw_value_admission(tmp_path):
    data = fixture_data()
    a, b = share(), share("float-row")
    b.update(source_id="float", announcement_date=None, fields={"float_a_shares": "900000"})
    data["shares"] = [a, b]
    v = view(tmp_path, data)
    for source in ("sina_structure", "sina_float"):
        r = v.shares_on(
            SEC, "2020-01-02", source=source, carry="last_observation", field="float_a_shares"
        )
        assert r["status"] == "source_conflict" and r["value"] is None
        assert (
            r["source_value"] and r["conflicts"][0]["reason"] == "cross_source_float_disagreement"
        )
    total = v.shares_on(SEC, "2020-01-02", carry="last_observation", field="total_shares")
    assert total["value"] == {"total_shares": "1000000"}
    assert not total["field_semantics_verified"]
    assert v.coverage(SEC, **WINDOW)["rows"][0]["share_diagnostic_rows"] == 1


def test_ah_components_cannot_be_treated_as_disjoint_when_sum_exceeds_total(tmp_path):
    data = fixture_data()
    r = share()
    r["fields"]["float_h_shares"] = "300000"
    data["shares"] = [r]
    v = view(tmp_path, data)
    result = v.shares_on(SEC, "2020-01-02", carry="last_observation")
    assert result["value"] is None and result["status"] == "source_conflict"
    assert result["conflicts"][0]["reason"] == "reported_float_components_exceed_total"
    assert result["source_value"]["float_a_shares"] == "800000"
