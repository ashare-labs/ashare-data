"""Portable producer acceptance gates: currently 6 failures on e72f713.
Run against a fresh installed wheel, with ASHARE_M2_COMPONENTS pointing to a private
read-only market/facts component directory; all mutations remain under pytest tmp_path.
A future fix may reject malformed components at import, or block product validation.
It must never publish malformed input or leak bare Python exceptions.
"""
from pathlib import Path
import copy,json,os
import pytest
from ashare_data import Store,DataError

@pytest.mark.parametrize('fault',['preclose_exponent','preclose_whitespace','preclose_nonnumeric','volume_overlong','event_unknown_security','event_missing_fields'])
def test_reject_before_product_publication(tmp_path,fault):
    inputs=Path(os.environ['ASHARE_M2_COMPONENTS']);s=Store.init(tmp_path/'store')
    def recipe(kind):
        doc=json.loads((inputs/kind/'component.json').read_text())
        for d in doc['documents']:
            for k,a in list(d['artifacts'].items()):d['artifacts'][k]={'path':str(inputs/kind/a['path'])}
        return {k:doc[k] for k in ('kind','classification','documents','claims')}
    m,f=recipe('market'),recipe('facts')
    if fault.startswith('preclose') or fault=='volume_overlong':
        d=next(d for d in m['documents'] if d['name']=='legacy-daily')
        raw=json.loads(Path(d['artifacts']['raw']['path']).read_text())
        row=next(row for row in raw['rows'] if row['date']=='2020-01-03')
        if fault=='volume_overlong':row['volume']='0'*5000
        else:row['preclose']={'preclose_exponent':'12.47e0','preclose_whitespace':' 12.4700 ','preclose_nonnumeric':'oops'}[fault]
        p=tmp_path/'mutated-source.json';p.write_text(json.dumps(raw));d['artifacts']['raw']={'path':str(p)}
    elif fault=='event_unknown_security':f['claims']['events'][0].update(entitled_security=None,record_date='2020-01-06')
    else:f['claims']['events']=[{'entitled_security':None,'evidence':copy.deepcopy(f['claims']['events'][0]['evidence'])}]
    try:
        mid=s.import_m2_sources(**m);fid=s.import_m2_sources(**f)
        request=dict(price_dataset_id=mid,facts_component_id=fid,window_ids=['m2a'],mode='conditional_research')
        report=s.validate_m2(**request).to_dict()
    except DataError as e:
        assert e.code in {'M2_COMPONENT_SCHEMA','M2_FACT_MISSING','M2_RELATED_EVENT','M2_SOURCE_CONFLICT','M2_DECIMAL','M2_UNIT_UNKNOWN','M2_INTEGER'}
        assert s.m2_snapshots()==[]
        return
    assert report['status']=='BLOCKED',f'{fault} wrongly admitted: {report["report_id"]}'
    assert report['gaps'] and s.m2_report(report['report_id']).to_dict()==report
    with pytest.raises(DataError) as caught:s.compose_m2(**request)
    assert caught.value.code=='M2_COMPOSITION_BLOCKED'
    assert s.m2_snapshots()==[]
