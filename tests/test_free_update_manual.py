import json
from pathlib import Path

import pytest

from ashare_data.providers import free_update_manual as manual
from ashare_data.providers.free_updates import encoded


def check(run="r1"):
    return {"run_id": run, "job_id": "j", "function": "fixture", "observed_at": "2026-10-29T00:00:00+00:00",
            "records": 2, "coverage": {"complete_returned_window": True, "source_count": 2},
            "semantics": {"business_key": ["month"], "units": {"value": "source native"}}}


def snapshot(tmp_path, rows=None, run="r1"):
    rows = rows or [{"month": "2026-08", "value": 10}, {"month": "2026-09", "value": 20}]
    return manual.write_snapshot(tmp_path, 150, {"function": "fixture"}, rows, check(run) | {"records": len(rows)})


def test_pinned_pagination_does_not_mix_later_revisions(tmp_path):
    first = snapshot(tmp_path)
    page = manual.query_snapshot(first["snapshot"], limit=1)
    second = snapshot(tmp_path, [{"month": "2026-09", "value": 21}, {"month": "2026-10", "value": 30}], "r2")
    assert second["new_records"] == second["revised_records"] == second["left_current_window"] == 1
    assert manual.query_snapshot(first["snapshot"], offset=page["next_offset"], limit=1)["records"] == [{"month": "2026-09", "value": 20}]
    assert manual.query_snapshot(second["snapshot"])["records"][0]["value"] == 21
    assert manual.read_snapshot(second["snapshot"])[0]["history_deleted"] is False


def test_same_checkpoint_does_not_create_another_snapshot(tmp_path):
    first, second = snapshot(tmp_path), snapshot(tmp_path)
    assert first["snapshot_id"] == second["snapshot_id"] and second["checkpoint_reused"]
    assert second["new_records"] == second["revised_records"] == 0


def test_shared_menu_wrappers_retain_the_same_incremental_checkpoint(tmp_path):
    first_rows = [{"month": "2026-08", "value": 10}, {"month": "2026-09", "value": 20}]
    first = manual.write_snapshot(tmp_path, 150, {"function": "fixture", "menu_rows": [150]}, first_rows, check())
    rows = first_rows+[{'month': '2026-10', 'value': 30}]
    second = manual.write_snapshot(tmp_path, 150, {"function": "fixture", "menu_rows": [150, 151]},
                                   rows, check('r2') | {'records': 3})
    assert second['new_records'] == 1 and second['revised_records'] == 0
    assert manual.read_snapshot(second['snapshot'])[0]['previous_snapshot'] == first['snapshot_id']
    repeated = manual.write_snapshot(tmp_path, 150, {"function": "fixture", "menu_rows": [150]},
                                     rows, check('r2') | {'records': 3})
    assert repeated['snapshot_id'] == second['snapshot_id'] and repeated['new_records'] == 0


def test_declared_complete_history_cannot_silently_drop_older_periods(tmp_path):
    snapshot(tmp_path)
    proof = check("r2") | {"records": 1, "coverage": {"complete_history": True, "source_count": 1}}
    with pytest.raises(ValueError, match="dropped previous keys"):
        manual.write_snapshot(tmp_path, 150, {"function": "fixture"}, [{"month": "2026-09", "value": 30}], proof)


def test_nonoverlapping_rolling_window_reports_possible_recovery_gap(tmp_path):
    snapshot(tmp_path)
    result = snapshot(tmp_path, [{"month": "2027-01", "value": 30}], "r2")
    assert result["history_gap_possible"] and result["left_current_window"] == 2


@pytest.mark.parametrize("virtual", ["_snapshot_date", "observation_snapshot_date"])
def test_snapshot_observation_date_is_metadata_not_a_fabricated_source_field(tmp_path, virtual):
    rows = [{"code": "000001", "value": 10}]
    first = check() | {"records": 1, "snapshot_observation_date": "2026-10-29",
                       "semantics": {"business_key": ["code", virtual], "units": {"value": "source native"}}}
    saved = manual.write_snapshot(tmp_path, 233, {"function": "fixture"}, rows, first)
    updated = manual.write_snapshot(tmp_path, 233, {"function": "fixture"}, rows,
        first | {"run_id": "r2", "observed_at": "2026-10-30T00:00:00+00:00", "snapshot_observation_date": "2026-10-30"})
    assert updated["new_records"] == updated["revised_records"] == 0
    assert saved["snapshot_id"] != updated["snapshot_id"]
    result = manual.query_snapshot(updated["snapshot"])
    assert result["records"] == rows and result["snapshot_observation_date"] == "2026-10-30"


