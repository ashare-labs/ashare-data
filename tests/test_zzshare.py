"""Synthetic controls and optional external fixed evidence; never live network."""

from dataclasses import FrozenInstanceError
from decimal import Decimal, InvalidOperation, localcontext
from email.message import Message
import hashlib
import http.client
import io
import json
import os
import socket
import urllib.error

import pytest

from ashare_data import DataError, Store, ZzshareSource
from ashare_data import zzshare as api
from ashare_data.cli import main


def fail_code(code):
    return pytest.raises(DataError, match=".") if code is None else ErrorCode(code)


class ErrorCode:
    def __init__(self, code):
        self.code = code

    def __enter__(self):
        self.check = pytest.raises(DataError)
        return self.check.__enter__()

    def __exit__(self, *exc):
        result = self.check.__exit__(*exc)
        assert exc[1].code == self.code
        return result


def source_row(day="20260928"):
    return dict(
        ts_code="600000.SH",
        trade_date=day,
        open=11.21,
        high=11.27,
        low=11.1,
        close=11.25,
        prev_close=11.20,
        high_limit=12.32,
        low_limit=10.08,
        volume=123400,
        turnover=1389001.25,
        is_st=0,
        is_paused=0,
        factor=1.01,
    )


def body(rows=None):
    rows = [source_row()] if rows is None else rows
    return json.dumps(
        dict(code=200, data=dict(ts_code="600000.SH", candle_mode=0, count=len(rows), list=rows)),
        separators=(",", ":"),
    ).encode()


def capture(tmp_path, *, raw=None, rows=None, status=200, end="20260928", changes=None):
    raw = body(rows) if raw is None else raw
    receipt = dict(
        url=api.ENDPOINT
        + "600000.SH?candle_mode=0&get_type=range&start_date=20260928&end_date="
        + end
        + "&limit=10",
        method="GET",
        request_headers={"sdk-key": "anonymous"},
        automatic_retries=0,
        redirects=False,
        credentials_loaded=False,
        cookies_sent=False,
        started_at="2026-10-10T07:00:00+00:00",
        finished_at="2026-10-10T07:00:01+00:00",
        state="response_saved",
        http_status=status,
        body_truncated=False,
        body_bytes=len(raw),
        body_sha256=hashlib.sha256(raw).hexdigest(),
        retry_after=None,
    )
    receipt.update(changes or {})
    root = tmp_path / "capture"
    root.mkdir(exist_ok=True)
    (root / "business.raw").write_bytes(raw)
    (root / "business.receipt.json").write_text(json.dumps(receipt))
    return root


def load(store, directory):
    sid = store.import_zzshare_capture(directory, enable_research=True)
    return store.zzshare(sid, enable_research=True)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("unexpected network")
    )


def test_disabled_before_any_io(store, monkeypatch):
    monkeypatch.setattr(
        api.urllib.request, "build_opener", lambda *a: pytest.fail("disabled fetch")
    )
    for call in (
        lambda: store.import_zzshare_capture("/missing"),
        lambda: store.zzshare("latest"),
        lambda: store.zzshare_snapshots(),
        lambda: store.recover_zzshare(),
        lambda: ZzshareSource().fetch(store, security="bad", start_date="bad", end_date="bad"),
    ):
        with fail_code("ZZSHARE_DISABLED"):
            call()
    assert ZzshareSource.capabilities()["default_enabled"] is False
    assert store.baostock_snapshots() == []
    assert store.snapshots() == []


def test_fixed_real_capture_roundtrip(store, monkeypatch):
    source = os.environ.get("ASHARE_ZZSHARE_CAPTURE")
    if not source:
        pytest.skip("real capture remains external")
    monkeypatch.setattr(api.urllib.request, "build_opener", lambda *a: pytest.fail("offline query"))
    view = load(store, source)
    sid = view.descriptor()["capture_id"]
    assert store.import_zzshare_capture(source, enable_research=True) == sid
    result = Store(store.root).zzshare(sid, enable_research=True).get_price()
    assert [row.prev_close.raw_token for row in result.rows] == ["9.0", "9.16", "9.18"]
    assert [row.source_claimed_limits.high.value for row in result.rows] == [
        Decimal("9.9"),
        Decimal("10.08"),
        Decimal("10.1"),
    ]
    assert (
        result.report["raw_response_sha256"]
        == "711843982c3d846529486837c73aeeb31c4103e001454b15bdededf5c89bab11"
    )
    assert result.report["coverage"]["requested_dates_present"]
    for row in result.rows:
        assert row.rule_derived_limits is row.historical_available_at is None
        assert not row.historical_pit and not row.execution_permission


