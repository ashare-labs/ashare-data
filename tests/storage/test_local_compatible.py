"""Opt-in local completion: genuine fills, safety counterexamples and provenance."""

import gzip
import hashlib
import json

import pandas as pd
import pytest

from ashare_data.cli import main
from test_local_alias import alias_case as alias_fixture
from test_local_daily import setup_store, bar

alias_case = alias_fixture


@pytest.fixture
def store(tmp_path):
    store = setup_store(tmp_path)
    yield store
    store.close()


def ingest(store, source, rows):
    job = dict(
        store.db.execute(
            "SELECT * FROM jobs WHERE symbol='sh600001' AND kind='bars' LIMIT 1"
        ).fetchone()
    )
    store.ingest(
        job,
        dict(
            source=source,
            rows=rows,
            receipts=[
                dict(
                    url="https://synthetic.test/" + source,
                    sha256=hashlib.sha256(source.encode()).hexdigest(),
                    received_at="2026-01-01T00:00:00+00:00",
                )
            ],
        ),
    )


def query(store, **kwargs):
    return store.get_price("600001.XSHG", "2010-01-04", "2010-01-07", mode="compatible", **kwargs)


def test_default_unchanged_day_fallback_and_exact_field_completion(store):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "baostock", [bar("2010-01-04"), bar("2010-01-05")])
    before = store.get_price("600001.XSHG", source="sina")
    changes = store.db.total_changes
    frame = query(store, source="sina")
    one, two = frame.to_dict("records")
    assert one["amount"] == "1000"
    assert one["source"] == "composed" and one["response_id"] is None
    assert one["row_hash"] is None and one["observed_at"] is None
    assert one["field_sources"]["amount"]["source"] == "baostock"
    assert one["field_sources"]["close"]["source"] == "sina"
    assert one["field_sources"]["amount"]["raw_sha256"]
    assert one["observations"][0]["amount"] is None
    assert two["completion"] == "fallback_day" and two["base_source"] == "baostock"
    assert frame.attrs["coverage"]["missing_days"] == 2
    assert store.db.total_changes == changes
    pd.testing.assert_frame_equal(before, store.get_price("600001.XSHG", source="sina"))
    assert before.attrs == store.get_price("600001.XSHG", source="sina").attrs


@pytest.mark.parametrize(
    "change",
    [
        dict(close="10.1"),
        dict(volume="10000", amount="100000"),
        dict(preclose="8"),
        dict(is_st="1"),
    ],
)
def test_overlapping_conflict_prevents_splice(store, change):
    base = dict(bar("2010-01-04"), amount=None, preclose="9", is_st="0")
    ingest(store, "sina", [base])
    ingest(store, "baostock", [{**base, "amount": "1000", **change}])
    row = query(store, source="sina").iloc[0]
    assert row.amount is None and not row.filled_fields
    assert row.has_conflicts
    assert any(c["kind"] == "value_conflict" for c in row.conflicts)


@pytest.mark.parametrize(
    "change",
    [
        dict(amount="100000"),
        dict(amount="NaN"),
        dict(volume="Infinity"),
        dict(tradestatus="unknown"),
    ],
)
def test_inconsistent_units_nonfinite_and_flags_are_rejected(store, change):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "baostock", [dict(bar("2010-01-04"), **change)])
    row = query(store, source="sina").iloc[0]
    assert row.amount is None
    assert any(c["kind"] == "invalid_observation" for c in row.conflicts)


def test_donors_disagree_null_remains_and_originals_retained(store):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "baostock", [bar("2010-01-04")])
    ingest(store, "sohu", [dict(bar("2010-01-04"), amount="1001")])
    row = query(store, source="sina").iloc[0]
    assert row.amount is None
    assert {r["amount"] for r in row.observations} == {None, "1000", "1001"}
    assert any(c["kind"] == "donor_disagreement" for c in row.conflicts)


def test_unknown_source_cannot_supply_fields(store):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "adjusted_custom", [bar("2010-01-04")])
    row = query(store).iloc[0]
    assert row.amount is None
    assert row.conflicts[0]["reason"] == "unknown_raw_units_or_adjustment"


