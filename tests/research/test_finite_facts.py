"""Storage/contract negatives use a test-only synthetic pin, never official claims.

The real pinned package is separately exercised by examples/finite_facts.py; it is not
redistributed as a wheel fixture. Production has no API to replace the review pin.
"""

import json
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from ashare_data import DataError, Store
from ashare_data.research import finite_facts as fact_module
from ashare_data.cli import main
from ashare_data.core.model import canonical, digest


def reject(code, fn):
    with pytest.raises(DataError) as error:
        fn()
    assert error.value.code == code


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    packet = tmp_path / "synthetic-packet"
    packet.mkdir()
    note = b"SYNTHETIC TEST ONLY. Not historical evidence."
    source = {
        "path": "notes/000.txt",
        "sha256": fact_module._sha(note),
        "url": "https://example.invalid/test",
        "locator": "test only",
        "retrieved_at": "2026-10-08T00:00:00+00:00",
        "historical_available_at": None,
    }
    values = {
        "security_identity": {"name": "合成", "exchange": "XSHG", "kind": "ordinary_A_share"},
        "initial_listing_date": "1999-11-10",
        "historical_board_candidate": "main_board",
        "buy_round_lot": 100,
        "price_tick": "0.01",
        "same_day_resale": False,
        "stock_trading_cycle": "T+1",
        "normal_daily_limit_ratio": "0.10",
        "window_corporate_action_review": "no_relevant_event_identified",
        "ex_right_reference": "unknown",
    }
    facts = [
        {
            "id": k,
            "value": v,
            "evidence_status": "synthetic",
            "sources": [source],
            "limitation": "SYNTHETIC; not evidence",
            "future_extension": {"a": [1]},
        }
        for k, v in values.items()
    ]
    events = [
        {
            "id": "synthetic",
            "event_type": "cash_dividend",
            "entitled_security": "600000.XSHG",
            "record_date": "2020-07-22",
            "ex_date": "2020-07-23",
            "pay_date": "2020-07-23",
            "cash_per_share_before_tax": "0.60",
            "facts_relation": "after_assumed_full_exit",
            "evidence_status": "synthetic",
            "sources": [source],
        }
    ]
    blobs = {
        "facts.json": canonical({"facts": facts}),
        "events.json": canonical(events),
        "ad08-candidates.json": b"[]",
    }
    blobs.update({f"notes/{i:03}.txt": note for i in range(147)})
    manifest = {"files": []}
    for name, blob in blobs.items():
        p = packet / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(blob)
        manifest["files"].append(
            {"path": name, "bytes": len(blob), "sha256": fact_module._sha(blob)}
        )
    manifest_bytes = canonical(manifest)
    (packet / "manifest.json").write_bytes(manifest_bytes)
    monkeypatch.setattr(fact_module, "SOURCE_MANIFEST", fact_module._sha(manifest_bytes))
    store = Store.init(tmp_path / "store")
    rows = [
        dict(
            date=d.isoformat(),
            code="sh.600000",
            open="10",
            high="11",
            low="9",
            close="10.05",
            preclose="10.05",
            volume="100",
            amount="1005.00",
            adjustflag="3",
            tradestatus="1",
            isST="0",
        )
        for d in (fact_module.PREVIOUS[0], *fact_module.DAYS)
    ]
    fields = list(rows[0])
    raw = canonical(
        {
            "method": "query_history_k_data_plus",
            "params": {
                "code": "sh.600000",
                "fields": ",".join(fields),
                "start_date": "2019-12-31",
                "end_date": "2020-01-07",
                "frequency": "d",
                "adjustflag": "3",
            },
            "error_code": "0",
            "fields": fields,
            "rows": rows,
        }
    )
    price = tmp_path / "price.json"
    price.write_bytes(raw)
    calendar = tmp_path / "calendar.json"
    calendar.write_bytes(
        canonical(
            {
                "method": "query_trade_dates",
                "params": {"start_date": "2019-12-31", "end_date": "2020-01-07"},
                "error_code": "0",
                "fields": ["calendar_date", "is_trading_day"],
                "rows": [
                    {
                        "calendar_date": d,
                        "is_trading_day": "1" if d in {r["date"] for r in rows} else "0",
                    }
                    for d in (
                        "2019-12-31",
                        "2020-01-01",
                        "2020-01-02",
                        "2020-01-03",
                        "2020-01-04",
                        "2020-01-05",
                        "2020-01-06",
                        "2020-01-07",
                    )
                ],
            }
        )
    )
    pid = store.import_research([price], calendar_path=calendar)
    monkeypatch.setattr(fact_module, "PRICE_DATASET", pid)
    monkeypatch.setattr(fact_module, "PRICE_OBJECT", fact_module._sha(raw))
    monkeypatch.setattr(fact_module, "CALENDAR_OBJECT", fact_module._sha(calendar.read_bytes()))
    return store, packet, pid


