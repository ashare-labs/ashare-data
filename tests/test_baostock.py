"""Synthetic protocol controls; never evidence of real source coverage."""

import json
from pathlib import Path
from types import SimpleNamespace
import zlib

import pytest

from ashare_data import Store, DataError, BaoStockSource
from ashare_data import baostock as api
from ashare_data._bao_receipt.capture import capture_query, save_capture, sh
from ashare_data._bao_receipt.protocol import decode_response

SEP = "\x01"
END = b"<![CDATA[]]>\n"
BASIC = ["sh.600000", "TEST ONLY 合成", "2000-01-01", "", "1", "1"]
BASIC_FIELDS = ["code", "code_name", "ipoDate", "outDate", "type", "status"]


def frame(body, kind, compressed=False):
    raw = body.encode()
    if compressed:
        data = zlib.compress(raw)
        return (
            ("00.9.10" + SEP + kind + SEP + str(len(data)).zfill(10)).encode()
            + data
            + b"\x01123\n"
            + END
        )
    core = ("00.9.10" + SEP + kind + SEP + str(len(body)).zfill(10) + body).encode()
    return core + SEP.encode() + str(zlib.crc32(core)).encode() + END


def wire(req, rows, *, error="0", override=None):
    p = req["params"]
    method = req["method"]
    if method == "query_stock_basic":
        kind = "46"
        names = BASIC_FIELDS
        tail = [p["code"], "", ",".join(names)]
    elif method == "query_trade_dates":
        kind = "34"
        names = ["calendar_date", "is_trading_day"]
        tail = [p["start_date"], p["end_date"], ",".join(names)]
    else:
        kind = "96"
        names = api.FIELDS.split(",")
        tail = [
            p[k] for k in ["code", "fields", "start_date", "end_date", "frequency", "adjustflag"]
        ]
    common = [
        error,
        "test-only",
        method,
        "anonymous",
        "1",
        "2000",
        json.dumps({"record": rows}, ensure_ascii=False, separators=(",", ":")),
    ]
    if override:
        tail[override[0]] = override[1]
    return frame(SEP.join(common + tail), kind, kind == "96"), names


def bundle(
    tmp_path,
    kind="basic",
    *,
    rows=None,
    error="0",
    mutation=None,
    chunks=None,
    sdk_rows=None,
    clock=None,
    max_rows=128,
):
    req = api.request(
        kind,
        None if kind == "calendar" else "600000.XSHG",
        None if kind == "basic" else "2020-01-02",
        None if kind == "basic" else "2020-01-02",
    )
    if rows is None:
        rows = (
            [BASIC]
            if kind == "basic"
            else [["2020-01-02", "1"]]
            if kind == "calendar"
            else [["2020-01-02", "sh.600000", "10", "11", "9", "10", "100", "1000", "3", "1"]]
        )
    raw, names = wire(req, rows, error=error)
    if mutation:
        raw = mutation(raw)
    actions = iter(chunks if chunks is not None else [raw])

    class Socket:
        def send(self, value):
            return len(value)

        def recv(self, n):
            a = next(actions, b"")
            if isinstance(a, Exception):
                raise a
            return a

    ctx = SimpleNamespace(default_socket=Socket())

    def query(**params):
        keys = {
            "basic": ["code", "code_name"],
            "calendar": ["start_date", "end_date"],
            "daily": ["code", "fields", "start_date", "end_date", "frequency", "adjustflag"],
        }[kind]
        body = SEP.join([req["method"], "anonymous", "1", "2000"] + [params[k] for k in keys])
        typ = {"basic": "45", "calendar": "33", "daily": "95"}[kind]
        core = ("00.9.40" + SEP + typ + SEP + str(len(body)).zfill(10) + body).encode()
        received = b""
        code = error
        try:
            ctx.default_socket.send(core + b"\x01" + str(zlib.crc32(core)).encode() + b"\n")
            while not received.endswith(END):
                received += ctx.default_socket.recv(8192)
        except Exception:
            code = "10002007"
        data = iter(rows if sdk_rows is None else sdk_rows)
        current = []

        def nxt():
            try:
                current[:] = next(data)
                return True
            except StopIteration:
                return False

        return SimpleNamespace(
            error_code=code, fields=names, next=nxt, get_row_data=lambda: current[:]
        )

    ticks = iter(["2026-01-01T00:00:01+00:00"] * 100)
    mono = iter([0, 1])
    result = capture_query(
        SimpleNamespace(**{req["method"]: query}),
        ctx,
        req,
        task_started_at="2026-01-01T00:00:00+00:00",
        clock=clock or (lambda: next(ticks)),
        monotonic=lambda: next(mono),
        max_rows=max_rows,
        max_pages=2,
        evidence_kind="offline_test_only",
    )
    pin = json.loads(Path(api.__file__).with_name("baostock-sdk.json").read_text())
    result["receipt"]["sdk_identity"] = pin
    result["receipt"].pop("receipt_id")
    rid = sh(result["receipt"])
    result["receipt"]["receipt_id"] = rid
    for r in result["research_rows"]:
        r["receipt_id"] = rid
    target = tmp_path / "capture"
    save_capture(target, result, clock=lambda: "2026-01-01T00:00:02+00:00")
    attempt = {
        "request": req,
        "status": "captured",
        "sdk_identity": pin,
        "endpoint": "public-api.baostock.com:10030",
        "account": "anonymous",
        "evidence_kind": "offline_test_only",
    }
    blobs = {str(p.relative_to(tmp_path)): p.read_bytes() for p in target.iterdir()}
    blobs["attempt.json"] = json.dumps(attempt).encode()
    return blobs, result


