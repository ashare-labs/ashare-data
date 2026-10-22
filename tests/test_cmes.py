"""Synthetic vendor protocol fixtures; no real packages or deployment discovery."""

import csv
from decimal import Decimal
from io import StringIO
import json
import subprocess
import sys
from zipfile import ZipFile

import pytest

from ashare_data.core.model import DataError
from ashare_data.product.market_release import ReleasedDailyProduct, minute_prices
from ashare_data.providers.cmes import bar_rows, normalize_date, normalize_security
from ashare_data.storage.cmes_release import import_cmes
from ashare_data.storage.market_release import MarketRelease, sha_file

BAR_HEADER = ["股票代码", "时间", "开盘价", "最高价", "最低价", "收盘价", "昨收价", "成交量", "成交额"]
MINUTE_HEADER = ["时间", "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"]


def fixture(path, members):
    values = {}
    for name, rows in members.items():
        output = StringIO(newline="")
        csv.writer(output).writerows(rows)
        values[name] = output.getvalue().encode("utf-8-sig")
    if path.suffix == ".csv":
        path.write_bytes(next(iter(values.values())))
    else:
        with ZipFile(path, "w") as archive:
            for name, body in values.items():
                archive.writestr(name, body)
    return path


def daily(path, code="SH.600000", close="10", duplicate=False):
    row = [code, "2024-01-02", "10", "11", "9", close, "9.8", "1.25", "1.25"]
    return fixture(path, {code + ".csv": [BAR_HEADER, row, *([row] if duplicate else [])]})


def build(root, **kwargs):
    return import_cmes(root, free_reserve_bytes=0, **kwargs)


@pytest.mark.parametrize("original,expected", [
    ("SH.600000", "600000.XSHG"), ("SZ.000001", "000001.XSHE"), ("BJ.920000", "BJ.920000"),
    ("000001.XSHE", "000001.XSHE"),
])
def test_explicit_venue_mapping(original, expected):
    assert normalize_security(original) == expected
    assert normalize_date("20240102") == "2024-01-02"
    with pytest.raises(DataError):
        normalize_security("000001")


def test_shared_bar_parser_units_dates_and_unknowns(tmp_path):
    source = daily(tmp_path / "daily.zip")
    row = list(bar_rows(source, profile="daily-lots-kcny"))[0]
    assert row["volume_shares"] == 125
    assert row["turnover_cny"] == Decimal("1250")
    assert row["close_price"] == Decimal("10")
    minute = fixture(tmp_path / "minute.zip", {"SH.600000.csv": [MINUTE_HEADER,
        ["2024-01-02 09:31:00", "10", "10", "10", "10", "12", "120"]]})
    share = list(bar_rows(minute, profile="minute-shares-cny"))[0]
    lot = list(bar_rows(minute, profile="minute-lots-cny"))[0]
    assert share["volume_shares"] == 12 and lot["volume_shares"] == 1200
    assert share["turnover_cny"] == lot["turnover_cny"] == 120
    assert share["label_at"] == "2024-01-02T09:31:00+08:00"
    assert share["preclose"] is None
    with pytest.raises(DataError, match="profile"):
        build(tmp_path / "bad", minute=[minute])


def test_full_candidate_standard_api_sql_references_and_replay(tmp_path):
    sources = [daily(tmp_path / f"{code}.zip", code, duplicate=True)
               for code in ("SH.600000", "SZ.000001", "BJ.920000")]
    minute = fixture(tmp_path / "minute.zip", {"SH.600000.csv": [MINUTE_HEADER,
        ["2024-01-02 09:31:00", "10", "10", "10", "10", "125", "1250"]]})
    calendars = [fixture(tmp_path / f"{venue}.zip", {venue + ".csv": [
        ["交易所", "时间", "是否交易", "上一交易日"],
        [venue, "2024-01-02", "1", "20231229"], [venue, "2024-01-01", "0", "20231229"],
    ]}) for venue in ("SSE", "SZSE")]
    st = fixture(tmp_path / "st.zip", {"ST股票列表.csv": [
        ["股票代码", "股票名称", "时间", "类型", "类型名称"],
        ["SH.600000", "ST测试", "2024-01-02", "ST", "风险警示板"],
    ]})
    root = tmp_path / "candidate"
    args = dict(daily=sources, minute=[minute], minute_profile="minute-shares-cny", calendars=calendars, st=[st])
    originals = {path: sha_file(path) for path in [*sources, minute, *calendars, st]}
    result = build(root, **args)
    release = MarketRelease(root, 1, expected_manifest_sha256=result["manifest_sha256"])
    assert len(release.files) == 4
    assert sum(f["rows"] for f in release.files) == 4
    assert sum(v.get("equal_duplicate", 0) for v in result["input_dispositions"].values()) == 3
    for counts in result["input_dispositions"].values():
        assert counts["input_rows"] == sum(v for k, v in counts.items() if k != "input_rows")
    with release.metadata() as db:
        assert db.execute("SELECT count(*) FROM cmes_calendar").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM calendar").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM cmes_st").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM listing_facts").fetchone()[0] == 0
    config = {"market_release": {"root": str(root), "release_id": 1, "manifest_sha256": release.sha256}}
    product = ReleasedDailyProduct(config)
    prices = product.prices(list(release.identifiers), "2024-01-02", "2024-01-03", "cmes")
    assert len(prices["rows"]) == 3
    assert all(r["source"] == "cmes" and Decimal(r["volume"]) == 125 for r in prices["rows"])
    assert {r["security"]: r["is_st"] for r in prices["rows"]} == {
        "600000.XSHG": "1", "000001.XSHE": None, "BJ.920000": None,
    }
    assert product.catalogue()["total"] == 3
    assert prices["receipt"]["network_used"] is False
    assert len(minute_prices(config, "600000.XSHG", "2024-01-02T09:31:00+08:00",
                             "2024-01-02T09:31:00+08:00", source="cmes")["rows"]) == 1
    with release.sql(tables=("bars_1d",), securities=["600000.XSHG"]) as db:
        assert db.execute("SELECT close_price,volume_shares FROM market.bars_1d").fetchall() == [(Decimal(10), 125)]
    with pytest.raises(DataError, match="no fallback"):
        product.prices(["600000.XSHG"], "2024-01-02", "2024-01-03", "sina")
    assert product.prices(["600000.XSHG"], "2025-01-02", "2025-01-03")["rows"] == []
    before = sorted((p.name, sha_file(p)) for p in (root / "objects").iterdir())
    replay = build(root, **args)
    assert replay["unchanged"] and replay["manifest_sha256"] == result["manifest_sha256"]
    assert before == sorted((p.name, sha_file(p)) for p in (root / "objects").iterdir())
    assert originals == {p: sha_file(p) for p in originals}
    assert not list(root.rglob("*.zip")) and not list(root.rglob("*.csv"))