@pytest.fixture
def published_facts(prepared):
    store, packet, pid = prepared
    fid = store.import_fact_component(packet)
    sid = store.compose_facts(pid, fid)
    return store, fid, sid


def test_end_to_end_idempotency_detachment_and_schema(prepared):
    store, packet, pid = prepared
    before = {p: p.read_bytes() for p in packet.rglob("*") if p.is_file()}
    fid = store.import_fact_component(packet)
    sid = store.compose_facts(pid, fid)
    catalog = store.catalog.read_bytes()
    assert fid == store.import_fact_component(packet)
    assert sid == store.compose_facts(pid, fid)
    assert store.catalog.read_bytes() == catalog
    v = Store(store.root).facts(sid)
    assert v.descriptor().price_dataset_id == pid
    assert v.descriptor().complete is False
    assert v.descriptor().manifest_sha256 == sid
    assert v.descriptor().execution_permission is False
    assert len(v.bars()) == 4 and len(v.calendar()) == 6
    assert v.bars()[0].volume == 100 and v.bars()[0].volume_unit == "share"
    assert v.bars()[0].close == Decimal("10.05")
    assert v.bars()[0].historical_available_at is None
    assert v.calendar()[0].previous_open.isoformat() == "2019-12-31"
    assert v.calendar()[2].is_trading_day is False
    assert v.calendar()[3].is_trading_day is False
    assert all(s.suspended is False and s.is_st is False for s in v.statuses())
    assert all(s.evidence_status == "source_observed" for s in v.statuses())
    assert v.instrument().historical_eligible is None
    assert v.instrument().board_evidence_status == "bounded_historical_inference"
    assert v.rules().buy_round_lot == 100 and v.rules().same_day_resale is False
    assert not v.quality().historical_pit and v.quality().finality == "UNVERIFIED"
    assert v.coverage().source_grid_complete and not v.coverage().complete
    assert v.source_prev_close("2020-01-02").value == Decimal("10.05")
    assert len(v.lineage().official_file_hashes) == 150
    f = store.fact_component(fid).records()[0]
    f.source_fields["future_extension"]["a"].append(2)
    assert f.source_fields["future_extension"] == {"a": [1]}
    detached = f.to_dict()
    detached["evidence"].clear()
    assert f.evidence
    with pytest.raises(FrozenInstanceError):
        v.bars()[0].close = Decimal(1)
    for p, data in before.items():
        assert p.read_bytes() == data
    assert store.snapshots() == []


def test_required_vs_assumed_and_owner_envelope(published_facts):
    v = published_facts[0].facts(published_facts[2])
    a = v.admission()
    assert a.blockers == ("instrument", "price_limits", "event_absence", "adjusted_prev_close")
    assert len(a.requirements) == 17
    assert sum(x.classification == "required_fact" for x in a.requirements) == 11
    assert sum(x.claim.conclusion == "supported" for x in a.requirements) == 7
    assert sum(x.classification == "declared_model_assumption" for x in a.requirements) == 5
    assert a.requirements[-1].classification == "not_used"
    assert a.status == "BLOCKED" and not a.execution_permission
    r = v.owner_receipt().to_dict()
    assert set(r) == {
        "owner",
        "owner_version",
        "request_id",
        "dataset_id",
        "manifest_sha256",
        "security",
        "scope_start",
        "scope_end",
        "facts",
    }
    assert r["dataset_id"] == r["manifest_sha256"] == published_facts[2]
    for x in a.requirements:
        if x.claim.conclusion == "supported":
            assert v.require_fact(x.claim.key) == x.claim
        else:
            reject("FACTS_FACT_UNAVAILABLE", lambda: v.require_fact(x.claim.key))
    reject("FACTS_UNKNOWN_FACT", lambda: v.require_fact("something"))
    reject("FACTS_PLAN_MISMATCH", lambda: v.require_execution(plan_sha256="0" * 64))
    reject(
        "FACTS_ADMISSION_BLOCKED", lambda: v.require_execution(plan_sha256=fact_module.PLAN_SHA256)
    )


