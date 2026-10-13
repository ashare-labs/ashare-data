"""Reopen an owner package using only AD's public API. No network or AQ edits."""

import argparse
import json
from pathlib import Path

from ashare_data import DailyResearchView


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("binding", type=Path)
    p.add_argument("--sha256", required=True, help="Externally pinned binding digest")
    args = p.parse_args()
    view = DailyResearchView(args.binding, expected_binding_sha256=args.sha256)
    results = []
    for w in view.descriptor()["windows"]:
        snapshot = view.read_window(w["id"], role="daily_strategy_retrospective_research")
        sample = snapshot["securities"][0]
        history = view.history(w["id"], sample, as_of=w["start"] + "T09:00:00+08:00", count=120)
        opening = view.opening(w["id"], sample, as_of=w["start"] + "T09:30:00+08:00")
        closing = view.closing(w["id"], sample, as_of=w["start"] + "T15:30:00+08:00")
        # Without accept_model=True the historical state remains unavailable.
        state = view.state(w["id"], sample, as_of=w["start"] + "T09:00:00+08:00")
        results.append(
            dict(
                window=w["id"],
                engine_rows=len(snapshot["bars"]),
                warmup_first=history["rows"][0]["date"],
                warmup_last=history["rows"][-1]["date"],
                opening=opening,
                closing=closing,
                preopen_state=state,
            )
        )
    print(
        json.dumps(
            dict(
                binding_sha256=args.sha256,
                windows=results,
                limitations=view.descriptor()["limitations"],
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
