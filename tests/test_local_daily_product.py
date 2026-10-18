"""Synthetic local-library fixtures; never evidence of real market coverage."""

from datetime import date, timedelta
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from ashare_data import DataError, Store
from ashare_data.core.model import canonical, digest
from ashare_data.product import LocalDailyProduct
from ashare_data.product.configuration import resolve
from ashare_data.storage.local_daily import BAR_FIELDS, LocalDailyStore

IDS = ["000858.XSHE", "600276.XSHG", "600519.XSHG"]


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "main"
    store = LocalDailyStore(root)
    days = [
        (date(2024, 1, 1) + timedelta(days=i)).isoformat()
        for i in range(200)
        if (date(2024, 1, 1) + timedelta(days=i)).weekday() < 5
    ][:140]
    receipts = [
        {
            "url": "https://fixture.invalid/synthetic",
            "sha256": "a" * 64,
            "received_at": "2026-01-01T00:00:00+00:00",
            "status": 200,
        }
    ]
    rid = store.receipts(receipts)[0]
    records = [
        {
            "symbol": ("sh" if s.endswith("XSHG") else "sz") + s[:6],
            "name": "测试" + s[:6],
            "ipo": "2000-01-01",
            "out": None,
            "board": "main",
            "source": "synthetic_test",
            "receipt": "a" * 64,
        }
        for s in IDS
    ]
    store.save_universe(records, receipts, days[0], days[-1])
    store.save_calendar(days, receipts)
    for native in (r["symbol"] for r in records):
        for session in days:
            for source in ("baostock", "sina"):
                row = {
                    "date": session,
                    "open": "10.00",
                    "high": "11.00",
                    "low": "9.00",
                    "close": "10.50",
                    "volume": "123456",
                    "amount": "1296288.00" if source == "baostock" else None,
                    "preclose": "10.40",
                    "tradestatus": "1" if source == "baostock" else None,
                    "is_st": None,
                }
                store.db.execute(
                    "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        native,
                        session,
                        source,
                        *(row[k] for k in BAR_FIELDS),
                        digest(row),
                        rid,
                        receipts[0]["received_at"],
                    ),
                )
    store.db.commit()
    store.close()
    return root, days


def update(library, sql, args=()):
    with sqlite3.connect(library[0] / "daily.sqlite3") as db:
        db.execute(sql, args)


def publish(library, target, **kwargs):
    return LocalDailyProduct(library[0]).publish(
        target, IDS, library[1][120], library[1][129], **kwargs
    )


def test_catalogue_search_pagination_coverage_and_read_only(library):
    root, days = library
    product = LocalDailyProduct(root)
    before = (root / "daily.sqlite3").stat().st_mtime_ns
    result = product.catalogue("600519.XSHG")
    assert result["total"] == 1
    item = result["securities"][0]
    assert item["security"] == "600519.XSHG" and item["first_bar"] == days[0]
    assert item["sources"][0] == {
        "source": "baostock",
        "rows": 140,
        "first": days[0],
        "last": days[-1],
    }
    assert product.catalogue("测试", limit=1, offset=1)["total"] == 3
    assert product.catalogue("%")["total"] == 0
    assert (root / "daily.sqlite3").stat().st_mtime_ns == before
    with product._read() as store:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.db.execute("DELETE FROM bars")
    json.dumps(result, allow_nan=False)


def test_prices_preserve_decimal_units_provenance_and_explicit_source(library):
    root, days = library
    product = LocalDailyProduct(root)
    answer = product.prices(IDS, days[120], days[124])
    assert len(answer["rows"]) == 15 and not answer["gaps"]
    assert {r["source"] for r in answer["rows"]} == {"baostock"}
    assert answer["rows"][0]["volume"] == "123456"
    assert answer["rows"][0]["close"] == "10.50"
    receipt = answer["receipt"]
    assert receipt["rows_sha256"] == digest(answer["rows"])
    assert receipt["responses"][0]["sha256"] == "a" * 64
    assert not receipt["network_used"] and receipt["asset"]["root"] == str(root)
    explicit = product.prices(IDS, days[120], days[124], source="sina")
    assert explicit["rows"][0]["amount"] is None
    assert explicit["rows"][0]["source"] == "sina"
    json.dumps(answer, allow_nan=False)