def test_candidate_arithmetic_is_never_engine_value(published_facts):
    v = published_facts[0].facts(published_facts[2])
    # 10.05 * 1.1 = 11.055 is an exact tie; Decimal must round upward.
    assert (v.limit_candidates()[0].lower, v.limit_candidates()[0].upper) == (
        Decimal("9.05"),
        Decimal("11.06"),
    )
    for c in (*v.limit_candidates(), *v.prev_close_candidates()):
        assert c.eligible_for_engine is False and c.engine_value is None
    for c in v.prev_close_candidates():
        assert c.absolute_adjustment_factor is None
        reject("FACTS_FACT_UNAVAILABLE", lambda: v.adjusted_prev_close(**c.call.to_dict()))
    reject("FACTS_FACT_UNAVAILABLE", lambda: v.price_limits("2020-01-03"))
    reject("FACTS_FACT_UNAVAILABLE", v.corporate_actions)
    assert not v.event_coverage().verified_absent
    assert not v.event_coverage().complete_economic_event_set
    assert v.known_events()[0].record_date.isoformat() == "2020-07-22"


@pytest.mark.parametrize(
    "key,value",
    [
        ("security", "000001.XSHE"),
        ("trade_date", "2020-01-08"),
        ("history_dt", "2019-12-30"),
        ("adjust_orig", "2026-10-08"),
        ("frequency", "5m"),
        ("field", "open"),
        ("bar_count", 2),
        ("bar_count", True),
        ("bar_count", 1.0),
        ("include_now", True),
        ("include_now", 0),
        ("skip_suspended", True),
        ("skip_suspended", 0),
        ("adjustment_requested", "none"),
        ("adjustment_requested", "post"),
    ],
)
def test_unreviewed_ad08_tuple(published_facts, key, value):
    v = published_facts[0].facts(published_facts[2])
    call = v.prev_close_candidates()[0].call.to_dict()
    call[key] = value
    reject("FACTS_UNREVIEWED_CALL", lambda: v.adjusted_prev_close(**call))


@pytest.mark.parametrize(
    "value,code",
    [
        ("2020-01-04", "FACTS_OUT_OF_SCOPE"),
        ("2019-12-31", "FACTS_OUT_OF_SCOPE"),
        ("2020-01-08", "FACTS_OUT_OF_SCOPE"),
        (None, "INVALID_DATE"),
        ("2020-1-2", "INVALID_DATE"),
    ],
)
def test_source_date_scope(published_facts, value, code):
    v = published_facts[0].facts(published_facts[2])
    reject(code, lambda: v.source_prev_close(value))
    reject(code, lambda: v.price_limits(value))


def test_unknown_manifest_cannot_self_authorize(prepared):
    store, packet, _ = prepared
    data = json.loads((packet / "facts.json").read_text())
    data["verified_absent"] = True
    (packet / "facts.json").write_bytes(canonical(data))
    m = json.loads((packet / "manifest.json").read_text())
    entry = next(x for x in m["files"] if x["path"] == "facts.json")
    entry.update(bytes=len(canonical(data)), sha256=digest(data))
    (packet / "manifest.json").write_bytes(canonical(m))
    reject("FACTS_UNREVIEWED_EVIDENCE", lambda: store.import_fact_component(packet))
    assert store.facts_snapshots() == []


def test_missing_and_changed_original_rejected(prepared):
    store, packet, _ = prepared
    p = packet / "notes/003.txt"
    p.write_bytes(b"changed")
    reject("FACTS_INTEGRITY", lambda: store.import_fact_component(packet))
    p.unlink()
    reject("FACTS_INPUT_INVALID", lambda: store.import_fact_component(packet))
    assert store.facts_snapshots() == []


def test_symlink_input_is_rejected(prepared):
    store, packet, _ = prepared
    p = packet / "notes/003.txt"
    p.unlink()
    p.symlink_to(packet / "notes/004.txt")
    reject("FACTS_INPUT_INVALID", lambda: store.import_fact_component(packet))


def test_wrong_component_and_price(published_facts):
    s, f, c = published_facts
    reject("FACTS_WRONG_COMPONENT", lambda: s.facts(f))
    reject("FACTS_WRONG_COMPONENT", lambda: s.fact_component(c))
    reject("FACTS_UNREVIEWED_PRICE", lambda: s.compose_facts("0" * 64, f))
    reject("INVALID_ID", lambda: s.facts("latest"))
    reject("FACTS_NOT_FOUND", lambda: s.facts("0" * 64))


@pytest.mark.parametrize("kind", ["manifest", "object", "price", "catalog"])
def test_corruption_on_reopen_fails(published_facts, kind):
    s, f, c = published_facts
    if kind == "catalog":
        with s._db(write=True) as db:
            db.execute("UPDATE facts_snapshots SET body='{}' WHERE id=?", (c,))
    else:
        p = {
            "manifest": s.root / "facts-manifests" / (c + ".json"),
            "object": s.root / "facts-objects" / fact_module.SOURCE_MANIFEST,
            "price": s.root / "research-objects" / fact_module.PRICE_OBJECT,
        }[kind]
        p.chmod(0o644)
        p.write_bytes(b"changed")
    with pytest.raises(DataError):
        s.facts(c)


