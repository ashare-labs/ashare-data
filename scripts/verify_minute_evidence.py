"""Offline acceptance of the external, fixed three-security source evidence pack.

This verifies reproducible evidence and rejection behavior, not an AQ backtest.
It also runs under an isolated installed wheel (python -I).
"""

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import ashare_data
from ashare_data import DataError, HistoricalMarketView, MinuteEvidenceView

REQUEST_SHA = "be200b710f64d5c81956a3c1c230ea89ab346a00018ce09e619197f254bbcc99"
SYMBOLS = ["600000.XSHG", "000001.XSHE", "000002.XSHE"]
DATES = ["2026-10-09", "2026-10-12"]
PREVIOUS = {
    "600000.XSHG": ["9.700", "9.540"],
    "000001.XSHE": ["11.780", "11.590"],
    "000002.XSHE": ["4.060", "4.060"],
}


def verify(binding, expected_sha):
    binding = Path(binding).resolve()
    root = binding.parent
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def reject(name, expected_code, operation):
        try:
            operation()
        except DataError as exc:
            check(name, exc.code == expected_code)
        else:
            raise AssertionError(name + ": unexpectedly accepted")

    def read(name):
        return json.loads((root / name).read_bytes())

    with patch(
        "socket.socket", side_effect=AssertionError("Offline verification attempted network")
    ):
        view = MinuteEvidenceView(binding, expected_binding_sha256=expected_sha)
        descriptor = view.descriptor()
        check(
            "fixed_request_identity",
            hashlib.sha256((root / "request/request.json").read_bytes()).hexdigest() == REQUEST_SHA,
        )
        check("fixed_dates", descriptor["minute_dates"] == DATES)
        check(
            "evidence_not_execution",
            descriptor["readiness"] == "blocked" and descriptor["historical_available_at"] is None,
        )
        check(
            "amount_unit_not_promoted",
            descriptor["amount_unit"] is None
            and not descriptor["amount_unit_independently_verified"],
        )
        originals = [
            json.loads(line)
            for line in (root / "request/source-rows.jsonl").read_text().splitlines()
        ]
        check("original_probe_retained_outside_scope", len(originals) == 1458)
        originals = [r for r in originals if r["source_label"][:10] in DATES]
        original_by_key = {(r["security"], r["source_label"]): r for r in originals}
        check("request_original_rows_unique", len(original_by_key) == len(originals) == 1428)
        all_bars, states = [], []
        for symbol in SYMBOLS:
            for index, label in enumerate(DATES):
                key = symbol + "/" + label
                state = view.state(
                    symbol, label, require_fields=["listed", "suspended", "is_st", "previous_close"]
                )
                states.append(state)
                check(
                    key + "/dated_flags",
                    state["listed"]["value"] is True
                    and state["suspended"]["value"] is False
                    and state["is_st"]["value"] is False,
                )
                check(
                    key + "/previous_raw_close",
                    state["previous_close"]["value"] == PREVIOUS[symbol][index],
                )
                check(
                    key + "/source_preclose_diagnostic",
                    Decimal(state["source_preclose"]["value"]) == Decimal(PREVIOUS[symbol][index]),
                )
                check(
                    key + "/exchange_reference_unknown",
                    all(
                        state[k]["value"] is None
                        for k in ("reference_price", "upper_limit", "lower_limit")
                    ),
                )
                check(
                    key + "/quality_preserved",
                    state["source_quality"]["is_st"]
                    == "source_claim_unverified_compressed_outer_integrity_unverified"
                    and not state["source_quality"]["execution_permission"],
                )
                check(
                    key + "/candidate_not_admitted",
                    not state["limit_model_candidate"]["execution_usable"]
                    and bool(state["limit_model_candidate"]["dependencies_unproved"]),
                )
                check(
                    key + "/clocks_unknown",
                    state["historical_available_at"] is state["replay_effective_at"] is None,
                )
                rows = view.bars(symbol, label)
                all_bars.extend(rows)
                check(key + "/source_rows", len(rows) == 238)
                check(
                    key + "/raw_request_identity",
                    all(
                        r["source_row"]
                        == original_by_key[(symbol, r["source_label"])]["raw_fields"]
                        and r["response_sha256"]
                        == original_by_key[(symbol, r["source_label"])]["source_response_sha256"]
                        and r["observation_id"]
                        == original_by_key[(symbol, r["source_label"])]["observation_id"]
                        for r in rows
                    ),
                )
                check(
                    key + "/no_interval_or_clock_fabrication",
                    all(
                        r["interval_start"]
                        is r["interval_end"]
                        is r["historical_available_at"]
                        is r["replay_effective_at"]
                        is None
                        and r["execution_usable"] is False
                        for r in rows
                    ),
                )
        coverage = view.label_coverage()
        check("1422_target_labels", sum(r["matched_labels"] for r in coverage) == 1422)
        check(
            "six_untouched_auction_tails",
            all(r["outside_labels"] == [r["date"] + " 15:00:00"] for r in coverage),
        )
        check(
            "label_grid_not_interval_coverage",
            all(r["label_grid_complete"] and not r["interval_coverage_verified"] for r in coverage),
        )
        check(
            "six_opening_counterexamples",
            all(int(r["opening_comparison"]["timepoint_0930_volume"]) > 0 for r in coverage),
        )
        for market, calendar in view.calendars().items():
            check(
                market + "/calendar_and_weekend",
                calendar["days"]
                == {
                    "2026-10-08": True,
                    "2026-10-09": True,
                    "2026-10-10": False,
                    "2026-10-11": False,
                    "2026-10-12": True,
                    "2026-10-13": True,
                },
            )
        action = view.corporate_actions(SYMBOLS, start="2026-10-08", end="2026-10-12")
        check(
            "empty_is_not_certified_absence",
            action["rows"] == [] and not action["complete"] and not action["absence_proved"],
        )
        queries = action["source_queries"]
        check(
            "six_unfiltered_history_queries",
            len(queries) == 6
            and sum(q["source_rows"] for q in queries) == 287
            and sum(q["pages"] for q in queries) == 17,
        )
        check(
            "no_known_nonnull_milestone_in_window",
            all(not m["in_range_dates"] for q in queries for m in q["milestones"].values()),
        )
        check(
            "null_milestones_retained",
            any(m["null_count"] > 0 for q in queries for m in q["milestones"].values()),
        )
        check(
            "prior_events_remain_linked",
            {(e["security"], e["cash_per_share"], e["ex_date"]) for e in action["linked_events"]}
            == {
                ("600000.XSHG", "0.42", "2026-07-16"),
                ("000001.XSHE", "0.249", "2026-09-24"),
                ("000002.XSHE", "0.68", "2023-08-25"),
            },
        )
        supplement = read("intervals/amount-consistency-supplement.json")
        check(
            "amount_diagnostic_not_unit_proof",
            supplement["requested_continuous_labels_checked"] == 1422
            and supplement["requested_continuous_labels_passed"] == 1422
            and not supplement["absolute_unit_certified"],
        )
        for daily in supplement["source_days"]:
            selected = [
                b["source_row"]
                for b in all_bars
                if b["security"] == daily["security"] and b["source_label"][:10] == daily["date"]
            ]
            check(
                daily["security"] + "/" + daily["date"] + "/exact_amount_sum",
                sum(Decimal(r["amount"]) for r in selected)
                == Decimal(daily["numeric_comparison"]["sina_full_source_day_raw_amount_sum"]),
            )
        sse_rule = read("intervals/sse-rule-report.json")
        check(
            "current_sse_rule_and_deferrals",
            sse_rule["status"] == "current_effective_rule_text_and_deferred_attachment_verified"
            and sse_rule["deferred_implementation"]["referenced_clause_intersection"] == [],
        )
        report = view.admission_report()
        contract = report["consumer_contract"]
        check(
            "r2_opt_in_contract_is_not_source_admission",
            contract is not None
            and contract["aq_candidate_sha"] == "ac8a8d9daf7449299c2d3c0063509a4a6d524fd8"
            and all(
                contract[k] is False
                for k in (
                    "state_replay_default",
                    "source_clocks_modelled",
                    "historical_pit_verified",
                    "evidence_admitted",
                )
            ),
        )
        check(
            "r2_G08_is_still_explicit_model_pending",
            next(c for c in report["assessments"] if c["gate_id"] == "G08")["status"]
            == "requires_explicit_model",
        )
        check(
            "gate_inventory",
            [r["gate_id"] for r in report["assessments"]] == [f"G{i:02d}" for i in range(12)],
        )
        check(
            "nine_explicit_unresolved_gates",
            report["blocking_gates"] == [f"G{i:02d}" for i in range(3, 12)],
        )
        check(
            "no_real_backtest_claim",
            not report["real_backtest_executed"]
            and not report["ready_for_execution"]
            and report["export_schema"] is None,
        )
        reject("execution_export_refused", "MINUTE_GATES_BLOCKED", view.export_backtest)
        reject(
            "interval_assertion_refused",
            "MINUTE_INTERVAL_UNVERIFIED",
            lambda: view.bars(SYMBOLS[0], DATES[0], require_intervals=True),
        )
        reject(
            "historical_pit_refused",
            "PIT_UNAVAILABLE",
            lambda: view.state(SYMBOLS[0], DATES[0], visibility="verified"),
        )
        reject(
            "early_receipt_refused",
            "RECEIPT_NOT_VISIBLE",
            lambda: view.state(
                SYMBOLS[0],
                DATES[0],
                visibility="received",
                knowledge_at="2026-10-09T09:00:00+08:00",
            ),
        )
        reject(
            "unknown_exchange_reference_refused",
            "HISTORICAL_UNKNOWN",
            lambda: view.state(SYMBOLS[0], DATES[0], require_fields=["reference_price"]),
        )
        reject(
            "outside_minute_scope_refused",
            "HISTORICAL_SCOPE",
            lambda: view.bars(SYMBOLS[0], "2026-10-13"),
        )
        reject(
            "outside_action_scope_refused",
            "HISTORICAL_SCOPE",
            lambda: view.corporate_actions(SYMBOLS, start="2026-10-01", end="2026-10-12"),
        )
        common = HistoricalMarketView(binding, expected_binding_sha256=expected_sha)
        check(
            "original_public_state_contract_compatible",
            common.state(SYMBOLS[0], DATES[0]) == view.state(SYMBOLS[0], DATES[0]),
        )
        check(
            "received_view_after_acquisition",
            view.state(
                SYMBOLS[0], DATES[0], visibility="received", knowledge_at=descriptor["received_at"]
            )["listed"]["value"]
            is True,
        )
    return {
        "schema": "ad.minute-real-evidence-verification.v1",
        "passed": len(checks),
        "checks": checks,
        "network_disabled": True,
        "binding_sha256": expected_sha,
        "import_path": ashare_data.__file__,
        "real_backtest_executed": False,
        "source_rows": len(all_bars),
        "state_rows": len(states),
        "admission": report,
        "states": states,
        "actions": action,
        "calendars": view.calendars(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.binding, args.sha256), ensure_ascii=False, indent=2))