def test_conflicts_and_live_roots_rejected(tmp_path):
    first = daily(tmp_path / "first.zip")
    second = daily(tmp_path / "second.zip", close="10.1")
    root = tmp_path / "conflict"
    with pytest.raises(DataError, match="Conflicting bars"):
        build(root, daily=[first, second])
    assert not (root / "CURRENT.json").exists()
    root = tmp_path / "good"
    build(root, daily=[first])
    with pytest.raises(DataError, match="new isolated"):
        build(root, daily=[second])
    unknown = tmp_path / "live"
    unknown.mkdir()
    (unknown / "CURRENT.json").write_text("unrelated live pointer")
    with pytest.raises(DataError, match="new isolated"):
        build(unknown, daily=[first])
    assert (unknown / "CURRENT.json").read_text() == "unrelated live pointer"


def test_quarantine_is_visible_and_does_not_fill_prices(tmp_path):
    source = fixture(tmp_path / "mixed.csv", {"mixed.csv": [BAR_HEADER,
        ["SH.600000", "2024-01-02", "10", "10", "10", "10", "", "", ""],
        ["SH.600000", "2024-01-03", "0", "0", "0", "0", "", "1", "1"],
        ["SH.600000", "2024-01-04", "10", "10", "10", "10", "", "0.0001", "1"],
    ]})
    root = tmp_path / "candidate"
    receipt = build(root, daily=[source])
    release = MarketRelease(root)
    assert sum(x.get("quarantined", 0) for x in receipt["input_dispositions"].values()) == 2
    rows = release.read("bars_1d", "600000.XSHG", "2024-01-01", "2024-01-04")
    assert len(rows) == 1 and rows[0]["volume_shares"] is None and rows[0]["turnover_cny"] is None
    with release.metadata() as db:
        assert db.execute("SELECT count(*) FROM cmes_rejections").fetchone()[0] == 2


def test_selection_ignores_unrequested_member_and_cli_reads_release(tmp_path):
    source = fixture(tmp_path / "market.zip", {
        "SH.600000.csv": [BAR_HEADER, ["SH.600000", "2024-01-02", "10", "10", "10", "10", "", "1", "1"]],
        "SZ.000001.csv": [["unselected invalid header"]],
    })
    root = tmp_path / "candidate"
    build(root, daily=[source], securities=["600000.XSHG"])
    process = subprocess.run([sys.executable, "-m", "ashare_data.cli", "cmes", "query",
                              "--root", str(root), "--security", "SH.600000",
                              "--start", "2024-01-02", "--end", "2024-01-02"],
                             text=True, capture_output=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["rows"][0]["volume_shares"] == 100
    with pytest.raises(DataError, match="no source records"):
        build(tmp_path / "missing", daily=[source], securities=["BJ.920000"])


def test_replay_detects_changed_objects_and_original_changes(tmp_path):
    source = daily(tmp_path / "daily.zip")
    root = tmp_path / "candidate"
    build(root, daily=[source])
    release = MarketRelease(root)
    (root / release.files[0]["path"]).write_bytes(b"changed")
    with pytest.raises(DataError, match="Immutable object changed"):
        build(root, daily=[source])
    daily(source, close="10.1")
    with pytest.raises(DataError, match="new isolated"):
        build(root, daily=[source])


def test_interrupted_pointer_publication_resumes_identically(tmp_path, monkeypatch):
    import ashare_data.storage.market_release as module

    source = daily(tmp_path / "daily.zip")
    root = tmp_path / "candidate"
    write = module.atomic_write

    def interrupt(path, body):
        if path.name == "CURRENT.json":
            raise OSError("interrupted publication")
        return write(path, body)

    monkeypatch.setattr(module, "atomic_write", interrupt)
    with pytest.raises(OSError, match="interrupted publication"):
        build(root, daily=[source])
    assert not (root / "CURRENT.json").exists()
    before = (root / "releases/00000001.json").read_bytes()
    monkeypatch.setattr(module, "atomic_write", write)
    assert build(root, daily=[source])["release_id"] == 1
    assert before == (root / "releases/00000001.json").read_bytes()


def test_corrupt_archive_cli_reports_failure_without_publication(tmp_path):
    source = tmp_path / "broken.zip"
    source.write_bytes(b"incomplete source ZIP")
    root = tmp_path / "candidate"
    process = subprocess.run([sys.executable, "-m", "ashare_data.cli", "cmes", "import",
                              "--root", str(root), "--daily", str(source)],
                             text=True, capture_output=True)
    assert process.returncode == 2, process.stderr
    assert json.loads(process.stdout)["error"]["code"] == "CMES_INPUT"
    assert not (root / "CURRENT.json").exists()
