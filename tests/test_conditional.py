"""CONDITIONAL opt-in, immutable policy and contradiction gates. No engine execution."""

import json
from dataclasses import replace, FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal

import pytest

from ashare_data import Store, DataError
from ashare_data.research import conditional as conditional_api
from ashare_data.research import finite_facts as facts
from ashare_data.cli import main
from ashare_data.core.model import canonical, digest
from test_finite_facts import prepared, published_facts, reject  # noqa: F401


@pytest.fixture
def conditional(request):
    store, fid, sid = request.getfixturevalue("published_facts")
    return store, sid


@pytest.fixture
def conditional_view(conditional):
    s, sid = conditional
    cid = s.compose_conditional(
        sid, mode="conditional_research", assumption_ids=("A-EQ", "A-NORMAL", "A-AD08")
    )
    return s, s.conditional(cid, mode="conditional_research")


def test_requires_explicit_opt_in_and_frozen_assumptions(conditional):
    s, sid = conditional
    with pytest.raises(TypeError):
        s.compose_conditional(sid)
    with pytest.raises(TypeError):
        s.conditional(sid)
    for mode in (None, "strict", "assumed", True, "CONDITIONAL_RESEARCH"):
        reject(
            "CONDITIONAL_EXPLICIT_MODE_REQUIRED",
            lambda: s.compose_conditional(
                sid, mode=mode, assumption_ids=conditional_api.ASSUMPTIONS
            ),
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
            "CONDITIONAL_ASSUMPTIONS_REQUIRED",
            lambda: s.compose_conditional(sid, mode=conditional_api.MODE, assumption_ids=ids),
        )
    reject(
        "CONDITIONAL_UNREVIEWED_INPUT",
        lambda: s.compose_conditional(
            "0" * 64, mode=conditional_api.MODE, assumption_ids=conditional_api.ASSUMPTIONS
        ),
    )
    assert s.conditional_snapshots() == []


def test_conditional_projection_preserves_strict_facts(conditional_view):
    s, v = conditional_view
    sid = v.descriptor().strict_dataset_id
    old = s.facts(sid).owner_receipt().to_dict()
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
    assert s.facts(sid).owner_receipt().to_dict() == old
    reject("FACTS_FACT_UNAVAILABLE", s.facts(sid).corporate_actions)
    reject("CONDITIONAL_REVIEW_REQUIRED", v.require_execution)


def test_backend_date_dto_interop_is_exact(conditional_view):
    v = conditional_view[1]
    for c in v.profile().allowed_calls:
        call = c.to_dict()
        for key in ("trade_date", "history_dt", "adjust_orig"):
            call[key] = date.fromisoformat(call[key])
        assert v.adjusted_prev_close(**call).call == c
        assert v.price_limits(call["trade_date"]).trade_date == c.trade_date
        call["history_dt"] = datetime.combine(call["history_dt"], datetime.min.time())
        reject("CONDITIONAL_UNREVIEWED_CALL", lambda: v.adjusted_prev_close(**call))


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
def test_complete_tuple_gate(conditional_view, key, value):
    v = conditional_view[1]
    for c in v.profile().allowed_calls:
        call = c.to_dict()
        call[key] = value
        reject("CONDITIONAL_UNREVIEWED_CALL", lambda: v.adjusted_prev_close(**call))


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
    original = facts.FiniteFactsView.statuses

    def invalid(view):
        rows = original(view)
        return (replace(rows[0], **{field: value}), *rows[1:])

    monkeypatch.setattr(facts.FiniteFactsView, "statuses", invalid)
    reject(
        "CONDITIONAL_STATUS_BLOCKED",
        lambda: s.compose_conditional(
            sid, mode=conditional_api.MODE, assumption_ids=conditional_api.ASSUMPTIONS
        ),
    )
    assert s.conditional_snapshots() == []


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("record_date", date(2020, 1, 3), "CONDITIONAL_RELATED_EVENT"),
        ("record_date", date(2020, 1, 6), "CONDITIONAL_RELATED_EVENT"),
        ("record_date", None, "CONDITIONAL_EVENT_UNKNOWN"),
        ("ex_date", date(2020, 1, 2), "CONDITIONAL_RELATED_EVENT"),
        ("effective_date", date(2019, 12, 31), "CONDITIONAL_RELATED_EVENT"),
        ("conversion_start", date(2020, 1, 7), "CONDITIONAL_RELATED_EVENT"),
        ("entitled_security", "999999.XSHG", "CONDITIONAL_IDENTITY_CONFLICT"),
    ],
)
def test_related_event_blocks_even_when_payment_is_later(
    conditional, monkeypatch, field, value, code
):
    s, sid = conditional
    original = facts.FiniteFactsView.known_events
    monkeypatch.setattr(
        facts.FiniteFactsView,
        "known_events",
        lambda view: tuple(replace(e, **{field: value}) for e in original(view)),
    )
    reject(
        code,
        lambda: s.compose_conditional(
            sid, mode=conditional_api.MODE, assumption_ids=conditional_api.ASSUMPTIONS
        ),
    )
    assert s.conditional_snapshots() == []