@pytest.mark.parametrize(
    "kind,method",
    [("basic", "get_security_info"), ("daily", "get_price"), ("calendar", "get_trade_days")],
)
def test_publish_reopen_offline_units(tmp_path, monkeypatch, kind, method):
    blobs, _ = bundle(tmp_path, kind)
    s = Store.init(tmp_path / "store")
    sid = api._publish(s, blobs)
    assert api._publish(s, blobs) == sid
    monkeypatch.setattr("socket.socket", lambda *a, **k: pytest.fail("unexpected network"))
    v = Store(s.root).baostock(sid)
    r = getattr(v, method)()
    assert len(r.data) == 1 and r.report["network_used"] is False
    assert r.report["quality"]["synthetic"] is True
    assert v.coverage()["complete_history"] is False
    if kind == "daily":
        assert r.data.iloc[0]["volume"] == 100
        assert str(r.data.iloc[0]["amount"]) == "1000"
        assert r.data.iloc[0]["source_row"]["volume"] == "100"
        assert v.coverage()["query_complete"] is False
    with pytest.raises(DataError):
        v.at("2026-01-02T00:00:00+00:00")
    with pytest.raises(DataError):
        v.at("2026-01-02T00:00:00+00:00", visibility="verified")


@pytest.mark.parametrize("kind", ["basic", "calendar", "daily"])
def test_mutation_isolation(tmp_path, kind):
    blobs, _ = bundle(tmp_path, kind)
    s = Store.init(tmp_path / "s")
    v = s.baostock(api._publish(s, blobs))
    before = v.lineage()
    d = v.descriptor()
    d["request"]["params"]["code"] = "edited"
    v.lineage()["attempt"]["status"] = "edited"
    r = getattr(
        v, {"basic": "get_security_info", "daily": "get_price", "calendar": "get_trade_days"}[kind]
    )()
    r.data.iloc[0, 0] = "edited"
    r.report["quality"]["units"]["volume"] = "hands"
    assert v.lineage() == before and v.quality()["units"]["volume"] == "share"


@pytest.mark.parametrize(
    "target",
    [
        "attempt.json",
        "capture/receipt.json",
        "capture/sdk-rows.json",
        "capture/research-rows.json",
        "capture/storage-record.json",
        "capture/page-01.request.bin",
        "capture/page-01.response.bin",
    ],
)
def test_disk_tamper_rejected(tmp_path, target):
    blobs, _ = bundle(tmp_path)
    s = Store.init(tmp_path / "s")
    sid = api._publish(s, blobs)
    p = s.root / "baostock-objects" / api.bh(blobs[target])
    p.chmod(0o600)
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(DataError):
        s.baostock(sid)


