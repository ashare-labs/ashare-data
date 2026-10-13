"""Synthetic protocol boundaries plus opt-in tests against the private real package."""

from copy import deepcopy
from decimal import localcontext
import hashlib
import json

import pytest

from ashare_data import DataError
from ashare_data.daily_execution import (
    ASSUMPTIONS,
    DATA_SCHEMA,
    SCHEMA,
    DailyExecutionView,
    standard_limits,
)
from test_daily_research import data, write_package  # noqa: F401 - shared synthetic fixture


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_supplement(root, base, payload, mutate_binding=None):
    root.mkdir(exist_ok=True)
    raw = json.dumps(payload).encode()
    (root / "data.json").write_bytes(raw)
    b = dict(
        schema=SCHEMA,
        historical_pit=False,
        execution_permission=False,
        research_binding_sha256=base.binding_sha256,
        producer={"name": "synthetic-test-only"},
        data="data.json",
        files=[dict(path="data.json", bytes=len(raw), sha256=digest(raw))],
    )
    if mutate_binding:
        mutate_binding(b)
    raw = json.dumps(b).encode()
    (root / "binding.json").write_bytes(raw)
    return DailyExecutionView(base, root / "binding.json", expected_binding_sha256=digest(raw))


@pytest.fixture
def package(tmp_path, data):  # noqa: F811 - imported pytest fixture
    data["windows"][0]["output_sessions"] = 4
    data["actions"][0].update(
        type="cash_dividend", bonus_share_ratio="0", capitalization_share_ratio="0"
    )
    base = write_package(tmp_path / "base", data)
    w = data["windows"][0]

    def f(value, status="source_observed", assumptions=()):
        return dict(
            value=value,
            status=status,
            source_refs=["base:source.txt"],
            historical_available_at=None,
            assumption_ids=list(assumptions),
            reason="Synthetic protocol evidence",
        )

    states = []
    for label in w["sessions"][120:-1]:
        up, down = standard_limits("10.05", False)
        states.append(
            dict(
                security="000001.XSHE",
                date=label,
                is_st=f(False),
                suspended=f(False),
                reference_price=f("10.05"),
                upper_limit=f(up, "derived", ["standard_mainboard_limits"]),
                lower_limit=f(down, "derived", ["standard_mainboard_limits"]),
            )
        )
    action = deepcopy(data["actions"][0])
    action.update(
        source_refs=["base:source.txt"],
        cash_available_at=None,
        new_shares_credit_date=None,
        new_shares_tradable_date=None,
        share_timing_status="not_applicable_explicit_zero_shares",
    )
    payload = dict(
        schema=DATA_SCHEMA,
        states=states,
        actions=[action],
        quality={"synthetic": True},
        calendars={
            "w": dict(sessions=w["sessions"], mismatches=[], source_refs=["base:source.txt"])
        },
        action_coverage={
            "w": {
                "000001.XSHE": dict(
                    status="scoped_checked", reason="Synthetic", source_refs=["base:source.txt"]
                )
            }
        },
    )
    return base, payload, tmp_path / "supplement"


def create(package):
    base, payload, root = package
    return write_supplement(root, base, payload)


def options(**extra):
    return dict(
        purpose="retrospective_simulation",
        accepted_assumptions=tuple(ASSUMPTIONS),
        cash_credit_policy="pay_date_after_close",
        **extra,
    )


def test_known_inputs_still_require_explicit_models_and_cash_phase(package):
    v = create(package)
    p = v.preflight("w", purpose="retrospective_simulation")
    assert not p["ready"]
    assert any(x["code"] == "MODEL_NOT_ACCEPTED" for x in p["issues"])
    assert any(x["field"] == "cash_available_at" for x in p["issues"])
    assert v.preflight("w", **options())["ready"]
    r = v.read_window("w", **options())
    assert len(r["bars"]) == 125 and len(r["execution_states"]) == 4
    assert "research_state" not in r["bars"][0]
    assert r["report"]["simulation_admitted"] and not r["report"]["execution_permission"]
    r["execution_states"][0]["is_st"]["value"] = True
    assert not v.read_window("w", **options())["execution_states"][0]["is_st"]["value"]


