"""Source pagination failures must never become invented completeness."""

from datetime import datetime, timedelta
import gzip
import json

import pytest

from ashare_data.financials import expansion as e
from ashare_data.financials import official_expansion as o


def row(number, day="2024-04-30", **extra):
    stamp = datetime.fromisoformat(day + "T00:00:00+08:00")
    return dict(
        secCode="600000", orgId="synthetic", announcementId=str(number),
        announcementTitle="2024年第一季度报告",
        announcementTime=int(stamp.timestamp() * 1000), adjunctUrl=f"{number}.pdf",
    ) | extra


class Captured:
    """Persistent mock capture, including interrupted multi-window enumeration."""

    def __init__(self, root, rows):
        self.root, self.rows = root, rows
        self.calls, self.cache = 0, {}
        self.interrupt_at = None
        self.stopped = lambda: False

    def fetch(self, key, url, form):
        if key in self.cache:
            return self.cache[key]
        if self.interrupt_at == self.calls:
            raise o.Deferred("synthetic transport interruption")
        self.calls += 1
        if key.startswith("identity-"):
            data = [dict(code="600000", category="A股", orgId="synthetic")]
        else:
            a, b = form["seDate"].split("~")
            rows = [
                r for r in self.rows
                if a <= o.report_identity(r)["published_date"] <= b
            ] if not form["searchkey"] else []
            data = dict(totalAnnouncement=len(rows), hasMore=len(rows) > 30, announcements=rows[:30])
        raw = e.encoded(data)
        self.cache[key] = raw, dict(sha256=e.digest(raw), observed_at=e.now())
        return self.cache[key]


MANIFEST = dict(target_start="2024-01-01", target_end="2024-12-31")


def test_disjoint_date_windows_resume_without_refetch_and_preserve_old_pages(tmp_path):
    old = tmp_path / "raw/index-sh600000-periodic-001.gz"
    e.immutable(old, b"original-count-drift-evidence")
    rows = [row(i, (datetime(2024, 1, 1) + timedelta(days=i * 5)).date().isoformat()) for i in range(65)]
    client = Captured(tmp_path, rows)
    client.interrupt_at = 4
    with pytest.raises(o.Deferred):
        o.recover_pagination(client, "sh600000", MANIFEST)
    assert not (tmp_path / "indices/sh600000.json.gz").exists()
    cached = dict(client.cache)
    client.interrupt_at = None
    result = o.recover_pagination(client, "sh600000", MANIFEST)
    assert old.read_bytes() == b"original-count-drift-evidence"
    assert all(client.cache[key] == value for key, value in cached.items())
    assert len(result["announcements"]) == 65
    assert all(q["pagination_complete_for_query"] for q in result["queries"])
    assert not result["historical_revision_inventory_complete"]
    leaves = result["queries"][0]["date_windows"]
    assert leaves[0]["start"] == MANIFEST["target_start"]
    assert leaves[-1]["end"] == MANIFEST["target_end"]
    for a, b in zip(leaves, leaves[1:]):
        assert datetime.fromisoformat(a["end"]) + timedelta(days=1) == datetime.fromisoformat(b["start"])
    assert json.loads(gzip.decompress((tmp_path / "indices/sh600000.json.gz").read_bytes())) == result


@pytest.mark.parametrize("rows,message", [
    ([row(1, secCode="601975")], "Cross-issuer"),
    ([row(1), row(1)], "duplicate"),
    ([row(i) for i in range(31)], "single-day"),
])
def test_ambiguous_or_capped_leaf_never_publishes_index(tmp_path, rows, message):
    client = Captured(tmp_path, rows)
    with pytest.raises(ValueError, match=message):
        o.recover_pagination(client, "sh600000", MANIFEST)
    assert not (tmp_path / "indices/sh600000.json.gz").exists()


def test_wrong_date_response_cannot_count_as_complete(tmp_path):
    client = Captured(tmp_path, [])
    original = client.fetch

    def fetch(key, url, form):
        if key.startswith("identity-"):
            return original(key, url, form)
        raw = e.encoded(dict(totalAnnouncement=1, hasMore=False, announcements=[row(1, "2023-04-30")]))
        return raw, dict(sha256=e.digest(raw), observed_at=e.now())

    client.fetch = fetch
    with pytest.raises(ValueError, match="outside requested"):
        o.recover_pagination(client, "sh600000", MANIFEST)
    assert not (tmp_path / "indices/sh600000.json.gz").exists()


def test_retained_document_scratch_survives_extractor_failure(tmp_path, monkeypatch):
    client = Captured(tmp_path, [])
    client.fetch = lambda *args: (b"%PDF-synthetic", dict(observed_at=e.now()))
    report = dict(announcement_id="one", source_url="https://static.cninfo.com.cn/one.pdf")
    monkeypatch.setattr(o.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("extractor failure")))
    with e.preserve_temporaries(), pytest.raises(RuntimeError, match="extractor failure"):
        o.collect_document(client, "sh600000", report, "/synthetic-extractor", tmp_path)
    assert [p.read_bytes() for p in (tmp_path / "retained-temporaries").glob("*/report.pdf")] == [b"%PDF-synthetic"]


def test_resume_only_resets_pagination_failures(tmp_path, monkeypatch):
    scope = dict(symbols=["sh600000", "sh600001"], document_symbols=[])
    raw = e.encoded(scope)
    e.immutable(tmp_path / "scope.json", raw)
    e.immutable(tmp_path / "manifest.json", e.encoded(dict(schema=o.SCHEMA, scope_sha256=e.digest(raw))))
    for symbol, reason in [
        ("sh600000", "Index count changed while paging"),
        ("sh600001", "Cross-issuer index row"),
    ]:
        e.atomic(tmp_path / "securities" / (symbol + ".json"), e.encoded(dict(
            state="parse_failed", symbol=symbol, reason="ValueError: " + reason,
        )))
    related = tmp_path / "securities/sh600001.json"
    before = related.read_bytes()
    client = Captured(tmp_path, [])
    client.state = {}
    monkeypatch.setattr(o, "Client", lambda *a: client)
    monkeypatch.setattr(o, "index_security", lambda *a: (_ for _ in ()).throw(
        ValueError("Index count changed while paging")))
    monkeypatch.setattr(o, "recover_pagination", lambda *a: dict(announcements=[]))
    monkeypatch.setattr(o, "audit", lambda *a: {})
    o.run(tmp_path, None, retry_parse_failed=True, recover_paging=True)
    assert json.loads((tmp_path / "securities/sh600000.json").read_text())["state"] == "complete"
    assert related.read_bytes() == before
    assert not (tmp_path / "parse-reviews/sh600001.json").exists()
