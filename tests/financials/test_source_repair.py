"""Synthetic cached responses only; recovery must never need a source request."""

from datetime import datetime
import gzip
import json
import sqlite3

import pytest

from ashare_data.financials import source_repair as follow
from ashare_data.financials import expansion as e
from ashare_data.financials import official_expansion as o
from ashare_data.financials import source_repair as repair
from ashare_data.financials.official_daily import SnapshotReader, SourceUseDenied


SYMBOL = "sh600000"
STAMP = "2026-10-19T00:00:00+00:00"


def row(aid="a", **extra):
    return dict(secCode="600000", orgId="synthetic", announcementId=aid,
                announcementTitle="2024年第一季度报告",
                announcementTime=int(datetime.fromisoformat("2024-04-30T00:00:00+08:00").timestamp() * 1000),
                adjunctUrl=aid + ".pdf") | extra


class Cache:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.scope = dict(instruments=[
            dict(symbol=SYMBOL, ipo="1999-01-01", out=None),
            dict(symbol="sh601975", ipo="2019-01-08", out=None),
        ], document_symbols=[])
        scope = e.encoded(self.scope)
        e.atomic(root / "scope.json", scope)
        e.atomic(root / "manifest.json", e.encoded(dict(scope_sha256=e.digest(scope),
            target_start="2010-01-01", target_end="2026-10-19")))
        self.capture("identity-" + SYMBOL, [dict(code="600000", category="A股", orgId="synthetic")],
                     o.ORIGIN + "/new/information/topSearch/query", dict(keyWord="600000", maxNum=30))
        for kind in repair.QUERIES:
            self.page(kind, 1, 0, False, [])
        e.atomic(root / "securities" / (SYMBOL + ".json"), e.encoded(dict(state="parse_failed", reason="synthetic old parser")))

    def capture(self, key, payload, url, form):
        raw = e.encoded(payload)
        e.atomic(self.root / "raw" / (key + ".gz"), gzip.compress(raw, mtime=0))
        e.atomic(self.root / "receipts" / (key + ".json"), e.encoded(dict(state="complete",
            path="raw/" + key + ".gz", sha256=e.digest(raw), bytes=len(raw), observed_at=STAMP,
            request=dict(url=url, form=form, authenticated=False))))

    def page(self, kind, number, total, more, rows, tie=None):
        key = "index-" + SYMBOL + "-" + kind + "-" + ("tie-" + tie if tie else f"{number:03}")
        form = dict(pageNum=number, pageSize=30, column="sse", plate="sh", tabName="fulltext",
            stock="600000,synthetic", searchkey={"periodic":"", "correction":"更正", "revision":"修订"}[kind],
            secid="", category=o.CATEGORIES if kind == "periodic" else "", trade="",
            seDate=tie + "~" + tie if tie else "2010-01-01~2026-10-19", sortName="time", sortType="asc", isHLtitle="false")
        self.capture(key, dict(totalAnnouncement=total, hasMore=more, announcements=rows),
                     o.ORIGIN + "/new/hisAnnouncement/query", form)


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "official")


def test_original_query_cache_is_reused_without_transport(cache):
    def forbidden_transport(*args):
        raise AssertionError("Existing query identities must reuse their original responses")

    client = o.Client(cache.root, transport=forbidden_transport)
    result = o.index_security(client, SYMBOL, repair.read(cache.root / "manifest.json"))
    assert client.calls == 0
    assert result["announcements"] == []
    assert all(query["pagination_complete_for_query"] for query in result["queries"])


def test_count_drift_keeps_cached_rows_without_claiming_completeness(cache):
    cache.page("periodic", 1, 3, True, [row("a")])
    cache.page("periodic", 2, 4, True, [row("b")])
    result = repair.recover_index(cache.root, SYMBOL)
    assert len(result["announcements"]) == 2
    assert not result["query_set_complete"]
    assert {g["reason"] for g in result["queries"][0]["gaps"]} >= {"source_count_drift", "missing_or_nonterminal_pages"}
    assert result["network_requests"] == 0 and not result["historical_pit_certified"]


