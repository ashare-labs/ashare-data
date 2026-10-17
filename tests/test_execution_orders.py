"""Actual-order scope must not erase unknown observations or promote scenarios."""

import pytest

from ashare_data import DataError
from test_daily_execution import data, package, create, options  # noqa: F401


def unknown(row):
    for field in ("is_st", "upper_limit", "lower_limit"):
        row[field].update(value=None, status="unavailable", assumption_ids=[])


def req(row, side="BUY"):
    return dict(security=row["security"], date=row["date"], side=side)


def test_only_actual_order_cells_block_and_missing_observations_are_retained(package):  # noqa: F811
    payload = package[1]
    unknown(payload["states"][2])
    v = create(package)
    assert not v.preflight("w", **options())["ready"]
    p = v.preflight_orders("w", [req(payload["states"][0])], **options())
    assert p["ready"] and not p["whole_window_ready"]
    assert len(p["issues"]) == 3 and all(not x["blocking"] for x in p["issues"])
    assert all(x["usage"] == "observation_only" for x in p["issues"])
    p = v.preflight_orders("w", [req(payload["states"][2])], **options())
    assert not p["ready"]
    assert {x["field"] for x in p["issues"] if x["blocking"]} == {"is_st", "upper_limit"}
    assert v.quality()["field_counts"]["is_st"]["unavailable"] == 1
    assert len(v.read_window("w", purpose="signal_research")["execution_states"]) == 4


def test_empty_orders_do_not_certify_whole_window_or_disable_action_pit_gates(package):  # noqa: F811
    unknown(package[1]["states"][0])
    v = create(package)
    p = v.preflight_orders("w", [], **options())
    assert p["ready"] and not p["whole_window_ready"] and p["requests"] == []
    no_cash = options()
    no_cash["cash_credit_policy"] = None
    assert not v.preflight_orders("w", [], **no_cash)["ready"]
    pit = options()
    pit["purpose"] = "historical_pit"
    assert not v.preflight_orders("w", [], **pit)["ready"]


def test_directional_source_limit_does_not_need_unrelated_unknown_band(package):  # noqa: F811
    row = package[1]["states"][0]
    row["upper_limit"].update(status="source_observed", assumption_ids=[])
    for f in ("is_st", "lower_limit"):
        row[f].update(value=None, status="unavailable", assumption_ids=[])
    v = create(package)
    assert v.preflight_orders("w", [req(row)], **options())["ready"]
    assert not v.preflight_orders("w", [req(row, "SELL")], **options())["ready"]


def test_order_state_cannot_reuse_permission_for_another_day_or_before_clock(package):  # noqa: F811
    row = package[1]["states"][0]
    unknown(package[1]["states"][1])
    v = create(package)
    args = options()
    args.pop("purpose")
    r = v.order_state(
        "w", row["security"], as_of=row["date"] + "T09:00:00+08:00", side="BUY", **args
    )
    assert r["admission"]["ready"] and r["report"]["admission_scope"] == "single_order"
    assert not set(r["row"]) & {"open", "high", "low", "close", "volume"}
    with pytest.raises(DataError):
        v.order_state(
            "w", row["security"], as_of=row["date"] + "T08:59:59+08:00", side="BUY", **args
        )
    with pytest.raises(DataError):
        v.order_state(
            "w",
            row["security"],
            as_of=package[1]["states"][1]["date"] + "T09:00:00+08:00",
            side="BUY",
            **args,
        )


@pytest.mark.parametrize(
    "reference,side,all_allow",
    [("11.20", "BUY", True), ("10.67", "BUY", False), ("11.79", "SELL", False)],
)
def test_scenarios_keep_unknowns_and_detect_limit_boundary_sensitivity(
    package,  # noqa: F811 - imported fixture
    reference,
    side,
    all_allow,
):
    row = package[1]["states"][0]
    unknown(row)
    row["reference_price"]["value"] = reference
    v = create(package)
    args = dict(as_of=row["date"] + "T09:30:00+08:00", side=side)
    with pytest.raises(DataError):
        v.order_limit_scenarios("w", row["security"], **args)
    r = v.order_limit_scenarios("w", row["security"], accept_ordinary_scenarios=True, **args)
    assert r["all_scenarios_allow_order"] is all_allow
    assert r["outcome_invariant"] is all_allow
    assert r["factual_is_st"]["value"] is None and r["factual_upper_limit"]["value"] is None
    assert not r["report"]["simulation_admitted"] and not r["report"]["source_status_filled"]
    assert not v.preflight_orders("w", [req(row, side)], **options())["ready"]
    args["as_of"] = row["date"] + "T09:29:59+08:00"
    with pytest.raises(DataError):
        v.order_limit_scenarios("w", row["security"], accept_ordinary_scenarios=True, **args)


@pytest.mark.parametrize(
    "requests",
    [
        None,
        {},
        "all",
        [dict(security="000001.XSHE", date="2020-01-01", side="BUY")],
        [dict(security="000001.XSHE", date="2020-05-01", side="UNKNOWN")],
        [dict(security="000001.XSHE", date="2020-05-01", side="BUY", quantity=0)],
    ],
)
def test_order_request_scope_is_explicit_and_validated(package, requests):  # noqa: F811
    with pytest.raises(DataError):
        create(package).preflight_orders("w", requests, **options())
