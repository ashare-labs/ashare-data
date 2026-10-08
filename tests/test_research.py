"""Research persistence boundaries: receipts, clocks, corruption and recovery."""

import hashlib
import json
import subprocess
import sys
from decimal import Decimal

import pytest

from ashare_data import Client, DataError, Store
from ashare_data.model import canonical, digest
from ashare_data import research, transport

FIELDS = [
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "turn",
    "tradestatus",
    "pctChg",
    "isST",
]


def row(label="2020-01-02", code="sh.600000", close="12.3400"):
    return dict(
        zip(
            FIELDS,
            [
                label,
                code,
                "12.2700",
                "12.3600",
                "12.1200",
                close,
                "12.3200",
                "41051555",
                "503090483.0000",
                "3",
                "1",
                "1",
                ".1",
                "0",
            ],
        )
    )


def payload(rows=None):
    return {
        "method": "query_history_k_data_plus",
        "params": {
            "code": "sh.600000",
            "fields": ",".join(FIELDS),
            "start_date": "2020-01-01",
            "end_date": "2020-01-03",
            "frequency": "d",
            "adjustflag": "3",
        },
        "fields": FIELDS,
        "rows": rows or [row(), row("2020-01-03")],
        "error_code": "0",
        "observed_at": "2020-01-01T00:00:00+00:00",
        "response_completed_at": "2020-01-01T00:00:01+00:00",
    }


