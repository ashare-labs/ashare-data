#!/usr/bin/env python3
"""Complete the existing approved local-data horizon once; never schedule future runs."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from ashare_data.storage.local_daily import DEFAULT_ROOT, LocalDailyStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    project = Path(__file__).resolve().parents[1]
    store = LocalDailyStore(root, readonly=True)
    try:
        end = store.meta("end")
    finally:
        store.close()
    state = dict(
        started_at=datetime.now(timezone.utc).isoformat(),
        pid=os.getpid(),
        root=str(root),
        end=end,
        phase="starting",
        outcome="running",
        steps=[],
    )
    try:
        state["code_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=project, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        state["code_commit"] = None

    def save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        fd, temporary = tempfile.mkstemp(dir=root)
        with os.fdopen(fd, "w") as out:
            json.dump(state, out, ensure_ascii=False, indent=2)
            out.write("\n")
        os.replace(temporary, root / "batch-state.json")

    def launch(label, command, log_name):
        log = root / log_name
        with log.open("ab", buffering=0) as output:
            process = subprocess.Popen(
                command,
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        step = dict(name=label, command=command, pid=process.pid, log=str(log), exit_code=None)
        state["steps"].append(step)
        state["phase"] = label
        descriptor = (
            "baostock-process"
            if label == "bao_all_bars"
            else "http-process"
            if label in ("sohu_supplement", "resume_missing_intervals")
            else None
        )
        if descriptor:
            (root / (descriptor + ".json")).write_text(
                json.dumps(
                    dict(
                        step,
                        launched_at=datetime.now(timezone.utc).isoformat(),
                        controller_pid=os.getpid(),
                        code_commit=state["code_commit"],
                        cwd=str(project),
                        scope_end=end,
                        note="Launch record only; batch-state.json and logs contain current outcome. No scheduler.",
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        save()
        return process, step

    def wait(process, step):
        step["exit_code"] = process.wait()
        step["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
        return step["exit_code"]

    with (root / "batch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any((root / name).exists() for name in ("STOP", "STOP_BAOSTOCK")):
            raise RuntimeError("Remove this batch's STOP/STOP_BAOSTOCK files before resuming")
        save()
        try:
            bao, bao_step = launch(
                "bao_all_bars",
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "ashare_data.providers.baostock_collection",
                    "--root",
                    str(root),
                    "--all-bars",
                ],
                "bao-all-bars.log",
            )
            base = [sys.executable, "-u", "-m", "ashare_data.storage.local_daily", "local-daily"]
            supplement, supplement_step = launch(
                "sohu_supplement",
                base
                + [
                    "supplement",
                    "--root",
                    str(root),
                    "--source",
                    "sohu",
                    "--end",
                    end,
                    "--workers",
                    "3",
                    "--interval",
                    "1",
                ],
                "sohu-supplement.log",
            )
            wait(supplement, supplement_step)
            if not (root / "STOP").exists():
                pending, pending_step = launch(
                    "resume_missing_intervals",
                    base
                    + [
                        "resume",
                        "--root",
                        str(root),
                        "--end",
                        end,
                        "--workers",
                        "4",
                        "--interval",
                        "0.5",
                    ],
                    "gap-repair-resumed.log",
                )
                wait(pending, pending_step)
            state["phase"] = "waiting_for_bao_all_bars"
            save()
            wait(bao, bao_step)
            publish, publish_step = launch(
                "publish_and_report",
                base + ["import-bao", "--root", str(root)],
                "batch-finalize.log",
            )
            wait(publish, publish_step)
            audit, audit_step = launch(
                "integrity_audit",
                [
                    sys.executable,
                    str(project / "scripts/audit_local_daily.py"),
                    "--root",
                    str(root),
                ],
                "batch-integrity.log",
            )
            wait(audit, audit_step)
            report = json.loads((root / "reports/summary.json").read_text())
            state["summary"] = {
                k: report.get(k)
                for k in (
                    "at",
                    "securities",
                    "delisted_securities",
                    "bar_rows",
                    "coverage",
                    "main_board_at_date",
                )
            }
            has_gaps = report["coverage"]["missing_security_days"] or any(
                row["state"] in ("failed", "blocked", "pending", "running") and row["count"]
                for row in report["jobs"]
            )
            paused = any((root / name).exists() for name in ("STOP", "STOP_BAOSTOCK"))
            failed = any(step["exit_code"] != 0 for step in state["steps"])
            state["outcome"] = (
                "paused"
                if paused
                else "finished_with_errors"
                if failed
                else "finished_with_reported_gaps"
                if has_gaps
                else "complete"
            )
            state["phase"] = "finished"
            save()
            return 2 if failed else 0
        except BaseException as error:
            state["outcome"] = "controller_error"
            state["error"] = str(error)
            # Children, if alive, retain their own bounded requests and journals.
            # No unrelated process, environment or scheduler is touched.
            save()
            raise


if __name__ == "__main__":
    raise SystemExit(main())
