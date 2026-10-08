"""BR1 opt-in, immutable policy and contradiction gates. No engine execution."""

import json
from dataclasses import replace, FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal

import pytest

from ashare_data import Store, DataError, br1, d1
from ashare_data.cli import main
from ashare_data.model import canonical, digest
from test_d1 import prepared, published_d1, reject  # noqa: F401


@pytest.fixture
def conditional(request, monkeypatch):
    store, fid, sid = request.getfixturevalue("published_d1")
    # Production has one fixed strict input. Only the synthetic test pin varies.
    monkeypatch.setattr(br1, "STRICT_DATASET", sid)
    return store, sid


@pytest.fixture
def br1_view(conditional):
    s, sid = conditional
    cid = s.compose_br1(
        sid, mode="conditional_research", assumption_ids=("A-EQ", "A-NORMAL", "A-AD08")
    )
    return s, s.br1(cid, mode="conditional_research")


def test_requires_explicit_opt_in_and_frozen_assumptions(conditional):
    s, sid = conditional
    with pytest.raises(TypeError):
        s.compose_br1(sid)
    with pytest.raises(TypeError):
        s.br1(sid)
    for mode in (None, "strict", "assumed", True, "CONDITIONAL_RESEARCH"):
        reject(
            "BR1_EXPLICIT_MODE_REQUIRED",
            lambda: s.compose_br1(sid, mode=mode, assumption_ids=br1.ASSUMPTIONS),
        )
    for ids in (
        None,
        (),
        ("A-EQ",),
        ("A-EQ", "A-NORMAL"),
        ("A-EQ", "A-EQ", "A-AD08"),
        ("A-EQ", "A-NORMAL", "A-AD08", "extra"),
        {"A-EQ", "A-NORMAL", "A-AD08"},
        ("A-EQ", [], "A-AD08"),
    ):
        reject(
            "BR1_ASSUMPTIONS_REQUIRED",
            lambda: s.compose_br1(sid, mode=br1.MODE, assumption_ids=ids),
        )
    reject(
        "BR1_UNREVIEWED_INPUT",
        lambda: s.compose_br1("0" * 64, mode=br1.MODE, assumption_ids=br1.ASSUMPTIONS),
    )
    assert s.br1_snapshots() == []


def test_conditional_projection_preserves_strict_facts(br1_view):
    s, v = br1_view
    sid = v.descriptor().strict_dataset_id
    old = s.d1(sid).owner_receipt().to_dict()
    assert v.instrument().model_eligible and v.instrument().fact.historical_eligible is None
    assert v.instrument().evidence_status == "assumed_normal_applicability"
    assert v.rules().buy_round_lot == 100 and v.rules().trading_cycle == "T+1"
    e = v.modeled_events()
    assert e.modeled_events == () and e.evidence_status == "assumed_no_relevant_events"
    assert not e.verified_absent and not e.complete_economic_event_set
    assert e.assumption_ids == ("A-EQ",)
    a = v.admission()
    assert a.model_projection_permission and not a.execution_permission
    assert len(a.strict_blockers) == 4
    assert {
        "source_suspended",
        "source_st",
        "buy_lot_100",
        "price_tick_0.01",
        "T+1",
        "normal_price_limits",
    } <= set(a.required_native_checks)
    for c in v.profile().allowed_calls:
        p = v.adjusted_prev_close(**c.to_dict())
        assert p.value == Decimal("10.05")
        assert p.assumption_ids == ("A-EQ", "A-AD08")
        assert p.absolute_adjustment_factor is None and not p.execution_permission
        assert p.evidence_status == "derived_under_declared_assumptions"
        assert (
            p.source_candidate.engine_value is None and not p.source_candidate.eligible_for_engine
        )
        limit = v.price_limits(c.trade_date.isoformat())
        assert (limit.lower, limit.upper) == (Decimal("9.05"), Decimal("11.06"))
        assert limit.assumption_ids == ("A-EQ", "A-NORMAL")
        assert limit.source_candidate.engine_value is None
    assert v.profile().assumptions[2].depends_on == ("A-EQ",)
    profile = v.profile().source_fields
    profile["assumptions"].clear()
    assert len(v.profile().assumptions) == 3
    with pytest.raises(FrozenInstanceError):
        v.descriptor().complete = True
    assert s.d1(sid).owner_receipt().to_dict() == old
    reject("D1_FACT_UNAVAILABLE", s.d1(sid).corporate_actions)
    reject("BR1_REVIEW_REQUIRED", v.require_execution)


def test_backend_date_dto_interop_is_exact(br1_view):
    v = br1_view[1]
    for c in v.profile().allowed_calls:
        call = c.to_dict()
        for key in ("trade_date", "history_dt", "adjust_orig"):
            call[key] = date.fromisoformat(call[key])
        assert v.adjusted_prev_close(**call).call == c
        assert v.price_limits(call["trade_date"]).trade_date == c.trade_date
        call["history_dt"] = datetime.combine(call["history_dt"], datetime.min.time())
        reject("BR1_UNREVIEWED_CALL", lambda: v.adjusted_prev_close(**call))


