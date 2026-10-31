"""Run this authorized Tushare reference/field-gap batch, then exit.

No SDK install, credential file creation, raw token argument or server deployment.
"""

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time
import warnings

from ashare_data.financials import tushare_reference as ts
from ashare_data.financials import tushare_gaps as gaps
from ashare_data.financials import source_repair as follow
from ashare_data.financials.expansion import atomic, encoded, now
from run_reference_expansion import share_source_restrictions, supervise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--factor-inventory", type=Path, help="Explicit small existing-factor coverage manifest; never a database scan")
    parser.add_argument("--coalesce-dividends", action="store_true", help="Reuse one code response across pending dividend dates, with scoped fallbacks")
    parser.add_argument("--followup-spec", type=Path, help="This batch's finite offline supervisor plan")
    parser.add_argument("--followup-root", type=Path)
    credential = parser.add_mutually_exclusive_group()
    credential.add_argument("--prompt-token", action="store_true", help="User types into a local hidden terminal prompt")
    credential.add_argument("--token-env", default="TUSHARE_TOKEN", help="Name of an already configured environment variable")
    credential.add_argument("--credential-fd", type=int, help=argparse.SUPPRESS)
    credential.add_argument("--keychain", action="store_true", help="Read only the explicitly configured macOS Keychain service/account")
    parser.add_argument("--keychain-helper", type=Path, help="Pinned native helper for the dedicated Tushare item")
    parser.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--credential-origin", choices=("local_hidden_prompt", "existing_environment_reference", "macos_keychain"), help=argparse.SUPPRESS)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--detach", action="store_true", help="Keep this local collection process running after terminal closes")
    args = parser.parse_args()
    if bool(args.followup_spec) != bool(args.followup_root):
        parser.error("Both follow-up plan and output directory are required")
    if ((args.ready_fd is not None and args.detach)
            or (args.credential_fd is not None and args.ready_fd is None)
            or (args.ready_fd is not None and args.credential_fd is None and not args.keychain)):
        parser.error("Private pipe mode requires paired descriptors in a foreground worker")
    if args.keychain != bool(args.keychain_helper):
        parser.error("Keychain mode requires its explicit native helper")
    root = args.root.resolve()
    calendar, limits, capped = [root / name for name in ("calendar", "limits", "capped-targets")]
    manifest = ts.prepare_calendar(calendar, args.baseline)
    if args.prepare_only:
        print(json.dumps({"state": "prepared_credentials_pending", "target_codes": manifest["target_codes"],
                          "start": manifest["target_start"], "end": manifest["target_end"],
                          "calendar_requests": manifest["planned_tasks"], "root": str(root)}))
        return 0
    if follow.lock_held(root / "worker.lock"):
        parser.error("This batch already has an active worker; no duplicate was started")
    if args.keychain:
        token = None  # A detached worker reads the scoped item after exec, not in the parent.
    elif args.credential_fd is not None:
        token = read_private_credential(args.credential_fd)
    elif args.prompt_token:
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
    if not token and not args.keychain:
        parser.error("No configured Tushare credential; use a local hidden prompt or existing environment reference")
    if args.detach:
        if os.name != "posix":
            parser.error("Detach is supported only on POSIX; run in the foreground here")
        command = [sys.executable, "-u", "-B", str(Path(__file__).resolve()), "--root", str(root),
                   "--baseline", str(args.baseline.resolve()), "--credential-origin",
                   "macos_keychain" if args.keychain else "local_hidden_prompt" if args.prompt_token else "existing_environment_reference"]
        if args.keychain:
            command.extend(("--keychain", "--keychain-helper", str(args.keychain_helper.resolve())))
        if args.coalesce_dividends:
            command.append("--coalesce-dividends")
        for flag, value in (("--factor-inventory", args.factor_inventory), ("--followup-spec", args.followup_spec),
                            ("--followup-root", args.followup_root)):
            if value is not None:
                command.extend((flag, str(value.resolve())))
        result = spawn_with_credential(command, token, root)
        del token
        print(json.dumps(result))
        return 0 if result["state"] == "worker_initialized_authentication_unverified" else 1
    if args.keychain:
        from ashare_data.financials.tushare_reference import read_token, verify_helper
        verify_helper(args.keychain_helper, root / "credential-policy.json")
        token = read_token(args.keychain_helper)
    transport = ts.Transport(token)
    del token
    with (root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Tushare batch worker is active") from None
        start_followup(args.followup_spec, args.followup_root)
        prompted = args.credential_origin == "local_hidden_prompt" if args.credential_fd is not None else args.prompt_token
        with ts.e.preserve_temporaries(args.coalesce_dividends):
            collect(root, args.baseline, transport, prompted, calendar, limits, capped, args.factor_inventory, args.ready_fd,
                    "macos_keychain" if args.keychain else None,
                    **({"coalesce_dividends": True} if args.coalesce_dividends else {}))
    return 0


MAX_CREDENTIAL_BYTES = 4096


def read_private_credential(descriptor):
    """Consume one anonymous pipe; reject files, stdin and oversized input."""
    if descriptor < 3 or not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        raise ValueError("Credential handoff requires an inherited private pipe")
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raise ValueError("Credential handoff exceeded its bounded size")
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Invalid credential handoff encoding") from None


def signal_ready(descriptor):
    if descriptor is None:
        return
    if descriptor < 3 or not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        raise ValueError("Startup acknowledgment requires a private pipe")
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(encoded(dict(pid=os.getpid(), state="worker_initialized_authentication_unverified")))


def spawn_with_credential(command, token, root, *, startup_timeout=20):
    """Exec a fresh interpreter: no Python-level fork continuation or preexec_fn.

    Only anonymous pipe descriptors appear in argv. The child receives neither a
    token argument nor the parent's environment. A startup timeout is uncertain,
    so an alive child is preserved rather than killed or launched a second time.
    """
    keychain = token is None and "--keychain" in command
    if not keychain and (not isinstance(token, str) or len(token.strip()) < 16):
        raise ValueError("Tushare credential is absent or invalid; configure locally")
    payload = b"" if keychain else token.strip().encode("utf-8")
    if len(payload) > MAX_CREDENTIAL_BYTES:
        raise ValueError("Credential exceeds the bounded handoff size")
    ready_read, ready_write = os.pipe()
    open_fds = {ready_read, ready_write}
    if not keychain:
        secret_read, secret_write = os.pipe()
        open_fds.update((secret_read, secret_write))
    def close(fd):
        if fd in open_fds:
            os.close(fd)
            open_fds.remove(fd)
    source = Path(ts.__file__).resolve().parents[2]
    environment = {"PATH": os.defpath, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        with (root / "worker.log").open("ab") as log:
            private_args = ["--ready-fd", str(ready_write)]
            if not keychain:
                private_args.extend(("--credential-fd", str(secret_read)))
            child = subprocess.Popen([*command, *private_args],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
                pass_fds=(ready_write,) if keychain else (secret_read, ready_write), env=environment)
        close(ready_write)
        if not keychain:
            close(secret_read)
            try:
                with os.fdopen(secret_write, "wb", closefd=False) as stream:
                    stream.write(payload)
            except BrokenPipeError:
                pass  # Child startup failure is reported without the credential body.
            finally:
                close(secret_write)
        del payload
        ready = b""
        until = time.monotonic() + startup_timeout
        while time.monotonic() < until:
            readable, _, _ = select.select([ready_read], [], [], max(0, until - time.monotonic()))
            if not readable:
                break
            chunk = os.read(ready_read, 1025 - len(ready))
            if not chunk:
                break
            ready += chunk
            if len(ready) > 1024:
                break
        try:
            acknowledgment = json.loads(ready)
        except (ValueError, UnicodeDecodeError):
            acknowledgment = {}
        confirmed = (acknowledgment == dict(pid=child.pid, state="worker_initialized_authentication_unverified"))
        exited = child.poll()
        state = ("worker_exited_during_startup" if exited is not None else
                 "worker_initialized_authentication_unverified" if confirmed else "startup_unconfirmed_inspect_before_retry")
        return dict(started_pid=child.pid, root=str(root), state=state, credential_saved=(True if confirmed else None) if keychain else False,
                    credential_transfer="scoped_macos_keychain" if keychain else "anonymous_pipe",
                    authentication_verified=False, exit_code=exited,
                    fresh_interpreter=True, existing_collector_interrupted=False)
    finally:
        for descriptor in list(open_fds):
            close(descriptor)


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


def wait_source(root, baseline, stopping, credential_saved=False):
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
               state="waiting_for_existing_source_boundary", source=str(baseline), credential_saved=credential_saved)))
        until = time.monotonic() + 30
        while time.monotonic() < until and not stopping():
            time.sleep(min(1, max(0, until - time.monotonic())))
    return False


