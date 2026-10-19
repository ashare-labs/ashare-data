"""Supervise the explicitly prepared local batch; no scheduler or server install."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from ashare_data.financials.expansion import atomic, audit, encoded, load_batch, now, run


def supervise(root):
    root, _, tasks = load_batch(root)
    while not (root / "STOP").exists():
        result = run(root)
        exited = json.loads((root / "worker-exit.json").read_text())
        if exited["stop_requested"] or (root / "STOP").exists():
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
        while time.time() < wake and not (root / "STOP").exists():
            time.sleep(min(30, max(0, wake - time.time())))
    else:
        state = "stopped"
    atomic(root / "supervisor.json", encoded({"at": now(), "pid": os.getpid(), "state": state}))
    print(json.dumps({"supervisor_state": state, "coverage": str(root / "coverage.json")}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, action="append")
    for root in parser.parse_args().root:
        supervise(root)
        exited = root / "worker-exit.json"
        if (root / "STOP").exists() or (exited.exists() and json.loads(exited.read_text()).get("stop_requested")):
            break