@pytest.mark.parametrize(
    "key,value",
    [
        ("security", "000001.XSHE"),
        ("trade_date", "2020-01-08"),
        ("history_dt", "2020-01-01"),
        ("adjust_orig", "2026-10-08"),
        ("frequency", "daily"),
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
def test_complete_tuple_gate(br1_view, key, value):
    v = br1_view[1]
    for c in v.profile().allowed_calls:
        call = c.to_dict()
        call[key] = value
        reject("BR1_UNREVIEWED_CALL", lambda: v.adjusted_prev_close(**call))


@pytest.mark.parametrize(
    "field,value",
    [
        ("suspended", True),
        ("is_st", True),
        ("suspended", None),
        ("is_st", None),
        ("suspended", 0),
        ("is_st", 0),
        ("security", "000001.XSHE"),
        ("trade_date", date(2020, 1, 8)),
    ],
)
def test_source_status_is_not_disabled(conditional, monkeypatch, field, value):
    s, sid = conditional
    original = d1.D1View.statuses

    def invalid(view):
        rows = original(view)
        return (replace(rows[0], **{field: value}), *rows[1:])

    monkeypatch.setattr(d1.D1View, "statuses", invalid)
    reject(
        "BR1_STATUS_BLOCKED",
        lambda: s.compose_br1(sid, mode=br1.MODE, assumption_ids=br1.ASSUMPTIONS),
    )
    assert s.br1_snapshots() == []


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("record_date", date(2020, 1, 3), "BR1_RELATED_EVENT"),
        ("record_date", date(2020, 1, 6), "BR1_RELATED_EVENT"),
        ("record_date", None, "BR1_EVENT_UNKNOWN"),
        ("ex_date", date(2020, 1, 2), "BR1_RELATED_EVENT"),
        ("effective_date", date(2019, 12, 31), "BR1_RELATED_EVENT"),
        ("conversion_start", date(2020, 1, 7), "BR1_RELATED_EVENT"),
        ("entitled_security", "999999.XSHG", "BR1_IDENTITY_CONFLICT"),
    ],
)
def test_related_event_blocks_even_when_payment_is_later(
    conditional, monkeypatch, field, value, code
):
    s, sid = conditional
    original = d1.D1View.known_events
    monkeypatch.setattr(
        d1.D1View,
        "known_events",
        lambda view: tuple(replace(e, **{field: value}) for e in original(view)),
    )
    reject(code, lambda: s.compose_br1(sid, mode=br1.MODE, assumption_ids=br1.ASSUMPTIONS))
    assert s.br1_snapshots() == []


@pytest.mark.parametrize(
    "kind,code",
    [
        ("identity", "BR1_IDENTITY_CONFLICT"),
        ("rule", "BR1_RULE_CONFLICT"),
        ("price", "BR1_PRICE_CONFLICT"),
        ("equality", "BR1_PRICE_CONFLICT"),
    ],
)
def test_other_conflicts(conditional, monkeypatch, kind, code):
    s, sid = conditional
    method = {
        "identity": "instrument",
        "rule": "rules",
        "price": "bars",
        "equality": "prev_close_candidates",
    }[kind]
    old = getattr(d1.D1View, method)

    def changed(view):
        x = old(view)
        if kind == "identity":
            return replace(x, kind="preferred_share")
        if kind == "rule":
            return replace(x, trading_cycle="T+0")
        if kind == "price":
            return (replace(x[0], price_basis="pre"), *x[1:])
        return (replace(x[0], observed_equality=False), *x[1:])

    monkeypatch.setattr(d1.D1View, method, changed)
    reject(code, lambda: s.compose_br1(sid, mode=br1.MODE, assumption_ids=br1.ASSUMPTIONS))


def test_reopen_idempotency_and_read_only(br1_view):
    s, v = br1_view
    descriptor = v.descriptor()
    before = {p: p.read_bytes() for p in s.root.rglob("*") if p.is_file()}
    sid = s.compose_br1(
        descriptor.strict_dataset_id, mode=br1.MODE, assumption_ids=tuple(reversed(br1.ASSUMPTIONS))
    )
    assert sid == descriptor.dataset_id
    assert Store(s.root).br1(sid, mode=br1.MODE).owner_receipt() == v.owner_receipt()
    assert all(p.read_bytes() == data for p, data in before.items())
    assert s.recover_br1() == []
    reject("BR1_EXPLICIT_MODE_REQUIRED", lambda: s.br1(sid, mode="strict"))
    reject("BR1_NOT_FOUND", lambda: s.br1("0" * 64, mode=br1.MODE))
    reject("INVALID_ID", lambda: s.br1("latest", mode=br1.MODE))