@pytest.mark.parametrize(
    "kind,code",
    [
        ("identity", "CONDITIONAL_IDENTITY_CONFLICT"),
        ("rule", "CONDITIONAL_RULE_CONFLICT"),
        ("price", "CONDITIONAL_PRICE_CONFLICT"),
        ("equality", "CONDITIONAL_PRICE_CONFLICT"),
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
    old = getattr(facts.FiniteFactsView, method)

    def changed(view):
        x = old(view)
        if kind == "identity":
            return replace(x, kind="preferred_share")
        if kind == "rule":
            return replace(x, trading_cycle="T+0")
        if kind == "price":
            return (replace(x[0], price_basis="pre"), *x[1:])
        return (replace(x[0], observed_equality=False), *x[1:])

    monkeypatch.setattr(facts.FiniteFactsView, method, changed)
    reject(
        code,
        lambda: s.compose_conditional(
            sid, mode=conditional_api.MODE, assumption_ids=conditional_api.ASSUMPTIONS
        ),
    )


def test_reopen_idempotency_and_read_only(conditional_view):
    s, v = conditional_view
    descriptor = v.descriptor()
    before = {p: p.read_bytes() for p in s.root.rglob("*") if p.is_file()}
    sid = s.compose_conditional(
        descriptor.strict_dataset_id,
        mode=conditional_api.MODE,
        assumption_ids=tuple(reversed(conditional_api.ASSUMPTIONS)),
    )
    assert sid == descriptor.dataset_id
    assert (
        Store(s.root).conditional(sid, mode=conditional_api.MODE).owner_receipt()
        == v.owner_receipt()
    )
    assert all(p.read_bytes() == data for p, data in before.items())
    assert s.recover_conditional() == []
    reject("CONDITIONAL_EXPLICIT_MODE_REQUIRED", lambda: s.conditional(sid, mode="strict"))
    reject("CONDITIONAL_NOT_FOUND", lambda: s.conditional("0" * 64, mode=conditional_api.MODE))
    reject("INVALID_ID", lambda: s.conditional("latest", mode=conditional_api.MODE))


@pytest.mark.parametrize("kind", ["manifest", "profile", "strict_manifest", "raw_price"])
def test_reopen_verifies_nested_inputs(conditional_view, kind):
    s, v = conditional_view
    p = {
        "manifest": s.root / "conditional-manifests" / (v.descriptor().dataset_id + ".json"),
        "profile": s.root / "conditional-objects" / conditional_api.PROFILE_SHA256,
        "strict_manifest": s.root
        / "facts-manifests"
        / (v.descriptor().strict_dataset_id + ".json"),
        "raw_price": s.root / "research-objects" / facts.PRICE_OBJECT,
    }[kind]
    p.chmod(0o644)
    p.write_bytes(b"changed")
    with pytest.raises(DataError):
        s.conditional(v.descriptor().dataset_id, mode=conditional_api.MODE)


def test_unknown_profile_hash_cannot_authorize(conditional, monkeypatch):
    s, sid = conditional
    monkeypatch.setattr(conditional_api, "PROFILE_SHA256", "0" * 64)
    reject(
        "CONDITIONAL_PROFILE_INTEGRITY",
        lambda: s.compose_conditional(
            sid, mode=conditional_api.MODE, assumption_ids=conditional_api.ASSUMPTIONS
        ),
    )


@pytest.mark.parametrize("break_reference", [False, True])
def test_crash_recovery_is_explicit_and_checks_references(
    conditional, monkeypatch, break_reference
):
    s, sid = conditional
    real = conditional_api.immutable_write

    def crash(p, data):
        if p.parent.name == "conditional-manifests":
            raise OSError("after prepared")
        real(p, data)

    monkeypatch.setattr(conditional_api, "immutable_write", crash)
    with pytest.raises(OSError):
        s.compose_conditional(
            sid, mode=conditional_api.MODE, assumption_ids=conditional_api.ASSUMPTIONS
        )
    cid = s.conditional_snapshots()[0]["dataset_id"]
    reject("CONDITIONAL_NOT_PUBLISHED", lambda: s.conditional(cid, mode=conditional_api.MODE))
    reject(
        "CONDITIONAL_RECOVERY_REQUIRED",
        lambda: s.compose_conditional(
            sid, mode=conditional_api.MODE, assumption_ids=conditional_api.ASSUMPTIONS
        ),
    )
    monkeypatch.setattr(conditional_api, "immutable_write", real)
    if break_reference:
        (s.root / "conditional-objects" / conditional_api.PROFILE_SHA256).unlink()
    assert s.recover_conditional()[0]["status"] == ("aborted" if break_reference else "published")
    if not break_reference:
        assert s.conditional(cid, mode=conditional_api.MODE).admission().model_projection_permission


def test_forged_policy_does_not_recover(conditional_view):
    s, v = conditional_view
    body = json.loads(
        (s.root / "conditional-manifests" / (v.descriptor().dataset_id + ".json")).read_text()
    )
    body["execution_permission"] = True
    body["assumption_ids"] = []
    with s._db(write=True) as db:
        db.execute(
            "INSERT INTO conditional_snapshots VALUES (?,?,?,?)",
            (digest(body), canonical(body).decode(), "prepared", "test"),
        )
    assert s.recover_conditional()[0]["status"] == "aborted"


def test_conditional_cli_requires_arguments_and_keeps_execution_blocked(
    conditional, tmp_path, capsys
):
    s, sid = conditional
    prefix = ["--store", str(s.root)]
    assert (
        main(
            prefix
            + [
                "conditional-compose",
                "--strict",
                sid,
                "--mode",
                conditional_api.MODE,
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
        assert main(
            prefix + ["conditional-query", name, "--dataset", cid, "--mode", conditional_api.MODE]
        ) == (2 if name == "admission" else 0)
        assert json.loads(capsys.readouterr().out)
    assert (
        main(
            prefix
            + [
                "conditional-query",
                "price-limits",
                "--dataset",
                cid,
                "--mode",
                conditional_api.MODE,
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
    call.write_text(
        json.dumps(
            s.conditional(cid, mode=conditional_api.MODE).profile().allowed_calls[0].to_dict()
        )
    )
    assert (
        main(
            prefix
            + [
                "conditional-query",
                "adjusted-prev-close",
                "--dataset",
                cid,
                "--mode",
                conditional_api.MODE,
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
            + [
                "conditional-query",
                "bars",
                "--dataset",
                cid,
                "--mode",
                conditional_api.MODE,
                "--date",
                "2020-01-03",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CONDITIONAL_QUERY_ARGUMENT"
