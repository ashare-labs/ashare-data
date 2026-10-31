import json
from pathlib import Path

import pytest

from ashare_data.providers.free_updates import free_update_manual as manual
from ashare_data.providers.free_updates import free_update_market_windows as windows
from ashare_data.providers.free_updates import free_update_rates as rates
from ashare_data.providers.free_updates.free_update_semantics import numeric


class Capture:
    def __init__(self, body):
        self.body = body
        self.urls = []

    def get(self, url, headers=None):
        self.urls.append(url)
        return self.body


def test_margin_native_units_and_all_rows_are_bound_before_export():
    body = {'keys': [dict(h) for h in windows.MARGIN_HEADERS], 'values': {
        '2026-10-28': [1, 100, 3, 4, 20, 120], '2026-10-27': [2, 90, 3, 4, 20, 110]}}
    capture = Capture(json.dumps(body).encode())
    rows = windows.collect(capture, 'macro_china_market_margin_sh', {})
    assert [r['融资余额'] for r in rows] == [90, 100]
    assert capture.urls == [windows.MARGIN_URL]
    assert capture.dataset_metadata['source_count'] == 2
    assert capture.dataset_metadata['units']['融券余量'] == 'shares'
    body['keys'][0] = {'name': '融资买入额', 'unit': '万元'}
    with pytest.raises(ValueError, match='units'):
        windows.collect(Capture(json.dumps(body).encode()), 'macro_china_market_margin_sh', {})


def curve_page(day='2026-10-28'):
    columns = ['曲线名称', '日期', *windows.TENORS]
    rows = [[windows.CURVE_NAME, day, *([2.0]*8)], ['中债商业银行债', day, *([4.0]*8)]]
    return ('<table><thead><tr>'+''.join('<th>'+v+'</th>' for v in columns)+'</tr></thead><tbody>'
            +''.join('<tr>'+''.join('<td>'+str(v)+'</td>' for v in row)+'</tr>' for row in rows)
            +'</tbody></table>').encode()


def test_curve_keeps_only_named_government_series_and_exact_bounds():
    params = {'start_date': '20261014', 'end_date': '20261028'}
    cap = Capture(curve_page())
    rows = windows.collect(cap, 'china_government_yield', params)
    assert len(rows) == 1 and rows[0]['曲线名称'] == windows.CURVE_NAME and rows[0]['10年'] == 2
    assert cap.dataset_metadata['complete_requested_window']
    assert not cap.dataset_metadata['complete_history']
    with pytest.raises(ValueError, match='outside request'):
        windows.collect(Capture(curve_page('2026-10-29')), 'china_government_yield', params)
    with pytest.raises(ValueError, match='at most31'):
        windows.curve_bounds(params | {'start_date': '20260101'})


def test_shibor_source_change_anomaly_is_retained_without_rejecting_fixings():
    rows = [{'日期': day, **{t+'-定价': rate for t in rates.TENORS},
             **{t+'-涨跌幅': change for t in rates.TENORS}}
            for day, rate, change in [('2026-10-27', '1.50', '0'), ('2026-10-28', '1.51', '999')]]
    result = rates.validate(rows, {'function': 'macro_china_shibor_all'}, '2026-10-29', numeric)
    assert not result['gaps'] and len(result['scale_disagreements']) == 8
    assert rows[-1]['O/N-涨跌幅'] == '999'
    assert result['validated_fields'] == [t+'-定价' for t in rates.TENORS]
    rows[-1]['O/N-定价'] = None
    assert rates.validate(rows, {'function': 'macro_china_shibor_all'}, '2026-10-29', numeric)['gaps']


@pytest.mark.parametrize('row,context,count', [(110, {'broadcast_date': '20261028'}, 1),
    (173, {}, 1), (190, {}, 1), (191, {'margin_date': '20261028'}, 1),
    (193, {}, 1), (194, {}, 1), (228, {'source_page': 1}, 1)])
def test_reviewed_current_windows_preview_does_not_need_all_history(tmp_path, row, context, count):
    p = tmp_path/'profile.json'
    p.write_text(json.dumps({'schema': 'ad.free-update-manual.v1', 'root': 'data', 'rows': [row], 'context': context}))
    root, plan, _ = manual.prepare(p, as_of='2026-10-29')
    assert len(plan['jobs']) == count and not root.exists()
    if row == 194:
        assert plan['jobs'][0]['function'] == 'macro_china_shibor_all'


def test_manual_source_date_and_page_cannot_silently_change_scope(tmp_path):
    p = tmp_path/'profile.json'
    data = {'schema': 'ad.free-update-manual.v1', 'root': 'data', 'rows': [110], 'context': {'broadcast_date': '20261030'}}
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='source date'):
        manual.prepare(p, as_of='2026-10-29')
    data.update(rows=[228], context={'source_page': 2})
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='first source page'):
        manual.prepare(p, as_of='2026-10-29')
    assert not (Path(tmp_path)/'data').exists()
