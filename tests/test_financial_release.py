"""Release-bound accounting, identity, correction and consumer behavior."""

from copy import deepcopy
import gzip
import json
import shutil
import sqlite3

import pytest
from test_official_expansion import reviewed_fixture as make_reviewed_fixture

from ashare_data.core.model import DataError
from ashare_data.financials import expansion as e
from ashare_data.financials import tables as t
from ashare_data.product.financial_release import FinancialTables
from ashare_data.storage.financial_release import (
    TableBuilder, compose, prepare, table_views, validate_tables, verify_object,
)
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, canonical, connection


@pytest.fixture
def official_reviewed(tmp_path):
    return make_reviewed_fixture.__wrapped__(tmp_path)


def market(root, aliases=()):
    with ReleaseWriter(root, free_reserve_bytes=0) as w:
        p = root / "work/meta.sqlite3"
        db = sqlite3.connect(p)
        db.executescript("""
          CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
          CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
          INSERT INTO release_instruments VALUES(17,'600000.XSHG','银行');
          INSERT INTO release_identifiers VALUES('600000.XSHG',17);
          CREATE TABLE source_assets(asset_id TEXT PRIMARY KEY,body TEXT);
          CREATE TABLE data_batches(batch_id INTEGER PRIMARY KEY,batch_key TEXT NOT NULL,
            release_id INTEGER NOT NULL,parser_sha256 TEXT NOT NULL,mapping_sha256 TEXT NOT NULL,body TEXT NOT NULL);
          CREATE TABLE batch_inputs(batch_id INTEGER,asset_id TEXT,PRIMARY KEY(batch_id,asset_id));
        """)
        db.executemany("INSERT INTO release_identifiers VALUES (?,17)", [(s,) for s in aliases])
        db.commit()
        db.close()
        c = connection()
        f = w.parquet(c, """SELECT 1::INTEGER series_id,17::INTEGER instrument_id,
          date '2024-04-30' trade_date,10::DECIMAL(18,6) close_price,1::INTEGER batch_id,
          '1' tradestatus,'0' is_st""", [], "bar")
        c.close()
        f.update(table="bars_1d", series_id=1, instrument_id=17, rows=1,
                 first_date="2024-04-30", last_date="2024-04-30", duplicate_keys=0)
        w.publish(dict(files=[f], metadata=w.object(p, "sqlite3"), evidence=[],
                       input_dispositions={"market": {"input_rows": 1, "adopted": 1}}), expected_parent=None)
    return MarketRelease(root)


def report(value="1234567890123456789.000000001"):
    return dict(schema="ad.financial-source-report.v1", source="sina", security="600000.XSHG",
                statement="lrb", period_start="2024-01-01", period_end="2024-03-31",
                basis="YTD", scope="consolidated", currency="CNY", announcement_date="2024-04-20",
                source_revision_id="original", version_kind="provider_snapshot",
                fields=[dict(source_field="P", source_title="利润", ordinal=0, raw_value=value,
                             value=value, state="source_observed"),
                        dict(source_field="R", source_title="收入", ordinal=1, raw_value="0",
                             value="0", state="source_observed"),
                        dict(source_field="unknown", source_title="未知单位", ordinal=2, raw_value="1.25",
                             value="1.25", state="source_observed"),
                        dict(source_field="empty", source_title="空字符串", ordinal=3, raw_value="",
                             value=None, state="source_missing"),
                        dict(source_field="null", source_title="空值", ordinal=4, raw_value=None,
                             value=None, state="source_missing")],
                metrics={"net_profit": dict(source_field="P", source_ordinal=0, value=value, unit="CNY"),
                         "operating_revenue": dict(source_field="R", source_ordinal=1, value="0", unit="CNY")},
                source_locator="report/20240331")


