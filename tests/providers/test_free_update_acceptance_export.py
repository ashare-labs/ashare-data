"""Latest local evidence must preserve every original menu identity and count."""
import copy
import json

import pytest

from ashare_data.providers.free_updates.free_update_acceptance import export_local_ledger, validate_ledger
from ashare_data.providers.free_updates import catalog


def ledger():
    original = catalog()
    return {'items':[i | {'status':'未开始','implementation_verified':False,'coverage_verified':False} for i in original['items']],
        'generated_at':'2026-10-29T00:00:00+00:00','workbook_sha256':original['workbook_sha256'],'completed':0,
        'coverage_verified_count':0,'status_counts':{'未开始':198},'daily_deployed':0,'formal_published':0}


def test_latest_entry_preserves_original_rows_with_content_bound_manifest(tmp_path):
    original = ledger()
    source = tmp_path/'history.json'
    source.write_text(json.dumps(original))
    first = source.read_bytes()
    out = tmp_path/'latest'
    manifest = export_local_ledger(source, out)
    assert manifest['scope_total'] == 198 and len(manifest['files']) == 3 and source.read_bytes() == first
    assert json.loads((out/'acceptance.json').read_text()) == original
    assert export_local_ledger(source, out) == manifest


@pytest.mark.parametrize('damage', ['field','duplicate','count','coverage','proof'])
def test_claims_or_original_identity_drift_stop_export(damage):
    value = copy.deepcopy(ledger())
    if damage == 'field':
        value['items'][0]['note'] = 'different definition'
    if damage == 'duplicate':
        value['items'][-1] = value['items'][0]
    if damage == 'count':
        value['completed'] = 1
    if damage == 'coverage':
        value['items'][0]['coverage_verified'] = True
    if damage == 'proof':
        value['items'][0]['implementation_verified'] = True
    with pytest.raises(ValueError):
        validate_ledger(value)


def test_user_labels_require_fixed_entry_and_do_not_hide_known_source_problems():
    from ashare_data.providers.free_updates.free_update_acceptance import user_progress
    item = {'implementation_verified':True, 'coverage_verified':True}
    assert user_progress(item)['user_status'] == '待验证'
    item['daily_staging_implementation_ready'] = True
    assert user_progress(item)['user_status'] == '可运行'
    assert user_progress(item | {'source_issue_summary':'最近应发布记录缺失'})['user_status'] == '来源问题'
    assert user_progress(item | {'status':'受阻'})['user_status'] == '来源问题'
    assert user_progress(item | {'acceptance_dimensions':{'semantic_gaps':['未知单位']}})['user_status'] == '待验证'
def test_work_templates_match_each_menu_function_without_cross_row_requirements():
    from ashare_data.providers.free_updates.free_update_acceptance import acceptance_work
    from ashare_data.providers.free_updates import catalog
    work = {i['excel_row']: acceptance_work(i) for i in catalog()['items']}
    assert len(work) == 198
    assert work[8]['group'] == work[9]['group'] == '复权因子'
    assert '公告' not in work[8]['next_action'] + work[8]['pass_condition']
    assert work[10]['group'] == '交易日历'
    assert '债券' not in str(work[10]) and '成分' not in str(work[10])
    assert '30条' in work[28]['next_action']
    assert all('30条' not in str(v) for k, v in work.items() if k != 28)

def test_factor_freshness_does_not_use_financial_publication_template():
    from ashare_data.providers.free_updates.free_update_acceptance import acceptance_dimensions
    item = {"mode": "event", "calls": [{"function": "stock_zh_a_daily"}], "sample_runs": []}
    result = acceptance_dimensions(item)
    assert result["freshness_state"] == "adjustment_events_not_compared"
    assert "除权除息" in result["freshness_label"]


def test_current_snapshot_is_accepted_without_historical_api_or_invented_effective_date():
    from ashare_data.providers.free_updates.free_update_acceptance import acceptance_dimensions, incremental_requirements, user_progress
    item = {"excel_row": 25, "mode": "snapshot", "sample_runs": [],
        "scoped_availability": {"verified": True, "scope": "current 2 securities", "entry": "fixed entry",
            "evidence": "proof", "readback_verified": True, "rerun_verified": True}}
    requirement = incremental_requirements(item)
    assert not requirement["historical_api_required"] and not requirement["full_history_required"]
    assert not requirement["full_market_required_for_bounded_delivery"]
    result = acceptance_dimensions(item)
    assert result["freshness_state"] == "observed_current_snapshot"
    assert result["source_latest_periods"] == [] and result["semantic_gaps"] == []
    assert user_progress(item)["user_status"] == "可运行"
    assert user_progress({k: v for k, v in item.items() if k != "scoped_availability"})["user_status"] == "待验证"


def test_incremental_goal_covers_future_candidates_without_claiming_implementation():
    from ashare_data.providers.free_updates.free_update_acceptance import incremental_requirements
    for row in [56, 112, 125, 263, 272, 273, 275]:
        requirement = incremental_requirements({"excel_row": row})
        assert requirement["snapshot_or_short_window_allowed"]
        assert not requirement["historical_api_required"]
        assert "先核tick是否指快照" in requirement["definition_rule"]
        assert "implemented" not in requirement


def test_cancellation_survives_recalculation_without_erasing_historical_evidence(tmp_path):
    from ashare_data.providers.free_updates.free_update_acceptance import user_progress
    value = ledger()
    item = value['items'][0]
    item['scope_decision'] = {'status': 'cancelled', 'reason': 'User cancelled further development'}
    item['scoped_availability'] = {'verified': True, 'scope': 'historical sample', 'entry': 'old entry',
        'evidence': 'old evidence', 'readback_verified': True, 'rerun_verified': True}
    item.update(user_progress(item))
    assert item['user_status'] == '用户取消'
    source = tmp_path/'ledger.json'
    source.write_text(json.dumps(value))
    result = export_local_ledger(source, tmp_path/'latest')
    assert result['user_status_counts']['用户取消'] == 1
    saved = json.loads((tmp_path/'latest'/'acceptance.json').read_text())
    assert saved['items'][0]['scoped_availability'] == item['scoped_availability']
    assert len(saved['items']) == 198
