"""Release reference admission: conflicts, bounded ledger evidence and replay identity."""

from copy import deepcopy
import json
import shutil
import sqlite3

import pytest
from test_financial_release import market

from ashare_data.core.model import DataError
from ashare_data.financials.tables import TABLES
from ashare_data.product.released_corporate import ReleasedCorporateView
from ashare_data.product.released_references import ReleasedReferences
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection
from ashare_data.storage.financial_release import DELTA_SQL, table_set_hash

SID = "600000.XSHG"
DAY = "2024-04-30"


def rows():
    coverage, captures = [], []
    for source in ("sina_actions", "sina_structure"):
        coverage.append(dict(coverage_id=source, batch_key="batch", task_id=source,
            source=source, security=SID, source_state="complete", input_rows=1, mapped_rows=1,
            quarantined_rows=0, coverage_json=json.dumps(dict(source_status="parsed_provider_tables",
                counts={"actions": 1}, table_states={
                    "sharebonus_1": {"state": "business_rows_observed"},
                    "sharebonus_2": {"state": "provider_no_rows_reported", "explicit_empty_marker": True}}))))
        captures.append(dict(capture_id=source, batch_key="batch", task_id=source,
            source=source, observed_at="2025-01-01T00:00:00+00:00", source_state="complete"))
    common = dict(instrument_id=17, security=SID, event_type="distribution", implemented=True,
                  source_revision_id="revision", record_date="2024-04-29", ex_date=DAY,
                  announcement_date="2024-04-20", gross_cash="0.249", quality_reasons_json="[]")
    return {
        "meta.financial_coverage": coverage, "meta.financial_captures": captures,
        "reference.price_limits": [dict(observation_id="limit", instrument_id=17, security=SID,
            source="tushare", source_revision_id="revision", trade_date=DAY,
            up_limit="11", down_limit="9", previous_close="10", unit="CNY/share")],
        "corporate_action.events": [
            dict(common, event_id="sina-event", source="sina_actions", bonus_ratio="0",
                 capitalization_ratio="0", source_fields_json=json.dumps(dict(fields={
                     "announcement_date": "2024-04-20", "record_date": "2024-04-29", "ex_date": DAY,
                     "cash_per_share": "0.249", "bonus_share_ratio": "0", "capitalization_share_ratio": "0"}))),
            dict(common, event_id="tushare-event", source="tushare", payment_date=DAY,
                 implementation_announcement_date="2024-04-20", source_fields_json="{}"),
        ],
    }


def publish(parent, data, *, target_end="2026-12-31"):
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as w:
        p = parent.root / "work/references.sqlite3"
        shutil.copyfile(parent.metadata_path, p)
        with sqlite3.connect(p) as db:
            db.executescript(DELTA_SQL)
            db.execute("DELETE FROM data_batches WHERE batch_key='batch'")
            db.execute("INSERT INTO data_batches VALUES(7,'batch',2,'p','m',?)",
                [json.dumps({"manifest": {"target_start": "2010-01-01", "target_end": target_end}})])
        c = connection()
        evidence = []
        for i, (table, records) in enumerate(data.items()):
            schema = TABLES[table]
            name = "t" + str(i)
            c.execute("CREATE TABLE " + name + "(" + ",".join(f"{k} {v}" for k, v in schema.items()) + ")")
            if records:
                c.executemany("INSERT INTO " + name + " VALUES(" + ",".join("?" for _ in schema) + ")",
                              [[r.get(k) for k in schema] for r in records])
            item = w.parquet(c, "SELECT * FROM " + name, [], name)
            item.update(kind="financial_table", table=table, rows=len(records),
                        partition_key=str(parent.release_id + 1), key_value_mismatches=0)
            evidence.append(item)
        c.close()
        evidence = parent.manifest.get("evidence", []) + evidence
        contract = dict(schema="ad.financial-tables.v1", table_names=list(TABLES), candidate_ids=[],
                        table_set_sha256=table_set_hash(evidence))
        with sqlite3.connect(p) as db:
            db.execute("CREATE TABLE IF NOT EXISTS release_contracts(name TEXT PRIMARY KEY,body TEXT)")
            db.execute("INSERT OR REPLACE INTO release_contracts VALUES('financial_reference_tables',?)",
                       [json.dumps(contract)])
        body = deepcopy(parent.manifest)
        body.update(metadata=w.object(p, "sqlite3"), evidence=evidence,
                    financial_reference_tables=contract,
                    financial_table_validation={"table_set_sha256": contract["table_set_sha256"]})
        w.publish(body, expected_parent=parent.release_id)
    return MarketRelease(parent.root)


@pytest.fixture
def released(tmp_path):
    return publish(market(tmp_path / "market"), rows())


def reader(release):
    return ReleasedReferences(release.receipt(), release.receipt())


def price():
    return dict(security=SID, date=DAY, source="baostock", row_hash="original", response_id="response",
                preclose="10", tradestatus="1", is_st="0")


def test_source_facts_merge_cash_schedule_without_inventing_stock_dates(released):
    r = reader(released)
    try:
        value, gaps = r.market([SID], DAY, DAY, [DAY], [price()])
        assert gaps == []
        assert value["rows"][0]["upper_limit"]["value"] == "11"
        assert value["rows"][0]["suspended"]["value"] is False
        schedule = ReleasedCorporateView(r, [SID], DAY, DAY).export_aq(
            [SID], start=DAY, end=DAY, tax_model="gross_cash_no_tax", accept_source_scoped_coverage=True)
        assert schedule["ready_for_declared_date_model"]
        assert len(schedule["rows"]) == 1
        event = schedule["rows"][0]
        assert event["cash_per_share"] == "0.249" and event["payment_date"] == DAY
        assert event.get("share_credit_date") is None
        assert len(event["revision_observation_ids"]) == 2
        assert schedule["coverage"]["rows"][0]["absence_proved"] is False
    finally:
        r.close()


