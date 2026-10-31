"""Resume bounded securities/financial work before opening the next daily cycle.

No timer is installed. One worker at a time uses the existing stores and source
cooldowns. A frozen profile hashes every child profile and the collector code.
"""

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from ashare_data.financials.expansion import atomic, encoded, immutable
from ashare_data.providers.free_updates import implementation_identity
from ashare_data.providers.free_updates.free_update_batch import run as security_batch
from run_legacy_financial_scope import run_profile as financial_batch


def run(
    profile,
    *,
    execute=False,
    today=None,
    security_runner=security_batch,
    financial_runner=financial_batch,
):
    profile = Path(profile).resolve()
    config = json.loads(profile.read_text())
    if config.get("schema") != "ad.incremental-update-cycle.v1":
        raise ValueError("unsupported cycle profile")
    if config["implementation_sha256"] != implementation_identity():
        raise ValueError("collector version differs from frozen cycle")
    root = Path(config["root"]).resolve()
    if any((root / x).exists() for x in ("CURRENT", "CURRENT.json", "releases")):
        raise ValueError("production roots are forbidden")
    items = config["items"]
    if not items or len(items) > 20 or len({i["id"] for i in items}) != len(items):
        raise ValueError("1..20 distinct configured batches required")
    chunks, budget, seconds = (
        config["chunks_per_run"],
        config["jobs_per_chunk"],
        config["seconds_per_run"],
    )
    if not (
        type(chunks) is int
        and 1 <= chunks <= 8
        and type(budget) is int
        and 1 <= budget <= 30
        and 30 <= seconds <= 900
    ):
        raise ValueError("cycle exceeds bounded serial work limits")
    children = []
    for i in items:
        path = Path(i["profile"]).resolve()
        if (
            i["kind"] not in ("securities", "financial")
            or hashlib.sha256(path.read_bytes()).hexdigest() != i["profile_sha256"]
        ):
            raise ValueError("child profile type or checksum differs")
        children.append((i, path, json.loads(path.read_text())))
    day = today or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    datetime.strptime(day, "%Y-%m-%d")
    if not execute:
        return {
            "dry_run": True,
            "items": items,
            "chunks_per_run": chunks,
            "jobs_per_chunk": budget,
            "network_used": False,
            "timer_installed": False,
            "formal_published": False,
        }
    root.mkdir(parents=True, exist_ok=True)
    with (root / "cycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("incremental cycle already active") from None
        immutable(root / "profile.json", encoded(config))
        state_path = root / "state.json"
        old = json.loads(state_path.read_text()) if state_path.exists() else None
        state = old
        if old and old["day"] > day:
            raise ValueError("collection clock moved backwards")
        if not old or old.get("settled", old["complete"]) and old["day"] < day:
            if old:
                immutable(root / "completed" / (old["day"] + ".json"), encoded(old))
            state = {
                "day": day,
                "complete": False,
                "settled": False,
                "cursor": 0,
                "items": {i["id"]: {"complete": False, "runs": 0} for i in items},
            }
        attempted, started = [], time.monotonic()
        for _ in range(chunks):
            if (
                state.get("settled", state["complete"])
                or (root / "STOP").exists()
                or time.monotonic() - started >= seconds
            ):
                break
            selected = next(
                (
                    (state["cursor"] + offset) % len(items)
                    for offset in range(len(items))
                    if not state["items"][items[(state["cursor"] + offset) % len(items)]["id"]][
                        "complete"
                    ]
                    and not state["items"][
                        items[(state["cursor"] + offset) % len(items)]["id"]
                    ].get("retired_unresolved", False)
                ),
                None,
            )
            if selected is None:
                break
            item, path, child = children[selected]
            current = state["items"][item["id"]]
            # Save next turn before work. An interrupted source has its own durable
            # per-job receipts; another source is not starved by this one's failure.
            state["cursor"] = (selected + 1) % len(items)
            atomic(state_path, encoded(state))
            limit = min(budget, child.get("max_jobs", budget))
            try:
                if item["kind"] == "securities":
                    result = security_runner(path, execute=True, as_of=state["day"], max_jobs=limit)
                    complete = result["all_requested_jobs_received"]
                    all_attempted = bool(result.get("items")) and all(
                        item["status"] != "未尝试" for item in result["items"]
                    )
                    detail = {
                        "attempted_jobs": result["attempted"],
                        "ready_jobs": result["ready_jobs"],
                        "pending_jobs": result["pending_jobs"],
                        "result_path": result["result_path"],
                    }
                else:
                    result = financial_runner(
                        path, execute=True, today=state["day"], max_jobs=limit
                    )
                    complete = result["audit"]["all_tasks_have_source_results"]
                    states = result["audit"].get("task_states", {})
                    all_attempted = bool(states) and not any(
                        states.get(name, 0) for name in ("pending", "running", "captured")
                    )
                    detail = {
                        "reports": result["reports"],
                        "pending_jobs": result["audit"]["pending_or_retryable"],
                        "result_path": str(Path(result["batch"]) / "result.json"),
                        "task_states": states,
                    }
                current.update(complete=complete, last_result=detail, error=None)
                # An old date's exhausted attempts must not pin all future
                # collection to that date. Keep the failure and open a fresh
                # date on the next invocation. Untouched jobs still resume first.
                if not complete and state["day"] < day and all_attempted:
                    current.update(
                        retired_unresolved=True,
                        retirement_reason="old scope fully attempted; failures retained, retry on new date",
                    )
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                current.update(error=f"{type(error).__name__}: {error}")
            current["runs"] += 1
            attempted.append({"id": item["id"], **current})
            state["complete"] = all(i["complete"] for i in state["items"].values())
            state["settled"] = all(
                i["complete"] or i.get("retired_unresolved", False) for i in state["items"].values()
            )
            atomic(state_path, encoded(state))
        result = {
            "cycle_day": state["day"],
            "actual_run_day": day,
            "complete": state["complete"],
            "cycle_settled": state.get("settled", state["complete"]),
            "unresolved_items": [
                name
                for name, item in state["items"].items()
                if item.get("retired_unresolved", False)
            ],
            "items": state["items"],
            "chunks_executed": len(attempted),
            "work": attempted,
            "stop_requested": (root / "STOP").exists(),
            "serial_workers": 1,
            "timer_installed": False,
            "formal_published": False,
        }
        atomic(root / "latest.json", encoded(result))
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    result = run(args.profile, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not args.execute or result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
