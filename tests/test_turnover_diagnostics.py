"""Synthetic diagnostic contracts; real golden evidence stays outside the distribution."""

import copy
import json
import socket

import pytest

from ashare_data import DataError, reconcile_sina_day
from ashare_data.cli import main
from ashare_data.model import digest

DAY = "2026-10-08"
SEC = "600000.XSHG"


def bar(label, volume="100", amount="1000.0000"):
    return {
        "day": f"{DAY} {label}:00",
        "open": "10",
        "high": "10",
        "low": "10",
        "close": "10",
        "volume": volume,
        "amount": amount,
    }


def quote(volume="500", amount="5000.0000", day=DAY, symbol="sh600000"):
    f = ["0"] * 34
    for i in (1, 3, 4, 5):
        f[i] = "10"
    f[0], f[8], f[9], f[30], f[31], f[33] = (
        "synthetic",
        volume,
        amount,
        day,
        "15:34:59",
        "D|999|9999",
    )
    return ("var hq_str_" + symbol + '="' + ",".join(f) + '";').encode()


def inputs():
    return {
        "minute_body": json.dumps([bar(f"09:{i}") for i in range(31, 36)]).encode(),
        "five_minute_body": json.dumps([bar("09:35", "500", "5000.0000")]).encode(),
        "quote_body": quote(),
    }


def test_offline_equal_totals_are_not_market_admission(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket, "socket", forbidden)
    raw = inputs()
    report = reconcile_sina_day(SEC, DAY, **raw)
    assert report["status"] == "unknown" and report["accepted"] is False
    assert report["network_requests"] == report["fabricated_rows"] == 0
    assert not report["source_authenticity_verified"] and not report["historical_pit_verified"]
    assert report["reference"]["uninterpreted_tail_field_33"] == "D|999|9999"
    assert report["comparisons"]["1m"]["reference_minus_bars_volume"] == "0"
    group = report["label_grouping"]["groups"][0]
    assert group["ohlc_equal"] and group["five_minus_minute_volume"] == "0"
    assert report["diagnostic_id"] == digest(
        {k: v for k, v in report.items() if k != "diagnostic_id"}
    )
    assert reconcile_sina_day(SEC, DAY, **raw) == report


def test_residual_is_exact_and_not_distributed_to_bars():
    raw = inputs()
    raw["quote_body"] = quote(volume="52800", amount="512179.1011")
    before = copy.deepcopy(raw)
    report = reconcile_sina_day(SEC, DAY, **raw)
    assert raw == before and report["status"] == "failed"
    assert report["comparisons"]["1m"]["reference_minus_bars_volume"] == "52300"
    assert report["comparisons"]["1m"]["reference_minus_bars_amount"] == "507179.1011"
    assert report["cause"] == "SOURCE_UNRESOLVED" and not report["execution_permission"]


def test_missing_labels_and_unassigned_rows_are_hypotheses_only():
    raw = inputs()
    raw["minute_body"] = json.dumps([bar("09:30"), bar("14:57"), bar("15:00")]).encode()
    raw["five_minute_body"] = json.dumps([bar("15:00", "200", "2000.0000")]).encode()
    report = reconcile_sina_day(SEC, DAY, **raw)["label_grouping"]
    assert report["hypothesized_missing_intra_half_labels"] == [
        DAY + " 14:58:00",
        DAY + " 14:59:00",
    ]
    assert report["unassigned_minute_labels"] == [DAY + " 09:30:00"]
    assert report["missing_trade_count"] is None and report["semantics_verified"] is False


def test_same_raw_row_different_payload_bytes_changes_identity():
    raw = inputs()
    before = reconcile_sina_day(SEC, DAY, **raw)
    raw["minute_body"] += b"\n"
    after = reconcile_sina_day(SEC, DAY, **raw)
    assert before["comparisons"] == after["comparisons"]
    assert (
        before["inputs"]["1m"]["selected_row_sha256"]
        == after["inputs"]["1m"]["selected_row_sha256"]
    )
    assert before["diagnostic_id"] != after["diagnostic_id"]


