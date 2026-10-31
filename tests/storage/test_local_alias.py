"""Later-code backfills require explicit provenance and never rewrite native bars."""

import gzip
import hashlib
import json

import pytest

from ashare_data.market import alias as local_alias
from ashare_data.providers.baostock_receipt.baostock_collection import FIELDS, verify_capture
from ashare_data.storage.local_daily import LocalDailyStore

READ_ALIAS_CAPTURE = local_alias.read_alias_capture


@pytest.fixture
def alias_case(tmp_path, monkeypatch):
    store = LocalDailyStore(tmp_path)
    notice = b"%PDF synthetic issuer code-change notice"
    sha = hashlib.sha256(notice).hexdigest()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / (sha + ".gz")).write_bytes(gzip.compress(notice))
    store.save_universe(
        [
            dict(
                symbol="sh600001",
                name="synthetic",
                ipo="2010-01-04",
                out=None,
                board="main",
                source="synthetic",
                receipt=sha,
            )
        ],
        [],
        "2010-01-01",
        "2010-01-09",
    )
    event = dict(
        old_symbol="sh600001",
        new_symbol="sh600002",
        effective_date="2010-01-08",
        issuer_ipo="2010-01-04",
        old_name="synthetic",
        receipt=sha,
    )
    store.save_code_events([event], [dict(sha256=sha, received_at="2026-01-01T00:00:00+00:00")])
    store.save_calendar(
        ["2010-01-01", "2010-01-04", "2010-01-05", "2010-01-08"],
        [dict(url="synthetic-calendar", sha256="synthetic")],
    )
    store.plan("2010-01-01", "2010-01-09")
    request = dict(
        method="query_history_k_data_plus",
        params=dict(
            code="sh.600002",
            fields=FIELDS,
            start_date="2010-01-01",
            end_date="2010-01-09",
            frequency="d",
            adjustflag="3",
        ),
    )
    envelope = dict(
        request=request,
        receipt=dict(receipt_id="synthetic", operation_completed_at="2026-01-01T00:00:00+00:00"),
    )
    rows = [
        dict(
            date=day,
            code="sh.600002",
            open="10",
            high="11",
            low="9",
            close="10",
            preclose="10",
            volume="100",
            amount="1000",
            tradestatus="1",
            adjustflag="3",
            isST="0",
        )
        for day in ("2010-01-01", "2010-01-04", "2010-01-05", "2010-01-08", "2010-01-09")
    ]
    rows[2].update(volume="", amount="", tradestatus="0")
    monkeypatch.setattr(local_alias, "read_alias_capture", lambda _: envelope)
    monkeypatch.setattr(local_alias, "verify_capture", lambda _: (rows, "synthetic_verified"))
    yield store, envelope, rows, sha
    store.close()


def test_alias_query_is_explicit_bounded_and_preserves_native_rows(alias_case):
    store, _, _, sha = alias_case
    before = store.db.total_changes
    frame = store.get_alias_price("600001.XSHG", "explicit-capture")
    assert frame.date.tolist() == ["2010-01-04", "2010-01-05"]
    assert set(frame.symbol) == {"sh600001"}
    assert set(frame.provider_symbol) == {"sh600002"}
    assert set(frame.source) == {"baostock_alias"}
    assert set(frame.issuer_notice_sha256) == {sha}
    assert frame.iloc[1]["amount"] is None
    assert frame.attrs["included_in_native_coverage"] is False
    assert store.db.total_changes == before
    assert store.get_price("600001.XSHG").empty
    assert store.get_alias_price("600001.XSHG", "explicit-capture", start="2010-01-08").empty


def test_alias_rejects_unrelated_security(alias_case):
    store, _, _, _ = alias_case
    with pytest.raises(ValueError, match="matching retained"):
        store.get_alias_price("600003.XSHG", "explicit-capture")


def test_alias_requires_original_notice_bytes(alias_case):
    store, _, _, sha = alias_case
    (store.root / "raw" / (sha + ".gz")).write_bytes(gzip.compress(b"%PDF changed"))
    with pytest.raises(ValueError, match="notice hash"):
        store.get_alias_price("600001.XSHG", "explicit-capture")


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "sh.600003"),
        ("adjustflag", "2"),
        ("tradestatus", "unknown"),
        ("date", "2010-01-10"),
    ],
)
def test_alias_rejects_inconsistent_identity_flags_or_request_dates(alias_case, field, value):
    store, _, rows, _ = alias_case
    rows[0][field] = value
    with pytest.raises(ValueError, match="identity/flags"):
        store.get_alias_price("600001.XSHG", "explicit-capture")


def test_alias_does_not_skip_wire_receipt_validation(alias_case, monkeypatch):
    store, envelope, _, _ = alias_case
    monkeypatch.setattr(local_alias, "verify_capture", verify_capture)
    with pytest.raises(ValueError, match="receipt identity"):
        store.get_alias_price("600001.XSHG", "explicit-capture")


def test_compressed_capture_loader(tmp_path):
    path = tmp_path / "capture.json.gz"
    value = {"synthetic": "no quotes"}
    path.write_bytes(gzip.compress(json.dumps(value).encode()))
    assert local_alias.read_alias_capture(path) == value


@pytest.mark.parametrize("malformed", ["empty_object", "truncated_gzip", "truncated_notice"])
def test_alias_cli_reports_malformed_input_as_structured_error(
    alias_case, monkeypatch, capsys, malformed
):
    from ashare_data.cli import main

    store, _, _, sha = alias_case
    capture = store.root / "malformed.json.gz"
    if malformed == "truncated_notice":
        (store.root / "raw" / (sha + ".gz")).write_bytes(gzip.compress(b"%PDF synthetic")[:-3])
    else:
        monkeypatch.setattr(local_alias, "read_alias_capture", READ_ALIAS_CAPTURE)
        monkeypatch.setattr(local_alias, "verify_capture", verify_capture)
        body = gzip.compress(b"{}")
        capture.write_bytes(body if malformed == "empty_object" else body[:-3])
    exit_code = main(
        [
            "local-daily",
            "query-alias",
            "--root",
            str(store.root),
            "--symbol",
            "600001.XSHG",
            "--capture",
            str(capture),
            "--end",
            "2010-01-09",
        ]
    )
    assert exit_code == 2
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == "INPUT_OR_IO_ERROR"
    assert "invalid" in json.loads(output.err)["error"]["message"]
