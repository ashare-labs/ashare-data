"""Historical directory membership reuses recorded listing/code intervals only."""

import json

import pytest

from ashare_data.core.model import DataError
from ashare_data.product.market_release import ReleasedDailyProduct
from ashare_data.storage.local_daily import LocalDailyStore
from ashare_data.storage.market_release import ReleaseWriter


def reader(root):
    with ReleaseWriter(root / 'published', free_reserve_bytes=0) as writer:
        published = writer.publish(dict(files=[], metadata=writer.object(root / 'daily.sqlite3', 'sqlite3'),
            input_dispositions={'constructed': {'input_rows': 0, 'adopted': 0}}), expected_parent=None)
    return ReleasedDailyProduct({'market_release': dict(root=str(root / 'published'),
        release_id=published['release_id'], manifest_sha256=published['sha256'])})


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
    owner = reader(tmp_path)
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
    owner = reader(tmp_path)
    assert [r['security'] for r in owner.catalogue(as_of='2024-03-04')['securities']] == ['600001.XSHG']
    assert [r['security'] for r in owner.catalogue(as_of='2024-03-05')['securities']] == ['600002.XSHG']


def test_pages_and_same_date_reads_scan_members_once_and_do_not_share_mutable_results(tmp_path, monkeypatch):
    count = 1001
    store = LocalDailyStore(tmp_path)
    store.db.executescript('''
        CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
        CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
    ''')
    ids = [f'{600000+i:06}.XSHG' for i in range(count)]
    store.db.executemany('INSERT INTO release_instruments VALUES(?,?,?)', [(i,s,'constructed') for i,s in enumerate(ids)])
    store.db.executemany('INSERT INTO release_identifiers VALUES(?,?)', [(s,i) for i,s in enumerate(ids)])
    store.db.executemany('INSERT INTO listing_facts VALUES(?,?,?,?,?,?,?)',
        [(s,'sh'+s[:6],'2000-01-01',None,'{}','2026-01-01',None) for s in ids])
    store.db.commit()
    store.close()
    owner = reader(tmp_path)
    evaluations = []
    expected = LocalDailyStore.expected

    def measured(self, *args, **kwargs):
        evaluations.append(args[0])
        return expected(self, *args, **kwargs)

    monkeypatch.setattr(LocalDailyStore, 'expected', measured)
    for _ in range(2):
        items = []
        for offset in (0,500,1000):
            page = owner.catalogue(as_of='2024-01-10', limit=500, offset=offset)
            items.extend(page['securities'])
        assert [r['security'] for r in items] == ids
        items[0]['name'] = 'client mutation'
        page['unknown_listing_securities'].append('client mutation')
    assert len(evaluations) == count
    assert owner.catalogue(as_of='2024-01-10')['securities'][0]['name'] == 'constructed'
    assert owner.catalogue(as_of='2024-01-10')['unknown_listing_securities'] == []
    assert owner.catalogue(as_of='1999-12-31')['total'] == 0
    assert len(evaluations) == 2 * count
    assert owner.catalogue(as_of='2024-01-10', query=ids[0])['total'] == 1
    assert len(evaluations) == 2 * count + 1
    # Warm cache cannot make corruption of the selected immutable object invisible.
    owner.release.metadata_path.write_bytes(b'corrupt')
    with pytest.raises(DataError, match='Immutable object changed'):
        owner.catalogue(as_of='2024-01-10', query=ids[0])