@pytest.mark.parametrize(
    "body,code",
    [
        (b"null", "SOURCE_NO_DATA"),
        (b"[]", "SOURCE_NO_DATA"),
        (b'{"error":"access denied"}', "SOURCE_SCHEMA_ERROR"),
        (b"[", "SOURCE_SCHEMA_ERROR"),
        (b"[null]", "SOURCE_SCHEMA_ERROR"),
        (b'[{"day":"x","day":"y"}]', "SOURCE_SCHEMA_ERROR"),
        (b"", "BOUNDED_IMPORT"),
        (b"a" * (2 * 1024 * 1024 + 1), "BOUNDED_IMPORT"),
        ("not bytes", "BOUNDED_IMPORT"),
    ],
)
def test_bad_originals_are_clear_errors(body, code):
    raw = inputs()
    raw["minute_body"] = body
    with pytest.raises(DataError) as exc:
        reconcile_sina_day(SEC, DAY, **raw)
    assert exc.value.code == code


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "unordered",
        "float",
        "missing_amount",
        "negative",
        "zero_price",
        "NaN",
        "fraction_volume",
        "bad_day",
        "seconds",
        "other_date",
    ],
)
def test_invalid_rows_cannot_generate_a_report(fault):
    raw = inputs()
    rows = json.loads(raw["minute_body"])
    if fault == "duplicate":
        rows.append(rows[-1])
    elif fault == "unordered":
        rows.reverse()
    elif fault == "float":
        rows[0]["volume"] = 100.0
    elif fault == "missing_amount":
        del rows[0]["amount"]
    elif fault == "negative":
        rows[0]["amount"] = "-1"
    elif fault == "zero_price":
        rows[0]["low"] = "0"
    elif fault == "NaN":
        rows[0]["amount"] = "NaN"
    elif fault == "fraction_volume":
        rows[0]["volume"] = "1.5"
    elif fault == "bad_day":
        rows[0]["day"] = "2026-02-30 09:31:00"
    elif fault == "seconds":
        rows[0]["day"] = DAY + " 09:31:30"
    else:
        for r in rows:
            r["day"] = r["day"].replace(DAY, "2026-10-07")
    raw["minute_body"] = json.dumps(rows).encode()
    with pytest.raises(DataError):
        reconcile_sina_day(SEC, DAY, **raw)


@pytest.mark.parametrize(
    "body",
    [
        quote(day="2026-10-09"),
        quote(symbol="sh600001"),
        quote(volume="1.5"),
        b'var hq_str_sh600000="";',
        b"javascript();",
        quote().replace(b"15:34:59", b"25:34:59"),
    ],
)
def test_reference_identity_and_numbers_checked(body):
    raw = inputs()
    raw["quote_body"] = body
    with pytest.raises(DataError):
        reconcile_sina_day(SEC, DAY, **raw)


def test_5m_grid_and_missing_group_minute_rows():
    raw = inputs()
    raw["five_minute_body"] = json.dumps([bar("09:36")]).encode()
    with pytest.raises(DataError):
        reconcile_sina_day(SEC, DAY, **raw)
    raw["five_minute_body"] = json.dumps([bar("10:00")]).encode()
    report = reconcile_sina_day(SEC, DAY, **raw)
    assert report["label_grouping"]["groups"][0]["ohlc_equal"] is False


def test_cli_uses_only_files_and_returns_nonadmission(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("CLI network attempted"))
    args = ["reconcile-files", SEC, "--date", DAY]
    for field, flag in (
        ("minute_body", "--one-minute"),
        ("five_minute_body", "--five-minute"),
        ("quote_body", "--quote"),
    ):
        p = tmp_path / field
        p.write_bytes(inputs()[field])
        args.extend([flag, str(p)])
    assert main(args) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["network_requests"] == 0 and report["accepted"] is False


def test_nonfinite_unknown_json_fields_are_not_silently_hashed():
    raw = inputs()
    raw["minute_body"] = raw["minute_body"].replace(
        b'"open": "10"', b'"extra": NaN, "open": "10"', 1
    )
    with pytest.raises(DataError) as exc:
        reconcile_sina_day(SEC, DAY, **raw)
    assert exc.value.code == "SOURCE_SCHEMA_ERROR"
