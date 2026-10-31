"""Native source reuse, with explicitly scoped securities and no invented units."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from ashare_data.providers.free_updates.free_update_sina_minutes import collect, validate
from ashare_data.providers.free_updates import plan


class Capture:
    def __init__(self, rows):
        self.rows = rows
        self.receipts = []
        self.dataset_metadata = {}

    def get(self, url, headers):
        self.url = url
        self.receipts.append({"received_at": "2026-10-29T04:50:00+00:00"})
        return json.dumps(self.rows).encode()


def bar(day="2026-10-29 11:30:00"):
    return dict(day=day, open="3.01", high="3.02", low="3.00", close="3.01",
                volume="123400", amount="371434.00")


@pytest.mark.parametrize("kind,symbol", [("stock", "sh600000"), ("etf", "sh510300"), ("index", "sh000300")])
@pytest.mark.parametrize("period", ["1", "5", "15", "30", "60"])
def test_native_fields_keys_and_unconverted_units(kind, symbol, period):
    params = dict(asset_type=kind, symbol=symbol, period=period)
    capture = Capture([bar()])
    result = collect(capture, params)
    proof = validate(result, {"function": "sina_native_minutes", "parameters": params}, "2026-10-29", Decimal)
    assert all(result[0][k] == v for k, v in bar().items())
    assert "datalen=800" in capture.url and "scale="+period in capture.url
    assert proof["business_key"] == ["security", "frequency", "day"]
    assert proof["gaps"] == [] and "unconverted" in proof["units"]["volume"]


def test_future_source_label_retained_in_raw_and_excluded_explicitly():
    capture = Capture([bar(), bar("2026-10-29 13:00:00")])
    rows = collect(capture, dict(asset_type="stock", symbol="sh600000", period="1"))
    assert len(rows) == 1 and len(capture.rows) == 2
    assert capture.dataset_metadata["source_returned_count"] == 2
    assert capture.dataset_metadata["deferred_source_labels"] == ["2026-10-29 13:00:00"]


def test_identity_and_duplicate_rejected_without_guessing():
    params = dict(asset_type="etf", symbol="sh510300", period="1")
    capture = Capture([bar()])
    with pytest.raises(ValueError, match="type"):
        collect(capture, dict(params, symbol="sh600000"))
    assert capture.receipts == []
    rows = collect(capture, params)
    with pytest.raises(ValueError, match="duplicate"):
        validate(rows + deepcopy(rows), {"function": "sina_native_minutes", "parameters": params}, "2026-10-29", Decimal)


def test_fifteen_original_rows_keep_distinct_explicit_identity_contexts():
    rows = list(range(12, 17)) + list(range(33, 38)) + list(range(43, 48))
    prepared = plan(rows=rows, context={"stock_symbol": "sh600000", "etf_symbol": "sh510300", "index_symbol": "sh000300"})
    assert len(prepared["jobs"]) == 15
    assert all(j["function"] == "sina_native_minutes" for j in prepared["jobs"])
    assert {i["excel_row"] for i in prepared["items"]} == set(rows)


def test_scoped_manual_entry_does_not_require_a_whole_market_directory(tmp_path):
    from ashare_data.providers.free_updates.free_update_manual import prepare
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"schema": "ad.free-update-manual.v1", "root": "observations",
                            "rows": [12, 33, 43], "context": {"stock_symbol": "sh600000",
                            "etf_symbol": "sh510300", "index_symbol": "sh000300"}}))
    _, prepared, _ = prepare(p)
    assert len(prepared["jobs"]) == 3


def test_bounded_verified_use_does_not_require_deployment_or_full_history():
    from ashare_data.providers.free_updates.free_update_acceptance import user_progress
    item = {"coverage_verified": False, "daily_deployment": False, "formal_published": False,
            "scoped_availability": {"verified": True, "scope": "one named security, 800 bars",
            "entry": "bounded collector", "evidence": "actual receipt and readback",
            "readback_verified": True, "rerun_verified": True}}
    assert user_progress(item)["user_status"] == "可运行"
    assert user_progress({"status": "受阻", "source_issue_summary": "prior wrong generalization",
                          "failure_diagnosis": {"kind": "cooldown_skipped", "reason": "not requested"}})["user_status"] == "待验证"