def write(tmp_path, data, name="raw.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def calendar(tmp_path):
    return write(
        tmp_path,
        {
            "method": "query_trade_dates",
            "params": {"start_date": "2020-01-01", "end_date": "2020-01-03"},
            "fields": ["calendar_date", "is_trading_day"],
            "error_code": "0",
            "rows": [
                {"calendar_date": "2020-01-01", "is_trading_day": "0"},
                {"calendar_date": "2020-01-02", "is_trading_day": "1"},
                {"calendar_date": "2020-01-03", "is_trading_day": "1"},
            ],
        },
        "calendar.json",
    )


@pytest.fixture
def imported(store, tmp_path):
    path = write(tmp_path, payload())
    return store, store.import_research([path], calendar_path=calendar(tmp_path)), path


def reject(code, fn):
    with pytest.raises(DataError) as error:
        fn()
    assert error.value.code == code


def test_public_reopen_decimal_descriptor_idempotency(imported, monkeypatch):
    store, sid, raw = imported
    monkeypatch.setattr(transport, "public_read", lambda *a: pytest.fail("network"))
    before = raw.read_bytes()
    assert store.import_research([raw], calendar_path=raw.parent / "calendar.json") == sid
    view = Store(store.root).research(sid)
    descriptor = view.descriptor()
    assert descriptor["dataset_id"] == descriptor["manifest_sha256"] == sid
    assert descriptor["identity_kind"] == "immutable_research_dataset"
    assert descriptor["reopen"]["network_required"] is False
    assert not descriptor["guarantees"]["historical_pit"]
    descriptor["scope"].clear()
    assert view.descriptor()["scope"]
    result = view.get_price(
        "600000.XSHG",
        start_date="2020-01-01",
        end_date="2020-01-03",
        fields=["close", "money", "volume"],
    )
    assert len(result.data) == 2
    assert result.data.iloc[0]["close"] == Decimal("12.3400")
    assert result.data.iloc[0]["money"] == Decimal("503090483.0000")
    assert result.report["provenance"][0]["available_at"] is None
    assert result.report["provenance"][0]["records"][0]["response_completed_at"] is None
    assert result.report["provenance"][0]["coverage_report"]["source_grid_complete"] is True
    assert result.to_dict()["rows"][0]["time"].isoformat() == "2020-01-02T00:00:00"
    assert raw.read_bytes() == before
    assert store.snapshots() == []
    assert len(store.research_snapshots()) == 1
    result.data.iloc[0, 0] = 999
    result.report["provenance"].clear()
    assert view.get_price("600000.XSHG", count=1).data.iloc[0]["close"] == Decimal("12.3400")


def test_separate_process_only_public_reopen(imported):
    store, sid, _ = imported
    program = """import socket, ssl
socket.socket=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network'))
from ashare_data import Store
from decimal import Decimal
v=Store(__import__('sys').argv[1]).research(__import__('sys').argv[2])
r=v.get_price('600000.XSHG',count=2)
assert len(r.data)==2 and r.data.iloc[0]['close']==Decimal('12.3400')
print(v.descriptor()['dataset_id'])
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(store.root), sid], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == sid


@pytest.mark.parametrize(
    "flag,code",
    [
        ("require_complete", "COVERAGE_UNKNOWN"),
        ("require_fresh", "FRESHNESS_UNKNOWN"),
        ("require_final", "BAR_NOT_FINAL"),
        ("require_tradable", "TRADING_STATUS_UNKNOWN"),
    ],
)
def test_strict_refusal(imported, flag, code):
    view = imported[0].research(imported[1])
    reject(code, lambda: view.get_price("600000.XSHG", count=1, **{flag: True}))


def test_clock_visibility_missing_and_count(imported):
    view = imported[0].research(imported[1])
    reject("PIT_UNAVAILABLE", lambda: view.at("2020-01-03T16:00:00+08:00"))
    reject(
        "RECEIPT_TIME_UNKNOWN", lambda: view.at("2020-01-03T16:00:00+08:00", visibility="received")
    )
    a = view.at("2020-01-03T14:55:00+08:00", visibility="assumed")
    b = view.at("2020-01-04T00:00:00+08:00", visibility="assumed")
    assert a.descriptor() == b.descriptor()
    assert len(a.get_price("600000.XSHG", start_date="2020-01-01").data) == 1
    assert len(b.get_price("600000.XSHG", count=2).data) == 2
    reject("COVERAGE_INCOMPLETE", lambda: a.get_price("600000.XSHG", count=2))
    assert a.coverage("600000.XSHG", "2020-01-01", "2020-01-03")["missing_labels"] == ["2020-01-03"]
    reject("SOURCE_NO_DATA", lambda: view.get_price("000001.XSHE", count=1))


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda p: p.update(error_code="10002007"), "SOURCE_INCOMPLETE_RESPONSE"),
        (lambda p: p["params"].update(code="sz.000001"), "SOURCE_IDENTITY_MISMATCH"),
        (lambda p: p["params"].update(adjustflag="2"), "UNSUPPORTED_SOURCE"),
        (lambda p: p["rows"][0].update(adjustflag="2"), "SOURCE_IDENTITY_MISMATCH"),
        (lambda p: p["rows"][0].update(date="2019-12-30"), "SOURCE_SCHEMA_ERROR"),
        (lambda p: p["rows"][0].update(volume="1.2"), "SOURCE_SCHEMA_ERROR"),
        (lambda p: p["rows"][0].update(close="NaN"), "SOURCE_SCHEMA_ERROR"),
        (lambda p: p["rows"][0].update(high="1"), "SOURCE_SCHEMA_ERROR"),
        (lambda p: p["rows"].reverse(), "SOURCE_SCHEMA_ERROR"),
        (lambda p: p["fields"].reverse(), "SOURCE_SCHEMA_ERROR"),
    ],
)
def test_bad_source_never_published(store, tmp_path, change, code):
    data = json.loads(json.dumps(payload()))
    change(data)
    reject(code, lambda: store.import_research([write(tmp_path, data)]))
    assert not store.research_snapshots()


def test_source_calendar_gap_and_missing_amount(store, tmp_path):
    data = payload([row()])
    data["rows"][0]["amount"] = ""
    sid = store.import_research([write(tmp_path, data)], calendar_path=calendar(tmp_path))
    view = store.research(sid)
    assert view.coverage("600000.XSHG", "2020-01-01", "2020-01-03")["missing_labels"] == [
        "2020-01-03"
    ]
    assert len(view.get_price("600000.XSHG", count=1).data) == 1
    reject("SOURCE_FIELD_MISSING", lambda: view.get_price("600000.XSHG", count=1, fields=["money"]))
    assert view.coverage("600000.XSHG", "2019-12-30", "2020-01-03")["source_grid_complete"] is None


def test_unknown_revision_order_and_old_immutable(imported, tmp_path):
    store, sid, path = imported
    changed = write(
        tmp_path, payload([row(close="12.35"), row("2020-01-03", close="12.35")]), "revision.json"
    )
    reject("VERSION_ORDER_UNKNOWN", lambda: store.import_research([path, changed]))
    newer = store.import_research([changed])
    assert newer != sid
    assert store.research(sid).get_price("600000.XSHG", count=1).data.iloc[0]["close"] == Decimal(
        "12.3400"
    )
    assert store.research(newer).get_price("600000.XSHG", count=1).data.iloc[0]["close"] == Decimal(
        "12.35"
    )


@pytest.mark.parametrize("target", ["object", "manifest", "catalog"])
def test_integrity_before_reopen(imported, target):
    store, sid, _ = imported
    if target == "catalog":
        with store._db(write=True) as db:
            db.execute("UPDATE research_snapshots SET body='{}' WHERE id=?", (sid,))
    else:
        path = store.root / "research-manifests" / (sid + ".json")
        if target == "object":
            path = (
                store.root
                / "research-objects"
                / store.research(sid).descriptor()["objects"][0]["sha256"]
            )
        path.chmod(0o644)
        path.write_bytes(b"{}")
    reject("INTEGRITY", lambda: store.research(sid))


@pytest.mark.parametrize("corrupt", [False, True])
def test_interruption_recovery(store, tmp_path, monkeypatch, corrupt):
    original = research.immutable_write

    def crash(path, body):
        if path.parent.name == "research-manifests":
            raise OSError("simulated process loss before manifest")
        original(path, body)

    with monkeypatch.context() as patch:
        patch.setattr(research, "immutable_write", crash)
        with pytest.raises(OSError):
            store.import_research([write(tmp_path, payload())])
    entry = store.research_snapshots()[0]
    assert entry["status"] == "prepared"
    reject("RESEARCH_NOT_PUBLISHED", lambda: store.research(entry["dataset_id"]))
    if corrupt:
        path = next((store.root / "research-objects").iterdir())
        path.chmod(0o644)
        path.write_bytes(b"bad")
    report = store.recover_research()
    assert report[0]["status"] == ("aborted" if corrupt else "published")
    assert store.recover_research() == []
    if not corrupt:
        assert len(store.research(entry["dataset_id"]).get_price("600000.XSHG", count=2).data) == 2


def sina_rows(day="2020-01-02 09:31:00", close="10.1234567890123"):
    return [
        {
            "day": day,
            "open": "10",
            "high": "11",
            "low": "9",
            "close": close,
            "volume": "100",
            "amount": "1012.34567890123",
        }
    ]


def receipt(cache, rows, observed="2020-01-02T09:32:00+08:00", started=None, datalen=1023):
    body = canonical(rows)
    h = hashlib.sha256(body).hexdigest()
    entry = {
        "schema": 1,
        "url": research.KLINE_URL + f"?symbol=sh600000&scale=1&ma=no&datalen={datalen}",
        "http_status": 200,
        "sha256": h,
        "observed_at": observed,
        "request_started_at": started or observed,
    }
    for name in ["objects", "observations"]:
        (cache / name).mkdir(parents=True, exist_ok=True)
    (cache / "objects" / h).write_bytes(body)
    (cache / "observations" / (digest(entry) + ".json")).write_bytes(canonical(entry))
    return entry


def test_sina_save_pinned_reopen_no_private_cache(store, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    receipt(cache, sina_rows())
    client = Client(cache=cache, cache_mode="only").at(
        "2020-01-02T09:33:00+08:00", visibility="received"
    )
    sid = client.save_research(store)
    assert client.save_research(store) == sid
    import shutil

    shutil.rmtree(cache)
    monkeypatch.setattr(transport, "public_read", lambda *a: pytest.fail("network"))
    v = Store(store.root).research(sid).at("2020-01-02T09:33:00+08:00", visibility="received")
    r = v.get_price("600000.XSHG", count=1, frequency="1m", fields=["close", "amount"])
    assert r.data.iloc[0]["close"] == Decimal("10.1234567890123")
    assert r.data.iloc[0]["amount"] == Decimal("1012.34567890123")
    assert r.report["network_used"] is False
    assert len(v.lineage()["observations"]) == 1
    reject("CACHE_MISS", lambda: v.get_price("000001.XSHE", count=1, frequency="1m"))


def test_sina_forming_never_matures(store, tmp_path):
    cache = tmp_path / "cache"
    receipt(cache, sina_rows(), observed="2020-01-02T09:30:30+08:00")
    sid = Client(cache=cache, cache_mode="only").save_research(store)
    view = store.research(sid).at("2020-01-03T10:00:00+08:00", visibility="assumed")
    with pytest.raises(DataError):
        view.get_price("600000.XSHG", count=1, frequency="1m")


def test_sina_revision_same_dataset_different_clocks_and_same_time_conflict(store, tmp_path):
    cache = tmp_path / "cache"
    receipt(cache, sina_rows(close="10"))
    receipt(cache, sina_rows(close="10.2"), observed="2020-01-02T09:34:00+08:00")
    sid = Client(cache=cache, cache_mode="only").save_research(store)
    v = store.research(sid)
    for stamp, expected in [("09:33", "10"), ("09:35", "10.2")]:
        q = v.at("2020-01-02T" + stamp + ":00+08:00", visibility="received")
        assert q.descriptor()["dataset_id"] == sid
        assert q.get_price("600000.XSHG", count=1, frequency="1m").data.iloc[0]["close"] == Decimal(
            expected
        )
    receipt(cache, sina_rows(close="10.3"), observed="2020-01-02T09:34:00+08:00", datalen=500)
    other = Client(cache=cache, cache_mode="only").save_research(store)
    assert other != sid
    reject(
        "OBSERVATION_CONFLICT",
        lambda: store.research(other)
        .at("2020-01-02T09:35:00+08:00", visibility="received")
        .get_price("600000.XSHG", count=1, frequency="1m"),
    )
    assert v.get_price("600000.XSHG", count=1, frequency="1m").data.iloc[0]["close"] == Decimal(
        "10.2"
    )


def test_fetch_uses_real_transport_path_bounded(store, monkeypatch):
    calls = []

    def request(url, timeout):
        calls.append((url, timeout))
        return canonical([dict(r, day="2020-01-02") for r in sina_rows()])

    monkeypatch.setattr(transport, "public_read", request)
    sid = store.fetch_price("600000.XSHG", count=1, timeout=4)
    assert len(calls) == 1 and "scale=240" in calls[0][0] and calls[0][1] == 4
    monkeypatch.setattr(transport, "public_read", lambda *a: pytest.fail("network"))
    v = store.research(sid)
    assert len(v.get_price("600000.XSHG", count=1).data) == 1
    assert not list(store.root.glob(".research-fetch-*"))
    assert v.lineage()["observations"][0]["request_started_at"]


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"count": True}, "INVALID_COUNT"),
        ({"count": 0}, "INVALID_COUNT"),
        ({"fields": ["money", "amount"]}, "INVALID_REQUEST"),
        ({"fq": "pre"}, "UNSUPPORTED_ADJUSTMENT"),
        ({"frequency": "1m"}, "UNSUPPORTED_FREQUENCY"),
        ({"fields": ["status"]}, "UNSUPPORTED_FIELD"),
        ({"require_complete": 1}, "INVALID_REQUEST"),
        ({"count": 1, "start_date": "2020-01-01"}, "INVALID_REQUEST"),
    ],
)
def test_query_invalid(imported, kwargs, code):
    reject(code, lambda: imported[0].research(imported[1]).get_price("600000.XSHG", **kwargs))


def test_cli_public_surface(imported, capsys):
    from ashare_data.cli import main

    store, sid, _ = imported
    assert main(["--store", str(store.root), "research-describe", "--dataset", sid]) == 0
    assert json.loads(capsys.readouterr().out)["dataset_id"] == sid
    assert (
        main(
            [
                "--store",
                str(store.root),
                "research-price",
                "600000.XSHG",
                "--dataset",
                sid,
                "--count",
                "1",
                "--fields",
                "close",
                "money",
            ]
        )
        == 0
    )
    value = json.loads(capsys.readouterr().out)
    assert value["rows"][0]["money"] == "503090483.0000"
    assert (
        main(
            [
                "--store",
                str(store.root),
                "research-price",
                "600000.XSHG",
                "--dataset",
                sid,
                "--count",
                "1",
                "--require-final",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "BAR_NOT_FINAL"


def test_bounds_and_single_writer(store, tmp_path):
    reject("BOUNDED_IMPORT", lambda: store.import_research([]))
    reject("BOUNDED_IMPORT", lambda: store.import_research(["unused"] * 128))
    reject("UNSUPPORTED_SOURCE", lambda: store.import_research([], format="tdx"))
    with store._writer():
        reject("WRITER_BUSY", lambda: store.import_research([write(tmp_path, payload())]))
    assert store.research_snapshots() == []


def test_source_calendar_must_be_contiguous(store, tmp_path):
    path = calendar(tmp_path)
    value = json.loads(path.read_text())
    value["rows"].pop(1)
    path.write_text(json.dumps(value))
    reject(
        "SOURCE_SCHEMA_ERROR",
        lambda: store.import_research([write(tmp_path, payload())], calendar_path=path),
    )


def test_unknown_calendar_no_weekday_inference(store, tmp_path):
    sid = store.import_research([write(tmp_path, payload())])
    view = store.research(sid)
    report = view.coverage("600000.XSHG", "2020-01-01", "2020-01-03")
    assert report["source_grid_complete"] is None
    assert report["expected_labels"] is None
    reject("BOUNDED_QUERY", lambda: view.coverage("600000.XSHG", "1900-01-01", "2099-01-01"))


def test_prepared_after_manifest_and_missing_object_recovery(imported):
    store, sid, _ = imported
    with store._db(write=True) as db:
        db.execute("UPDATE research_snapshots SET status='prepared' WHERE id=?", (sid,))
    assert store.recover_research()[0]["status"] == "published"
    h = store.research(sid).descriptor()["objects"][0]["sha256"]
    (store.root / "research-objects" / h).unlink()
    with store._db(write=True) as db:
        db.execute("UPDATE research_snapshots SET status='prepared' WHERE id=?", (sid,))
    assert store.recover_research()[0]["status"] == "aborted"
    reject("RESEARCH_NOT_PUBLISHED", lambda: store.research(sid))


def test_reopen_incompatible_parser(imported):
    store, sid, _ = imported
    body = json.loads((store.root / "research-manifests" / (sid + ".json")).read_text())
    body["parser_version"] = "future"
    data = canonical(body)
    sid2 = hashlib.sha256(data).hexdigest()
    (store.root / "research-manifests" / (sid2 + ".json")).write_bytes(data)
    with store._db(write=True) as db:
        db.execute(
            "INSERT INTO research_snapshots VALUES (?,?,?,?)",
            (sid2, data.decode(), "published", "unknown"),
        )
    reject("RESEARCH_VERSION_UNSUPPORTED", lambda: store.research(sid2))


def test_multisecurity_all_or_nothing_and_no_hidden_money(imported):
    store, sid, _ = imported
    view = store.research(sid)
    reject("SOURCE_NO_DATA", lambda: view.get_price(["600000.XSHG", "000001.XSHE"], count=1))
    assert list(view.get_price("600000.XSHG", count=1).data.columns) == research.DEFAULT_FIELDS
    assert view.get_price("600000.XSHG", count=1, fields=["amount"]).data.iloc[0, 0] == Decimal(
        "503090483.0000"
    )


def test_fetch_failure_never_publishes(store, monkeypatch):
    def fail(*a):
        raise DataError("NETWORK_ERROR", "simulated")

    monkeypatch.setattr(transport, "public_read", fail)
    reject("NETWORK_ERROR", lambda: store.fetch_price("600000.XSHG", count=1))
    assert store.research_snapshots() == []
    assert not list(store.root.glob(".research-fetch-*"))


def test_save_freezes_before_cache_later_revision(store, tmp_path):
    cache = tmp_path / "cache"
    receipt(cache, sina_rows(close="10"))
    client = Client(cache=cache, cache_mode="only").at(
        "2020-01-02T09:35:00+08:00", visibility="received"
    )
    receipt(cache, sina_rows(close="10.3"), observed="2020-01-02T09:34:00+08:00")
    sid = client.save_research(store)
    result = store.research(sid).get_price("600000.XSHG", count=1, frequency="1m")
    assert result.data.iloc[0]["close"] == Decimal("10")
    assert (
        result.report["provenance"][0]["records"][0]["response_completed_at"]
        == "2020-01-02T09:32:00+08:00"
    )
    assert result.report["provenance"][0]["records"][0]["available_at"] is None


def test_missing_manifest_and_latest_alias(imported):
    store, sid, _ = imported
    reject("INVALID_ID", lambda: store.research("latest"))
    reject("RESEARCH_NOT_PUBLISHED", lambda: store.research("f" * 64))
    (store.root / "research-manifests" / (sid + ".json")).unlink()
    reject("BOUNDED_IMPORT", lambda: store.research(sid))


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(schema_version=True),
        lambda m: m.update(objects=[[]]),
        lambda m: m.update(objects=None),
        lambda m: m.update(source="unknown"),
    ],
)
def test_corrupt_manifest_structures_are_clear_errors(imported, change):
    store, sid, _ = imported
    body = json.loads((store.root / "research-manifests" / (sid + ".json")).read_text())
    change(body)
    data = canonical(body)
    sid2 = hashlib.sha256(data).hexdigest()
    (store.root / "research-manifests" / (sid2 + ".json")).write_bytes(data)
    with store._db(write=True) as db:
        db.execute(
            "INSERT INTO research_snapshots VALUES (?,?,?,?)",
            (sid2, data.decode(), "published", "unknown"),
        )
    with pytest.raises(DataError):
        store.research(sid2)
