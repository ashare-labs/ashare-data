"""Run this authorized Tushare reference/field-gap batch, then exit.

No SDK install, credential file creation, raw token argument or server deployment.
"""

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import warnings

from ashare_data.financials import tushare_reference as ts
from ashare_data.financials import tushare_gaps as gaps
from ashare_data.financials import batch_followup as follow
from ashare_data.financials.expansion import atomic, encoded, now
from run_reference_expansion import share_source_restrictions, supervise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--factor-inventory", type=Path, help="Explicit small existing-factor coverage manifest; never a database scan")
    parser.add_argument("--followup-spec", type=Path, help="This batch's finite offline supervisor plan")
    parser.add_argument("--followup-root", type=Path)
    credential = parser.add_mutually_exclusive_group()
    credential.add_argument("--prompt-token", action="store_true", help="User types into a local hidden terminal prompt")
    credential.add_argument("--token-env", default="TUSHARE_TOKEN", help="Name of an already configured environment variable")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--detach", action="store_true", help="Keep this local collection process running after terminal closes")
    args = parser.parse_args()
    if bool(args.followup_spec) != bool(args.followup_root):
        parser.error("Both follow-up plan and output directory are required")
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
        start_followup(args.followup_spec, args.followup_root)
        collect(root, args.baseline, transport, args.prompt_token, calendar, limits, capped, args.factor_inventory)
    return 0


