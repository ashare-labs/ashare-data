"""Supervise prepared collectors and run offline follow-up without restarting them."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import time

from ashare_data.financials import batch_followup as follow
from ashare_data.financials.expansion import atomic, digest, encoded, now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()
    if args.interval < 10:
        parser.error("Local monitoring interval must be at least ten seconds")
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    raw = args.spec.read_bytes()
    spec = json.loads(raw)
    if spec.get("schema") != "ad.reference-followup-plan.v1":
        parser.error("An explicit follow-up plan is required")
    stop = {"requested": False}
    def stopped(*_):
        stop["requested"] = True
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopped)
    with (root / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic(root / "worker.json", encoded(dict(pid=os.getpid(), started_at=now(), plan_sha256=digest(raw),
               code_sha256=digest(Path(follow.__file__).read_bytes()), credentials_loaded=False, source_requests=0)))
        state = follow.read(root / "progress.json", {})
        try:
            while not stop["requested"] and not (root / "STOP").exists():
                state = follow.tick(spec, state, root)
                if args.once:
                    break
                until = time.monotonic() + args.interval
                while time.monotonic() < until and not stop["requested"] and not (root / "STOP").exists():
                    time.sleep(min(1, max(0, until - time.monotonic())))
        finally:
            atomic(root / "worker-exit.json", encoded(dict(pid=os.getpid(), at=now(),
                   stop_requested=stop["requested"] or (root / "STOP").exists(), source_collectors_stopped=False)))


if __name__ == "__main__":
    main()
