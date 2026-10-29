from io import BytesIO
import json

import pandas as pd
import pytest

from ashare_data.providers.free_update_current_members import szse_etfs, sw_members
from ashare_data.providers.free_update_semantics import validate_semantics
from ashare_data.providers.free_update_manual import prepare, run_manual


class Capture:
    def __init__(self, body): self.body = body
    def get(self, *_a, **_k): return self.body


def test_szse_current_etfs_exclude_other_funds_keep_native_share_units():
    rows = [{'基金代码':code,'基金简称':code,'基金类别':kind,'投资类别':'股票','上市日期':'2020-01-01',
             '当前规模(份)':'1,200','基金管理人':'管理人','基金发起人':'发起人','基金托管人':'托管人','净值':1.2}
            for code,kind in [('159001','ETF'),('160001','LOF'),('180001','不动产基金')]]
    stream = BytesIO()
    pd.DataFrame(rows).to_excel(stream,index=False)
    capture = Capture(stream.getvalue())
    result = szse_etfs(capture,{})
    assert len(result)==1 and result[0]['基金份额']==1200
    assert result[0]['基金代码']=='159001' and '统计日期' not in result[0]
    assert capture.dataset_metadata['excluded_non_etf_rows']==2
    assert capture.dataset_metadata['share_statistical_date'] is None
    sem=validate_semantics(result,{'function':'fund_etf_only_szse','parameters':{}},'2026-10-29')
    assert not sem['gaps'] and sem['latest_period'] is None


def page(code='850111.SI'):
    return ('<title>种子('+code+')申万行业成分股_来源</title><meta name="description" content="2026-10-28，种子">'
        '<table><thead><tr><th>序号</th><th>股票代码</th><th>股票简称</th><th>申万3级</th><th>纳入时间</th>'
        '<th>价格</th><th>市盈率</th><th>市盈率ttm</th><th>市净率</th><th>市值（亿元）</th><th>未知字段</th></tr></thead>'
        '<tbody><tr><td>1</td><td>000001.SZ</td><td>样本</td><td>种子</td><td>1997-01-01</td>'
        '<td>10</td><td>-</td><td>12</td><td>1</td><td>50</td><td>原值</td></tr></tbody></table>').encode()


def test_current_industry_members_bind_request_and_retain_unknown_fields():
    capture=Capture(page())
    result=sw_members(capture,{'symbol':'850111.SI'})
    assert result[0]['股票代码']=='000001.SZ'
    assert result[0]['未知字段']=='原值' and result[0]['纳入时间']=='1997-01-01'
    assert result[0]['市盈率'] is None and capture.dataset_metadata['source_page_date']=='2026-10-28'
    sem=validate_semantics(result,{'function':'sw_index_third_cons','parameters':{'symbol':'850111.SI'}},'2026-10-29')
    assert not sem['gaps'] and sem['units']['市值']=='100000000 CNY'
    with pytest.raises(ValueError,match='echo requested'):
        sw_members(Capture(page('850112.SI')),{'symbol':'850111.SI'})


def test_etf_profile_requires_both_exchanges_but_deduplicates_packaging(tmp_path):
    p=tmp_path/'p.json'
    p.write_text(json.dumps({'schema':'ad.free-update-manual.v1','root':'data','rows':[25,26],'max_jobs':2}))
    _,plan,_=prepare(p,as_of='2026-10-29')
    assert len(plan['jobs'])==2
    assert {j['function'] for j in plan['jobs']}=={'fund_etf_scale_sse','fund_etf_only_szse'}
    assert all(set(j['menu_rows'])=={25,26} for j in plan['jobs'])


def test_one_missing_exchange_does_not_claim_two_market_completion(tmp_path,monkeypatch):
    from ashare_data.storage import free_updates as store
    from ashare_data.providers import free_update_manual as m
    p=tmp_path/'p.json'
    p.write_text(json.dumps({'schema':'ad.free-update-manual.v1','root':'data','rows':[25,26]}))
    def collect(root, prepared, **kw):
        store.connect(root).close()
        return []
    def checked(root, db, job, day):
        if job['function']=='fund_etf_only_szse':
            raise ValueError('synthetic missing source')
        return [{'基金代码':'510300','基金份额':100}],{'records':1}
    monkeypatch.setattr(store,'collect',collect)
    monkeypatch.setattr(m,'checked_snapshot',checked)
    monkeypatch.setattr(m,'write_snapshot',lambda root,row,*a:dict(excel_row=row,status='可读取',records=1))
    r=run_manual(p,execute=True,as_of='2026-10-29')
    assert not r['all_requested_jobs_received'] and r['ready_count']==0
    assert all(len(i['snapshots'])==2 and not i['complete_requested_exchanges'] for i in r['items'])

@pytest.mark.parametrize('level,code,label', [(1,'801010.SI','申万1级'),(2,'801016.SI','申万2级')])
def test_current_member_levels_are_explicit_not_inferred(level,code,label):
    body=page(code).replace('申万3级'.encode(),label.encode())
    capture=Capture(body)
    rows=sw_members(capture,{'symbol':code},level=level)
    assert rows[0][label]=='种子' and capture.dataset_metadata['industry_level']==level
    fn='sw_index_first_cons' if level==1 else 'sw_index_second_cons'
    assert not validate_semantics(rows,{'function':fn,'parameters':{'symbol':code}},'2026-10-29')['gaps']
    with pytest.raises(ValueError,match='absent or ambiguous'):
        sw_members(Capture(body),{'symbol':code},level=3)
    with pytest.raises(ValueError,match='another classification'):
        sw_members(Capture(body.replace(b'<td>\xe7\xa7\x8d\xe5\xad\x90</td>',b'<td>other</td>')),{'symbol':code},level=level)


def test_membership_plans_keep_original_scope_but_use_reviewed_same_identity_page(tmp_path):
    path=tmp_path/'profile.json'
    path.write_text(json.dumps({'schema':'ad.free-update-manual.v1','root':'data','rows':[118,120],
                               'context':{'sw_first':'801010.SI','sw_second':'801016.SI'}}))
    _,p,_=prepare(path,as_of='2026-10-29')
    assert {j['function'] for j in p['jobs']}=={'sw_index_first_cons','sw_index_second_cons'}
    assert all(j['source_families']==['legulegu.com'] for j in p['jobs'])