def test_precision_and_detached_results(store, tmp_path):
    raw = body().replace(b"11.25", b"11.250000000000000000001")
    view = load(store, capture(tmp_path, raw=raw))
    result = view.get_daily()
    row = result.rows[0]
    assert row.close.raw_token == "11.250000000000000000001"
    assert row.close.value == Decimal("11.250000000000000000001")
    assert row.source_fields["close"] == row.close.value
    assert row.source_claimed_limits.classification == "source_claimed"
    assert row.rule_derived_limits is None
    with pytest.raises(FrozenInstanceError):
        row.close = None
    row.source_fields["close"] = 0
    result.report["quality"]["execution_permission"] = True
    view.lineage()["receipt"]["url"] = "https://wrong"
    assert view.get_daily().to_dict() == result.to_dict()
    assert not view.quality()["execution_permission"]


@pytest.mark.parametrize(
    "value,state",
    [
        (None, "source_null"),
        (False, "invalid_numeric_or_flag"),
        ("12.32", "invalid_numeric_or_flag"),
        (-1, "invalid_numeric_or_flag"),
    ],
)
def test_unknown_limit_never_filled(store, tmp_path, value, state):
    row = source_row()
    row["high_limit"] = value
    view = load(store, capture(tmp_path, rows=[row]))
    with fail_code("ZZSHARE_FIELDS_UNKNOWN"):
        view.get_daily()
    result = view.get_daily(strict=False)
    assert result.rows[0].source_claimed_limits.high.value is None
    assert result.rows[0].source_claimed_limits.high.state == state
    assert result.rows[0].rule_derived_limits is None


def test_missing_dates_and_fields(store, tmp_path):
    row = source_row()
    del row["is_paused"]
    view = load(store, capture(tmp_path, rows=[row], end="20260930"))
    with fail_code("ZZSHARE_COVERAGE_UNKNOWN"):
        view.get_daily()
    result = view.get_daily(strict=False)
    assert result.rows[0].is_paused.state == "field_missing"
    assert result.report["coverage"]["missing_dates"] == ["2026-09-29", "2026-09-30"]
    assert not result.report["coverage"]["complete_market_coverage"]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "ZZSHARE_ACCESS_DENIED"),
        (403, "ZZSHARE_ACCESS_DENIED"),
        (429, "ZZSHARE_RATE_LIMITED"),
        (302, "ZZSHARE_REDIRECT"),
        (500, "ZZSHARE_HTTP"),
    ],
)
def test_failed_http_sealed_but_query_errors(store, tmp_path, status, code):
    view = load(
        store, capture(tmp_path, raw=b"failure", status=status, changes={"retry_after": "120"})
    )
    with fail_code(code) as error:
        view.get_daily(strict=False)
    assert error.value.details["capture_id"] == view.descriptor()["capture_id"]
    assert error.value.details["retry_after"] == "120"
    assert view.coverage()["failure"]["code"] == code


@pytest.mark.parametrize(
    "change",
    [
        "invalid_json",
        "duplicate_key",
        "wrong_mode",
        "wrong_security",
        "duplicate_day",
        "out_of_range",
        "wrong_count",
        "bool_code",
    ],
)
def test_schema_failures_are_explicit(store, tmp_path, change):
    data = json.loads(body())
    if change == "invalid_json":
        raw = b"<html>no</html>"
    elif change == "duplicate_key":
        raw = body().replace(b'"code":200', b'"code":200,"code":200')
    else:
        if change == "wrong_mode":
            data["data"]["candle_mode"] = 1
        if change == "wrong_security":
            data["data"]["list"][0]["ts_code"] = "000001.SZ"
        if change == "duplicate_day":
            data["data"]["list"] *= 2
            data["data"]["count"] = 2
        if change == "out_of_range":
            data["data"]["list"][0]["trade_date"] = "20260927"
        if change == "wrong_count":
            data["data"]["count"] = 2
        if change == "bool_code":
            data["code"] = True
        raw = json.dumps(data).encode()
    view = load(store, capture(tmp_path, raw=raw))
    with fail_code("ZZSHARE_API" if change == "bool_code" else "ZZSHARE_SCHEMA"):
        view.get_daily(strict=False)


