"""A committed consumer refuses legacy data before reading it or constructing transport."""
import json

import pytest

from ashare_data.product.configuration import resolve
from ashare_data.product.daily import LocalDailyProduct
from ashare_data.storage.market_release import ReleaseWriter
from test_current_release import deployment
from test_market_release import chunk, manifest, metadata


@pytest.fixture
def strict(tmp_path, monkeypatch):
    path, doc = deployment(tmp_path)
    doc['profiles']['product'].pop('daily_asset')
    doc['profiles']['product']['consumer_policy'] = 'committed_market_only'
    path.write_text(json.dumps(doc))
    with ReleaseWriter(tmp_path / 'market', free_reserve_bytes=0) as writer:
        writer.publish(manifest(metadata(writer), [chunk(writer, 10)]), expected_parent=None)
    monkeypatch.setenv('ASHARE_CONSUMER_DEPLOYMENT', str(path))
    monkeypatch.setenv('ASHARE_DEPLOYMENT', str(path))
    return path


def test_committed_profile_has_no_sqlite_dependency(strict):
    config = resolve()
    assert config['daily_root'] is None
    assert 'old' not in config['selected_assets']
    assert LocalDailyProduct.from_deployment().release.release_id == 1


@pytest.mark.parametrize('variable', ['ASHARE_QUANT_FACTOR_SOURCES',
    'ASHARE_QUANT_PYTHON_HISTORY_FACTORS', 'ASHARE_QUANT_MARKET_STORES',
    'ASHARE_QUANT_DAILY_STORE', 'ASHARE_QUANT_DIAGNOSTICS', 'AQ_API_URL', 'QUANT_SERVICE_URL'])
def test_inherited_sources_do_not_block_resolution(strict, monkeypatch, variable):
    monkeypatch.setenv(variable, '/do-not-open')
    config = resolve()
    assert config['market_release']['release_id'] == 1