@pytest.mark.parametrize("target", ["facts", "composite"])
def test_publish_interruption_recovery(prepared, monkeypatch, target):
    s, packet, pid = prepared
    if target == "composite":
        fid = s.import_fact_component(packet)
    real = fact_module.immutable_write

    def interrupt(path, data):
        if path.parent.name == "facts-manifests":
            raise OSError("simulated interruption after prepared")
        return real(path, data)

    monkeypatch.setattr(fact_module, "immutable_write", interrupt)
    action = (
        (lambda: s.import_fact_component(packet))
        if target == "facts"
        else (lambda: s.compose_facts(pid, fid))
    )
    with pytest.raises(OSError):
        action()
    row = next(x for x in s.facts_snapshots() if x["status"] == "prepared")
    reject("FACTS_NOT_PUBLISHED", lambda: s.facts(row["dataset_id"]))
    reject("FACTS_RECOVERY_REQUIRED", action)
    monkeypatch.setattr(fact_module, "immutable_write", real)
    assert s.recover_facts() == [
        {"dataset_id": row["dataset_id"], "status": "published", "error": None}
    ]
    assert action() == row["dataset_id"]
    assert s.recover_facts() == []


def test_prepared_corruption_aborts_and_writer_lock(prepared, monkeypatch):
    s, packet, _ = prepared
    with s._writer():
        reject("WRITER_BUSY", lambda: s.import_fact_component(packet))
    real = fact_module.immutable_write

    def interrupt(path, data):
        if path.parent.name == "facts-manifests":
            raise OSError("crash")
        real(path, data)

    monkeypatch.setattr(fact_module, "immutable_write", interrupt)
    with pytest.raises(OSError):
        s.import_fact_component(packet)
    monkeypatch.setattr(fact_module, "immutable_write", real)
    (s.root / "facts-objects" / fact_module.SOURCE_MANIFEST).unlink()
    assert s.recover_facts()[0]["status"] == "aborted"
    reject("FACTS_RECOVERY_REQUIRED", lambda: s.import_fact_component(packet))


def test_bad_prepared_json_is_aborted(store):
    with store._db(write=True) as db:
        db.execute(fact_module.TABLE)
        db.execute(
            "INSERT INTO facts_snapshots VALUES (?,?,?,?)",
            ("1" * 64, "invalid", "prepared", "test"),
        )
    assert store.recover_facts()[0]["status"] == "aborted"


def test_changed_composite_policy_cannot_recover(published_facts):
    s, _, c = published_facts
    body = json.loads((s.root / "facts-manifests" / (c + ".json")).read_text())
    body["required_unknown"] = []
    body["execution_permission"] = True
    forged = digest(body)
    with s._db(write=True) as db:
        db.execute(
            "INSERT INTO facts_snapshots VALUES (?,?,?,?)",
            (forged, canonical(body).decode(), "prepared", "test"),
        )
    assert s.recover_facts()[0]["status"] == "aborted"
    assert s.facts(c).admission().blockers


@pytest.mark.parametrize(
    "name",
    [
        "../facts.json",
        "/facts.json",
        "notes//000.txt",
        "./facts.json",
        "notes/../facts.json",
        "notes\\000.txt",
    ],
)
def test_evidence_path_scope(published_facts, name):
    s, f, _ = published_facts
    reject("FACTS_INPUT_INVALID", lambda: s.fact_component(f).evidence(name))


def test_cli_offline_and_blocked_exit(prepared, capsys):
    s, packet, pid = prepared
    prefix = ["--store", str(s.root)]
    assert main(prefix + ["facts-import-facts", str(packet)]) == 0
    fid = json.loads(capsys.readouterr().out)["facts_component_id"]
    assert main(prefix + ["facts-compose", "--price", pid, "--facts", fid]) == 0
    sid = json.loads(capsys.readouterr().out)["dataset_id"]
    for method in (
        "descriptor",
        "instrument",
        "rules",
        "bars",
        "calendar",
        "statuses",
        "events",
        "event-coverage",
        "limit-candidates",
        "prev-close-candidates",
        "coverage",
        "lineage",
        "quality",
        "owner-receipt",
        "admission",
    ):
        assert main(prefix + ["facts-query", method, "--dataset", sid]) == (
            2 if method == "admission" else 0
        )
        assert json.loads(capsys.readouterr().out)
    for method in ("facts-snapshots", "facts-recover"):
        assert main(prefix + [method]) == 0
        json.loads(capsys.readouterr().out)
