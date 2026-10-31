import json
from types import SimpleNamespace

import pytest

from ashare_data.providers.free_updates.free_update_industry_directory import MEASURES, parse


def page(*, parent='农业', values=None):
    values = values or ['10.5', '11.0', '1.2', '--']
    groups = ''.join(f'<div class="lg-sw-industries-pepb"><span class="value">{v}</span>'
                     f'<span class="{key}">ignored percentile</span></div>'
                     for key, v in reversed(list(zip(MEASURES, values))))
    sections = []
    for level, code, name, upstream in [(1, '801010.SI', '农业', None),
        (2, '801016.SI', '种植业', parent), (3, '850111.SI', '种子', '种植业')]:
        label = f'{name}(8)' + (f'<span class="parent-industry-name">[{upstream}]</span>' if upstream else '')
        sections.append(f'<div id="level{level}Items"><li class="level{level}Item">'
            f'<div class="lg-industries-item" id="{code}">'
            f'<div class="lg-industries-item-chinese-title">{code}</div>'
            f'<div class="lg-industries-item-number">{label}</div>'
            f'<div class="lg-sw-industries-item-value">{groups}</div></div></li></div>')
    return ''.join(sections)


def test_complete_directory_reconciles_levels_and_named_measures():
    rows, coverage = parse(page(), 'sw_index_second_info')
    assert rows == [{'行业代码': '801016.SI', '行业名称': '种植业', '成份个数': 8, '上级行业': '农业',
                     '静态市盈率': 10.5, 'TTM(滚动)市盈率': 11.0, '市净率': 1.2, '静态股息率': None}]
    assert coverage['source_count'] == 1 and coverage['level_counts'] == {'1': 1, '2': 1, '3': 1}
    assert coverage['complete_returned_window'] and not coverage['complete_history']
    assert coverage['publication_date'] is None


@pytest.mark.parametrize('transform,error', [
    (lambda p: p.replace('id="level3Items"', 'id="missing"'), 'container'),
    (lambda p: p.replace('class="level2Item"', 'class="unknown"'), 'count'),
    (lambda p: p.replace('id="801016.SI"', 'id="801017.SI"'), 'identity'),
    (lambda p: p.replace('[农业]', '[未知行业]'), 'unknown parent'),
    (lambda p: p.replace('class="peTtmQuantile"', 'class="unreviewed"'), 'measure'),
    (lambda p: p.replace('>10.5<', '>NaN<'), 'finite'),
])
def test_partial_changed_or_misidentified_directory_is_not_complete(transform, error):
    with pytest.raises(ValueError, match=error):
        parse(transform(page()), 'sw_index_first_info')


@pytest.mark.parametrize('name,indicator,field', [
    ('cpi_rate_em', 'mom', 'NATIONAL_SEQUENTIAL'), ('cpi_rate_em', 'yoy', 'NATIONAL_SAME'),
    ('macro_indicator_em', 'ppi_yoy', 'BASE_SAME'), ('macro_indicator_em', 'industrial_yoy', 'BASE_SAME'),
    ('macro_indicator_em', 'non_manufacturing_pmi', 'NMAKE_INDEX'),
    ('macro_indicator_em', 'manufacturing_pmi', 'MAKE_INDEX'),
    ('macro_indicator_em', 'm2_yoy', 'BASIC_CURRENCY_SAME'),
])
def test_native_indicator_projection_preserves_all_page_counts_and_nulls(monkeypatch, name, indicator, field):
    from ashare_data.providers.free_updates.free_update_worker import direct_records
    from ashare_data.providers.free_updates import free_update_macro_history
    capture = SimpleNamespace(dataset_metadata={})
    coverage = {'complete_returned_window': True, 'complete_history': True,
                'source_count': 2, 'unique_observations': 2, 'pages_received': 2}
    def raw(capture, spec, params):
        capture.dataset_metadata = coverage.copy()
        return [{'REPORT_DATE': '2026-08-01', field: None}, {'REPORT_DATE': '2026-09-01', field: 1.2}]
    monkeypatch.setattr(free_update_macro_history, 'collect', raw)
    rows = direct_records(capture, name, {'indicator': indicator})
    assert len(rows) == 2 and rows[0]['value'] is None and rows[1]['value'] == 1.2
    assert all(capture.dataset_metadata[k] == v for k, v in coverage.items())
    assert all(r['source_field'] == field and r['publication_date'] is None for r in rows)


def test_manual_indicator_profile_only_selects_whole_source_window(tmp_path):
    from ashare_data.providers.free_updates.free_update_manual import prepare
    p = tmp_path / 'profile.json'
    p.write_text(json.dumps({'schema': 'ad.free-update-manual.v1', 'root': 'staging',
                            'rows': [133, 134, 138, 144, 145, 146, 168]}))
    _, jobs, _ = prepare(p, as_of='2026-10-29')
    assert len(jobs['jobs']) == 7
    assert all(set(j['parameters']) == {'indicator'} for j in jobs['jobs'])
