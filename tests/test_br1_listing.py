"""Synthetic contract counterexamples; never runs RQ events, orders or recovery."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
import json

import pytest

from ashare_data import DataError, br1, br1_listing, d1
from ashare_data.cli import main
from test_br1 import br1_view as fixture_br1_view, conditional  # noqa: F401
from test_d1 import prepared, published_d1, reject  # noqa: F401


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
    v = request.getfixturevalue("fixture_br1_view")[1]
    return v.listing(contract_sha256=v.listing_contract().contract_sha256)


def test_four_dates_and_two_successors_are_conditional(listing, request):
    br1_view = request.getfixturevalue("fixture_br1_view")
    c = listing.descriptor()
    assert c.owner_version == "0.6.0.dev3" and not c.execution_permission
    assert c.query_dates == d1.DAYS
    assert c.event_dates == (date(2020, 1, 3), date(2020, 1, 6))
    for role, current, target in c.allowed_requests:
        p = listing.status(
            security=d1.SECURITY,
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
                    security=d1.SECURITY,
                    current_date=current,
                    candidate_date=target,
                    context_sha256=CONTEXT,
                )
                == p
            )
    assert br1_view[1].instrument().fact.historical_eligible is None
    assert len(br1_view[1].admission().strict_blockers) == 4
    reject("BR1_LISTING_REVIEW_REQUIRED", listing.require_execution)
    reject("BR1_REVIEW_REQUIRED", br1_view[1].require_execution)


@pytest.mark.parametrize("key", ["current_date", "target_date"])
@pytest.mark.parametrize("bad", ["2019-12-31", "2020-01-04", "2020-01-05", "2020-01-08"])
def test_both_date_dimensions_are_bounded(listing, key, bad):
    reject("BR1_LISTING_SCOPE", lambda: listing.status(**dict(CURRENT, **{key: bad})))


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
    reject("BR1_LISTING_DATE", lambda: listing.status(**dict(CURRENT, current_date=bad)))


@pytest.mark.parametrize(
    "current,target,role,code",
    [
        ("2020-01-03", "2020-01-06", "current", "BR1_LISTING_ROLE"),
        ("2020-01-02", "2020-01-02", "current", "BR1_LISTING_ROLE"),
        ("2020-01-07", "2020-01-07", "current", "BR1_LISTING_ROLE"),
        ("2020-01-03", "2020-01-03", "warmup", "BR1_LISTING_ROLE"),
        ("2020-01-02", "2020-01-03", "settlement_successor", "BR1_LISTING_ROLE"),
        ("2020-01-03", "2020-01-03", "AFTER_TRADING", "BR1_LISTING_ROLE"),
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
        "BR1_LISTING_SUCCESSOR",
        lambda: listing.successor(
            security=d1.SECURITY,
            current_date=current,
            candidate_date=target,
            context_sha256=CONTEXT,
        ),
    )


@pytest.mark.parametrize("n", [0, 2, -1, True, 1.0, None, "1"])
def test_successor_n_is_exact(listing, n):
    reject(
        "BR1_LISTING_SUCCESSOR",
        lambda: listing.successor(
            security=d1.SECURITY,
            current_date="2020-01-03",
            candidate_date="2020-01-06",
            context_sha256=CONTEXT,
            n=n,
        ),
    )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("security", "000001.XSHE", "BR1_LISTING_SECURITY"),
        ("context_sha256", None, "BR1_LISTING_CONTEXT"),
        ("context_sha256", "A" * 64, "BR1_LISTING_CONTEXT"),
        ("role", None, "BR1_LISTING_ROLE"),
    ],
)
def test_binding_input_types(listing, field, value, code):
    reject(code, lambda: listing.status(**dict(CURRENT, **{field: value})))


def test_cache_and_recovery_context_must_match_current_request(listing):
    p = listing.status(**CURRENT)
    assert listing.revalidate(p, **CURRENT) == p
    reject(
        "BR1_LISTING_CACHE_MISMATCH",
        lambda: listing.revalidate(p, **dict(CURRENT, context_sha256="b" * 64)),
    )  # phase/cursor/run/recovery changed.
    reject(
        "BR1_LISTING_CACHE_MISMATCH",
        lambda: listing.revalidate(
            p, **dict(CURRENT, current_date="2020-01-06", target_date="2020-01-06")
        ),
    )
    reject(
        "BR1_LISTING_SCOPE",
        lambda: listing.revalidate(p, **dict(CURRENT, current_date="2020-01-08")),
    )
    reject("BR1_LISTING_CACHE_MISMATCH", lambda: listing.revalidate(p.to_dict(), **CURRENT))
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
            "BR1_LISTING_CACHE_MISMATCH",
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
    s, _ = request.getfixturevalue("fixture_br1_view")
    p = listing.status(**CURRENT)
    path = s.root / "research-objects" / d1.PRICE_OBJECT
    path.chmod(0o644)
    path.write_bytes(b"changed after binding")
    with pytest.raises(DataError):
        listing.revalidate(p, **CURRENT)
    with pytest.raises(DataError):
        listing.status(**CURRENT)


def test_wrong_pin_and_policy_change_fail_closed(request, listing, monkeypatch):
    v = request.getfixturevalue("fixture_br1_view")[1]
    reject("BR1_LISTING_BINDING", lambda: v.listing(contract_sha256="0" * 64))
    with pytest.raises(TypeError):
        v.listing()
    monkeypatch.setattr(br1_listing, "POLICY_SHA256", "0" * 64)
    reject("BR1_LISTING_POLICY_INTEGRITY", lambda: listing.status(**CURRENT))


@pytest.mark.parametrize("value", [False, None])
def test_unknown_or_ineligible_model_cannot_be_projected(listing, monkeypatch, value):
    old = br1.BR1View.instrument
    monkeypatch.setattr(br1.BR1View, "instrument", lambda v: replace(old(v), model_eligible=value))
    reject("BR1_LISTING_CONFLICT", lambda: listing.status(**CURRENT))


@pytest.mark.parametrize("value", [True, False])
def test_new_historical_eligibility_needs_new_review(listing, monkeypatch, value):
    old = d1.D1View.instrument
    monkeypatch.setattr(
        d1.D1View, "instrument", lambda v: replace(old(v), historical_eligible=value)
    )
    reject("BR1_LISTING_CONFLICT", lambda: listing.status(**CURRENT))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("is_trading_day", False, "BR1_LISTING_CALENDAR"),
        ("next_open", None, "BR1_LISTING_SUCCESSOR"),
        ("next_open", date(2020, 1, 3), "BR1_LISTING_SUCCESSOR"),
        ("next_open", date(2020, 1, 2), "BR1_LISTING_SUCCESSOR"),
    ],
)
def test_source_calendar_contradictions_stop(listing, monkeypatch, field, value, code):
    old = br1.BR1View.calendar
    monkeypatch.setattr(
        br1.BR1View,
        "calendar",
        lambda v: tuple(
            replace(x, **{field: value}) if x.trade_date == date(2020, 1, 3) else x for x in old(v)
        ),
    )
    reject(
        code,
        lambda: listing.successor(
            security=d1.SECURITY,
            current_date="2020-01-03",
            candidate_date="2020-01-06",
            context_sha256=CONTEXT,
        ),
    )


def test_source_status_and_related_event_rechecked_for_cached_use(listing, monkeypatch):
    p = listing.status(**CURRENT)
    old = d1.D1View.statuses
    monkeypatch.setattr(
        d1.D1View, "statuses", lambda v: tuple(replace(x, suspended=None) for x in old(v))
    )
    reject("BR1_STATUS_BLOCKED", lambda: listing.revalidate(p, **CURRENT))
    monkeypatch.setattr(d1.D1View, "statuses", old)
    events = d1.D1View.known_events
    monkeypatch.setattr(
        d1.D1View,
        "known_events",
        lambda v: tuple(replace(x, record_date=date(2020, 1, 3)) for x in events(v)),
    )
    reject("BR1_RELATED_EVENT", lambda: listing.revalidate(p, **CURRENT))


def test_cli_contract_query_and_rejections(request, tmp_path, capsys):
    s, v = request.getfixturevalue("fixture_br1_view")
    prefix = ["--store", str(s.root), "br1-listing"]
    pin = ["--dataset", v.descriptor().dataset_id, "--mode", br1.MODE]
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
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "BR1_LISTING_SCOPE"
    assert main(prefix + ["status"] + pin) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "BR1_LISTING_ARGUMENT"
