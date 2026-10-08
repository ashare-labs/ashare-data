"""Executable CLI lifecycle proof, using only synthetic local input."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run(root):
    calls = []
    def call(*args, expected=0):
        command = [sys.executable, "-m", "ashare_data.cli", "--store", str(root), *args]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != expected:
            raise RuntimeError(result.stderr or result.stdout)
        parsed = json.loads(result.stdout if result.stdout else result.stderr)
        calls.append({"args": list(args), "exit": result.returncode})
        return parsed
    fixtures = Path(__file__).parent / "fixtures"
    call("init")
    bid = call("import", str(fixtures / "golden.json"))["batch_id"]
    report = call("validate", "--batch", bid, "--requirements", str(fixtures / "requirements.json"), "--policy", "synthetic")
    sid = call("publish", "--report", report["id"])["snapshot_id"]
    base = ("query", "--snapshot", sid, "bars", "--symbol", "000001.XSHE",
            "--start", "2026-04-09T14:53:00+08:00", "--end", "2026-04-09T14:56:00+08:00")
    response = call(*base, "--quality", "synthetic")
    denied = call(*base, expected=2)
    assert len(response["rows"]) == 3 and denied["error"]["code"] == "COVERAGE_GAP"
    call("query", "--snapshot", sid, "lineage")
    call("query", "--snapshot", sid, "quality")
    call("snapshots")
    call("recover")
    return {"snapshot_id": sid, "rows": len(response["rows"]), "default_error": denied["error"]["code"], "calls": calls}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--store", default=".data/cli-smoke")
    args = p.parse_args()
    print(json.dumps(run(args.store), ensure_ascii=False, indent=2))