def test_native_source_label_cannot_bypass_alias_verification(store):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "baostock_alias", [bar("2010-01-04")])
    row = query(store).iloc[0]
    assert row.amount is None
    assert any(c.get("reason") == "alias_requires_verified_capture" for c in row.conflicts)


def test_unknown_zero_activity_never_becomes_halt_or_zero_amount(store):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None, volume="0")])
    row = query(store).iloc[0]
    assert row.trade_state == "activity_unknown" and row.amount is None
    assert row.tradestatus is None


@pytest.mark.parametrize("preferred", ["auto", "sina"])
def test_halt_blocks_repeated_activity_and_preserves_flags(store, preferred):
    ingest(store, "baostock", [dict(bar("2010-01-04"), tradestatus="0", volume=None, amount=None)])
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    row = query(store, source=preferred).iloc[0]
    assert row.trade_state == "status_conflict" and row.amount is None
    assert not row.filled_fields
    assert row.volume == ("100" if preferred == "sina" else None)
    assert row.tradestatus == (None if preferred == "sina" else "0")


def test_calendar_listing_and_unknown_gaps_never_fabricated(store):
    ingest(store, "sina", [bar(d) for d in ("2010-01-03", "2010-01-04", "2010-01-08")])
    frame = store.get_price("600001.XSHG", "2010-01-01", "2010-01-09", mode="compatible")
    assert frame.date.tolist() == ["2010-01-04"]
    assert frame.attrs["excluded_source_rows"] == dict(outside_calendar=1, outside_listing=1)
    assert len(frame.attrs["unfilled_days"]) == 3
    assert {r["trade_state"] for r in frame.attrs["unfilled_days"]} == {"activity_unknown"}


def test_missing_volume_not_filled_when_donors_disagree(store):
    ingest(store, "sina", [dict(bar("2010-01-04"), volume=None, amount=None)])
    ingest(store, "baostock", [bar("2010-01-04")])
    ingest(store, "sohu", [dict(bar("2010-01-04"), volume="101")])
    row = query(store, source="sina").iloc[0]
    assert row.volume is None and row.amount is None


@pytest.mark.parametrize("preferred", ["auto", "sina"])
def test_amount_only_donor_uses_known_base_quantity_with_separate_lineage(store, preferred):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "baostock", [dict(bar("2010-01-04"), volume=None, tradestatus="1")])
    before = store.db.total_changes
    row = query(store, source=preferred).iloc[0]
    assert row.volume == "100" and row.amount == "1000"
    assert row.trade_state == "source_activity" and not row.has_conflicts
    assert row.field_sources["amount"]["source"] == "baostock"
    assert row.field_sources["volume"]["source"] == "sina"
    assert next(r for r in row.observations if r["source"] == "baostock")["volume"] is None
    assert store.db.total_changes == before
    assert store.get_price("600001.XSHG", source="sina").iloc[0].amount is None


@pytest.mark.parametrize(
    "change",
    [
        dict(amount="100000"),
        dict(amount="1"),
        dict(volume="0"),
        dict(volume="101"),
        dict(close="10.01"),
        dict(tradestatus="0"),
        dict(amount="NaN"),
        dict(amount="-1000"),
    ],
)
def test_amount_only_donor_still_requires_compatible_combined_bar(store, change):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    donor = dict(bar("2010-01-04"), volume=None, tradestatus="1")
    ingest(store, "baostock", [{**donor, **change}])
    row = query(store, source="sina").iloc[0]
    assert row.amount is None and row.volume == "100"
    assert row.field_sources["amount"] is None


def test_amount_only_donor_disagreement_preserves_null(store):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "baostock", [dict(bar("2010-01-04"), volume=None)])
    ingest(store, "sohu", [dict(bar("2010-01-04"), volume=None, amount="1001")])
    row = query(store, source="sina").iloc[0]
    assert row.amount is None and row.has_conflicts
    assert any(c["kind"] == "donor_disagreement" and c["field"] == "amount" for c in row.conflicts)


