"""Real bounded issuer evidence only; absence explicitly skips, never fabricates a fact."""
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket

import pytest

from ashare_data import DataError, ListingFactDescriptor, ListingFactEvidence, ListingYearFact, Store
from ashare_data import listing_fact as module
from ashare_data.cli import main
from ashare_data.model import canonical, digest


@pytest.fixture
def package():
    location = os.environ.get("ASHARE_LISTING_EVIDENCE")
    if not location:
        pytest.skip("Set ASHARE_LISTING_EVIDENCE to the separately delivered real package")
    return Path(location)


@pytest.fixture
def loaded(tmp_path, package):
    store = Store.init(tmp_path / "store")
    sid = store.import_listing_evidence(package)
    return store, sid, store.listing_fact(sid)


def error(code, call):
    with pytest.raises(DataError) as caught:
        call()
    assert caught.value.code == code


def overwrite(path, blob):
    path.chmod(0o644)
    path.write_bytes(blob)


def test_real_three_day_fact_and_lineage(loaded, package):
    store, sid, view = loaded
    descriptor = view.descriptor()
    assert isinstance(descriptor, ListingFactDescriptor)
    assert descriptor.package_sha256 == module.PACKAGE_SHA256
    assert descriptor.query_dates == (date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30))
    assert descriptor.supported_fields == ("initial_listing_year",)
    assert descriptor.historical_available_at is None
    assert descriptor.complete_instrument_history is descriptor.execution_permission is False
    assert descriptor.network_required is False
    for day in descriptor.query_dates:
        fact = view.get("300750.XSHE", day)
        assert isinstance(fact, ListingYearFact)
        assert (fact.field, fact.value, fact.precision) == ("initial_listing_year", 2018, "year")
        assert fact.actual_initial_listing_date is fact.historical_eligible is fact.historical_available_at is None
        assert fact.visibility == "posthoc" and fact.knowledge_at is None
        assert fact.evidence_status == "issuer_reported_completed_listing_year"
        assert fact.execution_permission is False
        assert all(isinstance(e, ListingFactEvidence) for e in fact.evidence)
    sources = view.lineage()
    assert [e.role for e in sources] == ["postevent_completion_and_year", "prelisting_arrangement_only"]
    assert sources[0].pdf_pages == (2, 7) and sources[1].pdf_pages == (17,)
    for entry in json.loads((package / "package.json").read_bytes())["files"]:
        assert view.evidence(entry["path"]) == (package / entry["path"]).read_bytes()
    for entry in sources:
        assert hashlib.sha256(view.evidence(entry.path)).hexdigest() == entry.source_sha256
    assert view.validate()["status"] == "VALID_YEAR_FACT_ONLY"
    assert store.listing_fact_snapshots()[0]["status"] == "published"
    assert Store(store.root).listing_fact(sid).descriptor() == descriptor


def test_immutability_and_idempotence(loaded, package):
    store, sid, view = loaded
    before = {str(p): p.read_bytes() for name in ("listing-objects", "listing-manifests")
              for p in (store.root / name).iterdir()}
    catalog = store.catalog.read_bytes()
    assert store.import_listing_evidence(package) == sid
    assert store.catalog.read_bytes() == catalog
    assert before == {str(p): p.read_bytes() for name in ("listing-objects", "listing-manifests")
                      for p in (store.root / name).iterdir()}
    assert len(store.listing_fact_snapshots()) == 1
    fact = view.get("300750.XSHE", "2026-09-30")
    with pytest.raises(FrozenInstanceError):
        fact.value = 2020
    result = fact.to_dict()
    result["value"] = 2020
    result["evidence"][0]["pdf_pages"].append(99)
    assert view.get("300750.XSHE", "2026-09-30") == fact
    assert view.lineage()[0].pdf_pages == (2, 7)


@pytest.mark.parametrize("name", ["index.raw", "index.json", "document.raw", "document.json",
                                    "postevent-index.raw", "postevent-index.json", "postevent-document.json",
                                    "summary-document.raw", "summary-document.json", "document.txt",
                                    "summary-document.txt"])