def test_unknown_cannot_be_waived_or_silently_removed(package):
    base, payload, root = package
    row = payload["states"][1]
    for field in ("is_st", "upper_limit", "lower_limit"):
        row[field].update(value=None, status="unavailable", assumption_ids=[])
    v = create(package)
    p = v.preflight("w", **options())
    missing = [x for x in p["issues"] if x["code"] == "FIELD_UNAVAILABLE"]
    assert len(missing) == 3 and not p["ready"]
    assert {x["security"] for x in missing} == {"000001.XSHE"}
    assert {x["date"] for x in missing} == {row["date"]}
    with pytest.raises(DataError) as e:
        v.read_window("w", **options())
    assert e.value.code == "DAILY_EXECUTION_NOT_READY" and len(e.value.details["issues"]) == 3
    signals = v.read_window("w", purpose="signal_research")
    assert len(signals["bars"]) == 125 and len(signals["execution_states"]) == 4
    assert signals["admission"]["ready"] and not signals["report"]["simulation_admitted"]
    assert all(not x["blocking"] for x in signals["admission"]["issues"])


@pytest.mark.parametrize("purpose", ["historical_pit", "live_execution"])
def test_reconstruction_never_admits_pit_or_live(package, purpose):
    v = create(package)
    args = options()
    args["purpose"] = purpose
    p = v.preflight("w", **args)
    assert not p["ready"]
    assert any(x["code"] == "HISTORICAL_DELIVERY_UNVERIFIED" for x in p["issues"])
    with pytest.raises(DataError):
        v.read_window("w", **args)


def test_state_clock_and_strategy_history_do_not_leak_current_bar(package):
    v = create(package)
    label = package[1]["states"][0]["date"]
    args = dict(accepted_assumptions=tuple(ASSUMPTIONS), cash_credit_policy="pay_date_after_close")
    with pytest.raises(DataError, match="09:00"):
        v.state("w", "000001.XSHE", as_of=label + "T08:59:59+08:00", **args)
    r = v.state("w", "000001.XSHE", as_of=label + "T09:00:00+08:00", **args)
    assert not set(r["row"]) & {"open", "close", "volume", "money", "high", "low"}
    h = v.history("w", "000001.XSHE", as_of=label + "T09:00:00+08:00", count=120)
    assert len(h["rows"]) == 120 and h["rows"][-1]["date"] < label


def test_cash_receivable_and_spendability_remain_distinct(package):
    v = create(package)
    a = package[1]["actions"][0]
    args = dict(accepted_assumptions=tuple(ASSUMPTIONS), cash_credit_policy="pay_date_after_close")
    with pytest.raises(DataError, match="not yet visible"):
        v.cash_state(
            "w",
            "000001.XSHE",
            a["ex_date"],
            as_of=a["announcement_date"] + "T15:30:00+08:00",
            **args,
        )
    for clock, expected in [
        ("08:59:59", "not_ex"),
        ("09:00:00", "receivable"),
        ("15:29:59", "receivable"),
        ("15:30:00", "model_available"),
    ]:
        r = v.cash_state(
            "w", "000001.XSHE", a["ex_date"], as_of=a["ex_date"] + "T" + clock + "+08:00", **args
        )
        assert r["phase"] == expected and r["source_cash_available_at"] is None
    args["cash_credit_policy"] = "pay_date_before_open"
    assert (
        v.cash_state(
            "w", "000001.XSHE", a["ex_date"], as_of=a["ex_date"] + "T09:00:00+08:00", **args
        )["phase"]
        == "model_available"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "foreign",
        "unknown_is_zero",
        "nan",
        "unbound",
        "promoted_clock",
        "wrong_limit",
        "unknown_limit_dependency",
        "calendar_mismatch",
        "action_amount",
        "cash_clock",
        "base_pin",
        "parent_path",
        "duplicate_file",
    ],
)
def test_reject_malformed_or_promoted_supplement(package, mutation):
    base, p, root = package
    if mutation == "missing":
        p["states"].pop()
    if mutation == "duplicate":
        p["states"].append(deepcopy(p["states"][0]))
    if mutation == "foreign":
        p["states"][0]["security"] = "000002.XSHE"
    if mutation == "unknown_is_zero":
        p["states"][0]["is_st"].update(value=False, status="unavailable")
    if mutation == "nan":
        p["states"][0]["reference_price"]["value"] = "NaN"
    if mutation == "unbound":
        p["states"][0]["is_st"]["source_refs"] = ["missing.txt"]
    if mutation == "promoted_clock":
        p["states"][0]["is_st"]["historical_available_at"] = "2020-01-01T09:00:00+08:00"
    if mutation == "wrong_limit":
        p["states"][0]["upper_limit"]["value"] = "999"
    if mutation == "unknown_limit_dependency":
        p["states"][0]["is_st"].update(value=None, status="unavailable")
    if mutation == "calendar_mismatch":
        p["calendars"]["w"]["sessions"].pop(0)
    if mutation == "action_amount":
        p["actions"][0]["cash_per_share"] = "0"
    if mutation == "cash_clock":
        p["actions"][0]["cash_available_at"] = p["actions"][0]["ex_date"] + "T09:00:00+08:00"

    def mutate(b):
        if mutation == "base_pin":
            b.update(research_binding_sha256="0" * 64)
        if mutation == "parent_path":
            b["files"][0].update(path="../outside.json")
        if mutation == "duplicate_file":
            b["files"].append(deepcopy(b["files"][0]))

    with pytest.raises(DataError):
        write_supplement(root, base, p, mutate)


