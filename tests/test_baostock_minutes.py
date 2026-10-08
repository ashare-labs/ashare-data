"""Synthetic minute controls: separate from all real network success evidence."""

import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import zlib

import pytest

from ashare_data import BaoStockSource, DataError, Store
from ashare_data import baostock as api
from ashare_data import baostock_minutes as minute
from ashare_data._bao_receipt.capture import capture_query, save_capture, sh
from test_baostock import frame, SEP, END


def rows_for(frequency="5m", days=1):
    result = []
    for d in range(days):
        day = datetime(2020, 1, 2) + timedelta(days=d)
        for h, m in [(9, 30), (13, 0)]:
            start = day.replace(hour=h, minute=m)
            for step in range(int(frequency[:-1]), 121, int(frequency[:-1])):
                stamp = start + timedelta(minutes=step)
                result.append(
                    [
                        stamp.date().isoformat(),
                        stamp.strftime("%Y%m%d%H%M%S") + "000",
                        "sh.600000",
                        "10.00",
                        "11.00",
                        "9.00",
                        "10.00",
                        "100",
                        "1000.00",
                        "3",
                    ]
                )
    return result


def bundle(
    tmp_path, frequency="5m", *, days=1, rows=None, echo=None, sdk_rows=None, raw_mutation=None
):
    end = (datetime(2020, 1, 2) + timedelta(days=days - 1)).date().isoformat()
    req = api.request("minute", "600000.XSHG", "2020-01-02", end, frequency)
    names = minute.FIELDS.split(",")
    rows = rows_for(frequency, days) if rows is None else rows
    params = req["params"]
    keys = ["code", "fields", "start_date", "end_date", "frequency", "adjustflag"]
    fields = [params[k] for k in keys]
    if echo:
        fields[keys.index(echo[0])] = echo[1]
    body = SEP.join(
        [
            "0",
            "test-only",
            req["method"],
            "anonymous",
            "1",
            "2000",
            json.dumps({"record": rows}, separators=(",", ":")),
        ]
        + fields
    )
    wire = frame(body, "96", True)
    if raw_mutation:
        wire = raw_mutation(wire)
    actions = iter([wire])
    sock = SimpleNamespace(send=lambda b: len(b), recv=lambda n: next(actions, b""))
    ctx = SimpleNamespace(default_socket=sock)

    def query(**p):
        body = SEP.join([req["method"], "anonymous", "1", "2000"] + [p[k] for k in keys])
        core = ("00.9.40" + SEP + "95" + SEP + str(len(body)).zfill(10) + body).encode()
        code = "0"
        received = b""
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

    mono = iter([1, 2])
    r = capture_query(
        SimpleNamespace(query_history_k_data_plus=query),
        ctx,
        req,
        task_started_at="2026-01-01T00:00:00+00:00",
        clock=lambda: "2026-01-01T00:00:01+00:00",
        monotonic=lambda: next(mono),
        max_rows=128,
        max_pages=2,
        evidence_kind="offline_test_only",
    )
    pin = json.loads(Path(api.__file__).with_name("baostock-sdk.json").read_text())
    r["receipt"]["sdk_identity"] = pin
    r["receipt"].pop("receipt_id")
    r["receipt"]["receipt_id"] = sh(r["receipt"])
    for row in r["research_rows"]:
        row["receipt_id"] = r["receipt"]["receipt_id"]
    dest = tmp_path / "capture"
    save_capture(dest, r, clock=lambda: "2026-01-01T00:00:02+00:00")
    blobs = {"capture/" + p.name: p.read_bytes() for p in dest.iterdir()}
    blobs["attempt.json"] = json.dumps(
        {
            "request": req,
            "status": "captured",
            "sdk_identity": pin,
            "endpoint": "public-api.baostock.com:10030",
            "account": "anonymous",
            "evidence_kind": "offline_test_only",
        }
    ).encode()
    return blobs