@pytest.mark.parametrize(
    "failure",
    [
        "eof",
        "timeout",
        "crc",
        "identity",
        "sdk_mismatch",
        "provider",
        "extra_frame",
        "truncated",
        "bad_length",
    ],
)
def test_failure_not_success(tmp_path, failure):
    kw = {}
    if failure == "eof":
        kw["chunks"] = [b"partial", b""]
    if failure == "timeout":
        kw["chunks"] = [TimeoutError("test")]
    if failure == "crc":
        kw["mutation"] = lambda b: b.replace(b"2000-01-01", b"2000-01-02")
    if failure == "identity":
        kw["rows"] = [["sz.000001"] + BASIC[1:]]
    if failure == "sdk_mismatch":
        kw["sdk_rows"] = [["sh.600000", "wrong"] + BASIC[2:]]
    if failure == "provider":
        kw["error"] = "10001011"
    if failure == "extra_frame":
        kw["mutation"] = lambda b: b + b
    if failure == "truncated":
        kw["mutation"] = lambda b: b[:-2]
    if failure == "bad_length":
        kw["mutation"] = lambda b: b[:11] + b"0000000001" + b[21:]
    blobs, _ = bundle(tmp_path, **kw)
    s = Store.init(tmp_path / "s")
    v = s.baostock(api._publish(s, blobs))
    assert v.descriptor()["status"] == "source_response_unusable"
    with pytest.raises(DataError):
        v.get_security_info()


@pytest.mark.parametrize("kind", ["basic", "calendar", "daily"])
def test_empty_unknown(tmp_path, kind):
    blobs, _ = bundle(tmp_path, kind, rows=[])
    s = Store.init(tmp_path / "s")
    v = s.baostock(api._publish(s, blobs))
    assert v.descriptor()["status"] == "empty_unknown"
    assert v.coverage()["confirmed_no_events"] is False


@pytest.mark.parametrize(
    "alter",
    [
        "tail",
        "trailing",
        "zlib",
        "duplicate",
        "row_code",
        "negative_volume",
        "amount_nan",
        "ohlc",
        "fractional_volume",
        "date",
        "adjustflag",
    ],
)
def test_daily_schema_integrity(tmp_path, alter):
    rows = [["2020-01-02", "sh.600000", "10", "11", "9", "10", "100", "1000", "3", "1"]]
    kw = {}
    if alter == "tail":
        kw["mutation"] = lambda b: b.replace(b"\x01123\n", b"\x01oops\n")
    elif alter == "trailing":
        kw["mutation"] = lambda b: b[: -len(END)] + b"extra" + END
    elif alter == "zlib":
        kw["mutation"] = lambda b: b[:24] + b"junk" + b[28:]
    elif alter == "duplicate":
        rows *= 2
    else:
        ix, val = {
            "row_code": (1, "sz.000001"),
            "negative_volume": (6, "-1"),
            "amount_nan": (7, "NaN"),
            "ohlc": (3, "1"),
            "fractional_volume": (6, "1.5"),
            "date": (0, "2020-01-03"),
            "adjustflag": (8, "2"),
        }[alter]
        rows[0][ix] = val
    blobs, _ = bundle(tmp_path, "daily", rows=rows, **kw)
    try:
        values = api._verify(blobs)
        assert not values[3] and values[-1] in {"source_response_unusable", "source_schema_invalid"}
    except DataError:
        pass


@pytest.mark.parametrize("phase", ["manifest", "catalog"])
def test_publish_recovery(tmp_path, monkeypatch, phase):
    blobs, _ = bundle(tmp_path)
    s = Store.init(tmp_path / "s")
    original = api.immutable_write

    def fail(path, body):
        if path.parent.name == "baostock-manifests":
            raise OSError("test interruption")
        return original(path, body)

    monkeypatch.setattr(api, "immutable_write", fail)
    with pytest.raises(OSError):
        api._publish(s, blobs)
    assert s.baostock_snapshots()[0]["status"] == "prepared"
    sid = s.baostock_snapshots()[0]["capture_id"]
    with pytest.raises(DataError):
        s.baostock(sid)
    monkeypatch.setattr(api, "immutable_write", original)
    if phase == "catalog":
        h = api.bh(blobs["attempt.json"])
        p = s.root / "baostock-objects" / h
        p.chmod(0o600)
        p.write_bytes(b"bad")
    result = s.recover_baostock()
    assert result[0]["status"] == ("published" if phase == "manifest" else "aborted")