def test_supplement_bytes_pinned_and_no_symlink_escape(package, tmp_path):
    v = create(package)
    root = package[2]
    (root / "data.json").write_text("{}")
    with pytest.raises(DataError) as e:
        DailyExecutionView(package[0], v.path, expected_binding_sha256=v.binding_sha256)
    assert e.value.code == "DAILY_EXECUTION_INTEGRITY"
    v = create(package)
    outside = tmp_path / "outside.json"
    (root / "data.json").rename(outside)
    (root / "data.json").symlink_to(outside)
    with pytest.raises(DataError) as e:
        DailyExecutionView(package[0], v.path, expected_binding_sha256=v.binding_sha256)
    assert e.value.code == "DAILY_EXECUTION_FILE"


def test_nonzero_shares_require_both_credit_and_tradable_dates(package, tmp_path):
    base, payload, root = package
    bd = deepcopy(base._data)
    bd["actions"][0]["bonus_share_ratio"] = "0.1"
    base = write_package(tmp_path / "share-base", bd)
    payload["actions"][0]["bonus_share_ratio"] = "0.1"
    payload["actions"][0]["new_shares_tradable_date"] = payload["actions"][0]["ex_date"]
    v = write_supplement(root, base, payload)
    assert any(
        x["field"] == "new_shares_credit_date/new_shares_tradable_date"
        for x in v.preflight("w", **options())["issues"]
    )
    payload["actions"][0]["new_shares_credit_date"] = payload["actions"][0]["ex_date"]
    v = write_supplement(root, base, payload)
    assert v.preflight("w", **options())["ready"]


def test_decimal_rounding_unknown_and_minimum_tick():
    with localcontext() as ctx:
        ctx.prec = 2
        assert standard_limits("10.05", False) == ("11.06", "9.05")
    assert standard_limits("10.05", True) == ("10.55", "9.55")
    assert standard_limits("0.10", True) == ("0.11", "0.09")
    with pytest.raises(DataError):
        standard_limits("10", None)
    with pytest.raises(DataError):
        standard_limits("10.001", False)
    with pytest.raises(DataError):
        standard_limits("1e100", False)


def test_bad_policy_selection_and_scope(package):
    v = create(package)
    for args in [
        dict(purpose="guess"),
        dict(purpose="retrospective_simulation", accepted_assumptions=True),
        dict(purpose="retrospective_simulation", accepted_assumptions=["pretend_non_st"]),
        dict(purpose="retrospective_simulation", cash_credit_policy="unknown"),
    ]:
        with pytest.raises(DataError):
            v.preflight("w", **args)
    with pytest.raises(DataError):
        v.preflight("other", **options())




@pytest.mark.parametrize("policy", [[], {}, 9, "source_date_is_spendable"])
def test_cash_policy_rejects_malformed_or_implicit_phase(package, policy):
    with pytest.raises(DataError) as exc:
        create(package).preflight(
            "w", purpose="retrospective_simulation", cash_credit_policy=policy
        )
    assert exc.value.code == "DAILY_EXECUTION_ARGUMENT"