@pytest.mark.parametrize("frequency,count", [("5m", 48), ("15m", 16), ("30m", 8), ("60m", 4)])
@pytest.mark.parametrize("days", [1, 2])
def test_end_to_end_labels_units_and_version(tmp_path, monkeypatch, frequency, count, days):
    blobs = bundle(tmp_path, frequency, days=days)
    s = Store.init(tmp_path / "s")
    sid = api._publish(s, blobs)
    monkeypatch.setattr("socket.socket", lambda *a, **kw: pytest.fail("network during reopen"))
    v = Store(s.root).baostock(sid)
    r = v.get_price()
    assert len(r.data) == count * days
    assert v.descriptor()["version"] == minute.VERSION and r.report["network_used"] is False
    assert r.data.index.name == "time" and r.data.index.tz is None and "time" not in r.data.columns
    assert r.data.iloc[0]["source_time"] == rows_for(frequency)[0][1]
    assert r.data.iloc[0]["source_row"]["time"] == rows_for(frequency)[0][1]
    assert r.data.iloc[0]["volume"] == 100 and str(r.data.iloc[0]["amount"]) == "1000.00"
    assert r.data.iloc[0]["security"] == "600000.XSHG"
    assert len(r.to_dict()["rows"]) == count * days
    coverage = v.coverage()
    assert not coverage["query_complete"] and not coverage["complete_history"]
    labels = coverage["minute_labels"]
    assert not labels["closed_bar_verified"] and labels["bar_start"] is None
    for day in labels["days"]:
        assert (
            day["end_label_hypothesis_matches"]
            and day["observed_at_1130"]
            and day["observed_at_1500"]
        )
        assert not day["inside_lunch_break"] and not day["missing_hypothesized_labels"]
    with pytest.raises(DataError):
        v.at("2026-10-01T00:00:00+00:00")


@pytest.mark.parametrize(
    "frequency", ["1m", "1", "5", 5, None, "minute", "daily", "01m", "90m", [], True]
)
def test_invalid_frequency_before_io(frequency):
    with pytest.raises(DataError):
        api.request("minute", "600000.XSHG", "2020-01-02", "2020-01-02", frequency)


@pytest.mark.parametrize(
    "start,end", [("2020-01-01", "2020-01-03"), ("2020-01-02", "2020-01-01"), (None, None)]
)
def test_bounded_dates(start, end):
    with pytest.raises(DataError):
        api.request("minute", "600000.XSHG", start, end, "5m")


@pytest.mark.parametrize(
    "key,value",
    [
        ("code", "sz.000001"),
        ("frequency", "15"),
        ("start_date", "2020-01-01"),
        ("end_date", "2020-01-03"),
        ("adjustflag", "2"),
        ("fields", "date,code,close"),
    ],
)
def test_wire_identity_disagreement(tmp_path, key, value):
    blobs = bundle(tmp_path, echo=(key, value))
    s = Store.init(tmp_path / "s")
    v = s.baostock(api._publish(s, blobs))
    assert v.descriptor()["status"] == "source_response_unusable"
    with pytest.raises(DataError):
        v.get_price()


@pytest.mark.parametrize(
    "position,value",
    [
        (0, "2020-01-03"),
        (1, "20200103093500000"),
        (1, "2020010209350000"),
        (1, "20200102253500000"),
        (1, "20200230093500000"),
        (1, "2020-01-02T09:35"),
        (1, "２０２００１０２０９３５０００００"),
        (2, "sz.000001"),
        (3, "0"),
        (4, "1"),
        (5, "20"),
        (6, "NaN"),
        (7, "-1"),
        (7, "1.5"),
        (8, "Infinity"),
        (8, "-2"),
        (9, "2"),
    ],
)
def test_bad_source_rows_keep_failure_evidence(tmp_path, position, value):
    rows = rows_for()
    rows[0][position] = value
    blobs = bundle(tmp_path, rows=rows)
    s = Store.init(tmp_path / "s")
    sid = api._publish(s, blobs)
    v = s.baostock(sid)
    assert v.descriptor()["status"] in {"source_schema_invalid", "source_response_unusable"}
    assert v.lineage()["receipt"] is not None
    with pytest.raises(DataError) as error:
        v.get_price()
    assert error.value.code == "SOURCE_REQUEST_FAILED"


@pytest.mark.parametrize(
    "mutation", ["duplicate", "reverse", "sdk_difference", "partial", "trailing"]
)
def test_incomplete_or_conflicting_not_readable(tmp_path, mutation):
    kw = {}
    rows = rows_for()
    if mutation == "duplicate":
        rows.insert(1, copy.deepcopy(rows[0]))
    if mutation == "reverse":
        rows.reverse()
    if mutation == "sdk_difference":
        kw["sdk_rows"] = rows[:-1]
    if mutation == "partial":
        kw["raw_mutation"] = lambda b: b[:-20]
    if mutation == "trailing":
        kw["raw_mutation"] = lambda b: b[: -len(END)] + b"junk" + END
    blobs = bundle(tmp_path, rows=rows, **kw)
    s = Store.init(tmp_path / "s")
    v = s.baostock(api._publish(s, blobs))
    with pytest.raises(DataError):
        v.get_price()