@pytest.mark.parametrize("source", ["custom_qfq", "baostock_alias"])
def test_identity_rejected_halt_cannot_veto_compatible_native_fill(store, source):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "baostock", [dict(bar("2010-01-04"), tradestatus="1")])
    ingest(store, source, [dict(bar("2010-01-04"), volume=None, amount=None, tradestatus="0")])
    row = query(store, source="sina").iloc[0]
    assert row.amount == "1000" and row.trade_state == "source_activity"
    rejected = next(r for r in row.observations if r["source"] == source)
    assert rejected["rejection"] and rejected["tradestatus"] == "0"
    assert any(
        c["kind"] == "invalid_observation" and c["observation_id"] == rejected["observation_id"]
        for c in row.conflicts
    )
    assert not any(c["kind"] == "status_conflict" for c in row.conflicts)


@pytest.mark.parametrize("source", ["custom_qfq", "baostock_alias"])
def test_identity_rejected_activity_cannot_change_unknown_native_status(store, source):
    ingest(store, "sina", [dict(bar("2010-01-04"), volume="0", amount=None)])
    ingest(store, source, [dict(bar("2010-01-04"), tradestatus="1")])
    row = query(store).iloc[0]
    assert row.trade_state == "activity_unknown"
    assert row.volume == "0" and row.amount is None and row.tradestatus is None
    assert not row.filled_fields and row.has_conflicts


def test_trusted_halt_survives_bad_prices_without_untrusted_status_lineage(store):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "sohu", [bar("2010-01-04")])
    ingest(
        store,
        "baostock",
        [
            dict(
                bar("2010-01-04"),
                open="0",
                high="0",
                low="0",
                close="0",
                volume=None,
                amount=None,
                tradestatus="0",
            )
        ],
    )
    ingest(store, "baostock_alias", [bar("2010-01-04")])
    row = query(store, source="sina").iloc[0]
    assert row.trade_state == "status_conflict" and row.amount is None and not row.filled_fields
    observations = {r["source"]: r for r in row.observations}
    assert observations["baostock"]["rejection"] == "missing_or_invalid_ohlc"
    conflict = next(c for c in row.conflicts if c["kind"] == "status_conflict")
    assert observations["baostock"]["observation_id"] in conflict["observation_ids"]
    assert observations["baostock_alias"]["observation_id"] not in conflict["observation_ids"]


def test_alias_uses_existing_verifier_and_validity_with_no_native_mutation(alias_case):
    store, _, _, sha = alias_case
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    before = store.db.total_changes
    frame = query(store, alias_captures=["explicit-capture"])
    row = frame.iloc[0]
    assert row.amount == "1000"
    lineage = row.field_sources["amount"]
    assert lineage["provider_symbol"] == "sh600002"
    assert lineage["issuer_notice_sha256"] == sha
    assert lineage["valid_from"] == "2010-01-04"
    assert lineage["valid_to_exclusive"] == "2010-01-08"
    missing = frame.attrs["unfilled_days"]
    assert missing[0]["date"] == "2010-01-05"
    assert missing[0]["trade_state"] == "source_suspended"
    assert store.db.total_changes == before
    assert store.get_price("600001.XSHG").iloc[0].amount is None


def test_alias_whole_active_day_is_explicit_and_halt_is_not_a_trading_row(alias_case):
    store, _, _, _ = alias_case
    frame = query(store, alias_captures=["explicit-capture"])
    assert frame.date.tolist() == ["2010-01-04"]
    assert frame.iloc[0].completion == "alias_day"
    assert frame.attrs["coverage"]["native_observed_days"] == 0
    assert frame.attrs["coverage"]["returned_days"] == 1
    assert frame.attrs["coverage"]["missing_days"] == 1


def test_alias_notice_damage_is_not_silently_skipped(alias_case):
    store, _, _, sha = alias_case
    (store.root / "raw" / (sha + ".gz")).write_bytes(gzip.compress(b"%PDF changed"))
    with pytest.raises(ValueError, match="notice hash"):
        query(store, alias_captures=["explicit-capture"])