@pytest.mark.parametrize("kind", ["manifest", "profile", "strict_manifest", "raw_price"])
def test_reopen_verifies_nested_inputs(br1_view, kind):
    s, v = br1_view
    p = {
        "manifest": s.root / "br1-manifests" / (v.descriptor().dataset_id + ".json"),
        "profile": s.root / "br1-objects" / br1.PROFILE_SHA256,
        "strict_manifest": s.root / "d1-manifests" / (v.descriptor().strict_dataset_id + ".json"),
        "raw_price": s.root / "research-objects" / d1.PRICE_OBJECT,
    }[kind]
    p.chmod(0o644)
    p.write_bytes(b"changed")
    with pytest.raises(DataError):
        s.br1(v.descriptor().dataset_id, mode=br1.MODE)


def test_unknown_profile_hash_cannot_authorize(conditional, monkeypatch):
    s, sid = conditional
    monkeypatch.setattr(br1, "PROFILE_SHA256", "0" * 64)
    reject(
        "BR1_PROFILE_INTEGRITY",
        lambda: s.compose_br1(sid, mode=br1.MODE, assumption_ids=br1.ASSUMPTIONS),
    )


@pytest.mark.parametrize("break_reference", [False, True])
def test_crash_recovery_is_explicit_and_checks_references(
    conditional, monkeypatch, break_reference
):
    s, sid = conditional
    real = br1.immutable_write

    def crash(p, data):
        if p.parent.name == "br1-manifests":
            raise OSError("after prepared")
        real(p, data)

    monkeypatch.setattr(br1, "immutable_write", crash)
    with pytest.raises(OSError):
        s.compose_br1(sid, mode=br1.MODE, assumption_ids=br1.ASSUMPTIONS)
    cid = s.br1_snapshots()[0]["dataset_id"]
    reject("BR1_NOT_PUBLISHED", lambda: s.br1(cid, mode=br1.MODE))
    reject(
        "BR1_RECOVERY_REQUIRED",
        lambda: s.compose_br1(sid, mode=br1.MODE, assumption_ids=br1.ASSUMPTIONS),
    )
    monkeypatch.setattr(br1, "immutable_write", real)
    if break_reference:
        (s.root / "br1-objects" / br1.PROFILE_SHA256).unlink()
    assert s.recover_br1()[0]["status"] == ("aborted" if break_reference else "published")
    if not break_reference:
        assert s.br1(cid, mode=br1.MODE).admission().model_projection_permission


def test_forged_policy_does_not_recover(br1_view):
    s, v = br1_view
    body = json.loads(
        (s.root / "br1-manifests" / (v.descriptor().dataset_id + ".json")).read_text()
    )
    body["execution_permission"] = True
    body["assumption_ids"] = []
    with s._db(write=True) as db:
        db.execute(
            "INSERT INTO br1_snapshots VALUES (?,?,?,?)",
            (digest(body), canonical(body).decode(), "prepared", "test"),
        )
    assert s.recover_br1()[0]["status"] == "aborted"


def test_br1_cli_requires_arguments_and_keeps_execution_blocked(conditional, tmp_path, capsys):
    s, sid = conditional
    prefix = ["--store", str(s.root)]
    assert (
        main(
            prefix
            + [
                "br1-compose",
                "--strict",
                sid,
                "--mode",
                br1.MODE,
                "--assumption",
                "A-EQ",
                "--assumption",
                "A-NORMAL",
                "--assumption",
                "A-AD08",
            ]
        )
        == 0
    )
    cid = json.loads(capsys.readouterr().out)["dataset_id"]
    for name in [
        "descriptor",
        "profile",
        "admission",
        "owner-receipt",
        "instrument",
        "rules",
        "bars",
        "calendar",
        "statuses",
        "modeled-events",
    ]:
        assert main(prefix + ["br1-query", name, "--dataset", cid, "--mode", br1.MODE]) == (
            2 if name == "admission" else 0
        )
        assert json.loads(capsys.readouterr().out)
    assert (
        main(
            prefix
            + [
                "br1-query",
                "price-limits",
                "--dataset",
                cid,
                "--mode",
                br1.MODE,
                "--date",
                "2020-01-03",
            ]
        )
        == 0
    )
    assert (
        json.loads(capsys.readouterr().out)["evidence_status"]
        == "derived_under_declared_assumptions"
    )
    call = tmp_path / "call.json"
    call.write_text(json.dumps(s.br1(cid, mode=br1.MODE).profile().allowed_calls[0].to_dict()))
    assert (
        main(
            prefix
            + [
                "br1-query",
                "adjusted-prev-close",
                "--dataset",
                cid,
                "--mode",
                br1.MODE,
                "--call",
                str(call),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["value"] == "10.05"
    assert (
        main(
            prefix
            + ["br1-query", "bars", "--dataset", cid, "--mode", br1.MODE, "--date", "2020-01-03"]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "BR1_QUERY_ARGUMENT"
