"""Offline structural checks for frozen reference evidence and its price projection."""

from decimal import Decimal, InvalidOperation

from ashare_data.core.model import DataError, digest, require
from ashare_data.product.references import FIELDS, KINDS, _compatible
from ashare_data.storage.immutable import identifier


def validate_covered_input(document, schedule):
    """Validate a frozen ordinary base (also usable after the AQ-owned attach step).

    Authenticity comes from the independently pinned release/file identities. These
    checks prevent a text-only limit-origin claim from replacing source evidence.
    No external path is opened, so existing accounts can recover without source PDFs.
    """
    try:
        _validate(document, schedule)
    except (KeyError, TypeError, ValueError, IndexError, InvalidOperation) as exc:
        raise DataError(
            "PRODUCT_REFERENCE_INTEGRITY", "Malformed frozen reference evidence"
        ) from exc
    return True


def _check(value, message):
    require(value, "PRODUCT_REFERENCE_INTEGRITY", message)


def _validate(document, schedule):
    e = document["reference_evidence"]
    scope, selection, bindings = e["scope"], e["reference_set"], e["source_bindings"]
    _check(e["schema"] == "ad-product-reference-evidence.v1", "Unknown reference evidence schema")
    _check(
        document.get("research_model") != "price_only_no_actions",
        "Ordinary evidence conflicts with diagnostic mode",
    )
    _check(scope["securities"] == document["securities"], "Reference security selection differs")
    kind = selection["kind"]
    _check(
        kind in KINDS and isinstance(selection["name"], str) and selection["name"],
        "Invalid reference selection",
    )
    required = {**KINDS[kind], "corporate": "market_release" if kind == "market_release" else "corporate_history"}
    _check(set(bindings) == set(required), "Source binding roles differ")
    for role, expected in required.items():
        _check(bindings[role]["kind"] == expected, "Source binding kind differs")
        identifier(bindings[role]["binding_sha256"])
    market = e["market"]
    _check(market["kind"] == kind, "Market reference kind differs")
    days = [d for d in document["calendar"] if scope["start"] <= d <= scope["end"]]
    _check(
        days and days[0] == scope["start"] and days[-1] == scope["end"],
        "Reference window differs from calendar",
    )
    expected = {(s, d) for s in scope["securities"] for d in days}
    states = {(r["security"], r["date"]): r for r in market["rows"]}
    _check(
        len(states) == len(market["rows"]) and set(states) == expected,
        "Missing, duplicate or foreign source state",
    )
    selected = {
        (r["security"], r["date"]): r
        for r in document["daily"]
        if scope["start"] <= r["date"] <= scope["end"]
    }
    _check(
        set(selected) == expected
        and len(selected)
        == sum(scope["start"] <= r["date"] <= scope["end"] for r in document["daily"]),
        "Price scope differs from source states",
    )
    if kind == "daily_execution":
        admission = market["admission"]
        _check(
            admission["ready"] is True
            and admission["purpose"] == "retrospective_simulation"
            and admission["admission_scope"] == "explicit_order_requests"
            and not any(i["blocking"] for i in admission["issues"]),
            "Source order admission failed",
        )
        requests = {(r["security"], r["date"], r["side"]) for r in admission["requests"]}
        _check(
            requests == {(s, d, side) for s, d in expected for side in ("BUY", "SELL")}
            and len(requests) == len(admission["requests"]),
            "Order admission does not bind both selected sides",
        )
        _check(
            admission["report"]["binding_sha256"] == bindings["execution"]["binding_sha256"]
            and admission["report"]["research_binding_sha256"]
            == bindings["research"]["binding_sha256"],
            "Order admission binding differs",
        )
        _check(
            admission["report"]["historical_pit"] is False
            and admission["report"]["execution_permission"] is False,
            "Reference certification must stay false",
        )
        _check(market["calendar"]["dates"] == document["calendar"], "Reference calendar differs")
        _check(
            set(admission["required_assumptions"]) <= set(market["accepted_assumptions"])
            and admission["accepted_assumptions"] == market["accepted_assumptions"]
            and market["cash_credit_policy"] == admission["cash_credit_policy"],
            "Source model acceptance differs",
        )
    else:
        if kind == "market_release":
            _released(document, market, bindings)
        _check(
            market["descriptor"]["binding_sha256"] == bindings["market"]["binding_sha256"],
            "Historical binding differs",
        )
        admissions = {(r["security"], r["date"], r["side"]): r for r in market["admissions"]}
        _check(
            len(admissions) == len(market["admissions"])
            and set(admissions) == {(s, d, side) for s, d in expected for side in ("BUY", "SELL")},
            "Historical order selection differs",
        )
        for (sid, day, side), a in admissions.items():
            _check(
                not (set(a["issues"]) - {"DAILY_SUSPENDED"})
                and (a["ready"] is True or states[sid, day]["suspended"]["value"] is True),
                "Historical order admission failed",
            )
            _check(
                a["state_sha256"] == digest(states[sid, day])
                and a["execution_permission"] is False
                and a["historical_pit"] is False,
                "Historical state binding or certification differs",
            )
            _check(states[sid, day]["rule"]["kind"] != "unlimited", "Unlimited rule is unsupported")
            _check(
                states[sid, day].get("source_claimed_limits", {}).get("validation")
                != "conflict_with_rule_model",
                "Captured source limits conflict with the selected rule model",
            )
    for key, state in states.items():
        target = selected[key]
        _check(
            target["state_evidence"] == {"reference_sha256": digest(state)},
            "Projected state reference differs",
        )
        for field in FIELDS:
            fact = state[field]
            if field == "is_st" and fact["value"] is None:
                _check(fact["status"] == "unavailable", "Unknown ST status must remain unavailable")
                continue
            _check(
                isinstance(fact.get("source_refs"), list) and fact["source_refs"],
                "State requires original source references",
            )
            _check(
                fact["historical_available_at"] is None,
                "Historical availability must remain unverified",
            )
            if field == "is_st":
                continue
            _check(
                fact["status"] in {"source_observed", "derived"},
                "Unavailable source state cannot be consumed",
            )
            _check(target[field] == fact["value"], "Projected state value differs from source fact")
            if field == "suspended":
                _check(type(fact["value"]) is bool, "Suspension requires a known boolean")
            else:
                value = Decimal(str(fact["value"]))
                _check(
                    value.is_finite() and value > 0,
                    "A finite positive price limit/reference is required",
                )
        _check(
            Decimal(str(target["lower_limit"])) < Decimal(str(target["upper_limit"])),
            "Limit band is invalid",
        )
    _check(
        not _compatible(document["daily"], market["rows"], scope["start"]),
        "Main prices and reference states conflict",
    )
    _check(
        schedule["schema"] == "ad.aq-corporate-actions.v1"
        and schedule["event_identity_version"] == 2,
        "Unsupported corporate schedule",
    )
    _check(
        schedule["ready_for_declared_date_model"] is True
        and schedule["issues"] == []
        and schedule["source_scoped_coverage_accepted"] is True
        and schedule["tax_model"] == "gross_cash_no_tax",
        "Corporate coverage is not ready",
    )
    _check(
        schedule["binding_sha256"]
        == bindings["corporate"]["binding_sha256"]
        == e["corporate"]["binding_sha256"]
        and digest(schedule) == e["corporate"]["response_sha256"],
        "Corporate response binding differs",
    )
    for flag in (
        "historical_pit",
        "execution_permission",
        "strategy_visible",
        "economic_result_certified",
    ):
        _check(schedule[flag] is False, "Unsupported corporate certification claim")
    coverage = schedule["coverage"]
    _check(
        coverage["start"] == scope["start"]
        and coverage["end"] == scope["end"]
        and coverage["binding_sha256"] == schedule["binding_sha256"],
        "Corporate window differs",
    )
    _check(
        len(coverage["rows"]) == len(scope["securities"])
        and {r["security"] for r in coverage["rows"]} == set(scope["securities"]),
        "Corporate securities differ",
    )
    for row in coverage["rows"]:
        _check(
            row["provider_tables_captured"] is True
            and not row["source_gaps"]
            and not row["event_field_gaps"]
            and not row["identity_boundaries"]
            and row["absence_proved"] is False
            and row["all_action_types_complete"] is False,
            "Corporate source coverage is incomplete or promoted",
        )
        _check(
            row["events"] == sum(e["security"] == row["security"] for e in schedule["rows"])
            and (
                row["economic_schedule_fields_complete"] is True
                or (row["events"] == 0 and row["interval_state"] == "empty_source_scoped_interval")
            ),
            "Corporate event count or empty-source proof differs",
        )


def _released(document, market, bindings):
    from ashare_data.product.released_references import ASSUMPTIONS

    descriptor = market["descriptor"]
    pin = descriptor["release"]
    _check(market["accepted_assumptions"] == ASSUMPTIONS, "Released reference assumptions differ")
    _check(descriptor["historical_pit"] is False and descriptor["execution_permission"] is False,
           "Released references are retrospective only")
    _check(pin["manifest_sha256"] == bindings["market"]["binding_sha256"]
           == bindings["corporate"]["binding_sha256"], "Released reference pins differ")
    prices = [r["receipt"].get("release") for r in document["owner_api_reads"]
              if r["operation"] == "prices"]
    _check(prices == [pin], "Price and reference snapshots differ")
    for row in market["rows"]:
        for field in FIELDS:
            for ref in row[field]["source_refs"]:
                _check(ref["manifest_sha256"] == pin["manifest_sha256"]
                       and ref["release_id"] == pin["release_id"], "Fact release differs")