def test_duplicate_alias_path_does_not_duplicate_observations(alias_case):
    store, _, _, _ = alias_case
    frame = query(store, alias_captures=["explicit-capture", "explicit-capture"])
    assert len(frame.iloc[0].observations) == 1


def status_report(store, kind):
    raw = b"%PDF synthetic reviewed status notice"
    sha = hashlib.sha256(raw).hexdigest()
    (store.root / "raw").mkdir(exist_ok=True)
    (store.root / "raw" / (sha + ".gz")).write_bytes(gzip.compress(raw))
    (store.root / "reports").mkdir(exist_ok=True)
    if kind == "halt":
        name = "source-status-conflict-002647.json"
        data = dict(
            symbol="sh600001",
            announced_suspension_start="2010-01-04",
            announced_resumption="2010-01-05",
            issuer_notices=[dict(receipts=[dict(sha256=sha)])],
        )
    else:
        name = "trade-status-followup.json"
        data = dict(
            symbol="sh600001",
            date="2010-01-04",
            issuer_notice=dict(
                sha256=sha,
                stated_final_delisting_period_trading_day="2010-01-03",
                stated_formal_exit="2010-01-05",
            ),
        )
    (store.root / "reports" / name).write_text(json.dumps(data))
    return sha


@pytest.mark.parametrize("kind", ["halt", "final"])
def test_issuer_evidence_blocks_source_active_flag_and_discloses_basis(store, kind):
    status_report(store, kind)
    ingest(store, "baostock", [dict(bar("2010-01-04"), amount=None, volume=None, tradestatus="1")])
    ingest(store, "sina", [bar("2010-01-04")])
    row = query(store).iloc[0]
    assert row.tradestatus == "1" and row.volume is None and row.amount is None
    assert row.trade_state == "status_conflict"
    assert row.status_evidence[0]["report_sha256"]
    assert row.status_evidence[0]["evidence_kind"] == (
        "issuer_notice" if kind == "halt" else "issuer_final_trading_day_inference"
    )


def test_invalid_status_evidence_fails_closed(store):
    sha = status_report(store, "halt")
    (store.root / "raw" / (sha + ".gz")).write_bytes(gzip.compress(b"changed"))
    with pytest.raises(ValueError, match="status source mismatch"):
        query(store)


@pytest.mark.parametrize("damage", ["truncated", "structure"])
def test_bad_status_evidence_cli_returns_structured_error(store, capsys, damage):
    sha = status_report(store, "halt")
    if damage == "truncated":
        path = store.root / "raw" / (sha + ".gz")
        path.write_bytes(path.read_bytes()[:-3])
    else:
        (store.root / "reports/source-status-conflict-002647.json").write_text("{}")
    assert (
        main(
            [
                "local-daily",
                "query",
                "--root",
                str(store.root),
                "--symbol",
                "600001.XSHG",
                "--mode",
                "compatible",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INPUT_OR_IO_ERROR"


def test_cli_compatible_includes_diagnostics_default_shape_unchanged(store, capsys):
    ingest(store, "sina", [dict(bar("2010-01-04"), amount=None)])
    ingest(store, "baostock", [bar("2010-01-04")])
    args = [
        "local-daily",
        "query",
        "--root",
        str(store.root),
        "--symbol",
        "600001.XSHG",
        "--start",
        "2010-01-04",
        "--end",
        "2010-01-04",
        "--source",
        "sina",
    ]
    assert main(args) == 0
    default = json.loads(capsys.readouterr().out)
    assert isinstance(default, list) and default[0]["amount"] is None
    assert main([*args, "--mode", "compatible"]) == 0
    composed = json.loads(capsys.readouterr().out)
    assert composed["rows"][0]["amount"] == "1000"
    assert composed["metadata"]["coverage"]["filled_fields"] == {"amount": 1}


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(mode="typo"),
        dict(alias_captures=["file"]),
        dict(mode="compatible", alias_captures="file"),
    ],
)
def test_invalid_options_rejected_without_mutation(store, kwargs):
    before = store.db.total_changes
    with pytest.raises(ValueError):
        store.get_price("600001.XSHG", **kwargs)
    assert store.db.total_changes == before