def test_each_original_or_conversion_tamper_blocks_import(tmp_path, package, name):
    target = tmp_path / "package"
    shutil.copytree(package, target)
    (target / name).write_bytes((target / name).read_bytes() + b" ")
    store = Store.init(tmp_path / "store")
    error("LISTING_INTEGRITY", lambda: store.import_listing_evidence(target))
    assert store.listing_fact_snapshots() == []


@pytest.mark.parametrize("key,value", [("value", 2019), ("field", "initial_listing_date"),
    ("actual_initial_listing_date", "2018-06-11"), ("security", "600000.XSHG"),
    ("query_dates", ["2026-09-27"]), ("historical_available_at", "2018-06-08T00:00:00Z"),
    ("execution_permission", True)])
def test_self_signed_fact_edits_not_accepted(tmp_path, package, key, value):
    target = tmp_path / "package"
    shutil.copytree(package, target)
    document = json.loads((target / "package.json").read_bytes())
    document[key] = value
    (target / "package.json").write_bytes(canonical(document))
    store = Store.init(tmp_path / "store")
    error("LISTING_UNREVIEWED_PACKAGE", lambda: store.import_listing_evidence(target))
    assert store.listing_fact_snapshots() == []


@pytest.mark.parametrize("security", ["600000.XSHG", "000001.XSHE", "300750", None, ["300750.XSHE"]])
def test_security_scope(loaded, security):
    error("LISTING_SECURITY_SCOPE", lambda: loaded[2].get(security, "2026-09-30"))


@pytest.mark.parametrize("day", ["2026-09-27", "2026-10-01", "2018-06-11"])
def test_date_scope(loaded, day):
    error("LISTING_DATE_SCOPE", lambda: loaded[2].get("300750.XSHE", day))


@pytest.mark.parametrize("day", [None, "20260930", "2026-9-30", "2026-09-30T00:00:00Z",
                                  datetime(2026, 9, 30), "2026-02-30", 20260930])
def test_date_not_silently_truncated(loaded, day):
    error("LISTING_DATE_INVALID", lambda: loaded[2].get("300750.XSHE", day))


@pytest.mark.parametrize("field", ["initial_listing_date", "historical_eligible", "delisting_date",
                                    "suspended", "is_st", "price_limits", "event_absence", "", None])
def test_unknowns_not_promoted(loaded, field):
    error("LISTING_FACT_UNAVAILABLE", lambda: loaded[2].get("300750.XSHE", "2026-09-30", field=field))


def test_visibility_and_eligibility_refusals(loaded):
    view = loaded[2]
    received = view.descriptor().evidence_received_at
    def call(**kw):
        return view.get("300750.XSHE", "2026-09-30", **kw)
    error("LISTING_ELIGIBILITY_UNKNOWN", lambda: view.require_eligible("300750.XSHE", "2026-09-30"))
    error("VISIBILITY_UNKNOWN", lambda: call(visibility="received", knowledge_at=received - timedelta(microseconds=1)))
    error("VISIBILITY_UNKNOWN", lambda: call(visibility="received", knowledge_at="2026-09-30T15:00:00+08:00"))
    for clock in (received, received.isoformat(), received.astimezone(timezone(timedelta(hours=8)))):
        assert call(visibility="received", knowledge_at=clock).knowledge_at == received
    error("PIT_UNAVAILABLE", lambda: call(visibility="verified", knowledge_at=received))
    error("LISTING_CLOCK_MODE", lambda: call(knowledge_at=received))
    error("LISTING_VISIBILITY", lambda: call(visibility="assumed"))


@pytest.mark.parametrize("clock", [None, "2026-10-10", "2026-10-10T12:00:00", "bad", 1,
                                    datetime(2026, 10, 10)])
def test_naive_clock_rejected(loaded, clock):
    error("LISTING_CLOCK_INVALID", lambda: loaded[2].get("300750.XSHE", "2026-09-30",
                                                       visibility="received", knowledge_at=clock))