@pytest.mark.parametrize("change,code", [
    ({"up_limit": "10.5", "source": "other"}, "PRODUCT_SOURCE_STATE_CONFLICT"),
    ({"unit": "unknown"}, "PRODUCT_REFERENCE_UNIT"),
    ({"down_limit": "0"}, "PRODUCT_REFERENCE_VALUE"),
])
def test_conflicting_or_unknown_limits_block(released, change, code):
    data = rows()
    extra = dict(data["reference.price_limits"][0], observation_id="other", **change)
    data["reference.price_limits"].append(extra)
    revised = publish(market(released.root.parent / "revised"), data)
    r = reader(revised)
    try:
        _, gaps = r.market([SID], DAY, DAY, [DAY], [price()])
        assert code in {g["code"] for g in gaps}
    finally:
        r.close()


def test_no_status_inference_from_positive_volume(released):
    r = reader(released)
    try:
        p = price()
        p.update(tradestatus=None, volume="100000")
        _, gaps = r.market([SID], DAY, DAY, [DAY], [p])
        assert any(g["field"] == "suspended" and g["code"] == "PRODUCT_STATE_UNAVAILABLE" for g in gaps)
    finally:
        r.close()


@pytest.mark.parametrize("defect", ["range", "capture", "quarantine", "empty_marker", "payment"])
def test_incomplete_corporate_evidence_stays_blocked(released, defect):
    data = rows()
    if defect == "capture":
        for c in data["meta.financial_captures"]:
            c["observed_at"] = "2024-04-01T00:00:00+00:00"
    elif defect == "quarantine":
        data["meta.financial_coverage"][0]["quarantined_rows"] = 1
    elif defect == "empty_marker":
        body = json.loads(data["meta.financial_coverage"][0]["coverage_json"])
        body["table_states"]["sharebonus_2"]["explicit_empty_marker"] = False
        data["meta.financial_coverage"][0]["coverage_json"] = json.dumps(body)
    elif defect == "payment":
        data["corporate_action.events"][1]["payment_date"] = None
    revised = publish(market(released.root.parent / "revised"), data, target_end="2024-04-01" if defect == "range" else "2026-12-31")
    r = reader(revised)
    try:
        schedule = ReleasedCorporateView(r, [SID], DAY, DAY).export_aq(
            [SID], start=DAY, end=DAY, tax_model="gross_cash_no_tax", accept_source_scoped_coverage=True)
        assert not schedule["ready_for_declared_date_model"]
        assert schedule["issues"]
    finally:
        r.close()


def test_old_release_keeps_values_after_new_release_and_rejects_mixed_pin(released):
    old = released.receipt()
    data = rows()
    data["reference.price_limits"][0]["up_limit"] = "12"
    revised = publish(released, data)
    r = reader(MarketRelease(released.root, old["release_id"], expected_manifest_sha256=old["manifest_sha256"]))
    try:
        assert r.market([SID], DAY, DAY, [DAY], [price()])[0]["rows"][0]["upper_limit"]["value"] == "11"
    finally:
        r.close()
    with pytest.raises(DataError, match="same immutable release"):
        ReleasedReferences(old, revised.receipt())


def test_known_tradability_bounds_unknown_old_credit_without_filling_date():
    from ashare_data.corporate_actions.history import event_window_state
    fields = dict(record_date="2014-06-11", ex_date="2014-06-12", payment_date="2014-06-12",
                  new_shares_tradable_date="2014-06-12", share_credit_date=None,
                  cash_per_share="0.16", bonus_share_ratio="0", capitalization_share_ratio="0.2")
    event = dict(type="distribution", fields={k: {"value": v} for k, v in fields.items()},
                 observations=[{"fields": fields}])
    assert event_window_state(event, "2026-09-01", "2026-09-07", "economic") is None
    assert event_window_state(event, "2014-06-12", "2014-06-12", "economic") == "matched"
    assert event["fields"]["share_credit_date"]["value"] is None
    fields["new_shares_tradable_date"] = None
    event["fields"]["new_shares_tradable_date"]["value"] = None
    assert event_window_state(event, "2026-09-01", "2026-09-07", "economic") == "unknown_required_date"


def test_public_coverage_has_renderable_assumptions_and_frozen_bindings(released):
    from ashare_data.product.publication import _assess

    row = dict(price(), open="10", high="10", low="10", close="10", volume="100")
    bundle = dict(rows=[row], market_release=released.receipt(),
                  coverage=[dict(security=SID, source="baostock")],
                  receipt={"receipt_sha256": "a" * 64})
    references = dict(name="formal", kind="market_release", release=released.receipt())
    report, evidence, schedule = _assess(dict(calendar=[DAY]), bundle, [SID], DAY, DAY,
                                        "auto", references)
    assert report["ready"] is True
    assert report["assumptions"] == [
        {"text": "accept_daily_replay", "accepted": True},
        {"text": "accept_source_observed_limits", "accepted": True},
    ]
    assert evidence["source_bindings"]["market"]["binding_sha256"] == schedule["binding_sha256"]
