import hashlib
from pathlib import Path

import pytest

from ashare_data.providers.free_update_macro_contracts import CONTRACTS, repair_customs_snapshot
from ashare_data.providers.free_update_manual import query_snapshot, read_snapshot, write_snapshot


def old_snapshot(root):
    row = {k: "1" for k in CONTRACTS["macro_china_hgjck"] if k != "TRADE_BALANCE_100M_USD"}
    row.update(REPORT_DATE="2026-09-01", TIME="2026年09月份", EXIT_BASE="447184259.939", IMPORT_BASE="313652170.68")
    units = {k: v.replace("1000 USD", "100000000 USD") for k, v in CONTRACTS["macro_china_hgjck"].items()
             if k != "TRADE_BALANCE_100M_USD"}
    check = dict(run_id="original-run", job_id="original-job", function="macro_china_hgjck",
                 observed_at="2026-10-29T04:11:23+00:00", records=1,
                 semantics={"business_key": ["REPORT_DATE"], "units": units},
                 coverage={"complete_returned_window": True}, source_urls=["https://datacenter-web.eastmoney.com/api/data/v1/get"])
    return write_snapshot(root, 163, {"function": "macro_china_hgjck"}, [row], check)


def test_correction_keeps_native_values_original_timestamps_and_old_evidence(tmp_path):
    old = old_snapshot(tmp_path)
    path = Path(old["snapshot"])
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()}
    with pytest.raises(ValueError, match="retired"):
        query_snapshot(path)
    first = repair_customs_snapshot(path, tmp_path / "replacements")
    repeated = repair_customs_snapshot(path, tmp_path / "replacements")
    assert first["snapshot_id"] == repeated["snapshot_id"] and repeated["checkpoint_reused"]
    new, rows = read_snapshot(first["snapshot"])
    original = read_snapshot(path)[1][0]
    assert all(rows[0][k] == v for k, v in original.items())
    assert rows[0]["TRADE_BALANCE_100M_USD"] == "1335.32089259"
    assert new["run_id"] == "original-run" and new["observed_at"] == "2026-10-29T04:11:23+00:00"
    assert new["correction"]["supersedes_snapshot"] == old["snapshot_id"]
    assert not new["correction"]["new_source_request"] and new["execution_kind"] == "derived_unit_correction"
    assert query_snapshot(first["snapshot"])["semantics"]["units"]["EXIT_BASE"] == "1000 USD, current month"
    assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()}


def test_repair_rejects_corruption_and_formal_destination(tmp_path):
    saved = old_snapshot(tmp_path)
    production = tmp_path / "formal"
    production.mkdir()
    (production / "CURRENT").touch()
    with pytest.raises(ValueError, match="production"):
        repair_customs_snapshot(saved["snapshot"], production)
    (Path(saved["snapshot"]) / "records.json").write_text("[]")
    with pytest.raises(ValueError, match="hash"):
        repair_customs_snapshot(saved["snapshot"], tmp_path / "repaired")