def test_query_has_no_network_path(loaded, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("query attempted network")
    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    view = loaded[2]
    assert view.get("300750.XSHE", "2026-09-30").value == 2018
    assert view.validate()["execution_permission"] is False
    assert view.evidence("summary-document.raw").startswith(b"%PDF")


@pytest.mark.parametrize("target", ["object", "package", "manifest", "catalog"])
def test_bound_view_revalidates_integrity(loaded, target):
    store, sid, view = loaded
    if target == "object":
        path = store.root / "listing-objects" / view.lineage()[0].source_sha256
        overwrite(path, path.read_bytes() + b" ")
    elif target == "package":
        path = store.root / "listing-objects" / module.PACKAGE_SHA256
        overwrite(path, path.read_bytes() + b" ")
    elif target == "manifest":
        path = store.root / "listing-manifests" / (sid + ".json")
        overwrite(path, path.read_bytes() + b" ")
    else:
        with store._db(write=True) as db:
            db.execute("UPDATE listing_facts SET body=body || ' '")
    code = "LISTING_UNREVIEWED_PACKAGE" if target == "package" else "LISTING_INTEGRITY"
    for method in (view.descriptor, view.lineage, view.validate,
                   lambda: view.evidence("summary-document.raw"),
                   lambda: view.get("300750.XSHE", "2026-09-30")):
        error(code, method)


@pytest.mark.parametrize("target", ["root", "raw", "stored-objects", "stored-manifests"])
def test_links_rejected(tmp_path, package, target):
    store = Store.init(tmp_path / "store")
    if target == "root":
        path = tmp_path / "linked"
        path.symlink_to(package, target_is_directory=True)
    elif target == "raw":
        path = tmp_path / "package"
        shutil.copytree(package, path)
        (path / "document.raw").unlink()
        (path / "document.raw").symlink_to(package / "document.raw")
    else:
        path = package
        outside = tmp_path / "outside"
        outside.mkdir()
        (store.root / ("listing-" + target.removeprefix("stored-"))).symlink_to(outside, target_is_directory=True)
    error("LISTING_INPUT_INVALID", lambda: store.import_listing_evidence(path))
    assert store.listing_fact_snapshots() == []


def test_missing_and_bounded_inputs(tmp_path, package):
    store = Store.init(tmp_path / "store")
    assert store.recover_listing_facts() == []
    error("LISTING_NOT_FOUND", lambda: store.listing_fact("0" * 64))
    error("INVALID_ID", lambda: store.listing_fact("latest"))
    error("LISTING_INPUT_INVALID", lambda: store.import_listing_evidence(tmp_path / "absent"))
    path = tmp_path / "huge"
    path.mkdir()
    with (path / "package.json").open("wb") as stream:
        stream.truncate(1024 * 1024 + 1)
    error("LISTING_INPUT_INVALID", lambda: store.import_listing_evidence(path))
    sid = store.import_listing_evidence(package)
    error("LISTING_EVIDENCE_NOT_FOUND", lambda: store.listing_fact(sid).evidence("../catalog.sqlite"))


@pytest.mark.parametrize("corrupt", [False, True])
@pytest.mark.parametrize("stage", ["before_manifest", "after_manifest"])
def test_interrupted_publish_requires_explicit_recovery(tmp_path, package, monkeypatch, corrupt, stage):
    store = Store.init(tmp_path / "store")
    write = module.immutable_write
    def interrupt(path, blob):
        if path.parent.name == "listing-manifests":
            if stage == "after_manifest":
                write(path, blob)
            raise OSError("injected publication interruption")
        write(path, blob)
    monkeypatch.setattr(module, "immutable_write", interrupt)
    with pytest.raises(OSError, match="injected"):
        store.import_listing_evidence(package)
    record = store.listing_fact_snapshots()[0]
    sid = record["snapshot_id"]
    assert record["status"] == "prepared"
    error("LISTING_NOT_PUBLISHED", lambda: store.listing_fact(sid))
    error("LISTING_RECOVERY_REQUIRED", lambda: store.import_listing_evidence(package))
    monkeypatch.setattr(module, "immutable_write", write)
    if corrupt:
        p = store.root / "listing-objects" / module.PACKAGE_SHA256
        overwrite(p, b"corrupt")
    recovered = store.recover_listing_facts()
    assert recovered[0]["status"] == ("aborted" if corrupt else "published")
    assert store.recover_listing_facts() == []
    if corrupt:
        error("LISTING_NOT_PUBLISHED", lambda: store.listing_fact(sid))
        error("LISTING_RECOVERY_REQUIRED", lambda: store.import_listing_evidence(package))
    else:
        assert store.listing_fact(sid).get("300750.XSHE", "2026-09-30").value == 2018
        assert store.import_listing_evidence(package) == sid


def test_recovery_rejects_forged_catalog(tmp_path, package):
    store = Store.init(tmp_path / "store")
    sid = store.import_listing_evidence(package)
    with store._db(write=True) as db:
        body = json.loads(db.execute("SELECT body FROM listing_facts").fetchone()[0])
        body["owner_version"] = "unreviewed"
        fake = digest(body)
        db.execute("INSERT INTO listing_facts VALUES (?,?,'prepared','now')", (fake, canonical(body).decode()))
    assert store.recover_listing_facts() == [{"snapshot_id": fake, "status": "aborted", "error": "LISTING_INTEGRITY"}]
    assert store.listing_fact(sid).get("300750.XSHE", "2026-09-30").value == 2018


def test_cli_matches_public_api(loaded, package, capsys):
    store, sid, view = loaded
    common = ["--store", str(store.root)]
    assert main(common + ["listing-import", str(package)]) == 0
    assert json.loads(capsys.readouterr().out) == {"snapshot_id": sid}
    for operation in ("value", "descriptor", "lineage", "validate"):
        args = common + ["listing-query", operation, "--snapshot", sid]
        if operation == "value":
            args += ["--security", "300750.XSHE", "--date", "2026-09-30"]
            expected = view.get("300750.XSHE", "2026-09-30").to_dict()
        elif operation == "lineage":
            expected = [x.to_dict() for x in view.lineage()]
        else:
            expected = getattr(view, operation)()
            if hasattr(expected, "to_dict"):
                expected = expected.to_dict()
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out) == expected
    assert main(common + ["listing-snapshots"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "published"
    assert main(common + ["listing-recover"]) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.parametrize("args,code", [
    (["value"], "LISTING_QUERY_ARGUMENT"),
    (["value", "--security", "300750.XSHE"], "LISTING_QUERY_ARGUMENT"),
    (["descriptor", "--security", "300750.XSHE"], "LISTING_QUERY_ARGUMENT"),
    (["lineage", "--visibility", "posthoc"], "LISTING_QUERY_ARGUMENT"),
    (["validate", "--field", "initial_listing_year"], "LISTING_QUERY_ARGUMENT"),
    (["value", "--security", "300750.XSHE", "--date", "2026-09-30", "--field", "initial_listing_date"], "LISTING_FACT_UNAVAILABLE"),
    (["value", "--security", "300750.XSHE", "--date", "2026-09-30", "--visibility", "verified"], "PIT_UNAVAILABLE"),
])
def test_cli_fail_closed(loaded, args, code, capsys):
    store, sid, _ = loaded
    assert main(["--store", str(store.root), "listing-query", *args, "--snapshot", sid]) == 2
    output = capsys.readouterr()
    assert not output.out and json.loads(output.err)["error"]["code"] == code


def test_malformed_published_manifest_has_typed_error(loaded):
    store, _, _ = loaded
    malformed = b"not json"
    sid = hashlib.sha256(malformed).hexdigest()
    (store.root / "listing-manifests" / (sid + ".json")).write_bytes(malformed)
    with store._db(write=True) as db:
        db.execute("INSERT INTO listing_facts VALUES (?,?,'published','now')", (sid, malformed.decode()))
    error("LISTING_INTEGRITY", lambda: store.listing_fact(sid))


def test_single_writer_gate(loaded, package):
    store, sid, _ = loaded
    with store._writer():
        error("WRITER_BUSY", lambda: store.import_listing_evidence(package))
        error("WRITER_BUSY", store.recover_listing_facts)
        assert store.listing_fact(sid).get("300750.XSHE", "2026-09-30").value == 2018
