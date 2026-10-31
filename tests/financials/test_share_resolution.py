"""Synthetic negative source cells and inert chart evidence; never live files."""

import json
import sqlite3

import pytest

from ashare_data.corporate_actions.share_charts import chart_observation, structure_charts
from ashare_data.financials import expansion as e
from ashare_data.financials.official_daily import SnapshotReader, SourceUseDenied
from ashare_data.financials.share_resolution import ShareResolutionReader, listing_class, prepare

SECURITY = "001226.XSHE"
SYMBOL = "sz001226"
TABLE = "StockStructureNewTable1"
XML = "<graph><set name='限售A股' value='80'/><set name='流通A股' value='20'/></graph>"


def page(xml=XML, embed=None, anchor="20200101", unit="万股"):
    dates = ["20100101", "20200101", "20210101"]
    rows = [["变动日期", *dates], ["公告日期", "20100102", "20200102", "20210102"],
            ["变动原因", *["定期报告"] * 3], ["总股本", *[f"100 {unit}"] * 3],
            ["流通A股", *[f"-10 {unit}"] * 3], ["限售A股", *[f"80 {unit}"] * 3],
            ["高管股", *[f"30 {unit}"] * 3]]
    charts = []
    for day in dates:
        link_day = anchor if day == "20200101" else day
        charts.append(f'<param name="FlashVars" value="&dataXML={xml}">'
                      f'<embed FlashVars="&dataXML={embed if embed is not None else xml}">'
                      f'<a href="/corp/go.php/vCI_StockStructurePicture/stockid/001226/alertdate/{link_day}.phtml">图</a>')
    rows.append(["股本结构图", *charts])
    return ('<script>var page_symbol = "sz001226";</script><table id="' + TABLE + '">' +
            ''.join('<tr>' + ''.join('<td>' + cell + '</td>' for cell in row) + '</tr>' for row in rows) +
            '</table>').encode("gb18030")


def chart(raw):
    return chart_observation(structure_charts(raw, SECURITY), SECURITY, TABLE, 2, "2020-01-01")


def test_chart_reads_explicit_positive_observation_without_repairing_negative_table():
    result = chart(page())
    assert result["value"] == "200000" and result["table_float"] == "-10 万股"
    assert result["class_values"] == {"限售A股": "800000", "流通A股": "200000"}
    assert result["unit_evidence"] == [dict(label="限售A股", table_value="80 万股", chart_value="80")]


@pytest.mark.parametrize("change", [
    dict(xml=XML.replace("</graph>", "<set name='流通A股' value='20'/></graph>")),
    dict(embed=XML.replace("value='20'", "value='21'")),
    dict(xml=XML.replace("<set name='流通A股' value='20'/>", "")),
    dict(unit="股"), dict(anchor="20200102"),
    dict(xml=XML.replace("value='20'", "value='-20'")),
    dict(xml=XML.replace("value='20'", "value='22'")),
    dict(xml="<!DOCTYPE graph [<!ENTITY x '20'>]>" + XML),
    dict(xml=XML.replace("</graph>", "")),
])
def test_chart_rejects_unbound_ambiguous_or_inconsistent_values(change):
    with pytest.raises(ValueError):
        chart(page(**change))


def test_h_share_double_count_is_not_repaired_by_total_minus_restricted():
    raw = page(XML.replace("</graph>", "<set name='境外上市外资股' value='30'/></graph>"))
    raw = raw.replace(b"</table>", '<tr><td>境外上市外资股</td><td>30 万股</td><td>30 万股</td><td>30 万股</td></tr></table>'.encode("gb18030"))
    with pytest.raises(ValueError, match="partition"):
        chart(raw)