def batch(root, rows, *, state="complete", parser="parser1", capture="2026-10-19T12:00:00+00:00",
          symbol="sh600000"):
    root.mkdir(parents=True)
    for name in ("receipts", "raw", "normalized"):
        (root / name).mkdir()
    (root / "writer.lock").touch()
    task = dict(id="source-task", symbol=symbol, kind="sina_income", ipo="1999-01-01", out=None)
    tasks = canonical([task])
    universe = canonical({"instruments": []})
    (root / "tasks.json").write_bytes(tasks)
    (root / "universe.json").write_bytes(universe)
    (root / "manifest.json").write_bytes(canonical(dict(schema=e.SCHEMA, tasks_sha256=e.digest(tasks),
                                                      universe_sha256=e.digest(universe),
                                                      target_start="2010-01-01", target_end="2026-10-19")))
    reports = {}
    for row in rows:
        if row["schema"] == "ad.financial-source-report.v1":
            reports[row["period_end"].replace("-", "")] = dict(data=[
                {"item_field": x["source_field"], "item_title": x["source_title"], "item_value": x["raw_value"]}
                for x in row["fields"]])
    raw = canonical({"result": {"data": {"report_list": reports}}, "other_source_records": rows})
    raw_item = e.artifact(root, "raw/source.json.gz", gzip.compress(raw, mtime=0))
    records = [dict(row, response_sha256=e.digest(raw), observed_at=capture) for row in rows]
    normalized = e.artifact(root, "normalized/source.jsonl.gz",
                            gzip.compress(b"".join(canonical(row) + b"\n" for row in records), mtime=0))
    receipt = dict(task_id=task["id"], state=state, observed_at=capture, raw=raw_item,
                   normalized=normalized, row_count=len(rows), normalizer_sha256=parser,
                   attempts=[{"response_sha256": e.digest(raw)}], coverage={"absence_proved": False})
    (root / "receipts/source-task.json").write_bytes(canonical(receipt))
    return root


def test_lossless_long_tables_idempotent_reimport_and_shared_release(tmp_path):
    parent = market(tmp_path / "market")
    source = batch(tmp_path / "source", [report()])
    output = tmp_path / "candidate"
    value = prepare(parent, output, [source])
    assert prepare(parent, output, [source])["candidate_id"] == value["candidate_id"]
    assert value["validation"]["input_rows"] == 1
    assert value["validation"]["table_rows"]["financial.observations"] == 5
    candidate = FinancialTables.prepared(output, parent)
    with candidate.sql(market_tables=("bars_1d",), securities=["600000.XSHG"]) as c:
        rows = c.execute("""SELECT source_field,raw_value_text,raw_value_type,
           value_decimal_text,canonical_value_decimal_text FROM financial.observations
           ORDER BY source_ordinal""").fetchall()
        assert rows[0][3:] == ("1234567890123456789.000000001",) * 2
        assert rows[2][3:] == ("1.25", None)
        assert rows[3][1:3] == ("", "string")
        assert rows[4][1:3] == ("null", "null")
        assert c.execute("SELECT count(*) FROM reference.daily_attached_status").fetchone()[0] == 1
        assert c.execute("""SELECT count(*) FROM financial.report_versions r
           JOIN reference.issuer_instruments ii USING(issuer_id)
           JOIN reference.instruments i USING(instrument_id)
           JOIN market.bars_1d b USING(instrument_id)""").fetchone()[0] == 1
    assert candidate.point("600000.XSHG", "net_profit", "2024-03-31", source="sina",
                           as_of="2024-04-30T00:00:00+08:00")["status"] == "visibility_unproven"
    q = candidate.point("600000.XSHG", "net_profit", "2024-03-31", source="sina",
                        as_of="2026-10-20T00:00:00+08:00", mode="source_research")
    assert q["status"] == "ok" and q["value"] == rows[0][3]
    assert candidate.common_indicator("600000.XSHG", "netprofit_margin", "2024-03-31",
               source="sina", as_of="2026-10-20T00:00:00+08:00",
               mode="source_research")["status"] == "undefined_zero_denominator"
    old_manifest = deepcopy(parent.manifest)
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        manifest = compose(writer, parent, output, expected_parent_sha256=parent.sha256)
        writer.publish(manifest, expected_parent=parent.release_id)
    published = MarketRelease(parent.root)
    assert published.manifest["files"] == old_manifest["files"]
    assert parent.sha256 == MarketRelease(parent.root, 1).sha256
    assert FinancialTables(published).point("600000.XSHG", "net_profit", "2024-03-31", source="sina",
                as_of="2026-10-20T00:00:00+08:00", mode="source_research")["value"] == q["value"]
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        assert compose(writer, published, output, expected_parent_sha256=published.sha256) is None


