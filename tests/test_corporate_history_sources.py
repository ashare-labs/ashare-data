"""Parser/source receipts tested with synthetic raw responses, no live network."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_data.corporate_actions.sources import (
    number,
    source_day,
    parse_sina_actions,
    parse_sina_structure,
    parse_sina_float,
    parse_eastmoney,
)

ROOT = Path(__file__).parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = script("capture_action_history")
builder = script("build_corporate_history")
SEC = "000001.XSHE"


def table(name, rows):
    return (
        '<table id="'
        + name
        + '">'
        + "".join(
            "<tr>" + "".join("<td>" + str(cell) + "</td>" for cell in row) + "</tr>" for row in rows
        )
        + "</table>"
    )


def html(content):
    return ('<script>var page_symbol = "sz000001";</script>' + content).encode("gb18030")


def actions():
    return html(
        table(
            "sharebonus_1",
            [
                ["分红方案(每10股)", "派息(税前)(元)"],
                [
                    "2021-05-01",
                    "0",
                    "--",
                    "16.0058",
                    "实施",
                    "2021-06-02",
                    "2021-06-01",
                    "--",
                    "查看",
                ],
                ["2022-05-01", "0", "0", "3", "预案", "--", "--", "--", "查看"],
            ],
        )
        + table(
            "sharebonus_2",
            [
                ["配股方案(每10股配股股数)", "基准股本(股)"],
                [
                    "2019-04-03",
                    "3",
                    "4.65",
                    "2790800000",
                    "2019-04-17",
                    "2019-04-08",
                    "2019-04-09 00:00:00",
                    "2019-04-15",
                    "2019-04-29",
                    "",
                    "查看",
                ],
            ],
        )
    )


def structure():
    return html(
        table(
            "StockStructureNewTable1",
            [
                ["变动日期", "2021-06-02", "2019-12-31"],
                ["公告日期", "2021-06-03", "2020-03-31"],
                ["变动原因", "股份回购", "定期报告"],
                ["总股本", "100.25 万股", "101 万股"],
                ["流通A股", "90.05 万股", "--"],
                ["流通B股", "--", "--"],
            ],
        )
    )


def test_dividend_per10_and_rights_cost_not_free_stock():
    parsed = parse_sina_actions(actions(), SEC)
    assert len(parsed["actions"]) == 2
    assert len(parsed["nonimplemented"]) == 1
    cash, rights = parsed["actions"]
    assert cash["fields"]["cash_per_share"] == "1.60058"
    assert cash["fields"]["bonus_share_ratio"] == "0"
    assert cash["fields"]["capitalization_share_ratio"] is None
    assert cash["fields"]["payment_date"] is None
    assert rights["fields"]["rights_ratio"] == "0.3"
    assert rights["fields"]["rights_price"] == "4.65"
    assert rights["fields"]["base_shares"] == "2790800000"
    assert rights["fields"]["subscription_start"] == "2019-04-09"
    assert rights["fields"]["new_shares_tradable_date"] == "2019-04-29"


def test_units_identity_and_ragged_structure_fail_closed():
    with pytest.raises(ValueError, match="identity"):
        parse_sina_actions(actions(), "000002.XSHE")
    with pytest.raises(ValueError, match="units"):
        parse_sina_actions(
            actions().replace("基准股本(股)".encode("gb18030"), "基准股本(万股)".encode("gb18030")),
            SEC,
        )
    with pytest.raises(ValueError, match="unit"):
        parse_sina_structure(structure().replace(" 万股".encode("gb18030"), b" shares"), SEC)
    with pytest.raises(ValueError, match="ragged"):
        parse_sina_structure(structure().replace(b"<td>--</td>", b"", 1), SEC)


def test_share_units_precision_and_total_not_float():
    p = parse_sina_structure(structure(), SEC)
    a, b = p["shares"]
    assert a["fields"]["total_shares"] == "1002500"
    assert a["fields"]["float_a_shares"] == "900500"
    assert a["source_quantum_shares"]["total_shares"] == "100"
    assert b["source_quantum_shares"]["total_shares"] == "10000"
    assert b["date_semantics"] == "report_snapshot_date"
    assert b["fields"]["float_a_shares"] is None
    assert b["announcement_date"] == "2020-03-31"


def test_negative_share_cell_is_explicit_gap_and_keeps_other_dates_and_fields():
    raw = structure().replace(b"90.05", b"-10.25")
    parsed = parse_sina_structure(raw, SEC)
    assert parsed["source_status"] == "partial_provider_tables"
    first, second = parsed["shares"]
    assert first["fields"]["total_shares"] == "1002500"
    assert first["fields"]["float_a_shares"] is None
    assert first["source_quantum_shares"]["float_a_shares"] is None
    assert first["native_fields"]["流通A股"] == "-10.25 万股"
    assert second == parse_sina_structure(structure(), SEC)["shares"][1]
    gap, = parsed["unparsed_records"]
    assert gap["reason"] == "negative_source_share_count"
    assert gap["source_value"] == "-10.25 万股"
    assert gap["date"] == "2021-06-02"
    assert gap["locator"] == first["locator"] + "/field/float_a_shares"
    with pytest.raises(ValueError, match="unit"):
        parse_sina_structure(raw.replace(" 万股".encode("gb18030"), b" shares"), SEC)


@pytest.mark.parametrize("label", ["总股本", "流通A股", "流通B股", "流通H股", "优先股", "限售A股"])
def test_negative_share_quarantine_never_changes_sign_or_assumes_zero(label):
    rows = [["变动日期", "20200101"], ["公告日期", "20200102"], ["变动原因", "定期报告"],
            ["总股本", "100 万股"], ["流通A股", "10 万股"]]
    rows = [r for r in rows if r[0] != label] + [[label, "-12.34 万股"]]
    parsed = parse_sina_structure(html(table("StockStructureNewTable1", rows)), SEC)
    gap, = parsed["unparsed_records"]
    assert parsed["shares"][0]["fields"][gap["field"]] is None
    assert gap["source_value"] == "-12.34 万股"
    assert gap["native_fields"][label] == "-12.34 万股"


def test_float_series_not_total_and_inert_jsonp():
    raw = b'var KKE_ShareAmount_sz000001=([{"date":"2021-06-02","amount":90.05}]);'
    row = parse_sina_float(raw, SEC)["shares"][0]
    assert row["fields"] == {"float_a_shares": "900500"}
    assert row["announcement_date"] is None
    assert (
        parse_sina_float(b"var KKE_ShareAmount_sz000001=(null);", SEC)["source_status"]
        == "provider_no_data"
    )
    with pytest.raises(ValueError):
        parse_sina_float(raw + b'__import__("os").system("anything")', SEC)


@pytest.mark.parametrize("value", [None, "--", "1900-01-01", "1900-01-01 00:00:00", "19000101"])
def test_missing_date_remains_unknown(value):
    assert source_day(value) is None


@pytest.mark.parametrize("value", ["NaN", "-1", "Infinity", "1e10000000", "1e-10000000", "bad"])
def test_invalid_amount_cannot_become_cash(value):
    with pytest.raises((ValueError, ArithmeticError)):
        number(value)


def test_eastmoney_pagination_and_identity_and_null_zero():
    row = dict(
        SECURITY_CODE="000001",
        SECUCODE="000001.SZ",
        ASSIGN_PROGRESS="实施分配",
        NOTICE_DATE="2021-05-25 00:00:00",
        PLAN_NOTICE_DATE="2021-03-01 00:00:00",
        EQUITY_RECORD_DATE="2021-06-01 00:00:00",
        EX_DIVIDEND_DATE="2021-06-02 00:00:00",
        PRETAX_BONUS_RMB="16.005847",
        BONUS_RATIO=None,
        IT_RATIO=0,
    )
    result = dict(success=True, result=dict(data=[row], pages=1, count=1))
    fields = parse_eastmoney(json.dumps(result).encode(), SEC)["actions"][0]["fields"]
    assert fields["cash_per_share"] == "1.6005847"
    assert fields["bonus_share_ratio"] is None and fields["capitalization_share_ratio"] == "0"
    assert fields["announcement_date"] == "2021-05-25"
    assert fields["plan_announcement_date"] == "2021-03-01"
    result["result"]["pages"] = 2
    with pytest.raises(ValueError, match="pagination"):
        parse_eastmoney(json.dumps(result).encode(), SEC)
    result["result"]["pages"] = 1
    row["SECUCODE"] = "000002.SZ"
    with pytest.raises(ValueError, match="identity"):
        parse_eastmoney(json.dumps(result).encode(), SEC)
    assert parse_eastmoney(b'{"success":false,"result":null}', SEC)["absence_proved"] is False


def request():
    return dict(
        id="test",
        kind="sina_actions",
        security=SEC,
        url="https://vip.stock.finance.sina.com.cn/test",
    )


def test_capture_append_only_idempotence_and_hash_validation(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        Path(args[args.index("--output") + 1]).write_bytes(actions())
        return SimpleNamespace(stdout=b"200", stderr=b"", returncode=0)

    monkeypatch.setattr(capture.subprocess, "run", run)
    assert capture.capture(tmp_path, request())["status"] == "captured"
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert capture.capture(tmp_path, request())["status"] == "reused"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert len(calls) == 1
    with pytest.raises(ValueError, match="parameters"):
        capture.capture(tmp_path, {**request(), "url": request()["url"] + "other"})
    next(tmp_path.glob("*.body")).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="integrity"):
        capture.capture(tmp_path, request())


@pytest.mark.parametrize("status", ["401", "402", "403", "429"])
def test_host_stops_after_permission_payment_or_rate_response(tmp_path, monkeypatch, status):
    calls = []

    def blocked(root, req, retry):
        calls.append(req["id"])
        return dict(id=req["id"], status="failed", http=status)

    monkeypatch.setattr(capture, "capture", blocked)
    rows = list(capture.run_batch(tmp_path, [request(), {**request(), "id": "second"}], workers=2))
    assert len(calls) == 1
    assert rows[1]["status"] == "host_stopped"


def test_http200_html_not_accepted_as_pdf(tmp_path, monkeypatch):
    def run(args, **kwargs):
        Path(args[args.index("--output") + 1]).write_bytes(b"<html>blocked</html>")
        return SimpleNamespace(stdout=b"200", stderr=b"", returncode=0)

    monkeypatch.setattr(capture.subprocess, "run", run)
    assert capture.capture(tmp_path, {**request(), "kind": "issuer_pdf"})["status"] == "failed"


def test_offline_build_is_deterministic_and_retains_failed_raw(tmp_path):
    captures = tmp_path / "captures"
    captures.mkdir()
    for name, kind, body, ok in [
        ("actions", "sina_actions", actions(), True),
        ("shares", "sina_structure", structure(), True),
        ("failed", "issuer_pdf", b"permission denied", False),
    ]:
        (captures / (name + ".body")).write_bytes(body)
        receipt = dict(
            request=dict(id=name, kind=kind, security=SEC, url="https://example.invalid/test"),
            body=name + ".body",
            sha256=hashlib.sha256(body).hexdigest(),
            bytes=len(body),
            ok=ok,
            http_status="200" if ok else "403",
            error="" if ok else "forbidden",
            started_at="2026-10-15T12:00:00+00:00",
            received_at="2026-10-15T12:00:01+00:00",
        )
        (captures / (name + ".receipt.json")).write_text(json.dumps(receipt))
    scope = tmp_path / "scope.json"
    scope.write_text(
        json.dumps(dict(securities=[dict(security=SEC, start="2020-01-01", end="2025-12-31")]))
    )
    curation = tmp_path / "curation.json"
    curation.write_text(json.dumps(dict(schema="ad.corporate-history-curation.v1", actions=[])))
    a = builder.build(captures, curation, scope, tmp_path / "a")
    b = builder.build(captures, curation, scope, tmp_path / "b")
    assert a["sha256"] == b["sha256"]
    data = json.loads((tmp_path / "a/data.json").read_text())
    assert len(data["actions"]) == 1  # 2019 rights stay outside frozen consumer window.
    assert len(data["shares"]) == 2  # One prior snapshot, one in window.
    failure = data["acquisition_failures"][0]
    assert (tmp_path / "a" / failure["body"]).read_bytes() == b"permission denied"


def test_explicit_stock_credit_detail_not_cash_payment_or_tradable_date():
    from ashare_data.corporate_actions.sources import parse_sina_action_detail

    detail = html(
        table(
            "sharebonusdetail",
            [
                ["转增比例（10转增）", "4.00"],
                ["送股比例（10送）", "--"],
                ["登记日", "2022-06-02"],
                ["除息日", "2022-06-06"],
                ["上市日", "2022-06-07"],
                ["红利/配股起始日（送、转股到账日）", "2022-06-06"],
            ],
        )
    )
    row = parse_sina_action_detail(detail, SEC)["actions"][0]
    assert row["fields"]["share_credit_date"] == "2022-06-06"
    assert row["fields"]["new_shares_tradable_date"] == "2022-06-07"
    assert row["fields"]["capitalization_share_ratio"] == "0.40"
    assert "payment_date" not in row["fields"]
    cash_only = detail.replace(b"4.00", b"--")
    assert (
        parse_sina_action_detail(cash_only, SEC)["actions"][0]["fields"]["share_credit_date"]
        is None
    )