@pytest.fixture
def bundle(tmp_path):
    db = tmp_path / "daily.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE instruments(symbol,ipo,out,board,metadata_source)")
        con.execute("INSERT INTO instruments VALUES(?, '2012-01-01', NULL, 'main', 'szse')", (SYMBOL,))
        con.execute("CREATE TABLE code_events(id,old_symbol,new_symbol,effective_date)")
        con.execute("CREATE TABLE metadata(key,value)")
    structure, floats = tmp_path / "structure", tmp_path / "float"
    for root, kind, raw in (
        (structure, "sina_structure", page()),
        (floats, "sina_float", b'var KKE_ShareAmount_sz001226=([{"date":"2010-01-01","amount":20},{"date":"2020-01-01","amount":20}]);'),
    ):
        e.prepare(root, db, "2010-01-01", "2026-10-19", kinds=[kind])
        result = e.run(root, interval=0.001, minimum_interval=0.001,
                       fetcher=lambda url, raw=raw: (200, raw, {}), retain_temporaries=True)
        assert result["all_tasks_have_source_results"]
    output = tmp_path / "resolution"
    manifest = prepare(output, structure, floats)
    return output, structure, floats, manifest


def test_same_date_two_representations_are_explicit_point_resolution(bundle):
    root, structure, floats, manifest = bundle
    assert manifest["classification"] == {"before_listing": 1, "after_listing": 2}
    assert manifest["states"] == {"before_listing": 1, "source_value_recovered": 1, "unresolved": 1}
    result = ShareResolutionReader(root).read(SECURITY, "2020-01-01", purpose="listed_security", as_of="2099-01-01T00:00:00+00:00")
    assert result["value"] == "200000"
    assert result["original_table_value_unchanged"] and not result["historical_pit_certified"]
    original = SnapshotReader(structure).read("sina_structure-" + SYMBOL, locator=TABLE + "/column/2", purpose="source_audit")
    assert original["source_record"]["observation"]["fields"]["float_a_shares"] is None
    with pytest.raises(ValueError, match="new resolution"):
        prepare(root, structure, floats)


@pytest.mark.parametrize("day,purpose,as_of", [
    ("2010-01-01", "listed_security", "2099-01-01T00:00:00+00:00"),
    ("2020-01-01", "historical_pit", "2099-01-01T00:00:00+00:00"),
    ("2020-01-01", "listed_security", "2021-01-01T00:00:00+00:00"),
    ("2020-01-02", "listed_security", "2099-01-01T00:00:00+00:00"),
    ("2021-01-01", "listed_security", "2099-01-01T00:00:00+00:00"),
])
def test_resolution_does_not_grant_lifetime_historical_or_carry_forward_access(bundle, day, purpose, as_of):
    with pytest.raises(SourceUseDenied):
        ShareResolutionReader(bundle[0]).read(SECURITY, day, purpose=purpose, as_of=as_of)


def test_resolution_rechecks_source_receipt_identity(bundle):
    reader = ShareResolutionReader(bundle[0])
    path = bundle[2] / "receipts" / ("sina_float-" + SYMBOL + ".json")
    receipt = json.loads(path.read_text())
    receipt["revision_note"] = "changed"
    path.write_bytes(e.encoded(receipt))
    with pytest.raises(ValueError, match="receipt changed"):
        reader.read(SECURITY, "2020-01-01", as_of="2099-01-01T00:00:00+00:00")


def test_listing_class_requires_official_code_lifetime_and_respects_code_events():
    task = dict(symbol=SYMBOL, ipo="2012-01-01", out="2024-01-01")
    instrument = task | dict(metadata_source="szse")
    assert listing_class(task, instrument, "2010-01-01", []) == "before_listing"
    assert listing_class(task, instrument, "2020-01-01", []) == "after_listing"
    assert listing_class(task, instrument, "2024-01-01", []) == "identity_unresolved"
    assert listing_class(task, task, "2020-01-01", []) == "identity_unresolved"
    event = dict(old_symbol=SYMBOL, new_symbol="sz001999", effective_date="2019-01-01")
    assert listing_class(task, instrument, "2020-01-01", [event]) == "identity_unresolved"