def test_recapture_and_reparse_do_not_create_report_versions(tmp_path):
    parent = market(tmp_path / "market")
    a = batch(tmp_path / "a", [report()])
    b = batch(tmp_path / "b", [report()], capture="2026-10-20T12:00:00+00:00")
    c = batch(tmp_path / "c", [report()], parser="parser2")
    result = prepare(parent, tmp_path / "candidate", [a, b, c])
    counts = result["validation"]["table_rows"]
    assert counts["financial.report_versions"] == 1
    assert counts["financial.extractions"] == 2
    assert counts["financial.disclosure_events"] == 1
    assert counts["meta.financial_captures"] == 3
    assert counts["financial.observations"] == 10


def test_interrupted_import_resumes_committed_shards_without_double_counting(tmp_path, monkeypatch):
    parent = market(tmp_path / "market")
    source = batch(tmp_path / "source", [report()])
    tasks = json.loads((source / "tasks.json").read_bytes())
    tasks.append(dict(tasks[0], id="second-task"))
    (source / "tasks.json").write_bytes(canonical(tasks))
    manifest = json.loads((source / "manifest.json").read_bytes())
    manifest["tasks_sha256"] = e.digest(canonical(tasks))
    (source / "manifest.json").write_bytes(canonical(manifest))
    receipt = json.loads((source / "receipts/source-task.json").read_bytes())
    (source / "receipts/second-task.json").write_bytes(canonical(dict(receipt, task_id="second-task")))

    flush, calls = TableBuilder.flush, []

    def interrupted(self, *args):
        calls.append(None)
        if len(calls) == 2:
            raise RuntimeError("simulated interruption after first durable shard")
        return flush(self, *args)

    output = tmp_path / "resumed"
    with monkeypatch.context() as scoped:
        scoped.setattr(TableBuilder, "flush", interrupted)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            with TableBuilder(parent, output, rows_per_shard=1) as builder:
                builder.import_batch(source)
    progress = json.loads((output / "BUILD-PROGRESS.json").read_bytes())
    assert progress["completed_tasks"] == 1
    with TableBuilder(parent, output, rows_per_shard=1) as builder:
        result = builder.finish([builder.import_batch(source)])
    clean = prepare(parent, tmp_path / "clean", [source])
    assert result["validation"] == clean["validation"]
    assert result["validation"]["input_rows"] == 2
    assert result["validation"]["table_rows"]["financial.report_versions"] == 1
    assert result["validation"]["table_rows"]["meta.financial_captures"] == 2


def test_unknown_versions_are_not_sorted_by_capture_clock(tmp_path):
    parent = market(tmp_path / "market")
    changed = report("9")
    changed["source_revision_id"] = "second-version"
    a = batch(tmp_path / "a", [report()])
    b = batch(tmp_path / "b", [changed])
    prepare(parent, tmp_path / "candidate", [a, b])
    q = FinancialTables.prepared(tmp_path / "candidate", parent).point(
        "600000.XSHG", "net_profit", "2024-03-31", source="sina",
        as_of="2026-10-20T00:00:00+08:00", mode="source_research")
    assert q["status"] == "conflict" and q["value"] is None


def test_partial_and_unparsed_records_remain_explicit(tmp_path):
    parent = market(tmp_path / "market")
    share = dict(schema="ad.corporate-source-observation.v1", category="shares", source="sina_structure",
                 security="600000.XSHG", source_revision_id="share",
                 observation={"date": "2024-04-01", "fields": {"total_shares": None, "float_a_shares": "100"},
                              "field_gaps": ["negative_source_share_count"], "native_fields": {"总股本": "-1 万股"}})
    unknown = dict(schema="ad.unknown.v1", source="new", security="600000.XSHG")
    source = batch(tmp_path / "source", [share, unknown], state="partial_source")
    result = prepare(parent, tmp_path / "candidate", [source])
    assert result["validation"]["input_rows"] == 2
    assert result["validation"]["quarantined_rows"] == 1
    assert result["validation"]["table_rows"]["reference.share_observations"] == 2
    with FinancialTables.prepared(tmp_path / "candidate", parent).sql() as c:
        assert c.execute("""SELECT value_decimal_text FROM reference.share_observations
           WHERE field='total_shares'""").fetchone()[0] is None
        assert c.execute("SELECT source_state FROM meta.financial_coverage").fetchone()[0] == "partial_source"


