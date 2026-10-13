"""Raw-source identity/clock gates, independent of normalized package fixtures."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from ashare_data import DataError

spec = importlib.util.spec_from_file_location(
    "historical_builder", Path(__file__).parents[1] / "scripts/build_historical_market.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def capture(tmp_path, *, query="SELECT count(*) FROM example", status="Success"):
    from urllib.parse import urlencode

    raw = json.dumps(
        dict(
            repository_owner="chenditc",
            repository_name="investment_data",
            commit_ref="master",
            sql_query=query,
            query_execution_status=status,
            rows=[{"row_count": "1"}],
        )
    ).encode()
    (tmp_path / "capture.json").write_bytes(raw)
    receipt = dict(
        status=200,
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        url="https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master?"
        + urlencode({"q": query}),
        captured_start="2026-10-13T02:00:00+00:00",
        captured_end="2026-10-13T02:00:01+00:00",
    )
    (tmp_path / "capture.json.receipt.json").write_text(json.dumps(receipt))
    return builder.Sources(tmp_path), query


def test_source_query_identity_bound_to_receipt_and_echo(tmp_path):
    source, sql = capture(tmp_path)
    assert source.archive("capture.json", sql)[0] == [{"row_count": "1"}]
    with pytest.raises(DataError, match="query identity"):
        source.archive("capture.json", sql + " WHERE different = 1")


def test_raw_tamper_even_same_size_rejected(tmp_path):
    source, sql = capture(tmp_path)
    path = tmp_path / "capture.json"
    path.write_bytes(path.read_bytes().replace(b'"1"', b'"2"'))
    with pytest.raises(DataError) as exc:
        source.archive("capture.json", sql)
    assert exc.value.code == "HISTORICAL_INTEGRITY"


def test_rowlimit_not_a_complete_query(tmp_path):
    source, sql = capture(tmp_path, status="RowLimit")
    with pytest.raises(DataError) as exc:
        source.archive("capture.json", sql)
    assert exc.value.code == "HISTORICAL_SOURCE"
    # Only an explicitly identified initial population page may use this;
    # the builder separately requires the aggregate to equal pinned COUNT.
    assert source.archive("capture.json", sql, rowlimit=True)[0]


def test_source_error_is_not_empty_success(tmp_path):
    source, sql = capture(tmp_path, status="Error")
    with pytest.raises(DataError):
        source.archive("capture.json", sql, rowlimit=True)


@pytest.mark.parametrize(
    "rows",
    [
        [{"symbol": "SH600000", "tradedate": "2024-01-02"}] * 2,
        [{"symbol": "SZ000001", "tradedate": "2024-01-02"}],
        [{"symbol": "SH600000", "tradedate": "2024-01-03"}],
    ],
)
def test_source_panel_rejects_duplicates_and_scope_escape(rows):
    with pytest.raises(DataError):
        builder.validate_panel(
            rows, {"symbols": ["SH600000"], "start": "2024-01-02", "end": "2024-01-02"}
        )


@pytest.mark.parametrize("raw", [None, "", "2", "true", 0, False])
def test_flag_unknown_not_false(raw):
    projected = builder.flag({"is_st": raw}, "is_st", ["capture.json"])
    assert projected["value"] is None and projected["status"] == "unavailable"


def test_flags_independent_and_suspension_inverted():
    row = {"is_st": "1", "tradestatus": "0"}
    assert builder.flag(row, "is_st", ["capture.json"])["value"] is True
    assert builder.flag(row, "tradestatus", ["capture.json"], invert=True)["value"] is True
    assert builder.flag(None, "tradestatus", ["capture.json"], invert=True)["value"] is None