@pytest.mark.parametrize("complete", [True, False])
def test_multi_page_same_day_union_is_proven_from_cached_rows(cache, complete):
    cache.page("periodic", 1, 33, True, [row(str(i)) for i in range(30)])
    cache.page("periodic", 2, 33, False, [row(str(i)) for i in (29, 30, 31)])
    cache.page("periodic", 1, 33, True, [row(str(i)) for i in list(range(29)) + [32 if complete else 29]], tie="2024-04-30")
    result = repair.recover_index(cache.root, SYMBOL)
    assert result["query_set_complete"] is complete
    assert len(result["announcements"]) == (33 if complete else 32)
    observed = next(r for r in result["announcements"] if r["announcement_id"] == "0")
    assert len(observed["source_occurrences"]) == 2
    assert result["queries"][0]["tie_date_repairs"][0]["complete_from_cached_union"] is complete


def test_ambiguous_id_and_invalid_rows_only_quarantine_affected_observations(cache):
    cache.page("periodic", 1, 6, False, [row("a"), row("a", adjunctUrl="changed.pdf"), row("b"),
        row("wrong-issuer", orgId="other"), None, row("unknown-code", secCode="601999")])
    result = repair.recover_index(cache.root, SYMBOL)
    assert [r["announcement_id"] for r in result["announcements"]] == ["b"]
    assert list(result["conflicting_versions"]) == ["a"]
    assert len(result["quarantined_rows"]) == 3
    assert not result["query_set_complete"]


def test_shared_org_routes_by_returned_code_without_merging_listing_history(cache):
    cache.page("periodic", 1, 2, False, [row("a"), row("b", secCode="601975")])
    result = repair.recover_index(cache.root, SYMBOL)
    assert len(result["announcements"]) == len(result["related_code_announcements"]) == 1
    related = result["related_code_announcements"][0]
    assert related["source_security"] == "601975.XSHG"
    assert related["requested_security"] == "600000.XSHG"
    assert not related["issuer_continuity_proven"] and not related["historical_code_intervals_merged"]
    cache.page("periodic", 1, 1, False, [row("b", secCode="601975", announcementTime=1293836400000)])
    result = repair.recover_index(cache.root, SYMBOL)
    assert not result["related_code_announcements"]
    assert result["quarantined_rows"][0]["reason"] == "returned_code_lifetime_unverified"


@pytest.mark.parametrize("tamper", ["raw", "request", "scope"])
def test_changed_cached_evidence_is_rejected(cache, tamper):
    if tamper == "raw":
        (cache.root / "raw/identity-sh600000.gz").write_bytes(gzip.compress(b"[]"))
    elif tamper == "scope":
        (cache.root / "scope.json").write_text("{}")
    else:
        path = cache.root / "receipts/index-sh600000-periodic-001.json"
        receipt = repair.read(path)
        receipt["request"]["form"]["stock"] = "601975,synthetic"
        e.atomic(path, e.encoded(receipt))
    with pytest.raises(ValueError):
        repair.recover_index(cache.root, SYMBOL)


@pytest.fixture
def financial(tmp_path):
    db = tmp_path / "daily.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE instruments(symbol,name,ipo,out,board)")
        con.execute("INSERT INTO instruments VALUES('sh600000','synthetic','1999-01-01',NULL,'main')")
        con.execute("CREATE TABLE code_events(id,old_symbol,new_symbol,effective_date)")
        con.execute("CREATE TABLE metadata(key,value)")
    root = tmp_path / "financial"
    e.prepare(root, db, "2010-01-01", "2026-10-19", kinds=["sina_income"])
    raw = e.encoded(dict(result=dict(status=dict(code=0), data=dict(report_count=1,
        report_date=[dict(date_value="20200331")], report_list={"20200331":dict(
            rType="合并期末", rCurrency="CNY", publish_date="20200428", data=[
                dict(item_field="NETPROFIT", item_title="含少数股东损益的净利润", item_value="12.50")])}))))
    tid = "sina_income-" + SYMBOL
    receipt = dict(task_id=tid, state="parse_failed", parse_error="synthetic prior title mismatch", observed_at=STAMP,
        attempts=[dict(response_sha256=e.digest(raw))], normalized=None,
        raw=e.artifact(root, "raw/" + tid + ".json.gz", gzip.compress(raw, mtime=0)))
    e.atomic(root / "receipts" / (tid + ".json"), e.encoded(receipt))
    return root, tid


