"""Reopen two pinned owner packages without networking or importing a trading engine."""

import argparse
from collections import Counter
import json

from ashare_data import DailyExecutionView, DailyResearchView


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("research_binding")
    p.add_argument("supplement_binding")
    p.add_argument("--research-sha256", required=True)
    p.add_argument("--supplement-sha256", required=True)
    args = p.parse_args()
    research = DailyResearchView(
        args.research_binding, expected_binding_sha256=args.research_sha256
    )
    v = DailyExecutionView(
        research, args.supplement_binding, expected_binding_sha256=args.supplement_sha256
    )
    # Explicit example choices, not defaults or source facts. Consumers record their own selections.
    accepted = (
        "retrospective_daily_status",
        "shared_market_calendar",
        "standard_mainboard_limits",
        "scoped_action_absence",
        "gross_cash_zero_withholding",
        "dated_name_history_status",
    )
    checks = {}
    for wid in v.descriptor()["windows"]:
        check = v.preflight(
            wid,
            purpose="retrospective_simulation",
            accepted_assumptions=accepted,
            cash_credit_policy="pay_date_after_close",
        )
        checks[wid] = dict(
            ready=check["ready"],
            issues_by_field=dict(Counter(x["field"] for x in check["issues"])),
            affected_securities=sorted({x["security"] for x in check["issues"] if x["security"]}),
            signal_research_ready=v.preflight(wid, purpose="signal_research")["ready"],
            historical_pit_ready=v.preflight(wid, purpose="historical_pit")["ready"],
        )
        if check["ready"]:
            engine = v.read_window(
                wid,
                purpose="retrospective_simulation",
                accepted_assumptions=accepted,
                cash_credit_policy="pay_date_after_close",
            )
            checks[wid].update(
                bars=len(engine["bars"]),
                states=len(engine["execution_states"]),
                cash_events=len(engine["actions"]),
            )
    print(
        json.dumps(
            dict(
                checks=checks,
                field_counts=v.quality()["field_counts"],
                accepted_assumptions=accepted,
                cash_credit_policy="pay_date_after_close",
                report=v.descriptor()["report"],
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
