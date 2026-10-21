"""Import retained responses without resuming a stopped source or inventing captures."""

import fcntl
import json

import pytest
from test_financial_release import batch, market, report

from ashare_data.core.model import DataError
from ashare_data.financials import expansion as e
from ashare_data.product.financial_release import FinancialTables
from ashare_data.storage.financial_release import TableBuilder, prepare
from ashare_data.storage.market_release import canonical


def stopped_source(root):
    source = batch(root, [report()])
    tasks = json.loads((source / "tasks.json").read_text())
    tasks.append(dict(tasks[0], id="never-requested"))
    raw = canonical(tasks)
    (source / "tasks.json").write_bytes(raw)
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["tasks_sha256"] = e.digest(raw)
    (source / "manifest.json").write_bytes(canonical(manifest))
    (source / "STOP").write_bytes(canonical({"legacy_batch": str(source)}))
    (source / "worker-exit.json").write_bytes(canonical({"requests": 1}))
    return source


def test_stopped_source_keeps_unvisited_coverage_without_fake_capture(tmp_path):
    parent = market(tmp_path / "market")
    source = stopped_source(tmp_path / "source")
    with pytest.raises(DataError, match="Unvisited task"):
        prepare(parent, tmp_path / "ordinary", [source])
    output = tmp_path / "retained"
    candidate = prepare(parent, output, [], stopped_batches=[source])
    assert prepare(parent, output, [], stopped_batches=[source])["candidate_id"] == candidate["candidate_id"]
    assert candidate["validation"]["input_rows"] == 1
    assert candidate["validation"]["table_rows"]["financial.observations"] == 5
    with FinancialTables.prepared(output, parent).sql() as db:
        assert db.execute("SELECT count(*) FROM meta.financial_captures").fetchone()[0] == 1
        gap = db.execute("SELECT source_state,input_rows,mapped_rows,quarantined_rows,coverage_json "
                         "FROM meta.financial_coverage WHERE task_id='never-requested'").fetchone()
        assert gap[:4] == ("unvisited_stopped", 0, 0, 0)
        assert json.loads(gap[4])["source_response_observed"] is False
    assert not (source / "receipts/never-requested.json").exists()


def test_stopped_source_requires_stop_proof_and_shared_writer_lock(tmp_path):
    parent = market(tmp_path / "market")
    source = stopped_source(tmp_path / "source")
    (source / "worker-exit.json").unlink()
    with pytest.raises(DataError, match="stop and worker-exit"):
        prepare(parent, tmp_path / "no-exit", [], stopped_batches=[source])
    (source / "worker-exit.json").write_bytes(canonical({"requests": 1}))
    with (source / "writer.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DataError, match="still being written"):
            prepare(parent, tmp_path / "active", [], stopped_batches=[source])


def test_stopped_source_resume_rejects_changed_receipt_inventory(tmp_path):
    parent = market(tmp_path / "market")
    source = stopped_source(tmp_path / "source")
    output = tmp_path / "retained"
    with TableBuilder(parent, output) as builder:
        first = builder.import_batch(source, stopped=True)
    assert first["task_states"] == {"complete": 1, "unvisited_stopped": 1}
    receipt = json.loads((source / "receipts/source-task.json").read_text())
    receipt["observed_at"] = "2026-10-21T00:00:00+00:00"
    (source / "receipts/source-task.json").write_bytes(canonical(receipt))
    with TableBuilder(parent, output) as builder:
        with pytest.raises(DataError, match="Frozen candidate input changed"):
            builder.import_batch(source, stopped=True)