@pytest.mark.parametrize(
    "stamp", ["20200102093000000", "20200102120000000", "20200102153000000", "20200102113000001"]
)
def test_off_grid_is_diagnostic_not_fabricated(tmp_path, stamp):
    rows = [["2020-01-02", stamp, "sh.600000", "10", "11", "9", "10", "100", "1000", "3"]]
    blobs = bundle(tmp_path, rows=rows)
    s = Store.init(tmp_path / "s")
    v = s.baostock(api._publish(s, blobs))
    r = v.get_price()
    assert len(r.data) == 1 and r.data.iloc[0]["source_time"] == stamp
    day = v.coverage()["minute_labels"]["days"][0]
    assert not day["end_label_hypothesis_matches"] and day["outside_hypothesized_grid"]
    assert len(day["missing_hypothesized_labels"]) == 48


def test_missing_bar_and_empty_amount_remain_explicit(tmp_path):
    rows = rows_for()
    rows.pop(2)
    rows[0][8] = ""
    blobs = bundle(tmp_path, rows=rows)
    s = Store.init(tmp_path / "s")
    v = s.baostock(api._publish(s, blobs))
    result = v.get_price()
    assert len(result.data) == 47 and result.data.iloc[0]["amount"] is None
    day = v.coverage()["minute_labels"]["days"][0]
    assert day["missing_amount_rows"] == 1 and len(day["missing_hypothesized_labels"]) == 1
    assert "2020-01-02T09:45:00.000" in day["missing_hypothesized_labels"]


def test_empty_unknown_and_stable_columns(tmp_path):
    blobs = bundle(tmp_path, rows=[])
    s = Store.init(tmp_path / "s")
    v = s.baostock(api._publish(s, blobs))
    result = v.get_price()
    assert result.data.empty and "source_time" in result.data.columns
    assert v.coverage()["minute_labels"]["dates_without_rows"] == ["2020-01-02"]
    assert not v.coverage()["confirmed_no_events"]


def test_result_mutation_does_not_change_fixed_minute_version(tmp_path):
    blobs = bundle(tmp_path)
    s = Store.init(tmp_path / "s")
    sid = api._publish(s, blobs)
    v = s.baostock(sid)
    before = v.lineage()
    r = v.get_price()
    r.data.iloc[0]["source_row"]["time"] = "edited"
    r.report["coverage"]["minute_labels"]["days"][0]["missing_hypothesized_labels"].append("edited")
    v.coverage()["minute_labels"]["days"].clear()
    v.descriptor()["request"]["params"]["frequency"] = "1"
    assert v.lineage() == before and s.baostock(sid).lineage() == before


def test_minute_manifest_cannot_masquerade_as_daily_version(tmp_path):
    blobs = bundle(tmp_path)
    s = Store.init(tmp_path / "s")
    sid = api._publish(s, blobs)
    m = json.loads((s.root / "baostock-manifests" / (sid + ".json")).read_text())
    m["version"] = api.VERSION
    body = api.canonical(m)
    with pytest.raises(DataError):
        api._load(s, body, api.bh(body))


@pytest.mark.parametrize("frequency", ["5m", "15m", "30m", "60m"])
def test_public_get_price_selects_minute_request(tmp_path, monkeypatch, frequency):
    blobs = bundle(tmp_path, frequency)
    s = Store.init(tmp_path / "s")
    sid = api._publish(s, blobs)
    monkeypatch.setattr(api, "sdk_identity", lambda p: {})
    source = BaoStockSource(sdk_path=tmp_path)
    seen = []

    def fetch(store, **kwargs):
        seen.append(kwargs)
        return sid

    monkeypatch.setattr(source, "fetch", fetch)
    r = source.get_price(
        "600000.XSHG", store=s, start_date="2020-01-02", end_date="2020-01-02", frequency=frequency
    )
    assert (
        seen[0]["kind"] == "minute"
        and seen[0]["frequency"] == frequency
        and r.report["network_used"] is True
    )
