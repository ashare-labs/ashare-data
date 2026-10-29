"""Read-only pages remain pinned while the daily collector advances."""
import importlib.util
import json
from pathlib import Path

import pytest

from ashare_data.providers import free_update_batch as batch
from test_free_update_batch import profile, protocol_worker

spec = importlib.util.spec_from_file_location("batch_query", Path(__file__).parents[1] / "scripts/query_security_update_batch.py")
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def test_multi_security_date_pages_and_old_batch_unchanged(tmp_path, monkeypatch):
    path = profile(tmp_path, rows=[12], instruments=[
        {"asset_type":"stock","symbol":"sh600000"}, {"asset_type":"stock","symbol":"sz000001"}])
    calls, day = protocol_worker(monkeypatch)
    old = batch.run(path, execute=True, as_of=day[0])
    first = reader.query(old["result_path"], 12, source_date="2026-10-28", limit=1)
    second = reader.query(old["result_path"], 12, source_date="2026-10-28", offset=1, limit=1)
    assert first["total"] == 2 and first["next_offset"] == 1 and second["next_offset"] is None
    assert {r["security"] for r in first["records"]+second["records"]} == {"sh600000","sz000001"}
    assert reader.query(old["result_path"],12,symbol="sh600000")["total"] == 2
    assert reader.query(old["result_path"],12,source_date="2026-10-26")["total"] == 0
    day[0] = "2026-10-29"
    batch.run(path, execute=True, as_of=day[0])
    assert reader.query(old["result_path"],12,source_date="2026-10-28",limit=1) == first
    assert len(calls) == 4
    with pytest.raises(ValueError, match="immutable"):
        reader.query(Path(old["root"])/"batch-latest.json",12)
    with pytest.raises(ValueError, match="scope"):
        reader.query(old["result_path"],12,symbol="sh600519")
    with pytest.raises(ValueError, match="offset"):
        reader.query(old["result_path"],12,offset=5)
    saved=Path(old["result_path"])
    value=json.loads(saved.read_text())
    value["as_of"]="2026-01-01"
    saved.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="immutable"):
        reader.query(saved,12)


def test_incomplete_scope_is_explicit(tmp_path, monkeypatch):
    path = profile(tmp_path, rows=[12], instruments=[
        {"asset_type":"stock","symbol":"sh600000"}, {"asset_type":"stock","symbol":"sz000001"}])
    _, day = protocol_worker(monkeypatch)
    partial = batch.run(path, execute=True, as_of=day[0], max_jobs=1)
    with pytest.raises(ValueError, match="complete"):
        reader.query(partial["result_path"],12)
    assert reader.query(partial["result_path"],12,symbol="sh600000")["total"] == 2