def test_missing_unknown_and_listing_boundaries_are_not_filled(library):
    root, days = library
    update(library, "DELETE FROM bars WHERE symbol='sh600519' AND date=?", (days[124],))
    answer = LocalDailyProduct(root).prices(["600519.XSHG"], days[123], days[125])
    assert len(answer["rows"]) == 2
    assert answer["gaps"][0]["code"] == "MISSING_DAILY"
    update(library, "UPDATE listing_facts SET out=? WHERE symbol='sh600519'", (days[125],))
    answer = LocalDailyProduct(root).prices(["600519.XSHG"], days[125], days[126])
    assert not answer["rows"]
    assert {r["code"] for r in answer["gaps"]} == {"OUTSIDE_LISTING"}


def test_published_input_is_complete_deterministic_portable_and_hash_checked(library, tmp_path):
    release = publish(library, tmp_path / "release")
    assert publish(library, tmp_path / "release") == release
    view = Store(tmp_path / "release").local_daily_product(release["dataset_id"])
    doc = view.read()
    assert len(doc["calendar"]) == 131 and len(doc["daily"]) == 390
    assert doc["calendar"][120] == library[1][120]
    assert doc["calendar"][-1] == library[1][130]
    assert doc["action_coverage"]["status"] == "unavailable" and doc["actions"] == []
    assert doc["price_limit_policy"] == "disabled_for_price_only_diagnostic"
    assert doc["state_mode"] == "retrospective_model"
    expected_history = {sid: ["10.50"] * 120 for sid in IDS}
    expected_hash = hashlib.sha256(
        json.dumps(expected_history, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert set(doc["owner_history_sha256"].values()) == {expected_hash}
    assert {r["operation"] for r in doc["owner_api_reads"]} == {
        "prices",
        "history",
        "successor_session",
        "source_selection",
    }
    assert release["manifest"]["selected_sources"] == dict.fromkeys(IDS, "baostock")
    assert (
        release["manifest"]["portfolio_input_sha256"]
        == hashlib.sha256(view.portfolio_input_path.read_bytes()).hexdigest()
    )
    restored = tmp_path / "restored"
    shutil.copytree(tmp_path / "release", restored)
    shutil.rmtree(library[0])
    assert Store(restored).local_daily_product(release["dataset_id"]).read() == doc
    copied = Store(restored).local_daily_product(release["dataset_id"]).portfolio_input_path
    copied.chmod(0o600)
    copied.write_bytes(copied.read_bytes() + b" ")
    with pytest.raises(DataError) as exc:
        Store(restored).local_daily_product(release["dataset_id"]).read()
    assert exc.value.code == "PRODUCT_INTEGRITY"


def test_auto_publication_never_changes_displayed_source_for_warmup(library, tmp_path):
    root, days = library
    # Display-window auto prefers BaoStock amount coverage; Sina alone has all
    # warmup observations. The publication must refuse, not silently switch.
    update(library, "DELETE FROM bars WHERE source='baostock' AND date=?", (days[1],))
    displayed = LocalDailyProduct(root).prices(IDS, days[120], days[129])
    assert {r["source"] for r in displayed["coverage"]} == {"baostock"}
    with pytest.raises(DataError) as exc:
        publish(library, tmp_path / "release")
    assert exc.value.code == "PRODUCT_DATA_GAPS"
    assert any(g["date"] == days[1] for g in exc.value.details["gaps"])
    assert not (tmp_path / "release").exists()
    explicit = publish(library, tmp_path / "release", source="sina")
    assert explicit["manifest"]["selected_sources"] == dict.fromkeys(IDS, "sina")
    doc = Store(tmp_path / "release").local_daily_product(explicit["dataset_id"]).read()
    assert all(
        r["state_origin"] == "retrospective_positive_observed_volume"
        for r in doc["daily"]
        if r["date"] >= days[120]
    )


@pytest.mark.parametrize(
    "change,code",
    [
        ("close=NULL", "DAILY_NUMBER_UNAVAILABLE"),
        ("close='NaN'", "DAILY_NUMBER_UNAVAILABLE"),
        ("high='8'", "INCONSISTENT_OHLC"),
        ("volume='0',tradestatus=NULL", "SUSPENSION_UNKNOWN"),
        ("tradestatus='0'", "SOURCE_STATE_CONFLICT"),
        ("response_id='missing'", "SOURCE_RECEIPT_MISSING"),
    ],
)
def test_publication_rejects_incomplete_or_conflicting_observations(
    library, tmp_path, change, code
):
    update(library, "UPDATE bars SET " + change + " WHERE date=?", (library[1][120],))
    with pytest.raises(DataError) as exc:
        publish(library, tmp_path / "rejected")
    assert exc.value.code == "PRODUCT_DATA_GAPS"
    assert any(g["code"] == code for g in exc.value.details["gaps"])
    assert not (tmp_path / "rejected").exists()


def test_publication_requires_calendar_receipts_and_successor(library, tmp_path):
    root, days = library
    product = LocalDailyProduct(root)
    for start, end, code in [
        (days[119], days[125], "PRODUCT_WARMUP_UNAVAILABLE"),
        (days[120], days[-1], "PRODUCT_WARMUP_UNAVAILABLE"),
        (days[120], "2099-01-01", "PRODUCT_SESSION_UNAVAILABLE"),
    ]:
        with pytest.raises(DataError) as exc:
            product.publish(tmp_path / "rejected", IDS, start, end)
        assert exc.value.code == code
    update(library, "UPDATE calendar SET receipt='missing' WHERE date=?", (days[130],))
    with pytest.raises(DataError) as exc:
        publish(library, tmp_path / "rejected")
    assert any(g["code"] == "CALENDAR_RECEIPT_MISSING" for g in exc.value.details["gaps"])
    assert not (tmp_path / "rejected").exists()


def test_no_output_inside_library_and_missing_configuration_never_initializes(tmp_path, library):
    with pytest.raises(DataError) as exc:
        LocalDailyProduct(tmp_path / "absent")
    assert exc.value.code == "PRODUCT_NOT_CONFIGURED"
    assert not (tmp_path / "absent").exists()
    with pytest.raises(DataError) as exc:
        publish(library, library[0] / "release")
    assert exc.value.code == "PRODUCT_OUTPUT_OVERLAP"
    assert not (library[0] / "release").exists()


def test_deployment_precedence_paths_and_offline_cli(library, tmp_path, monkeypatch):
    manifest = {
        "schema": "ashare-deployment.v1",
        "data_root": ".",
        "assets": {
            "main": {"kind": "local_daily", "path": "main"},
            "releases": {"kind": "store", "path": "releases"},
        },
        "profiles": {
            "product": {"kind": "product", "daily_asset": "main", "release_store_asset": "releases"}
        },
    }
    path = tmp_path / "deployment.json"
    path.write_bytes(canonical(manifest))
    monkeypatch.setenv("ASHARE_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("ASHARE_DEPLOYMENT", raising=False)
    assert resolve()["daily_root"] == library[0]
    assert resolve()["release_store_root"] == tmp_path / "releases"
    monkeypatch.setenv("ASHARE_DEPLOYMENT", str(tmp_path / "missing.json"))
    with pytest.raises(DataError):
        resolve()
    assert LocalDailyProduct.from_deployment(path).root == library[0]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ashare_data.product",
            "--deployment",
            str(path),
            "catalogue",
            "--query",
            "600519",
        ],
        cwd=tmp_path,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["securities"][0]["security"] == "600519.XSHG"
    assert not (tmp_path / "releases").exists()


def test_empty_calendar_unknown_security_and_explicit_unavailable_source(library):
    root, days = library
    product = LocalDailyProduct(root)
    assert (
        product.prices(IDS, "2099-01-01", "2099-01-02")["gaps"][0]["code"] == "CALENDAR_NO_SESSIONS"
    )
    answer = product.prices(["600999.XSHG"], days[120], days[121])
    assert not answer["rows"] and answer["gaps"][0]["code"] == "SECURITY_UNAVAILABLE"
    answer = product.prices(IDS, days[120], days[121], source="eastmoney")
    assert not answer["rows"] and len(answer["gaps"]) == 6
    assert {c["source"] for c in answer["coverage"]} == {"eastmoney"}