def test_reference_queries_require_capture_and_exact_date_and_keep_conflicts(tmp_path):
    parent = market(tmp_path / "market")
    share = dict(schema="ad.corporate-source-observation.v1", category="shares", source="sina_structure",
                 security="600000.XSHG", source_revision_id="first",
                 observation={"date": "2024-04-01",
                              "fields": {"total_shares": None, "float_a_shares": "100"},
                              "field_gaps": ["negative_source_share_count"],
                              "native_fields": {"总股本": "-1 万股"}})
    source = batch(tmp_path / "source", [share])
    candidate = tmp_path / "candidate"
    prepare(parent, candidate, [source])
    reader = FinancialTables.prepared(candidate, parent)
    query = dict(source="sina_structure", security="600000.XSHG", field="float_a_shares")
    before = reader.reference_point("share", "2024-04-01",
                                    as_of="2026-10-19T11:59:59+00:00", **query)
    assert before["value"] is None
    assert before["reasons"] == ["evidence_not_captured_at_cutoff"]
    after = reader.reference_point("share", "2024-04-01",
                                   as_of="2026-10-19T12:00:00+00:00", **query)
    assert after["status"] == "ok" and after["value"] == "100"
    assert after["carry_forward_authorized"] is False
    absent = reader.reference_point("share", "2024-04-02",
                                    as_of="2026-10-20T00:00:00+00:00", **query)
    assert absent["status"] == "missing" and absent["value"] is None
    query["field"] = "total_shares"
    negative = reader.reference_point("share", "2024-04-01",
                                      as_of="2026-10-20T00:00:00+00:00", **query)
    assert negative["value"] is None and negative["status"] == "missing"

    different = deepcopy(share)
    different["source_revision_id"] = "second"
    different["observation"]["fields"]["float_a_shares"] = "101"
    changed = batch(tmp_path / "changed", [different], capture="2026-10-20T12:00:00+00:00")
    prepare(parent, tmp_path / "conflict", [source, changed])
    conflict = FinancialTables.prepared(tmp_path / "conflict", parent).reference_point(
        "share", "2024-04-01", source="sina_structure", security="600000.XSHG",
        field="float_a_shares", as_of="2026-10-21T00:00:00+00:00")
    assert conflict["status"] == "conflict" and conflict["value"] is None


def test_reference_units_are_separate_from_tax_and_factor_basis(tmp_path):
    parent = market(tmp_path / "market")
    action = dict(schema="ad.tushare-dividend-version.v1", source="tushare", security="600000.XSHG",
                  source_revision_id="distribution", source_status="实施", implemented=True, fields={
                      "ex_date": {"value": "2024-04-01"},
                      "cash_div_tax": {"value": "1.2", "unit": "CNY/eligible_share", "tax_basis": "gross_pre_tax"},
                      "cash_div": {"value": "0.96", "unit": "CNY/eligible_share",
                                   "tax_basis": "after_tax_source_claimed_not_investor_specific"},
                      "base_share": {"value": "1000000", "unit": "shares"},
                  })
    factor = dict(schema="ad.tushare-factor-observation.v1", source="tushare", security="600000.XSHG",
                  source_revision_id="factor", trade_date="2024-04-01",
                  fields={"adj_factor": {"value": "12.3"}},
                  basis="tushare_dividend_reinvestment_provider_factor")
    source = batch(tmp_path / "source", [action, factor])
    prepare(parent, tmp_path / "candidate", [source])
    reader = FinancialTables.prepared(tmp_path / "candidate", parent)
    query = dict(source="tushare", security="600000.XSHG", as_of="2026-10-20T00:00:00+00:00")
    gross = reader.reference_point("corporate_action", "2024-04-01", field="gross_cash", **query)
    net = reader.reference_point("corporate_action", "2024-04-01", field="net_cash", **query)
    assert gross["value"] == "1.2" and gross["unit"] == "CNY/eligible_share"
    assert gross["tax_basis"] == "gross_pre_tax"
    assert net["value"] == "0.96" and net["unit"] == "CNY/eligible_share"
    assert net["tax_basis"] == "after_tax_source_claimed_not_investor_specific"
    adjusted = reader.reference_point("factor", "2024-04-01", **query)
    assert adjusted["value"] == "12.3" and adjusted["unit"] == "ratio"
    assert adjusted["factor_basis"] == factor["basis"]


