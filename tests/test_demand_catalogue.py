"""Historical directory membership reuses recorded listing/code intervals only."""

import json
from types import SimpleNamespace

from ashare_data.product.market_release import ReleasedDailyProduct
from ashare_data.storage.local_daily import LocalDailyStore


def test_as_of_directory_reports_unknown_intervals_and_keeps_name_boundary(tmp_path):
    store = LocalDailyStore(tmp_path)
    db = store.db
    db.execute('CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT)')
    db.execute('CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER)')
    ids = ['600001.XSHG', '600002.XSHG', '600003.XSHG']
    for i, sid in enumerate(ids, 1):
        db.execute('INSERT INTO release_instruments VALUES(?,?,?)', [i, sid, 'current name'])
        db.execute('INSERT INTO release_identifiers VALUES(?,?)', [sid, i])
    for sid, ipo, out, inclusive in [
        ('600001.XSHG', '2000-01-01', None, False),
        ('600002.XSHG', '2000-01-01', '2024-03-05', True),
    ]:
        db.execute('INSERT INTO listing_facts VALUES(?,?,?,?,?,?,?)',
                   [sid, 'sh'+sid[:6], ipo, out, json.dumps({'out_inclusive': inclusive}), '2026-01-01', None])
    db.commit()
    store.close()
    owner = object.__new__(ReleasedDailyProduct)
    owner.release = SimpleNamespace(root=tmp_path, metadata_path=tmp_path/'daily.sqlite3',
                                    identifiers={s:i for i,s in enumerate(ids,1)},
                                    receipt=lambda: {'manifest_sha256': 'a'*64})
    before = owner.catalogue(as_of='2024-03-05', limit=1)
    assert before['total'] == 2 and len(before['securities']) == 1
    assert before['unknown_listing_securities'] == ['600003.XSHG']
    assert before['historical_pit_verified'] is False
    assert before['securities'][0]['name_is_historical'] is False
    page = owner.catalogue(as_of='2024-03-05', limit=1, offset=1)
    assert page['securities'][0]['security'] == '600002.XSHG'
    after = owner.catalogue(as_of='2024-03-06')
    assert [r['security'] for r in after['securities']] == ['600001.XSHG']
    assert owner.catalogue(as_of='1999-12-31')['total'] == 0


def test_historical_code_does_not_inherit_a_future_alias_interval(tmp_path):
    store = LocalDailyStore(tmp_path)
    db = store.db
    db.executescript('''
        CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
        CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
        INSERT INTO release_instruments VALUES(1,'600002.XSHG','current name');
        INSERT INTO release_identifiers VALUES('600001.XSHG',1),('600002.XSHG',1);
    ''')
    for sid in ['600001.XSHG', '600002.XSHG']:
        db.execute('INSERT INTO listing_facts VALUES(?,?,?,?,?,?,?)',
                   [sid,'sh'+sid[:6],'2000-01-01',None,'{}','2026-01-01',None])
    event = dict(old_symbol='sh600001',new_symbol='sh600002',effective_date='2024-03-05')
    db.execute('INSERT INTO code_events VALUES(?,?,?,?,?,?,?)',
               ['change','sh600001','sh600002','2024-03-05',json.dumps(event),'2026-01-01',None])
    db.commit()
    store.close()
    owner = object.__new__(ReleasedDailyProduct)
    owner.release = SimpleNamespace(root=tmp_path, metadata_path=tmp_path/'daily.sqlite3',
                                    identifiers={'600001.XSHG':1,'600002.XSHG':1}, receipt=lambda:{})
    assert [r['security'] for r in owner.catalogue(as_of='2024-03-04')['securities']] == ['600001.XSHG']
    assert [r['security'] for r in owner.catalogue(as_of='2024-03-05')['securities']] == ['600002.XSHG']