def test_empty_and_truncated(store, tmp_path):
    view = load(store, capture(tmp_path, rows=[]))
    with fail_code("ZZSHARE_COVERAGE_UNKNOWN"):
        view.get_daily()
    assert not view.get_daily(strict=False).rows
    view = load(store, capture(tmp_path, changes={"body_truncated": True}))
    with fail_code("ZZSHARE_TRUNCATED"):
        view.get_daily()


def test_content_length_mismatch_not_promoted(store, tmp_path):
    view = load(store, capture(tmp_path, changes={"response_headers": {"Content-Length": "99999"}}))
    with fail_code("ZZSHARE_LENGTH_MISMATCH"):
        view.get_daily(strict=False)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"body_sha256": "0" * 64}, "INTEGRITY"),
        ({"url": "http://localhost/x"}, "ZZSHARE_RECEIPT"),
        ({"request_headers": {"sdk-key": "secret"}}, "ZZSHARE_RECEIPT"),
        ({"redirects": True}, "ZZSHARE_RECEIPT"),
        ({"finished_at": "2026-10-10T06:00:00+00:00"}, "ZZSHARE_RECEIPT"),
    ],
)
def test_invalid_receipt_never_published(store, tmp_path, changes, code):
    with fail_code(code):
        load(store, capture(tmp_path, changes=changes))
    assert store.zzshare_snapshots(enable_research=True) == []


def test_versions_are_immutable(store, tmp_path):
    source = capture(tmp_path)
    view = load(store, source)
    old = view.get_daily().to_dict()
    row = source_row()
    row["close"] = 11.26
    newer = load(store, capture(tmp_path, rows=[row]))
    assert newer.descriptor()["capture_id"] != old["report"]["capture_id"]
    assert Store(store.root).zzshare(view._id, enable_research=True).get_daily().to_dict() == old
    object_file = store.root / "zzshare-objects" / view.lineage()["raw_response_sha256"]
    object_file.chmod(0o644)
    object_file.write_bytes(b"changed")
    with fail_code("INTEGRITY"):
        store.zzshare(view._id, enable_research=True)


