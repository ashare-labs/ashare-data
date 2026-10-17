"""Order-use gates and explicitly conditional limit sensitivity; no source filling."""

from copy import deepcopy
from datetime import time
from decimal import Decimal

from ashare_data.research.daily import TZ
from ashare_data.core.model import day, require, timestamp


def _requests(view, window_id, requests):
    base = view._context(window_id)
    w = base["window"]
    require(
        isinstance(requests, (list, tuple)) and len(requests) <= 100000,
        "DAILY_EXECUTION_ARGUMENT",
        "Order requests must be a bounded list",
    )
    normalized, required_fields = [], {}
    for request in requests:
        require(
            isinstance(request, dict) and set(request) == {"security", "date", "side"},
            "DAILY_EXECUTION_ARGUMENT",
            "Order request needs exactly security/date/side",
        )
        security, label, side = request["security"], day(request["date"]), request["side"]
        require(
            isinstance(security, str) and side in ("BUY", "SELL"),
            "DAILY_EXECUTION_ARGUMENT",
            "Invalid security or order side",
        )
        key = security, label
        require(
            security in base["securities"]
            and w["start"] <= label <= w["end"]
            and key in view._states,
            "DAILY_EXECUTION_SCOPE",
            "Order outside fixed output scope",
        )
        limit_field = "upper_limit" if side == "BUY" else "lower_limit"
        needed = {"suspended", "reference_price", limit_field}
        # A directly sourced limit does not inherently require a separate ST flag.
        # Reconstructed or absent limits do require their status dependency.
        if view._states[key][limit_field]["status"] != "source_observed":
            needed.add("is_st")
        required_fields.setdefault(key, set()).update(needed)
        normalized.append(dict(security=security, date=label, side=side))
    return normalized, required_fields


def preflight_orders(
    view, window_id, requests, *, purpose, accepted_assumptions=(), cash_credit_policy=None
):
    requests, fields = _requests(view, window_id, requests)
    full = view.preflight(
        window_id,
        purpose=purpose,
        accepted_assumptions=accepted_assumptions,
        cash_credit_policy=cash_credit_policy,
    )
    # Calendar/action coverage and all action timing checks remain window scoped.
    required = {"shared_market_calendar", "scoped_action_absence"}
    if requests:
        required.add("retrospective_daily_status")
    w = view._context(window_id)["window"]
    if any(
        w["start"] <= a["ex_date"] <= w["end"] and Decimal(a["cash_per_share"]) > 0
        for a in view._actions.values()
    ):
        required.add("gross_cash_zero_withholding")
    for key, names in fields.items():
        for name in names:
            required.update(view._states[key][name]["assumption_ids"])
    issues = []
    for entry in full["issues"]:
        issue = deepcopy(entry)
        if issue["code"] == "MODEL_NOT_ACCEPTED" and issue["field"] not in required:
            continue
        if issue["code"] == "FIELD_UNAVAILABLE":
            needed = issue["field"] in fields.get((issue["security"], issue["date"]), ())
            issue["blocking"] = purpose != "signal_research" and needed
            issue["usage"] = "requested_order" if needed else "observation_only"
        issues.append(issue)
    return dict(
        **{
            k: deepcopy(v)
            for k, v in full.items()
            if k not in ("ready", "issues", "required_assumptions")
        },
        ready=not any(x["blocking"] for x in issues),
        whole_window_ready=full["ready"],
        admission_scope="explicit_order_requests",
        requests=requests,
        required_fields=[
            dict(security=s, date=d, fields=sorted(names))
            for (s, d), names in sorted(fields.items())
        ],
        required_assumptions=sorted(required),
        issues=issues,
        warning="Only these order requests are assessed; each new order must be checked. All fixed observations and nulls are retained.",
    )


def order_state(
    view, window_id, security, *, as_of, side, accepted_assumptions=(), cash_credit_policy=None
):
    at = timestamp(as_of).astimezone(TZ)
    require(at.time() >= time(9), "DAILY_EXECUTION_VISIBILITY", "State replay begins at 09:00")
    label = at.date().isoformat()
    admission = preflight_orders(
        view,
        window_id,
        [dict(security=security, date=label, side=side)],
        purpose="retrospective_simulation",
        accepted_assumptions=accepted_assumptions,
        cash_credit_policy=cash_credit_policy,
    )
    require(
        admission["ready"],
        "DAILY_EXECUTION_NOT_READY",
        "Requested order has unresolved execution inputs",
        admission,
    )
    return deepcopy(
        dict(
            row=view._states[(security, label)],
            admission=admission,
            report=view._report(
                as_of=at.isoformat(),
                model_available_at=label + "T09:00:00+08:00",
                admission_scope="single_order",
                admitted_side=side,
            ),
        )
    )


def order_limit_scenarios(
    view, window_id, security, *, as_of, side, accept_ordinary_scenarios=False
):
    """Research sensitivity only; never supplies a chosen ST flag or usable limit."""
    from ashare_data.research.execution_inputs import standard_limits

    require(
        accept_ordinary_scenarios is True,
        "DAILY_EXECUTION_MODEL",
        "Explicitly accept ordinary 5%/10% scenarios; this is not execution admission",
    )
    at = timestamp(as_of).astimezone(TZ)
    label = at.date().isoformat()
    _requests(view, window_id, [dict(security=security, date=label, side=side)])
    require(
        "2019-01-01" <= label <= "2024-12-31" and security.startswith(("000", "600", "601")),
        "DAILY_EXECUTION_SCOPE",
        "Scenarios outside bounded ordinary-mainboard model",
    )
    row = view._states[(security, label)]
    require(
        row["reference_price"]["value"] is not None and row["suspended"]["value"] is not None,
        "DAILY_EXECUTION_NOT_READY",
        "Scenario requires known reference and daily suspension state",
    )
    opening = view.research.opening(window_id, security, as_of=as_of)
    price = Decimal(opening["open"])
    status = row["is_st"]["value"]
    scenarios = []
    for candidate in (False, True) if status is None else (status,):
        upper, lower = standard_limits(row["reference_price"]["value"], candidate)
        inside = Decimal(lower) <= price <= Decimal(upper)
        allowed = (
            inside
            and not row["suspended"]["value"]
            and (price < Decimal(upper) if side == "BUY" else price > Decimal(lower))
        )
        scenarios.append(
            dict(
                hypothetical_is_st=candidate,
                upper_limit=upper,
                lower_limit=lower,
                observed_open_inside_band=inside,
                price_limit_allows_order=allowed,
            )
        )
    decisions = {s["price_limit_allows_order"] for s in scenarios}
    return dict(
        security=security,
        date=label,
        side=side,
        purpose="signal_research",
        factual_is_st=deepcopy(row["is_st"]),
        factual_upper_limit=deepcopy(row["upper_limit"]),
        factual_lower_limit=deepcopy(row["lower_limit"]),
        reference_price=deepcopy(row["reference_price"]),
        observed_open=opening["open"],
        observed_open_source_ref="base:" + opening["source_ref"],
        scenarios=scenarios,
        outcome_invariant=len(decisions) == 1,
        all_scenarios_allow_order=decisions == {True},
        assumptions=[
            "ordinary seasoned mainboard 5% or 10% cases only; extraordinary overrides not certified",
            "observed raw opening-price research model; no queue, liquidity or broker entitlement claim",
        ],
        report=view._report(
            simulation_admitted=False, scenario_only=True, source_status_filled=False
        ),
    )