def collect(root, baseline, transport, prompted, calendar, limits, capped, factor_inventory=None, ready_fd=None, credential_origin=None, *, coalesce_dividends=False):
    stored = credential_origin == "macos_keychain"
    modules = [("transport", ts), ("fundamentals", gaps.f), ("gap_planner", gaps)]
    if coalesce_dividends:
        from ashare_data.financials import tushare_reference as dividends
        modules.append(("dividend_coalescing", dividends))
    atomic(root / "worker.json", encoded({"pid": os.getpid(), "started_at": now(),
           "source": "tushare", "credential_source": credential_origin or ("local_hidden_prompt" if prompted else "existing_environment_reference"),
           "credential_saved": stored, "requests_serial": True, "coalesce_dividends": coalesce_dividends,
           "credential_transfer": "scoped_macos_keychain" if stored else "anonymous_pipe" if ready_fd is not None else "direct_process_memory",
           "code_sha256": {name: ts.e.digest(Path(module.__file__).read_bytes()) for name, module in modules},
           "runner_sha256": ts.e.digest(Path(__file__).read_bytes())}))
    signal_ready(ready_fd)
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
    def phase(batch, prepare=None, runner=None):
        if stopping():
            return False
        if prepare:
            prepare()
        atomic(root / "progress.json", encoded({"at": now(), "pid": os.getpid(), "phase": batch.name,
               "state": "collecting", "credential_saved": stored}))
        roots.append(batch)
        share_source_restrictions(batch, roots)
        runner = runner or (lambda b: ts.run(b, transport, stop_paths=(root / "STOP",),
                                            **({"retain_temporaries": True} if coalesce_dividends else {})))
        supervise(batch, runner, stop_paths=(root / "STOP",))
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
            if not wait_source(root, baseline, stopping, stored):
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
        if coalesce_dividends:
            from ashare_data.financials import tushare_reference as dividends
            capture = root / "dividend-coalesced"
            dividends.prepare(capture, target)
            dividends.apply(capture)  # Reuse captured responses before another request.
            share_source_restrictions(capture, [target, *roots])
            def collect_dividends(batch):
                result = ts.run(batch, transport, max_jobs=50, stop_paths=(root / "STOP",), retain_temporaries=True)
                dividends.apply(batch)  # Source writer has released its lock at this boundary.
                return result
            if not phase(capture, runner=collect_dividends):
                state = "stopped_or_source_blocked"
                return
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
            credential_saved=stored, system_service_installed=False)))
        atomic(root / "worker-exit.json", encoded(dict(at=now(), pid=os.getpid(), state=state, stop_requested=stopping())))
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
