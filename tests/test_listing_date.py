"""Small real-evidence regressions for the exact-date increment and its time boundaries."""
from datetime import date, timedelta
import json
import os
from pathlib import Path
import shutil

import pytest

from ashare_data import DataError, FactRecord, Store
from ashare_data import listing_fact as module
from ashare_data.cli import main
from ashare_data.model import canonical


@pytest.fixture
def date_package():
    path = os.environ.get('ASHARE_LISTING_DATE_EVIDENCE')
    if not path:
        pytest.skip('Set ASHARE_LISTING_DATE_EVIDENCE to the reviewed real date package')
    return Path(path)


@pytest.fixture
def exact(tmp_path, date_package):
    store = Store.init(tmp_path / 'store')
    sid = store.import_listing_evidence(date_package)
    return store, sid, store.listing_fact(sid)


def reject(code, action):
    with pytest.raises(DataError) as caught:
        action()
    assert caught.value.code == code


def test_exact_fact_reuses_generic_model_and_derives_year(exact):
    _, sid, view = exact
    assert view.descriptor().supported_fields == ('initial_listing_date', 'initial_listing_year')
    assert view.descriptor().package_sha256 == module.DATE_PACKAGE_SHA256
    assert view.validate()['status'] == 'VALID_LISTING_DATE_FACT_ONLY'
    for day in ('2026-09-28', '2026-09-29', '2026-09-30'):
        fact = view.get('300750.XSHE', day, field='initial_listing_date')
        assert isinstance(fact, FactRecord) and fact.value == '2018-06-11'
        meta = fact.source_fields
        assert meta['snapshot_id'] == sid and meta['on_date'] == day
        assert meta['event_date'] == meta['valid_date'] == '2018-06-11'
        assert meta['document_published_date'] == '2018-08-24'
        assert meta['document_published_datetime_label'] == '2018-08-24 17:12:17'
        assert meta['document_published_timezone'] is None
        assert meta['evidence_received_at'] == '2026-10-10T04:43:49.030165+00:00'
        assert meta['historical_available_at'] is meta['historical_eligible'] is None
        assert meta['execution_permission'] is False and meta['visibility'] == 'posthoc'
        assert len(fact.evidence) == 2 and all(e.historical_available_at is None for e in fact.evidence)
        assert fact.evidence[0].retrieved_at.isoformat() == '2026-10-10T04:42:41.537331+00:00'
        assert fact.evidence[1].retrieved_at.isoformat() == meta['evidence_received_at']
        assert fact.evidence[0].source_fields['pdf_pages'] == [1, 38, 39, 40]
        year = view.get('300750.XSHE', day)
        assert (year.value, year.actual_initial_listing_date) == (2018, date.fromisoformat(fact.value))
        assert year.evidence_status == 'derived_from_initial_listing_date'
        assert year.precision == 'year' and year.historical_eligible is None


def test_legacy_snapshot_unchanged_when_new_package_coexists(exact, date_package):
    location = os.environ.get('ASHARE_LISTING_EVIDENCE')
    if not location:
        pytest.skip('Legacy real year package needed for compatibility proof')
    store, new_sid, _ = exact
    old_sid = store.import_listing_evidence(location)
    assert old_sid == 'ec335a72212b277e2ced8b04e774a7e312bc364e8e35086ba776dcc29fb43bab'
    old = store.listing_fact(old_sid)
    before = old.get('300750.XSHE', '2026-09-30').to_dict()
    assert store.import_listing_evidence(date_package) == new_sid != old_sid
    assert old.get('300750.XSHE', '2026-09-30').to_dict() == before
    assert old.descriptor().owner_version == '0.8.2.dev1'
    assert old.descriptor().supported_fields == ('initial_listing_year',)
    assert before['actual_initial_listing_date'] is None
    reject('LISTING_FACT_UNAVAILABLE', lambda: old.get('300750.XSHE', '2026-09-30', field='initial_listing_date'))
    assert len(store.listing_fact_snapshots()) == 2


@pytest.mark.parametrize('clock', ['2018-06-11T15:00:00+08:00', '2018-08-24T17:12:17+08:00',
                                    '2026-10-10T04:43:49.030164+00:00'])
def test_event_or_publication_date_does_not_backdate_knowledge(exact, clock):
    view = exact[2]
    for field in ('initial_listing_date', 'initial_listing_year'):
        reject('VISIBILITY_UNKNOWN', lambda: view.get('300750.XSHE', '2026-09-30', field=field,
                                                       visibility='received', knowledge_at=clock))