def test_instrument_aliases_do_not_carry_source_financials_or_shares_across_codes(tmp_path):
    parent = market(tmp_path / "market", aliases=["600001.XSHG"])
    share = dict(schema="ad.corporate-source-observation.v1", category="shares", source="sina_structure",
                 security="600000.XSHG", source_revision_id="share",
                 observation={"date": "2024-04-01", "fields": {"float_a_shares": "100"}})
    original = batch(tmp_path / "original", [report(), share])
    prepare(parent, tmp_path / "one-code", [original])
    reader = FinancialTables.prepared(tmp_path / "one-code", parent)
    query = dict(source="sina", as_of="2026-10-20T00:00:00+00:00", mode="source_research")
    assert reader.point("600001.XSHG", "net_profit", "2024-03-31", **query)["status"] == "missing"
    assert reader.reference_point("share", "2024-04-01", source="sina_structure",
                                  as_of=query["as_of"], security="600001.XSHG",
                                  field="float_a_shares")["value"] is None
    different = dict(report("9"), security="600001.XSHG")
    second = batch(tmp_path / "second", [different], symbol="sh600001")
    prepare(parent, tmp_path / "both-codes", [original, second])
    reader = FinancialTables.prepared(tmp_path / "both-codes", parent)
    assert reader.point("600000.XSHG", "net_profit", "2024-03-31", **query)["value"] == report()["fields"][0]["value"]
    assert reader.point("600001.XSHG", "net_profit", "2024-03-31", **query)["value"] == "9"