@pytest.mark.parametrize("corrupt", [False, True])
def test_publication_recovery(store, tmp_path, monkeypatch, corrupt):
    source = capture(tmp_path)
    original = api.immutable_write

    def crash(path, raw):
        if path.parent.name == "zzshare-manifests":
            raise OSError("simulated interruption")
        return original(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(api, "immutable_write", crash)
        with pytest.raises(OSError):
            load(store, source)
    entry = store.zzshare_snapshots(enable_research=True)[0]
    assert entry["status"] == "prepared"
    with fail_code("ZZSHARE_RECOVERY_REQUIRED"):
        load(store, source)
    if corrupt:
        item = next((store.root / "zzshare-objects").iterdir())
        item.chmod(0o644)
        item.write_bytes(b"bad")
    assert store.recover_zzshare(enable_research=True)[0]["status"] == (
        "aborted" if corrupt else "published"
    )
    if not corrupt:
        assert store.zzshare(entry["capture_id"], enable_research=True).get_daily().rows


class Response(io.BytesIO):
    code = 200

    def __init__(self, raw, status=200):
        super().__init__(raw)
        self.code = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Retry-After"] = "60"


@pytest.mark.parametrize("outcome", ["ok", "403", "429", "302", "network", "partial", "schema"])
def test_fetch_explicit_one_attempt(store, monkeypatch, outcome):
    calls = []

    def open_once(request, timeout):
        calls.append(request)
        assert request.headers["Sdk-key"] == "anonymous"
        assert timeout == 15
        if outcome == "network":
            raise urllib.error.URLError("offline")
        if outcome == "partial":
            raise http.client.IncompleteRead(b"{", 5)
        if outcome in ("403", "429", "302"):
            response = Response(b"denied", int(outcome))
            raise urllib.error.HTTPError(
                request.full_url, int(outcome), "denied", response.headers, response
            )
        return Response(body() if outcome == "ok" else b"broken")

    class Opener:
        open = staticmethod(open_once)

    def build(*handlers):
        assert any(
            isinstance(h, api.urllib.request.ProxyHandler) and h.proxies == {} for h in handlers
        )
        assert any(isinstance(h, api._NoRedirect) for h in handlers)
        return Opener()

    monkeypatch.setattr(api.urllib.request, "build_opener", build)
    source = ZzshareSource(enabled=True)
    if outcome == "ok":
        sid = source.fetch(
            store, security="600000.XSHG", start_date="2026-09-28", end_date="2026-09-28"
        )
        assert store.zzshare(sid, enable_research=True).get_daily().rows
    else:
        with pytest.raises(DataError) as error:
            source.fetch(
                store, security="600000.XSHG", start_date="2026-09-28", end_date="2026-09-28"
            )
        sid = error.value.details["capture_id"]
        assert store.zzshare(sid, enable_research=True).coverage()["failure"]
    assert len(calls) == 1
    assert len(store.zzshare_snapshots(enable_research=True)) == 1


def test_cli(store, tmp_path, capsys):
    assert main(["zzshare-capabilities"]) == 0
    assert json.loads(capsys.readouterr().out)["default_enabled"] is False
    source = capture(tmp_path)
    args = ["--store", str(store.root)]
    assert main(args + ["zzshare-import", str(source)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "ZZSHARE_DISABLED"
    assert main(args + ["zzshare-import", str(source), "--enable-research"]) == 0
    sid = json.loads(capsys.readouterr().out)["capture_id"]
    assert main(args + ["zzshare-query", "daily", "--capture", sid, "--enable-research"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"][0]["close"]["raw_token"] == "11.25"
    assert (
        main(
            args
            + ["zzshare-query", "quality", "--capture", sid, "--enable-research", "--allow-partial"]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "security,start,end",
    [
        ("wrong", "2026-09-28", "2026-09-30"),
        ("600000.XSHG", "2026-09-30", "2026-09-28"),
        ("600000.XSHG", "2026-08-01", "2026-09-30"),
    ],
)
def test_bound_before_network(store, security, start, end, monkeypatch):
    monkeypatch.setattr(
        api.urllib.request, "build_opener", lambda *a: pytest.fail("invalid scope sent")
    )
    with pytest.raises(DataError):
        ZzshareSource(enabled=True).fetch(store, security=security, start_date=start, end_date=end)


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize(
    "missing,changes",
    [
        ("open", {"high": 8, "low": 9, "close": 10.5}),
        ("close", {"high": 11, "low": 11.1}),
        ("high", {"low": 11.22}),
        ("low", {"high": 11.2}),
        ("open", {"low": 11.26}),
        ("low", {"high": 11.22}),
    ],
)
def test_partial_ohlc_known_contradictions_rejected(store, tmp_path, strict, missing, changes):
    row = source_row()
    row.update(changes)
    del row[missing]
    view = load(store, capture(tmp_path, rows=[row]))
    with fail_code("ZZSHARE_PRICE_INVALID") as error:
        view.get_daily(strict=strict)
    assert error.value.details["capture_id"] == view.descriptor()["capture_id"]


@pytest.mark.parametrize("missing", ["open", "high", "low", "close"])
def test_consistent_partial_ohlc_retains_unknown(store, tmp_path, missing):
    row = source_row()
    del row[missing]
    view = load(store, capture(tmp_path, rows=[row]))
    result = view.get_daily(strict=False)
    assert result.report["unknown_fields"] == [
        {"date": "20260928", "field": missing, "state": "field_missing"}
    ]
    with fail_code("ZZSHARE_FIELDS_UNKNOWN"):
        view.get_daily()


@pytest.mark.parametrize("variant", ["single", "conflict_first", "conflict_last", "same", "case"])
def test_wire_response_preserves_lengths_before_validation(store, monkeypatch, variant):
    raw = body()
    size = str(len(raw))
    lengths = {
        "single": [("Content-Length", size)],
        "conflict_first": [("Content-Length", "99999"), ("Content-Length", size)],
        "conflict_last": [("Content-Length", size), ("Content-Length", "99999")],
        "same": [("Content-Length", size), ("Content-Length", size)],
        "case": [("Content-Length", "99999"), ("content-length", size)],
    }[variant]
    wire = (
        "HTTP/1.1 200 OK\r\n"
        + "".join(f"{key}: {value}\r\n" for key, value in lengths)
        + "Content-Type: application/json\r\nSet-Cookie: omitted\r\nConnection: close\r\n\r\n"
    ).encode() + raw

    class WireSocket:
        def makefile(self, *args, **kwargs):
            return io.BytesIO(wire)

    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            response = http.client.HTTPResponse(WireSocket())
            response.begin()
            return response

    monkeypatch.setattr(api.urllib.request, "build_opener", lambda *args: Opener())

    def fetch():
        return ZzshareSource(enabled=True).fetch(
            store, security="600000.XSHG", start_date="2026-09-28", end_date="2026-09-28"
        )

    if variant == "single":
        sid = fetch()
    else:
        with fail_code("ZZSHARE_LENGTH_MISMATCH") as error:
            fetch()
        sid = error.value.details["capture_id"]
    view = Store(store.root).zzshare(sid, enable_research=True)
    receipt = view.lineage()["receipt"]
    assert receipt["response_header_pairs"][: len(lengths)] == [list(pair) for pair in lengths]
    assert receipt["response_headers"] == dict(receipt["response_header_pairs"])
    assert all(pair[0].lower() != "set-cookie" for pair in receipt["response_header_pairs"])
    assert view.quality()["response_header_evidence"] == "ordered_pairs_recorded_unattested"
    assert len(calls) == 1
    if variant != "single":
        with fail_code("ZZSHARE_LENGTH_MISMATCH"):
            view.get_daily(strict=False)


def test_legacy_headers_visibility_is_unknown(store, tmp_path):
    view = load(store, capture(tmp_path))
    assert (
        view.quality()["response_header_evidence"] == "legacy_mapping_duplicate_visibility_unknown"
    )
    assert view.get_daily().report["projection_version"] == "zzshare-projection-2"
    assert "response_header_pairs" not in view.lineage()["receipt"]


@pytest.mark.parametrize(
    "pairs", [None, {"Content-Length": "10"}, [["Content-Length"]], [[1, "10"]], [["x", "y"]]]
)
def test_invalid_ordered_header_record_rejected(store, tmp_path, pairs):
    view = load(store, capture(tmp_path, changes={"response_header_pairs": pairs}))
    with fail_code("ZZSHARE_RECEIPT"):
        view.get_daily(strict=False)


@pytest.mark.parametrize("bad", ["nested", "numeric"])
@pytest.mark.parametrize("strict", [True, False])
def test_unknown_source_field_boundaries_are_structured(store, tmp_path, capsys, bad, strict):
    token = b"[" * 700 + b"0" + b"]" * 700 if bad == "nested" else b"1e9999999999999999999"
    raw = body().replace(b'"factor":1.01', b'"factor":' + token)
    view = load(store, capture(tmp_path, raw=raw))
    sid = view.descriptor()["capture_id"]
    with localcontext() as context:
        context.traps[InvalidOperation] = False
        with fail_code("ZZSHARE_SCHEMA") as error:
            view.get_daily(strict=strict)
    assert error.value.details["capture_id"] == sid
    assert view.coverage()["failure"]["code"] == "ZZSHARE_SCHEMA"
    assert view.lineage()["raw_response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (
        main(
            [
                "--store",
                str(store.root),
                "zzshare-query",
                "daily",
                "--capture",
                sid,
                "--enable-research",
                "--allow-partial",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "ZZSHARE_SCHEMA"


def test_bounded_unknown_source_fields_keep_precision(store, tmp_path):
    token = b"[" * 50 + b"1.000000000000000000000000000000001" + b"]" * 50
    raw = body().replace(b'"factor":1.01', b'"factor":' + token)
    row = load(store, capture(tmp_path, raw=raw)).get_daily().rows[0]
    assert token.decode() in row.raw_json
    value = row.source_fields["factor"]
    for _ in range(50):
        value = value[0]
    assert value == Decimal("1.000000000000000000000000000000001")