def test_receipt_boundary_is_not_strict_pit_or_eligibility(exact):
    view = exact[2]
    receipt = view.descriptor().evidence_received_at
    fact = view.get('300750.XSHE', '2026-09-30', field='initial_listing_date',
                    visibility='received', knowledge_at=receipt)
    assert fact.source_fields['knowledge_at'] == receipt.isoformat()
    reject('PIT_UNAVAILABLE', lambda: view.get('300750.XSHE', '2026-09-30', field='initial_listing_date',
                                             visibility='verified', knowledge_at=receipt + timedelta(days=1)))
    reject('LISTING_ELIGIBILITY_UNKNOWN', lambda: view.require_eligible('300750.XSHE', '2026-09-30'))
    reject('LISTING_DATE_SCOPE', lambda: view.get('300750.XSHE', '2018-06-11', field='initial_listing_date'))
    reject('LISTING_SECURITY_SCOPE', lambda: view.get('600000.XSHG', '2026-09-30', field='initial_listing_date'))
    reject('LISTING_FACT_UNAVAILABLE', lambda: view.get('300750.XSHE', '2026-09-30', field='is_st'))


@pytest.mark.parametrize('key,value', [('value', '2018-08-24'), ('document_published_timezone', 'Asia/Shanghai')])
def test_self_signed_date_or_timezone_forgery_refused(tmp_path, date_package, key, value):
    changed = tmp_path / 'changed'
    shutil.copytree(date_package, changed)
    body = json.loads((changed / 'package.json').read_bytes())
    body[key] = value
    (changed / 'package.json').write_bytes(canonical(body))
    store = Store.init(tmp_path / 'store')
    reject('LISTING_UNREVIEWED_PACKAGE', lambda: store.import_listing_evidence(changed))
    assert store.listing_fact_snapshots() == []


def test_public_evidence_binds_actual_pdf_and_exact_metadata(exact, date_package):
    view = exact[2]
    entries = json.loads(view.evidence('issuer-metadata.raw'))['data']
    fact = view.get('300750.XSHE', '2026-09-30', field='initial_listing_date')
    matches = [x for x in entries if 'https://www.catl.com' + x['file'] == fact.evidence[0].url]
    assert len(matches) == 1 and matches[0] == fact.source_fields['publication_metadata_record']
    assert view.evidence('issuer-halfyear.pdf') == (date_package / 'issuer-halfyear.pdf').read_bytes()
    for entry in view.lineage():
        assert view.evidence(entry.receipt_path)
    mutable = fact.source_fields
    mutable['publication_metadata_record']['publishDate'] = '2018-06-11'
    assert view.get('300750.XSHE', '2026-09-30', field='initial_listing_date').source_fields['document_published_date'] == '2018-08-24'


def test_bound_reads_refuse_later_metadata_corruption(exact):
    store, _, view = exact
    path = store.root / 'listing-objects' / view.lineage()[1].source_sha256
    path.chmod(0o644)
    path.write_bytes(b'{}')
    reject('LISTING_INTEGRITY', lambda: view.get('300750.XSHE', '2026-09-30', field='initial_listing_date'))
    reject('LISTING_INTEGRITY', lambda: view.get('300750.XSHE', '2026-09-30'))


def test_date_package_interruption_recovery(tmp_path, date_package, monkeypatch):
    store = Store.init(tmp_path / 'store')
    real_write = module.immutable_write
    def interrupt(path, blob):
        real_write(path, blob)
        if path.parent.name == 'listing-manifests':
            raise OSError('interrupt after date manifest')
    monkeypatch.setattr(module, 'immutable_write', interrupt)
    with pytest.raises(OSError, match='interrupt after'):
        store.import_listing_evidence(date_package)
    sid = store.listing_fact_snapshots()[0]['snapshot_id']
    reject('LISTING_NOT_PUBLISHED', lambda: store.listing_fact(sid))
    monkeypatch.setattr(module, 'immutable_write', real_write)
    assert store.recover_listing_facts() == [{'snapshot_id': sid, 'status': 'published', 'error': None}]
    assert store.listing_fact(sid).get('300750.XSHE', '2026-09-30', field='initial_listing_date').value == '2018-06-11'


def test_cli_uses_existing_fact_serialization_and_rejects_historical_clock(exact, capsys):
    store, sid, view = exact
    args = ['--store', str(store.root), 'listing-query', 'value', '--snapshot', sid,
            '--security', '300750.XSHE', '--date', '2026-09-30', '--field', 'initial_listing_date']
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == view.get('300750.XSHE', '2026-09-30', field='initial_listing_date').to_dict()
    assert main(args + ['--visibility', 'received', '--knowledge-at', '2018-06-11T15:00:00+08:00']) == 2
    assert json.loads(capsys.readouterr().err)['error']['code'] == 'VISIBILITY_UNKNOWN'
