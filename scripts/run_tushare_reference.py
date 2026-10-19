"""Run authorized Tushare calendar and daily limit batches using a safe local credential.

No SDK install, credential file creation, raw token argument or server deployment.
"""

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import sys
import warnings

from ashare_data.financials import tushare_reference as ts
from ashare_data.financials.expansion import atomic, encoded, now
from run_reference_expansion import share_source_restrictions, supervise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    credential = parser.add_mutually_exclusive_group()
    credential.add_argument("--prompt-token", action="store_true", help="User types into a local hidden terminal prompt")
    credential.add_argument("--token-env", default="TUSHARE_TOKEN", help="Name of an already configured environment variable")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--detach", action="store_true", help="Keep this local collection process running after terminal closes")
    args = parser.parse_args()
    root = args.root.resolve()
    calendar, limits, capped = [root / name for name in ("calendar", "limits", "capped-targets")]
    manifest = ts.prepare_calendar(calendar, args.baseline)
    if args.prepare_only:
        print(json.dumps({"state": "prepared_credentials_pending", "target_codes": manifest["target_codes"],
                          "start": manifest["target_start"], "end": manifest["target_end"],
                          "calendar_requests": manifest["planned_tasks"], "root": str(root)}))
        return 0
    if args.prompt_token:
        if not sys.stdin.isatty():
            parser.error("Hidden credential input requires the user's local terminal")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                token = getpass.getpass("Tushare token（隐藏输入，仅本次进程使用，不保存）: ")
            except getpass.GetPassWarning:
                parser.error("Terminal cannot disable echo; no credential was requested in plain text")
    else:
        token = os.environ.pop(args.token_env, None)
    if not token:
        parser.error("No configured Tushare credential; use a local hidden prompt or existing environment reference")
    transport = ts.Transport(token)
    del token
    if args.detach:
        if not hasattr(os, "fork"):
            parser.error("Detach is supported only on POSIX; run in the foreground here")
        pid = os.fork()
        if pid:
            print(json.dumps({"started_pid": pid, "root": str(root), "credential_saved": False}))
            return 0
        os.setsid()
        with (root / "worker.log").open("a") as log, open(os.devnull) as devnull:
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            os.dup2(devnull.fileno(), 0)
    with (root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Tushare batch worker is active") from None
        collect(root, args.baseline, transport, args.prompt_token, calendar, limits, capped)
    return 0


def collect(root, baseline, transport, prompted, calendar, limits, capped):
    atomic(root / "worker.json", encoded({"pid": os.getpid(), "started_at": now(),
           "source": "tushare", "credential_source": "local_hidden_prompt" if prompted else "existing_environment_reference",
           "credential_saved": False, "requests_serial": True}))
    roots = []
    for batch, prepare in [(calendar, None), (limits, lambda: ts.prepare_limits(limits, baseline, calendar)),
                           (capped, lambda: ts.prepare_capped_targets(capped, baseline, limits))]:
        if (root / "STOP").exists():
            break
        if prepare:
            prepare()
        atomic(root / "progress.json", encoded({"at": now(), "pid": os.getpid(), "phase": batch.name,
               "state": "collecting", "credential_saved": False}))
        roots.append(batch)
        share_source_restrictions(batch, roots)
        supervise(batch, lambda b: ts.run(b, transport, stop_paths=(root / "STOP",)), stop_paths=(root / "STOP",))
        status = json.loads((batch / "supervisor.json").read_text())
        atomic(root / "progress.json", encoded({"at": now(), "pid": os.getpid(), "phase": batch.name, **status}))
        if status["state"] != "source_pass_finished_with_explicit_gaps":
            break


if __name__ == "__main__":
    raise SystemExit(main())
