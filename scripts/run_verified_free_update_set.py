"""Run a hash-bound set of existing manual profiles in order, without timers."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


def checked_profiles(path):
    value = json.loads(Path(path).read_text())
    if value.get("schema") != "ad.verified-manual-profile-set.v1":
        raise ValueError("unsupported verified profile set")
    ids, rows = set(), set()
    for profile in value["profiles"]:
        name = profile["name"]
        if not re.fullmatch(r"[a-z0-9-]+", name) or name in ids:
            raise ValueError("profile name is invalid or duplicated")
        ids.add(name)
        for key in ("entry", "runtime_manifest", "boundaries"):
            file = Path(profile[key])
            if not file.is_absolute() or hashlib.sha256(file.read_bytes()).hexdigest() != profile[key + "_sha256"]:
                raise ValueError("frozen profile file changed: " + key)
        bounds = json.loads(Path(profile["boundaries"]).read_text())
        if rows & set(bounds["rows"]):
            raise ValueError("profile set repeats menu rows")
        rows.update(bounds["rows"])
    if not ids or sorted(rows) != value["rows"]:
        raise ValueError("profile set row count or scope differs")
    return value


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profiles", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)
    config = checked_profiles(args.profiles)
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    for profile in config["profiles"]:
        frozen = json.loads(Path(profile["runtime_manifest"]).read_text())
        command = [frozen["python"], profile["entry"], "--runtime-manifest", profile["runtime_manifest"],
                   "--boundaries", profile["boundaries"], "--output", str(args.output / profile["name"])]
        if args.execute:
            command.append("--execute")
        child = subprocess.run(command, capture_output=True, text=True)
        try:
            pointer = json.loads(child.stdout.splitlines()[-1])
            result = json.loads(Path(pointer["result"]).read_text())
            reports.append({"profile": profile["name"], "exit_code": child.returncode,
                            "result": pointer["result"], "status": result["status"],
                            "ready_rows": [r["excel_row"] for r in result.get("data", {}).get("rows", [])],
                            "records": result.get("data", {}).get("records", 0)})
        except (ValueError, KeyError, IndexError, OSError):
            reports.append({"profile": profile["name"], "exit_code": child.returncode or 2,
                            "status": "未完成", "error": child.stderr[-1500:], "ready_rows": [], "records": 0})
    ready = sorted(r for profile in reports for r in profile["ready_rows"])
    success = all(p["exit_code"] == 0 for p in reports) and (not args.execute or ready == config["rows"])
    summary = {"checked_at": datetime.now(timezone.utc).isoformat(), "execute": args.execute,
               "status": "手动更新完成" if success and args.execute else "预览通过" if success else "部分或全部未完成",
               "ready_rows": ready, "ready_count": len(ready), "expected_rows": config["rows"],
               "records": sum(r["records"] for r in reports), "profiles": reports,
               "timer_installed": False, "formal_published": False}
    target = args.output / "result.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": summary["status"], "ready_count": len(ready), "result": str(target)}, ensure_ascii=False))
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