@pytest.mark.parametrize(
    "args",
    [
        ("1m", "600000.XSHG", "2020-01-01", "2020-01-02"),
        ("daily", None, "2020-01-01", "2020-01-02"),
        ("daily", "600000.XSHG", None, None),
        ("daily", "600000.XSHG", "2020-01-01", "2020-02-01"),
        ("daily", "600000.XSHG", "2020-01-02", "2020-01-01"),
        ("basic", "600000.XSHG", "2020-01-01", None),
        ("calendar", "600000.XSHG", "2020-01-01", "2020-01-02"),
        ("basic", "*", None, None),
    ],
)
def test_scope_rejected_before_io(args):
    with pytest.raises(DataError):
        api.request(*args)


@pytest.mark.parametrize("value", [0, -1, 31, float("nan"), float("inf"), True, "15"])
def test_invalid_timeout(value):
    with pytest.raises(DataError):
        BaoStockSource(sdk_path="/missing", timeout=value)


def test_missing_sdk():
    with pytest.raises(DataError):
        BaoStockSource(sdk_path="/does-not-exist")


def test_compressed_final_numeric_field_is_not_crc(tmp_path):
    b, _ = bundle(tmp_path, "daily")
    d = decode_response(b["capture/page-01.response.bin"])
    assert (
        d["echo"]["adjustflag"] == "3" and d["integrity"] == "compressed_outer_integrity_unverified"
    )


def test_changed_row_after_rehash_rejected(tmp_path):
    blobs, _ = bundle(tmp_path)
    r = json.loads(blobs["capture/receipt.json"])
    r["pages"][0]["rows"][0]["code_name"] = "changed"
    r.pop("receipt_id")
    r["receipt_id"] = sh(r)
    blobs["capture/receipt.json"] = json.dumps(r).encode()
    with pytest.raises(DataError):
        api._verify(blobs)


def test_clock_failure_still_research_not_received(tmp_path):
    blobs, result = bundle(tmp_path, clock=lambda: None)
    assert result["receipt"]["query_complete"] is False
    s = Store.init(tmp_path / "s")
    v = s.baostock(api._publish(s, blobs))
    assert len(v.get_security_info().data) == 1
    with pytest.raises(DataError):
        v.at("2026-01-02T00:00:00+00:00")


def reseal(blobs, mutate_receipt=None, mutate_storage=None):
    r = json.loads(blobs["capture/receipt.json"])
    if mutate_receipt:
        mutate_receipt(r)
    r.pop("receipt_id")
    r["receipt_id"] = sh(r)
    blobs["capture/receipt.json"] = json.dumps(r).encode()
    rows = json.loads(blobs["capture/research-rows.json"])
    for row in rows:
        row["receipt_id"] = r["receipt_id"]
    blobs["capture/research-rows.json"] = json.dumps(rows).encode()
    s = json.loads(blobs["capture/storage-record.json"])
    s["receipt_id"] = r["receipt_id"]
    for a in s["artifacts"]:
        value = blobs["capture/" + a["path"]]
        a.update(sha256=api.bh(value), bytes=len(value))
    if mutate_storage:
        mutate_storage(s)
    s.pop("storage_id")
    s["storage_id"] = sh(s)
    blobs["capture/storage-record.json"] = json.dumps(s).encode()