def start_followup(spec, root):
    if not spec:
        return
    plan = json.loads(spec.read_text())
    if plan.get("finish_policy") != "current_batch_only":
        raise ValueError("Only a finite current-batch supervisor may be started")
    root.mkdir(parents=True, exist_ok=True)
    if follow.lock_held(root / "writer.lock") or (root / "STOP").exists():
        return
    source = Path(ts.__file__).resolve().parents[2]
    with (root / "worker.log").open("ab") as log:
        subprocess.Popen([sys.executable, "-u", "-B", str(Path(__file__).with_name("watch_reference_expansion.py")),
                          "--spec", str(spec), "--root", str(root)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, env={"PATH": os.defpath, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"})


def wait_source(root, baseline, stopping):
    """Wait for existing requests and repair only their captured failures once idle."""
    while not stopping():
        outcome = follow.follow_batch(baseline, "public")
        if outcome["state"] == "verified_with_explicit_gaps":
            return True
        inactive = follow.classify_inactive(dict(root=str(baseline), kind="public"), {})
        if inactive or outcome["state"] in follow.TERMINAL_FOLLOWUP:
            atomic(root / "dependency-result.json", encoded(inactive or outcome))
            return False
        atomic(root / "progress.json", encoded(dict(at=now(), pid=os.getpid(), phase="fundamentals",
               state="waiting_for_existing_source_boundary", source=str(baseline), credential_saved=False)))
        until = time.monotonic() + 30
        while time.monotonic() < until and not stopping():
            time.sleep(min(1, max(0, until - time.monotonic())))
    return False


def collect(root, baseline, transport, prompted, calendar, limits, capped, factor_inventory=None):
    atomic(root / "worker.json", encoded({"pid": os.getpid(), "started_at": now(),
           "source": "tushare", "credential_source": "local_hidden_prompt" if prompted else "existing_environment_reference",
           "credential_saved": False, "requests_serial": True,
           "code_sha256": {name: ts.e.digest(Path(module.__file__).read_bytes()) for name, module in
                           (("transport", ts), ("fundamentals", gaps.f), ("gap_planner", gaps))},
           "runner_sha256": ts.e.digest(Path(__file__).read_bytes())}))
    stopped = {"value": False}
    def stop_signal(*_):
        stopped["value"] = True
        try:
            with (root / "STOP").open("x") as output:
                output.write("Signal requested this batch to stop at its next safe boundary.\n")
        except FileExistsError:
            pass
    previous = {s: signal.signal(s, stop_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    def stopping():
        return stopped["value"] or (root / "STOP").exists()
    roots, gap_roots, state, failure = [], [], "incomplete", None
    def phase(batch, prepare=None):
        if stopping():
            return False
        if prepare:
            prepare()
        atomic(root / "progress.json", encoded({"at": now(), "pid": os.getpid(), "phase": batch.name,
               "state": "collecting", "credential_saved": False}))
        roots.append(batch)
        share_source_restrictions(batch, roots)
        supervise(batch, lambda b: ts.run(b, transport, stop_paths=(root / "STOP",)), stop_paths=(root / "STOP",))
        status = json.loads((batch / "supervisor.json").read_text())
        if status["state"] == "stopped":
            stopped["value"] = True
        atomic(root / "progress.json", encoded({"at": now(), "pid": os.getpid(), "phase": batch.name, **status}))
        return status["state"] == follow.FINISHED and not stopping()
    try:
        for batch, prepare in [(calendar, None), (limits, lambda: ts.prepare_limits(limits, baseline, calendar)),
                               (capped, lambda: ts.prepare_capped_targets(capped, baseline, limits))]:
            if not phase(batch, prepare):
                state = "stopped_or_source_blocked"
                return
        target = root / "fundamentals"
        if not (target / "manifest.json").exists():
            if not wait_source(root, baseline, stopping):
                state = "stopped_or_existing_source_dependency_incomplete"
                return
            snapshot, _ = gaps.inventory(baseline, root / "field-inventory")
            factors = json.loads(factor_inventory.read_text()) if factor_inventory else None
            plan = gaps.build_plan(baseline, snapshot, final=True, factor_inventory=factors)
            gaps.prepare(target, baseline, plan, calendar_root=calendar)
        else:
            _, manifest, _ = ts.e.load_batch(target)
            plan = json.loads((target / "gap-plan.json").read_text())
            if ts.e.digest(ts.e.encoded(plan)) != manifest["gap_plan_sha256"]:
                raise ValueError("Frozen gap plan changed")
        for depth in range(18):  # More than enough to bisect 2010-present into single days.
            if depth:
                next_root = root / ("fundamentals-split-" + str(depth))
                split = gaps.prepare_splits(next_root, baseline, target)
                if not split["planned_tasks"]:
                    break
                target = next_root
            gap_roots.append(target)
            if not phase(target):
                state = "stopped_or_source_blocked"
                return
        else:
            state = "split_depth_exhausted_with_explicit_gaps"
            return
        discovered = root / "discovered-period-amounts"
        discovered_manifest = gaps.prepare_discovered_periods(discovered, baseline, gap_roots)
        if discovered_manifest["planned_tasks"]:
            gap_roots.append(discovered)
            if not phase(discovered):
                state = "stopped_or_source_blocked"
                return
        state = "source_pass_finished_with_explicit_gaps"
    except Exception as error:
        state = "worker_failed_with_receipts_preserved"
        failure = f"{type(error).__name__}: {error}"[:300]
        raise
    finally:
        try:
            reconciliation = gaps.reconcile(plan, gap_roots, root) if gap_roots else None
        except Exception as error:
            reconciliation = dict(state="reconciliation_failed", error=f"{type(error).__name__}: {error}"[:300])
            state = "reconciliation_failed_with_source_receipts_preserved"
        atomic(root / "completion.json", encoded(dict(at=now(), pid=os.getpid(), state=state,
            stop_requested=stopping(), phases=[str(p) for p in roots], reconciliation=reconciliation,
            failure=failure,
            full_history_complete=False, database_imported=False,
            exit_condition="fixed_calendar_limits_capped_and_closed_source_field_gap_plan; bounded_split_depth_18; response_proven_period_supplements",
            credential_saved=False, system_service_installed=False)))
        atomic(root / "worker-exit.json", encoded(dict(at=now(), pid=os.getpid(), state=state, stop_requested=stopping())))
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
