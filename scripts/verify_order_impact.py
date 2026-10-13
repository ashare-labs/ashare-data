"""AD-only impact analysis of local AQ order requests; never read JQ outputs as data."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import socket
import urllib.request

from execution_delivery_refs import action_reference, check, verify_cash_contract

attempts = []


def deny(*a, **k):
    attempts.append(True)
    raise RuntimeError("offline verification forbids network")


socket.create_connection = deny
socket.socket.connect = deny
urllib.request.urlopen = deny

from ashare_data import DailyResearchView, DailyExecutionView, DataError  # noqa: E402 - deny network before imports
from ashare_data.daily_execution import ASSUMPTIONS  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--research-package", required=True, type=Path)
parser.add_argument("--execution-package", required=True, type=Path)
parser.add_argument("--output-package", required=True, type=Path)
parser.add_argument(
    "--captured-orders",
    required=True,
    type=Path,
    help="Frozen package reports containing the minimal local order inputs",
)
cli = parser.parse_args()
root = cli.output_package.resolve()
base = cli.research_package.resolve()
supplement = cli.execution_package.resolve()
check(
    root != cli.captured_orders.resolve(), "Output must differ from frozen captured-order package"
)
(root / "reports").mkdir(exist_ok=True, parents=True)
research_sha = "174450952a5f1dd7491c4e3dc059440aae605f25bdf1b260057fec3f039ac310"
supplement_sha = "c7f05ea4b9079bd19576d17ff2e0a7bf07569df9bb86ac587b6f563b6833368b"
r = DailyResearchView(base / "binding.json", expected_binding_sha256=research_sha)
v = DailyExecutionView(r, supplement / "binding.json", expected_binding_sha256=supplement_sha)
args = dict(accepted_assumptions=tuple(ASSUMPTIONS), cash_credit_policy="pay_date_after_close")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(name, doc):
    (root / "reports" / name).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


summaries = []
union = set()
for strategy in ["equal-weight", "momentum", "trend"]:
    path = cli.captured_orders / "reports" / (strategy + "-order-input.json")
    document = json.loads(path.read_text())
    receipt = document["receipt"]
    assert receipt["strategy"] == strategy and receipt["research_binding_sha256"] == research_sha
    assert receipt["fields_consumed"] == ["orders.security", "orders.date", "orders.side"]
    assert receipt["source_kind"] == "local AQ native ledger, not JQ"
    # Replay the frozen minimal requests, without accessing a running AQ tree or JQ output.
    orders = document["requests"]
    assert all(set(o) == {"security", "date", "side"} for o in orders)
    save(strategy + "-order-input.json", dict(receipt=receipt, requests=orders))
    preflight = v.preflight_orders("w2024", orders, purpose="retrospective_simulation", **args)
    save(strategy + "-order-preflight.json", preflight)
    unknown = []
    for order in orders:
        scenarios = v.order_limit_scenarios(
            "w2024",
            order["security"],
            as_of=order["date"] + "T09:30:00+08:00",
            side=order["side"],
            accept_ordinary_scenarios=True,
        )
        if scenarios["factual_is_st"]["value"] is None:
            unknown.append(scenarios)
            union.add((order["security"], order["date"]))
            assert not scenarios["report"]["source_status_filled"]
            try:
                v.order_state(
                    "w2024",
                    order["security"],
                    as_of=order["date"] + "T09:00:00+08:00",
                    side=order["side"],
                    **args,
                )
            except DataError as error:
                assert error.code == "DAILY_EXECUTION_NOT_READY"
            else:
                raise AssertionError("Unknown required execution input admitted")
        else:
            assert v.order_state(
                "w2024",
                order["security"],
                as_of=order["date"] + "T09:00:00+08:00",
                side=order["side"],
                **args,
            )["admission"]["ready"]
    assert all(x["all_scenarios_allow_order"] for x in unknown)
    save(
        strategy + "-limit-scenarios.json",
        dict(
            input_receipt=receipt,
            cases=unknown,
            source_facts_changed=False,
            simulation_admitted=False,
            inference="On the recorded local order path only, ordinary 5% and 10% bands give identical no-limit-block outcomes; this does not certify historical ST, special exchange overrides or portfolio-path equivalence.",
        ),
    )
    blockers = [x for x in preflight["issues"] if x["blocking"]]
    summaries.append(
        dict(
            strategy=strategy,
            total_local_orders=len(orders),
            unresolved_status_orders=len(unknown),
            missing_required_fields=len(blockers),
            warning_fields=len(preflight["issues"]) - len(blockers),
            unknown_by_security=dict(Counter(x["security"] for x in unknown)),
            first_blocking_date=min(x["date"] for x in blockers),
            no_unknown_status_orders_for=sorted(
                set(["600000.XSHG", "600036.XSHG", "600519.XSHG", "601318.XSHG"])
                - {x["security"] for x in unknown}
            ),
            price_limit_scenario_disagreements=0,
            strict_orders_ready=preflight["ready"],
            whole_window_ready=preflight["whole_window_ready"],
        )
    )

# Exact gaps remain unchanged, including all non-order dates.
full = v.preflight("w2024", purpose="retrospective_simulation", **args)
all_gaps = [x for x in full["issues"] if x["code"] == "FIELD_UNAVAILABLE"]
assert len(all_gaps) == 696
sessions = sorted({x["date"] for x in all_gaps})
assert len(sessions) == 58
save(
    "exact-gap-scope.json",
    dict(
        dates=sessions,
        fields=["is_st", "upper_limit", "lower_limit"],
        securities=["600000.XSHG", "600036.XSHG", "600519.XSHG", "601318.XSHG"],
        cells=232,
        fields_count=696,
        exact_issues=all_gaps,
    ),
)
empty = v.preflight_orders("w2024", [], purpose="retrospective_simulation", **args)
assert empty["ready"] and not empty["whole_window_ready"]
save("no-order-observation-preflight.json", empty)

events = v.read_window("w2022", purpose="signal_research")["actions"]
assert len(events) == 1
event = events[0]
assert event["security"] == "000651.XSHE" and event["cash_per_share"] == "1"
assert event["ex_price_cash_deduction"] == "0.936124"
assert (
    event["record_date"] == "2022-04-07"
    and event["ex_date"] == event["payment_date"] == "2022-04-08"
)
raw_doc = base / "raw/gree-2022-cash-issuer.pdf"
save(
    "cash-event-contract.json",
    dict(
        source_event=event,
        research_binding_sha256=research_sha,
        execution_binding_sha256=supplement_sha,
        paths={
            "research_event": action_reference(base / "binding-data.json", event),
            "execution_event": action_reference(supplement / "execution-data.json", event),
            "issuer_pdf": str(raw_doc),
            "issuer_pdf_sha256": sha(raw_doc.read_bytes()),
            "receipt": str(raw_doc) + ".receipt.json",
        },
        public_api="DailyExecutionView.read_window('w2022', purpose='signal_research')['actions'][0]",
        accounting={
            "cash_entitlement_per_eligible_share": "1",
            "ex_price_deduction": "0.936124",
            "eligible_share_count": None,
            "cash_available_at": None,
            "cash_credit_phase": "explicit consumer choice; before_open 09:00 or after_close 15:30",
            "tax_policy": "source gross entitlement; fixed consumer zero withholding is a model",
        },
        source_derived_from_jq=False,
        jq_amount_used_as_input=False,
        coverage="Only source-qualified in-window action in w2022; all other security/window action absence remains provider-scoped.",
    ),
)
verify_cash_contract(json.loads((root / "reports/cash-event-contract.json").read_text()), root)
assert not attempts
save(
    "impact-summary.json",
    dict(
        schema="ad.order-use-impact.v1",
        result="passed",
        research_binding_sha256=research_sha,
        execution_binding_sha256=supplement_sha,
        strategies=summaries,
        total_unresolved_status_orders=sum(x["unresolved_status_orders"] for x in summaries),
        distinct_unknown_order_cells_across_strategies=len(union),
        unknown_observation_cells=232 - len(union),
        historical_status_fields_resolved=0,
        network_attempts=len(attempts),
        continuous_account_simulation_performed=False,
        jq_used_as_market_or_state_input=False,
        strict_scope="Actual order security/date/side; non-order states remain warnings with the fixed sample intact",
    ),
)
print(json.dumps(summaries, ensure_ascii=False))