@pytest.mark.parametrize(
    "field,value",
    [
        ("query_complete", False),
        ("clock_order_state", "unknown"),
        ("clock_order_valid", False),
        ("request_started_at", None),
        ("operation_completed_at", None),
        ("validation_completed_at", None),
        ("task_started_at", None),
        ("sdk_rows_sha256", "0" * 64),
        ("row_limit", 129),
        ("sdk_identity", {}),
        ("schema_version", 5),
        ("evidence_kind", "live_local_capture"),
    ],
)
def test_receipt_claims_recomputed(tmp_path, field, value):
    blobs, _ = bundle(tmp_path)
    reseal(blobs, mutate_receipt=lambda r: r.update({field: value}))
    with pytest.raises(DataError):
        api._verify(blobs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("storage_eligible", False),
        ("stored_visibility_at", None),
        ("staging_recorded_at", "2025-01-01T00:00:00+00:00"),
        ("storage_clock_evidence", {}),
        ("artifacts", []),
    ],
)
def test_storage_claims_recomputed(tmp_path, field, value):
    blobs, _ = bundle(tmp_path)
    reseal(blobs, mutate_storage=lambda s: s.update({field: value}))
    with pytest.raises(DataError):
        api._verify(blobs)


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "missing_result"])
def test_worker_failure_preserved_without_network(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(
        api,
        "sdk_identity",
        lambda root: json.loads(Path(api.__file__).with_name("baostock-sdk.json").read_text()),
    )
    source = BaoStockSource(sdk_path=tmp_path)

    def run(*args, **kwargs):
        if failure == "timeout":
            raise api.subprocess.TimeoutExpired("worker", 1)
        return SimpleNamespace(returncode=1 if failure == "nonzero" else 0)

    monkeypatch.setattr(api.subprocess, "run", run)
    s = Store.init(tmp_path / "store")
    sid = source.fetch(s, kind="basic", security="600000.XSHG")
    v = s.baostock(sid)
    assert v.descriptor()["status"] == "source_request_failed"
    with pytest.raises(DataError) as caught:
        v.get_security_info()
    assert caught.value.details["capture_id"] == sid


def test_machine_lock_prevents_second_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "sdk_identity", lambda root: {})
    source = BaoStockSource(sdk_path=tmp_path)
    lock = Path(api.tempfile.gettempdir()) / f"ashare-baostock-{api.os.getuid()}.lock"
    with lock.open("a+b") as fd:
        api.fcntl.flock(fd, api.fcntl.LOCK_EX | api.fcntl.LOCK_NB)
        with pytest.raises(DataError) as caught:
            source.fetch(Store.init(tmp_path / "s"), kind="basic", security="600000.XSHG")
        assert caught.value.code == "SOURCE_BUSY"


def test_source_bad_ohlc_stored_as_failed_evidence(tmp_path):
    blobs, _ = bundle(
        tmp_path,
        "daily",
        rows=[["2020-01-02", "sh.600000", "10", "1", "9", "10", "100", "1000", "3", "1"]],
    )
    store = Store.init(tmp_path / "store")
    sid = api._publish(store, blobs)
    view = store.baostock(sid)
    assert view.descriptor()["status"] == "source_schema_invalid"
    assert len(view.lineage()["receipt"]["pages"][0]["rows"]) == 1
    with pytest.raises(DataError) as caught:
        view.get_price()
    assert caught.value.code == "SOURCE_REQUEST_FAILED"


@pytest.mark.parametrize("fault", ["eof", "timeout", "bytes", "short_send", "vip"])
def test_sdk_login_socket_bounded(fault):
    from ashare_data.baostock_worker import BoundedSocket
    import time

    fake = SimpleNamespace(
        settimeout=lambda n: None,
        recv=lambda n: b"" if fault == "eof" else b"x",
        send=lambda b: len(b) - 1 if fault == "short_send" else len(b),
        connect=lambda a: None,
    )
    sock = BoundedSocket(fake, time.monotonic() + (10 if fault != "timeout" else -1))
    if fault == "bytes":
        sock.received = 4 * 1024 * 1024
    with pytest.raises((ConnectionError, TimeoutError, ValueError)):
        if fault == "short_send":
            sock.send(b"example")
        elif fault == "vip":
            sock.connect(("vip-api.baostock.com", 10030))
        else:
            sock.recv(8192)


@pytest.mark.parametrize("kind", ["daily", "basic", "calendar"])
def test_acquisition_report_distinct_from_offline_view(tmp_path, monkeypatch, kind):
    blobs, _ = bundle(tmp_path, kind)
    store = Store.init(tmp_path / "s")
    sid = api._publish(store, blobs)
    monkeypatch.setattr(api, "sdk_identity", lambda p: {})
    source = BaoStockSource(sdk_path=tmp_path)
    monkeypatch.setattr(source, "fetch", lambda *a, **kw: sid)
    if kind == "daily":
        result = source.get_price(
            "600000.XSHG", store=store, start_date="2020-01-02", end_date="2020-01-02"
        )
    elif kind == "basic":
        result = source.get_security_info("600000.XSHG", store=store)
    else:
        result = source.get_trade_days(store=store, start_date="2020-01-02", end_date="2020-01-02")
    assert result.report["network_used"] is True
    assert result.report["acquisition"] == "explicit_baostock_query"
    view = store.baostock(sid)
    assert (
        getattr(
            view,
            {"daily": "get_price", "basic": "get_security_info", "calendar": "get_trade_days"}[
                kind
            ],
        )().report["network_used"]
        is False
    )
