"""Explicit CMES proposal stages must not create executable entitlements."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from ashare_data.product.released_corporate import ReleasedCorporateView

SID = "600000.XSHG"
DAY = "2026-10-20"


class FrozenRows:
    release = SimpleNamespace(sha256="a" * 64, release_id=38)

    def __init__(self, rows):
        self.rows = rows

    def query(self, table, condition, parameters):
        assert table == "meta.financial_coverage"
        return []

    def selected(self, table, securities):
        assert table == "corporate_action.events" and securities == [SID]
        return self.rows

    def ref(self, row, table):
        return {"table": table, "source": row["source"], "observation_id": row["event_id"]}


def event(*, source="cmes", status=None, implemented=None):
    return dict(event_id="source-event", source=source, source_status=status,
                security=SID, event_type="distribution", implemented=implemented,
                source_fields_json=json.dumps({"fields": {
                    "record_date": "2026-10-19", "ex_date": DAY, "payment_date": DAY,
                    "cash_per_share": "0.42", "bonus_share_ratio": "0",
                    "capitalization_share_ratio": "0"}}))


def schedule(row):
    before = deepcopy(row)
    value = ReleasedCorporateView(FrozenRows([row]), [SID], DAY, DAY).export_aq(
        [SID], start=DAY, end=DAY, tax_model="gross_cash_no_tax",
        accept_source_scoped_coverage=True)
    assert row == before  # Source records stay intact, including their null status.
    return value


@pytest.mark.parametrize("status", ["预案", "股东大会通过"])
def test_explicit_cmes_preimplementation_stages_are_excluded_without_attesting_coverage(status):
    value = schedule(event(status=status))
    assert value["rows"] == []
    assert not value["ready_for_declared_date_model"]
    assert any(x["reason"] == "source_table_coverage_missing" for x in value["issues"])
    assert not any(x["reason"] == "source_implementation_unknown" for x in value["issues"])


@pytest.mark.parametrize("status", [None, "", "实施", "未知", "预案 ", "已通过"])
def test_unresolved_cmes_status_is_still_blocking(status):
    value = schedule(event(status=status))
    assert len(value["rows"]) == 1
    assert not value["ready_for_declared_date_model"]
    assert any(x["reason"] == "source_implementation_unknown" for x in value["issues"])


@pytest.mark.parametrize("source", ["other", "sina_actions", "CMES"])
@pytest.mark.parametrize("status", ["预案", "股东大会通过"])
def test_cmes_stage_mapping_never_applies_to_other_sources(source, status):
    value = schedule(event(source=source, status=status))
    assert len(value["rows"]) == 1
    assert any(x["reason"] == "source_implementation_unknown" for x in value["issues"])


@pytest.mark.parametrize("status", ["实施", "预案", "股东大会通过", None])
def test_explicit_implementation_is_not_overridden_by_stage_text(status):
    value = schedule(event(status=status, implemented=True))
    assert len(value["rows"]) == 1
    assert value["rows"][0]["cash_per_share"] == "0.42"
    assert not any(x["reason"] == "source_implementation_unknown" for x in value["issues"])
    assert not value["ready_for_declared_date_model"]  # Missing coverage is unchanged.


@pytest.mark.parametrize("source", ["cmes", "other"])
def test_explicit_nonimplementation_remains_excluded(source):
    value = schedule(event(source=source, status="未知", implemented=False))
    assert value["rows"] == []
    assert not value["ready_for_declared_date_model"]


def test_implemented_cmes_cash_dividend_projects_zero_stock_ratios_without_mutating_row():
    row = dict(
        event_id="cmes-cash-only",
        source="cmes",
        source_status="实施",
        security=SID,
        event_type="distribution",
        implemented=True,
        announcement_date="2026-06-27",
        implementation_announcement_date="2026-07-10",
        record_date=None,
        ex_date=DAY,
        payment_date=None,
        shares_tradable_date=None,
        gross_cash="0.42",
        bonus_ratio=None,
        capitalization_ratio=None,
        source_fields_json=json.dumps({"source_record_relation": "source.cmes_action_records"}),
    )
    value = schedule(row)
    assert len(value["rows"]) == 1
    projected = value["rows"][0]
    assert projected["cash_per_share"] == "0.42"
    assert projected["bonus_share_ratio"] == "0"
    assert projected["capitalization_share_ratio"] == "0"
    assert projected["record_date"] is None
    assert projected["payment_date"] == DAY
    assert not any(
        x.get("field") in {"record_date", "payment_date", "bonus_share_ratio", "capitalization_share_ratio", "share_credit_date", "new_shares_tradable_date"}
        for x in value["issues"]
    )

