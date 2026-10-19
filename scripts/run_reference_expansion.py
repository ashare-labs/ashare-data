"""Supervise the explicitly prepared local batch; no scheduler or server install."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from ashare_data.financials.expansion import atomic, audit, encoded, load_batch, now, run


def share_source_restrictions(root, roots):
    """A new batch must not bypass another batch's host block or cooldown."""
    merged = {}
    for batch in roots:
        path = batch / "source-state.json"
        if not path.exists():
            continue
        for host, state in json.loads(path.read_text()).items():
            target = merged.setdefault(host, {})
            if state.get("blocked"):
                target["blocked"] = state["blocked"]
            for key in ("cooldown_until", "interval"):
                if key in state:
                    target[key] = max(target.get(key, 0), state[key])
    with (root / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / "source-state.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        for host, state in merged.items():
            current.setdefault(host, {}).update(state)
        atomic(path, encoded(current))


def supervise(root, runner=run, *, stop_paths=()):
    root, _, tasks = load_batch(root)
    stop_paths = (root / "STOP", *(Path(p) for p in stop_paths))
    def stopping():
        return any(path.exists() for path in stop_paths)
    while not stopping():
        result = runner(root)
        exited = json.loads((root / "worker-exit.json").read_text())
        if exited["stop_requested"] or stopping():
            state = "stopped"
            break
        if not result["pending_or_retryable"]:
            state = "source_pass_finished_with_explicit_gaps"
            result = audit(root, verify=True, write=True)
            break
        hosts = json.loads((root / "source-state.json").read_text())
        pending = []
        for task in tasks:
            path = root / "receipts" / (task["id"] + ".json")
            receipt = json.loads(path.read_text()) if path.exists() else {"state": "pending"}
            if receipt["state"] in {"pending", "running", "captured", "failed_retryable"}:
                host = hosts.get(urlparse(task["url"]).hostname, {})
                if not host.get("blocked"):
                    pending.append(max(receipt.get("retry_after", 0), host.get("cooldown_until", 0)))
        if not pending:
            state = "remaining_sources_blocked_access_or_payment"
            break
        wake = max(time.time() + 2, min(pending))
        atomic(root / "supervisor.json", encoded({"at": now(), "pid": os.getpid(),
               "state": "cooldown", "next_attempt_epoch": wake}))
        while time.time() < wake and not stopping():
            time.sleep(min(30, max(0, wake - time.time())))
    else:
        state = "stopped"
    atomic(root / "supervisor.json", encoded({"at": now(), "pid": os.getpid(), "state": state}))
    print(json.dumps({"supervisor_state": state, "coverage": str(root / "coverage.json")}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, action="append")
    parser.add_argument("--provider", choices=("public", "zzshare-reference"), default="public")
    args = parser.parse_args()
    roots = [root.resolve() for root in args.root]
    runner = run
    if args.provider == "zzshare-reference":
        from ashare_data.financials.limit_expansion import run as runner
    for root in roots:
        share_source_restrictions(root, roots)
        supervise(root, runner)
        exited = root / "worker-exit.json"
        if (root / "STOP").exists() or (exited.exists() and json.loads(exited.read_text()).get("stop_requested")):
            break