def test_preparation_rejects_active_incomplete_and_corrupt_sources(tmp_path):
    import fcntl
    parent = market(tmp_path / "market")
    source = batch(tmp_path / "source", [report()])
    with (source / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DataError, match="still being written"):
            prepare(parent, tmp_path / "active", [source])
    receipt_path = source / "receipts/source-task.json"
    r = json.loads(receipt_path.read_text())
    r["state"] = "running"
    receipt_path.write_bytes(canonical(r))
    with pytest.raises(DataError, match="Pending or running"):
        prepare(parent, tmp_path / "pending", [source])
    r["state"] = "complete"
    receipt_path.write_bytes(canonical(r))
    (source / r["raw"]["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        prepare(parent, tmp_path / "bad", [source])


def test_context_dimensions_are_order_independent_and_typed():
    a = report()
    a["dimensions"] = {"b": {"type": "number", "value": "2"}, "a": {"type": "string", "value": "x"}}
    b = deepcopy(a)
    b["dimensions"] = dict(reversed(list(a["dimensions"].items())))
    assert t.context(a, "issuer", "CNY")[0] == t.context(b, "issuer", "CNY")[0]
    b["dimensions"]["a"] = "x"
    with pytest.raises(DataError, match="Untyped"):
        t.context(b, "issuer", "CNY")


def test_revision_foreign_keys_and_cycles_fail_closed(tmp_path):
    parent = market(tmp_path / "market")
    source = batch(tmp_path / "source", [report()])
    with TableBuilder(parent, tmp_path / "candidate") as builder:
        done = builder.import_batch(source)
        c = connection()
        table_views(c, done["files"], lambda x: verify_object(builder.root, x))
        try:
            c.execute("CREATE TEMP TABLE injected AS SELECT * FROM financial.links")
            c.execute("INSERT INTO injected(link_id,from_type,from_id,to_type,to_id,relation) "
                      "VALUES ('broken','report','missing','capture','absent','captured_in')")
            c.execute("CREATE OR REPLACE VIEW financial.links AS SELECT * FROM injected")
            with pytest.raises(DataError, match="endpoint"):
                validate_tables(c, builder.db)
        finally:
            c.close()


def test_reviewed_official_history_replacements_and_known_unknown_scope(official_reviewed, monkeypatch, tmp_path):
    from ashare_data.financials import official_daily
    root, review = official_reviewed
    sessions = {"XSHG": {d: {"source": "synthetic_session_evidence"}
                         for d in ("2015-04-30", "2015-06-08")}}
    monkeypatch.setattr(official_daily, "traded_sessions", lambda *a: sessions)
    review_path = root / "review.json"
    review_path.write_bytes(canonical(review))
    spec = root / "daily-spec.json"
    spec.write_bytes(canonical(dict(official_root=str(root), review=str(review_path),
                                    review_sha256=e.digest(review_path.read_bytes()),
                                    reference_root=str(root), reference_tasks=[])))
    parent = market(tmp_path / "market")
    output = tmp_path / "prepared"
    value = prepare(parent, output, [], daily_reviews=[spec])
    assert value["validation"]["table_rows"]["financial.visibility"] == 2
    reader = FinancialTables.prepared(output, parent)
    for at, value in [("2015-04-29", None), ("2015-04-30", "100.00"),
                      ("2015-06-07", "100.00"), ("2015-06-08", "90.00"), ("2015-07-24", None)]:
        result = reader.point("600000.XSHG", "net_operate_cash_flow", "2015-03-31",
                              source="cninfo", as_of=at + "T00:00:00+08:00", mode="official_daily")
        assert result["value"] == value, result
    assert reader.point("600000.XSHG", "net_operate_cash_flow", "2015-03-31",
                        source="cninfo", as_of="2015-06-08T00:00:00+08:00")["value"] is None
    with reader.sql() as c:
        assert c.execute("SELECT count(*) FROM financial.links WHERE relation='replaces'").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM financial.links WHERE relation='unresolved_revision'").fetchone()[0] == 2


@pytest.mark.parametrize("verify_unchanged", [False, True])
def test_later_market_publication_cannot_drop_financial_tables(tmp_path, verify_unchanged):
    parent = market(tmp_path / "market")
    source = batch(tmp_path / "source", [report()])
    output = tmp_path / "candidate"
    prepare(parent, output, [source])
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        manifest = compose(writer, parent, output, expected_parent_sha256=parent.sha256)
        writer.publish(manifest, expected_parent=1)
    published = MarketRelease(parent.root)
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        invalid = dict(published.manifest, evidence=[])
        with pytest.raises(DataError, match="table set"):
            writer.publish(invalid, expected_parent=2, verify_unchanged=verify_unchanged)
        invalid.pop("financial_reference_tables")
        invalid.pop("financial_table_validation")
        with pytest.raises(DataError, match="financial contract"):
            writer.publish(invalid, expected_parent=2, verify_unchanged=verify_unchanged)
        forgotten = deepcopy(published.manifest)
        forgotten["financial_reference_tables"]["candidate_ids"] = []
        with pytest.raises(DataError, match="candidate history"):
            writer.publish(forgotten, expected_parent=2, verify_unchanged=verify_unchanged)
        for index, statement in enumerate((
            "DROP TABLE release_issuer_instruments",
            "DELETE FROM source_assets",
            "UPDATE release_identifiers SET instrument_id=99",
        )):
            revised = parent.root / "work" / ("invalid-financial-metadata-" + str(index) + ".sqlite3")
            shutil.copyfile(published.metadata_path, revised)
            with sqlite3.connect(revised) as db:
                db.execute(statement)
            invalid = dict(published.manifest, metadata=writer.object(revised, "sqlite3"))
            with pytest.raises(DataError, match="financial metadata or identifier|financial metadata table"):
                writer.publish(invalid, expected_parent=2, verify_unchanged=verify_unchanged)
        writer.publish(published.manifest, expected_parent=2, verify_unchanged=verify_unchanged)
    assert MarketRelease(parent.root).financial().point("600000.XSHG", "net_profit", "2024-03-31",
               source="sina", as_of="2026-10-20T00:00:00+08:00", mode="source_research")["status"] == "ok"