def test_write_failure_keeps_previous_pointer_and_valid_export(tmp_path, monkeypatch):
    first = snapshot(tmp_path)
    pointer = next((tmp_path / "manual-snapshots").glob("latest-*.json"))
    previous = pointer.read_bytes()
    original = manual.os.replace
    def fault(source, target):
        if Path(target) == pointer:
            raise OSError("injected interrupted pointer replacement")
        return original(source, target)
    monkeypatch.setattr(manual.os, "replace", fault)
    with pytest.raises(OSError, match="injected"):
        snapshot(tmp_path, [{"month": "2026-09", "value": 30}], "r2")
    assert pointer.read_bytes() == previous
    assert manual.query_snapshot(first["snapshot"])["total"] == 2
    monkeypatch.setattr(manual.os, "replace", original)
    recovered = snapshot(tmp_path, [{"month": "2026-09", "value": 30}], "r2")
    assert manual.query_snapshot(recovered["snapshot"])["records"][0]["value"] == 30


def test_corrupt_snapshot_is_rejected_before_readback(tmp_path):
    saved = snapshot(tmp_path)
    (Path(saved["snapshot"]) / "records.json").write_bytes(encoded([{"month": "2026-09", "value": 999}]))
    with pytest.raises(ValueError, match="hash mismatch"):
        manual.query_snapshot(saved["snapshot"])


@pytest.mark.parametrize("rows,context,error", [([248], {}, "reviewed"), ([150, 150], {}, "distinct"),
    ([224], {"city_first": "北京", "city_second": "上海"}, "entire source"), ([224], {}, "full-window")])
def test_incomplete_or_unreviewed_scope_is_rejected_before_network(tmp_path, rows, context, error):
    profile = tmp_path / "config.json"
    profile.write_text(json.dumps({"schema": "ad.free-update-manual.v1", "root": "observations", "rows": rows, "context": context}))
    with pytest.raises(ValueError, match=error):
        manual.run_manual(profile, execute=True)
    assert not (tmp_path / "observations").exists()


def test_preview_has_no_database_or_snapshot_side_effect(tmp_path):
    profile = tmp_path / "config.json"
    profile.write_text(json.dumps({"schema": "ad.free-update-manual.v1", "root": "observations", "rows": [150]}))
    assert manual.run_manual(profile)["dry_run"]
    assert not (tmp_path / "observations").exists()


def test_failed_row_does_not_export_older_snapshot_or_block_other_rows(tmp_path, monkeypatch):
    from ashare_data.storage import free_updates as store
    profile = tmp_path / "config.json"
    profile.write_text(json.dumps({"schema": "ad.free-update-manual.v1", "root": "observations", "rows": [150, 151]}))
    def collect(root, prepared, **kwargs):
        store.connect(root).close()
        return [{"run_id": "ok", "status": "received"}, {"run_id": "bad", "status": "failed"}]
    def checked(root, db, job, as_of):
        if 151 in job["menu_rows"]:
            raise ValueError("source unavailable after prior success")
        return [{"month": "2026-09", "value": 20}], check() | {"records": 1}
    monkeypatch.setattr(store, "collect", collect)
    monkeypatch.setattr(manual, "checked_snapshot", checked)
    result = manual.run_manual(profile, execute=True)
    assert result["ready_count"] == 1 and not result["all_requested_jobs_received"]
    assert result["items"][1]["old_snapshot_exported_as_new"] is False
    assert "snapshot" not in result["items"][1]
    assert manual.query_snapshot(result["items"][0]["snapshot"])["total"] == 1


def test_business_key_duplicates_cannot_be_used_as_snapshot(tmp_path):
    with pytest.raises(ValueError, match="not unique"):
        snapshot(tmp_path, [{"month": "2026-09", "value": 1}, {"month": "2026-09", "value": 2}])