def bundle(tmp_path, financial, cache):
    root, tid = financial
    output = tmp_path / "repaired"
    repair.prepare(output, root, cache.root, [tid], [SYMBOL])
    return output


def test_independent_batch_is_readable_and_source_unchanged_then_idle_adoption_is_idempotent(tmp_path, financial, cache):
    root, tid = financial
    before = (root / "receipts" / (tid + ".json")).read_bytes()
    output = bundle(tmp_path, financial, cache)
    assert (root / "receipts" / (tid + ".json")).read_bytes() == before
    repaired = repair.read(output / "receipts" / (tid + ".json"))
    assert (output / repaired["raw"]["path"]).stat().st_ino == (root / repaired["raw"]["path"]).stat().st_ino
    result = SnapshotReader(output).read(tid, period="2020-03-31", purpose="source_audit")
    assert result["source_record"]["metrics"]["net_profit"]["value"] == "12.50"
    with repair.writer(root):
        assert repair.apply_idle(output, root)["state"] == "writer_active"
        assert (root / "receipts" / (tid + ".json")).read_bytes() == before
    assert repair.apply_idle(output, root)["applied"] == [tid]
    assert repair.apply_idle(output, root)["already_applied"] == [tid]
    repair.verify_inputs(output)
    current = repair.read(root / "receipts" / (tid + ".json"))
    current["observed_at"] = "2025-01-01T00:00:00+00:00"
    e.atomic(root / "receipts" / (tid + ".json"), e.encoded(current))
    with pytest.raises(ValueError, match="provenance"):
        repair.verify_inputs(output)


def test_official_complete_adoption_resumes_after_index_write_and_partial_stays_independent(tmp_path, financial, cache, monkeypatch):
    cache.page("periodic", 1, 1, False, [row()])
    output = bundle(tmp_path, financial, cache)
    original = repair.e.atomic
    status_path = cache.root / "securities" / (SYMBOL + ".json")
    def interrupted(path, raw):
        if path == status_path:
            raise OSError("simulated interruption after index publication")
        return original(path, raw)
    with monkeypatch.context() as scoped:
        scoped.setattr(repair.e, "atomic", interrupted)
        with pytest.raises(OSError, match="interruption"):
            repair.apply_idle(output, cache.root)
    assert repair.apply_idle(output, cache.root)["applied"] == [SYMBOL]
    assert repair.apply_idle(output, cache.root)["already_applied"] == [SYMBOL]
    assert len(repair.recovered_announcements(output, security="600000.XSHG")) == 1
    second = Cache(tmp_path / "partial")
    second.page("periodic", 1, 2, True, [row()])
    partial = tmp_path / "partial-repair"
    repair.prepare(partial, financial[0], second.root, [financial[1]], [SYMBOL])
    assert repair.apply_idle(partial, second.root)["partial_indexes_available"] == [SYMBOL]
    assert not (second.root / "indices" / (SYMBOL + ".json.gz")).exists()
    assert repair.read(second.root / "securities" / (SYMBOL + ".json"))["state"] == "parse_failed"


def test_failed_prepare_removes_staging_directory(tmp_path, financial, cache):
    with pytest.raises(ValueError, match="Unknown"):
        repair.prepare(tmp_path / "broken", financial[0], cache.root, ["missing"], [SYMBOL])
    assert not (tmp_path / "broken").exists() and not (tmp_path / "broken.preparing").exists()


def test_retained_failure_keeps_staging_for_review(tmp_path, financial, cache):
    with pytest.raises(ValueError, match="Unknown"):
        repair.prepare(tmp_path / "retained", financial[0], cache.root, ["missing"], [], retain_temporaries=True)
    assert (tmp_path / "retained.preparing").is_dir()


