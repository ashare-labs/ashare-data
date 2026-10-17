"""Synthetic contract counterexamples; never runs RQ events, orders or recovery."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
import json

import pytest

from ashare_data import DataError
from ashare_data.research import conditional as conditional_api
from ashare_data.research import conditional_listing as conditional_listing
from ashare_data.research import finite_facts as facts
from ashare_data.cli import main
from test_conditional import conditional_view as fixture_conditional_view, conditional  # noqa: F401
from test_finite_facts import prepared, published_facts, reject  # noqa: F401


CONTEXT = "a" * 64  # Explicit test-only context, not a real run permission.
CURRENT = dict(
    security="600000.XSHG",
    current_date="2020-01-03",
    target_date="2020-01-03",
    role="current",
    context_sha256=CONTEXT,
)


@pytest.fixture
def listing(request):
    v = request.getfixturevalue("fixture_conditional_view")[1]
    return v.listing(contract_sha256=v.listing_contract().contract_sha256)


def test_four_dates_and_two_successors_are_conditional(listing, request):
    conditional_view = request.getfixturevalue("fixture_conditional_view")
    c = listing.descriptor()
    assert c.owner_version == "0.10.0.dev1" and not c.execution_permission
    assert c.query_dates == facts.DAYS
    assert c.event_dates == (date(2020, 1, 3), date(2020, 1, 6))
    for role, current, target in c.allowed_requests:
        p = listing.status(
            security=facts.SECURITY,
            current_date=current,
            target_date=target,
            role=role,
            context_sha256=CONTEXT,
        )
        assert p.model_listed is True and p.model_delisted is False
        assert p.absolute_delisting_date is None and p.absolute_date_status == "unknown"
        assert p.assumption_ids == ("A-NORMAL",)
        assert p.evidence_status == "derived_under_declared_assumptions"
        assert not p.execution_permission and not p.historical_pit
        if role == "settlement_successor":
            assert (
                listing.successor(
                    security=facts.SECURITY,
                    current_date=current,
                    candidate_date=target,
                    context_sha256=CONTEXT,
                )
                == p
            )
    assert conditional_view[1].instrument().fact.historical_eligible is None
    assert len(conditional_view[1].admission().strict_blockers) == 4
    reject("CONDITIONAL_LISTING_REVIEW_REQUIRED", listing.require_execution)
    reject("CONDITIONAL_REVIEW_REQUIRED", conditional_view[1].require_execution)


@pytest.mark.parametrize("key", ["current_date", "target_date"])
@pytest.mark.parametrize("bad", ["2019-12-31", "2020-01-04", "2020-01-05", "2020-01-08"])
def test_both_date_dimensions_are_bounded(listing, key, bad):
    reject("CONDITIONAL_LISTING_SCOPE", lambda: listing.status(**dict(CURRENT, **{key: bad})))


@pytest.mark.parametrize(
    "bad",
    [
        datetime(2020, 1, 3),
        datetime(2020, 1, 2, 16, tzinfo=timezone.utc),
        "20200103",
        "2020-1-3",
        "2020-01-03T00:00:00+08:00",
        None,
        True,
    ],
)
def test_no_implicit_clock_or_timezone_coercion(listing, bad):
    reject("CONDITIONAL_LISTING_DATE", lambda: listing.status(**dict(CURRENT, current_date=bad)))


@pytest.mark.parametrize(
    "current,target,role,code",
    [
        ("2020-01-03", "2020-01-06", "current", "CONDITIONAL_LISTING_ROLE"),
        ("2020-01-02", "2020-01-02", "current", "CONDITIONAL_LISTING_ROLE"),
        ("2020-01-07", "2020-01-07", "current", "CONDITIONAL_LISTING_ROLE"),
        ("2020-01-03", "2020-01-03", "warmup", "CONDITIONAL_LISTING_ROLE"),
        ("2020-01-02", "2020-01-03", "settlement_successor", "CONDITIONAL_LISTING_ROLE"),
        ("2020-01-03", "2020-01-03", "AFTER_TRADING", "CONDITIONAL_LISTING_ROLE"),
    ],
)
def test_query_roles_do_not_extend_execution_days(listing, current, target, role, code):
    reject(
        code,
        lambda: listing.status(
            **dict(CURRENT, current_date=current, target_date=target, role=role)
        ),
    )


@pytest.mark.parametrize(
    "current,target",
    [
        ("2020-01-07", "2020-01-07"),  # RQ clamp at last calendar day.
        ("2020-01-06", "2020-01-03"),  # Backwards, although both dates are covered.
        ("2020-01-03", "2020-01-03"),
        ("2020-01-03", "2020-01-07"),
    ],
)
def test_same_backwards_missing_or_wrong_successor_rejected(listing, current, target):
    reject(
        "CONDITIONAL_LISTING_SUCCESSOR",
        lambda: listing.successor(
            security=facts.SECURITY,
            current_date=current,
            candidate_date=target,
            context_sha256=CONTEXT,
        ),
    )


@pytest.mark.parametrize("n", [0, 2, -1, True, 1.0, None, "1"])
def test_successor_n_is_exact(listing, n):
    reject(
        "CONDITIONAL_LISTING_SUCCESSOR",
        lambda: listing.successor(
            security=facts.SECURITY,
            current_date="2020-01-03",
            candidate_date="2020-01-06",
            context_sha256=CONTEXT,
            n=n,
        ),
    )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("security", "000001.XSHE", "CONDITIONAL_LISTING_SECURITY"),
        ("context_sha256", None, "CONDITIONAL_LISTING_CONTEXT"),
        ("context_sha256", "A" * 64, "CONDITIONAL_LISTING_CONTEXT"),
        ("role", None, "CONDITIONAL_LISTING_ROLE"),
    ],
)
def test_binding_input_types(listing, field, value, code):
    reject(code, lambda: listing.status(**dict(CURRENT, **{field: value})))


def test_cache_and_recovery_context_must_match_current_request(listing):
    p = listing.status(**CURRENT)
    assert listing.revalidate(p, **CURRENT) == p
    reject(
        "CONDITIONAL_LISTING_CACHE_MISMATCH",
        lambda: listing.revalidate(p, **dict(CURRENT, context_sha256="b" * 64)),
    )  # phase/cursor/run/recovery changed.
    reject(
        "CONDITIONAL_LISTING_CACHE_MISMATCH",
        lambda: listing.revalidate(
            p, **dict(CURRENT, current_date="2020-01-06", target_date="2020-01-06")
        ),
    )
    reject(
        "CONDITIONAL_LISTING_SCOPE",
        lambda: listing.revalidate(p, **dict(CURRENT, current_date="2020-01-08")),
    )
    reject("CONDITIONAL_LISTING_CACHE_MISMATCH", lambda: listing.revalidate(p.to_dict(), **CURRENT))
    for field, value in [
        ("model_delisted", True),
        ("model_delisted", None),
        ("model_delisted", 0),
        ("model_listed", False),
        ("contract_sha256", "0" * 64),
        ("dataset_id", "0" * 64),
        ("profile_sha256", "0" * 64),
        ("absolute_delisting_date", date(2999, 12, 31)),
        ("evidence_status", "verified"),
        ("execution_permission", True),
    ]:
        reject(
            "CONDITIONAL_LISTING_CACHE_MISMATCH",
            lambda: listing.revalidate(replace(p, **{field: value}), **CURRENT),
        )


def test_results_are_immutable_and_copies_detached(listing):
    p = listing.status(**CURRENT)
    with pytest.raises(FrozenInstanceError):
        p.model_delisted = True
    j = p.to_dict()
    j["assumption_ids"].clear()
    c = listing.descriptor().source_fields
    c["policy"]["requests"].clear()
    assert listing.status(**CURRENT).assumption_ids == ("A-NORMAL",)
    assert len(listing.descriptor().allowed_requests) == 5


def test_reopen_after_binding_rechecks_underlying_objects(listing, request):
    s, _ = request.getfixturevalue("fixture_conditional_view")
    p = listing.status(**CURRENT)
    path = s.root / "research-objects" / facts.PRICE_OBJECT
    path.chmod(0o644)
    path.write_bytes(b"changed after binding")
    with pytest.raises(DataError):
        listing.revalidate(p, **CURRENT)
    with pytest.raises(DataError):
        listing.status(**CURRENT)


def test_wrong_pin_and_policy_change_fail_closed(request, listing, monkeypatch):
    v = request.getfixturevalue("fixture_conditional_view")[1]
    reject("CONDITIONAL_LISTING_BINDING", lambda: v.listing(contract_sha256="0" * 64))
    with pytest.raises(TypeError):
        v.listing()
    monkeypatch.setattr(conditional_listing, "POLICY_SHA256", "0" * 64)
    reject("CONDITIONAL_LISTING_POLICY_INTEGRITY", lambda: listing.status(**CURRENT))


@pytest.mark.parametrize("value", [False, None])
def test_unknown_or_ineligible_model_cannot_be_projected(listing, monkeypatch, value):
    old = conditional_api.ConditionalView.instrument
    monkeypatch.setattr(
        conditional_api.ConditionalView,
        "instrument",
        lambda v: replace(old(v), model_eligible=value),
    )
    reject("CONDITIONAL_LISTING_CONFLICT", lambda: listing.status(**CURRENT))


@pytest.mark.parametrize("value", [True, False])
def test_new_historical_eligibility_needs_new_review(listing, monkeypatch, value):
    old = facts.FiniteFactsView.instrument
    monkeypatch.setattr(
        facts.FiniteFactsView, "instrument", lambda v: replace(old(v), historical_eligible=value)
    )
    reject("CONDITIONAL_LISTING_CONFLICT", lambda: listing.status(**CURRENT))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("is_trading_day", False, "CONDITIONAL_LISTING_CALENDAR"),
        ("next_open", None, "CONDITIONAL_LISTING_SUCCESSOR"),
        ("next_open", date(2020, 1, 3), "CONDITIONAL_LISTING_SUCCESSOR"),
        ("next_open", date(2020, 1, 2), "CONDITIONAL_LISTING_SUCCESSOR"),
    ],
)
def test_source_calendar_contradictions_stop(listing, monkeypatch, field, value, code):
    old = conditional_api.ConditionalView.calendar
    monkeypatch.setattr(
        conditional_api.ConditionalView,
        "calendar",
        lambda v: tuple(
            replace(x, **{field: value}) if x.trade_date == date(2020, 1, 3) else x for x in old(v)
        ),
    )
    reject(
        code,
        lambda: listing.successor(
            security=facts.SECURITY,
            current_date="2020-01-03",
            candidate_date="2020-01-06",
            context_sha256=CONTEXT,
        ),
    )


def test_source_status_and_related_event_rechecked_for_cached_use(listing, monkeypatch):
    p = listing.status(**CURRENT)
    old = facts.FiniteFactsView.statuses
    monkeypatch.setattr(
        facts.FiniteFactsView,
        "statuses",
        lambda v: tuple(replace(x, suspended=None) for x in old(v)),
    )
    reject("CONDITIONAL_STATUS_BLOCKED", lambda: listing.revalidate(p, **CURRENT))
    monkeypatch.setattr(facts.FiniteFactsView, "statuses", old)
    events = facts.FiniteFactsView.known_events
    monkeypatch.setattr(
        facts.FiniteFactsView,
        "known_events",
        lambda v: tuple(replace(x, record_date=date(2020, 1, 3)) for x in events(v)),
    )
    reject("CONDITIONAL_RELATED_EVENT", lambda: listing.revalidate(p, **CURRENT))


def test_cli_contract_query_and_rejections(request, tmp_path, capsys):
    s, v = request.getfixturevalue("fixture_conditional_view")
    prefix = ["--store", str(s.root), "conditional-listing"]
    pin = ["--dataset", v.descriptor().dataset_id, "--mode", conditional_api.MODE]
    assert main(prefix + ["contract"] + pin) == 0
    c = json.loads(capsys.readouterr().out)
    assert c["contract_sha256"] == v.listing_contract().contract_sha256
    call = tmp_path / "listing-call.json"
    call.write_text(json.dumps(CURRENT))
    args = prefix + ["status"] + pin + ["--contract", c["contract_sha256"], "--call", str(call)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["absolute_delisting_date"] is None
    call.write_text(json.dumps(dict(CURRENT, target_date="2020-01-08")))
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CONDITIONAL_LISTING_SCOPE"
    assert main(prefix + ["status"] + pin) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CONDITIONAL_LISTING_ARGUMENT"