def test_checkpoint_path_cannot_escape_snapshot_directory(tmp_path):
    snapshot(tmp_path)
    pointer = next((tmp_path / "manual-snapshots").glob("latest-*.json"))
    pointer.write_text(json.dumps({"snapshot_id": "../../outside"}))
    with pytest.raises(ValueError, match="invalid snapshot checkpoint"):
        snapshot(tmp_path, run="r2")


def test_futures_native_entries_and_limit_pool_wrappers_share_explicit_scope(tmp_path):
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"schema": "ad.free-update-manual.v1", "root": "observations",
        "rows": list(range(248, 261)), "context": {"future_main": "V0", "future_contract": "V2701"}}))
    _, planned, _ = manual.prepare(p, as_of="2026-10-29")
    assert len(planned["jobs"]) == 13
    assert {j["parameters"]["symbol"] for j in planned["jobs"]} == {"V0", "V2701"}
    value = json.loads(p.read_text()) | {"rows": [68, 69, 70], "context": {}}
    p.write_text(json.dumps(value))
    _, planned, _ = manual.prepare(p, as_of="2026-10-29")
    assert len(planned["jobs"]) == 3
    assert sorted(len(i["job_ids"]) for i in planned["items"]) == [1, 2, 3]
    assert all(j["parameters"] == {"date": "20261029"} for j in planned["jobs"])


def test_limit_pool_date_mismatch_keeps_raw_but_cannot_pass_dated_manual_acceptance(tmp_path,monkeypatch):
    from ashare_data.storage.free_updates import connect
    job={'job_id':'x','function':'stock_zt_pool_em','parameters':{'date':'20261029'},'implementation_sha256':'fixture'}
    with connect(tmp_path) as db:
        db.execute('INSERT INTO runs(run_id,job_id,as_of,started_at,status,job_json,result_json) VALUES(?,?,?,?,?,?,?)',
                   ('r','x','2026-10-29','2026-10-29T14:00:00+00:00','received',json.dumps(job),'{}'))
        db.commit()
        (tmp_path/'runs').mkdir(exist_ok=True)
        result={'implementation_sha256':'fixture','coverage':{'reported_qdate':'20261028'}}
        raw=json.dumps(result)
        (tmp_path/'runs/r.json').write_text(raw)
        monkeypatch.setattr(manual,'implementation_identity',lambda:'fixture')
        monkeypatch.setattr(manual,'reparse',lambda *a:[])
        with pytest.raises(ValueError,match='source date differs'):
            manual.checked_snapshot(tmp_path,db,job,'2026-10-29')
        assert (tmp_path/'runs/r.json').read_text()==raw


def test_shared_source_capture_accepts_different_menu_wrapper_but_not_parameters(tmp_path,monkeypatch):
    from ashare_data.storage.free_updates import connect
    job={'job_id':'x','function':'macro_china_hgjck','parameters':{},'menu_rows':[147],'implementation_sha256':'fixture'}
    with connect(tmp_path) as db:
        db.execute('INSERT INTO runs(run_id,job_id,as_of,started_at,status,job_json,result_json) VALUES(?,?,?,?,?,?,?)',
                   ('r','x','2026-10-29','2026-10-29T04:00:00+00:00','received',json.dumps(job),'{}'))
        db.commit()
        (tmp_path/'runs').mkdir()
        (tmp_path/'runs/r.json').write_text(json.dumps({'implementation_sha256':'fixture'}))
        monkeypatch.setattr(manual,'implementation_identity',lambda:'fixture')
        def reached(*args):raise RuntimeError('raw verification reached')
        monkeypatch.setattr(manual,'reparse',reached)
        with pytest.raises(RuntimeError,match='raw verification reached'):
            manual.checked_snapshot(tmp_path,db,job|{'menu_rows':[148,149,163]},'2026-10-29')
        with pytest.raises(ValueError,match='scope'):
            manual.checked_snapshot(tmp_path,db,job|{'menu_rows':[148],'parameters':{'end_date':'20260101'}},'2026-10-29')