def test_structure_partial_cache_replay_preserves_bad_cell_original_receipt_and_active_writer(tmp_path, financial, cache):
    root = tmp_path / "structure"
    e.prepare(root, tmp_path / "daily.sqlite3", "2010-01-01", "2026-10-19", kinds=["sina_structure"])
    rows = [["变动日期", "20200101", "20210101"], ["公告日期", "20200102", "20210102"],
            ["变动原因", "定期报告", "定期报告"], ["总股本", "100 万股", "100 万股"],
            ["流通A股", "-20 万股", "30 万股"], ["限售A股", "90 万股", "70 万股"],
            ["高管股", "30 万股", "20 万股"]]
    raw = ('<script>var page_symbol = "sh600000";</script><table id="StockStructureNewTable1">' +
           ''.join('<tr>' + ''.join('<td>' + value + '</td>' for value in row) + '</tr>' for row in rows) +
           '</table>').encode("gb18030")
    tid = "sina_structure-" + SYMBOL
    receipt = dict(task_id=tid, state="parse_failed", parse_error="prior unit rejection", observed_at=STAMP,
        attempts=[dict(response_sha256=e.digest(raw))],
        raw=e.artifact(root, "raw/" + tid + ".json.gz", gzip.compress(raw, mtime=0)))
    original = e.encoded(receipt)
    path = root / "receipts" / (tid + ".json")
    e.atomic(path, original)
    output = tmp_path / "structure-repair"
    with repair.writer(root):
        result = repair.prepare(output, root, cache.root, [tid], [], retain_temporaries=True)
        assert repair.apply_idle(output, root)["state"] == "writer_active"
        assert path.read_bytes() == original
    assert result["network_requests"] == 0
    assert result["financial"][0]["new_state"] == "partial_source"
    assert (output / "source-receipts" / path.name).read_bytes() == original
    repaired = repair.read(output / "receipts" / path.name)
    assert repaired["raw"] == receipt["raw"] and repaired["attempts"] == receipt["attempts"]
    observations = [json.loads(line) for line in gzip.decompress((output / repaired["normalized"]["path"]).read_bytes()).splitlines()]
    shares = [r for r in observations if r["category"] == "shares"]
    assert [r["observation"]["fields"]["float_a_shares"] for r in shares] == [None, "300000"]
    assert shares[0]["quality_flags"] == ["source_share_fields_quarantined"]
    assert repaired["coverage"]["counts"] == dict(shares=2, unparsed_records=1)
    reader = SnapshotReader(output)
    with pytest.raises(SourceUseDenied, match="canonical_share_value_unavailable"):
        reader.read(tid, locator="StockStructureNewTable1/column/1", field="float_a_shares",
                    purpose="source_research", as_of=STAMP)
    assert reader.read(tid, locator="StockStructureNewTable1/column/1", field="total_shares",
                       purpose="source_research", as_of=STAMP)["value"] == "1000000"
    assert repair.apply_idle(output, root)["applied"] == [tid]
    assert repair.read(path)["state"] == "partial_source"
    repair.verify_inputs(output)
    (output / "source-receipts" / path.name).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="Preserved"):
        repair.verify_inputs(output)


def test_existing_followup_adopts_only_at_natural_boundary_and_checks_manifest(tmp_path, financial, cache, monkeypatch):
    output = bundle(tmp_path, financial, cache)
    spec = dict(root=str(output), manifest_sha256=e.digest((output / "repair-manifest.json").read_bytes()))
    assert follow.follow_batch(cache.root, "official", cache_repair=spec)["state"] == "waiting_for_natural_source_boundary"
    monkeypatch.setattr(follow, "official_ready", lambda _: True)
    monkeypatch.setattr(follow, "official_integrity", lambda _: dict(integrity_errors=[]))
    with repair.writer(cache.root):
        assert follow.follow_batch(cache.root, "official", cache_repair=spec)["state"] == "writer_active"
    with pytest.raises(ValueError, match="manifest changed"):
        follow.follow_batch(cache.root, "official", cache_repair=spec | dict(manifest_sha256="changed"))
    result = follow.follow_batch(cache.root, "official", cache_repair=spec)
    assert result["cache_repair"]["applied"] == [SYMBOL]
    assert result["state"] == "verified_with_explicit_gaps"
