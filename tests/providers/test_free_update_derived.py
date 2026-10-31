"""No-network projections retain source observation times and new revisions."""
from datetime import datetime
import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest

from ashare_data.providers.free_updates.free_update_derived import run_cached_macro
from ashare_data.providers.free_updates.free_update_billboard_queue import detail_plan
from ashare_data.providers.free_updates.free_update_macro_contracts import CONTRACTS
from ashare_data.providers.free_updates.free_update_manual import query_snapshot
from ashare_data.providers.free_updates.free_update_worker import direct_records
from ashare_data.providers.free_updates import encoded, plan, implementation_identity
from ashare_data.storage.free_updates import writer, ingest


def seed(root, run_id="first", value=.2, observed="2026-10-29T10:00:00+00:00"):
    job = plan(rows=[156], as_of="2026-10-29")["jobs"][0]
    row = {k: 101 if 'index:' in unit else 1 for k,unit in CONTRACTS['macro_china_cpi'].items()}
    row.update(REPORT_DATE="2026-09-01", TIME="2026年09月份", NATIONAL_SEQUENTIAL=value)
    body=encoded({'result':{'data':[row],'count':1,'pages':1}})
    receipts=[]
    def get(url):
        receipts.append(dict(url=url,protocol='http',status=200,received_at=observed,
                             sha256=hashlib.sha256(body).hexdigest()))
        return body
    capture=SimpleNamespace(get=get)
    records=direct_records(capture,job['function'],{})
    result=dict(status='received',records=records,receipts=receipts,coverage=capture.dataset_metadata,
                observation_as_of='2026-10-29',started_at=observed,finished_at=observed,
                implementation_sha256=implementation_identity())
    with writer(root) as db:
        (root/'raw').mkdir(exist_ok=True)
        (root/'runs').mkdir(exist_ok=True)
        (root/'raw'/(receipts[0]['sha256']+'.gz')).write_bytes(gzip.compress(body))
        (root/'runs'/(run_id+'.json')).write_bytes(encoded(result))
        db.execute('INSERT INTO runs(run_id,job_id,as_of,started_at,status,job_json,result_json) VALUES(?,?,?,?,?,?,?)',
                   (run_id,job['job_id'],'2026-10-29',observed,'running',encoded(job).decode(),'{}'))
        ingest(db,root,run_id,job,result,as_of='2026-10-29')


def test_cached_projection_keeps_dates_reuses_checkpoint_and_tracks_revision(tmp_path, monkeypatch):
    source=tmp_path/'source'
    seed(source)
    p=tmp_path/'profile.json'
    p.write_text(json.dumps(dict(schema='ad.cached-macro.v1',rows=[133],source_root=str(source),root=str(tmp_path/'derived'))))
    def forbidden(*args,**kwargs):raise AssertionError('local projection attempted network')
    monkeypatch.setattr('urllib.request.OpenerDirector.open', forbidden)
    first=run_cached_macro(p,execute=True,as_of='2026-10-30')
    snap=first['items'][0]
    page=query_snapshot(snap['snapshot'])
    assert first['remote_requests']==0 and page['observed_at']=='2026-10-29T10:00:00+00:00'
    assert page['records'][0]['value']==.2
    again=run_cached_macro(p,execute=True,as_of='2026-10-30')
    assert again['items'][0]['snapshot_id']==snap['snapshot_id']
    seed(source,'revised',.3,'2026-10-29T11:00:00+00:00')
    revision=run_cached_macro(p,execute=True,as_of='2026-10-30')['items'][0]
    assert revision['revised_records']==1 and revision['new_records']==0
    assert query_snapshot(snap['snapshot'])['records'][0]['value']==.2
    (next((source/'raw').glob('*.gz'))).write_bytes(gzip.compress(b'corrupt'))
    # Damage both responses so the currently selected source cannot escape it.
    for path in (source/'raw').glob('*.gz'):
        path.write_bytes(gzip.compress(b'corrupt'))
    with pytest.raises(ValueError,match='hash mismatch'):
        run_cached_macro(p,execute=True,as_of='2026-10-30')


def test_billboard_queue_deduplicates_source_reasons_and_uses_latest_actual_date():
    records=[dict(TRADE_DATE='2026-10-29',SECURITY_CODE=s) for s in ['600000','600000','000001']]
    records.append(dict(TRADE_DATE='2026-10-28',SECURITY_CODE='600519'))
    source=({'run_id':'r','as_of':'2026-10-30','finished_at':datetime.now().isoformat()},
            {'parameters':{'dataset':'billboard'}},{'records':records})
    result=detail_plan(source,'2026-10-30')
    assert result['securities']==['000001','600000'] and len(result['jobs'])==5
    assert result['source_trade_date']=='2026-10-29'
    assert all(j['parameters'].get('date','20261029')=='20261029' for j in result['jobs'])
    source[0]['as_of']='2026-10-28'
    with pytest.raises(ValueError,match='not run'):
        detail_plan(source,'2026-10-30')
