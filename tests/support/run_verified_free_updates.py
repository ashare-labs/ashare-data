"""Run a previously frozen manual collector and export its verified snapshot.

No timer installation, environment replacement, or production publication occurs.
The runtime/config manifests are explicit inputs; working-tree modules are never
loaded by the collector. A failed iteration never exports an older snapshot as new.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def require(ok, message):
    if not ok:
        raise ValueError(message)


def preflight(runtime_path, boundaries_path):
    frozen, boundaries = read(runtime_path), read(boundaries_path)
    require(os.getuid() == frozen["uid"], "运行身份与冻结记录不符；未启动采集")
    require(not frozen["editable"], "拒绝运行可编辑安装")
    if "entry_path" in frozen:
        require(Path(__file__).resolve() == Path(frozen["entry_path"]).resolve()
                and sha(__file__) == frozen["entry_sha256"], "手动入口与冻结记录不符")
    root = Path(frozen["package_origin"]).parent
    expected = frozen["frozen_package_files"]
    actual = {str(p.relative_to(root)) for p in root.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    require(actual == set(expected), "冻结包文件清单已变化；需重新验证")
    require(all(sha(root / name) == digest for name, digest in expected.items()),
            "冻结包内容已变化；需重新验证")
    manual = boundaries.get("mode") == "manual-source-windows"
    for name in (("config",) if manual else ("config", "plist")):
        require(sha(boundaries[name]) == frozen[name + "_sha256"],
                "冻结" + name + "内容已变化；需重新验证")
    config = read(boundaries["config"])
    staging = Path(config["root"])
    if not staging.is_absolute():
        staging = Path(boundaries["config"]).parent / staging
    require(staging.resolve() == Path(boundaries["staging_root"]).resolve(),
            "暂存路径不匹配")
    require(config["rows"] == boundaries["rows"], "验收行范围不匹配")
    require(not (staging / "CURRENT.json").exists(), "拒绝以正式库作为暂存目录")
    require(Path(frozen["python"]).is_file(), "冻结解释器不可用")
    return frozen, boundaries


def export_manual(summary, boundaries, target):
    """Partial success is explicit; failed rows never reuse an older export."""
    base = Path(boundaries["staging_root"]).resolve() / "manual-snapshots"
    require(summary.get("schema") == "ad.free-update-manual-result.v1"
            and summary.get("rows") == sorted(boundaries["rows"]), "手动结果范围不符")
    require(sorted(r["excel_row"] for r in summary.get("items", [])) == sorted(boundaries["rows"]),
            "手动结果条目缺失或重复")
    reports = []
    for item in summary.get("items", []):
        require(item["excel_row"] in boundaries["rows"], "结果含未批准条目")
        if item["status"] != "可读取":
            continue
        folder = Path(item["snapshot"]).resolve()
        require(folder.parent == base and folder.name == item["snapshot_id"], "快照不属于已验暂存目录")
        manifest = read(folder / "manifest.json")
        identity = hashlib.sha256(json.dumps({k: v for k, v in manifest.items() if k != "snapshot_id"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        require(manifest.get("schema") == "ad.free-source-manual-snapshot.v1"
                and identity == item["snapshot_id"] and manifest["excel_row"] == item["excel_row"]
                and manifest["records"] == item["records"]
                and not manifest["formal_published"], "快照身份或验收行不符")
        for name, digest in manifest["files"].items():
            file = (folder / name).resolve()
            require(file.parent == folder and sha(file) == digest, "快照文件校验失败")
        target.mkdir(exist_ok=True)
        csv_path = target / (str(item["excel_row"]) + ".csv")
        shutil.copyfile(folder / "data.csv", csv_path)
        reports.append({**item, "csv": str(csv_path), "sha256": sha(csv_path),
                        "observed_at": manifest["observed_at"], "source_units": manifest["semantics"]["units"],
                        "source_latest_period": manifest["semantics"].get("latest_period"),
                        "source_coverage": manifest["coverage"]})
    require(len(reports) == summary.get("ready_count"), "成功条目数量不符")
    return {"rows": reports, "records": sum(r["records"] for r in reports),
            "failed_rows": [r for r in summary.get("items", []) if r["status"] != "可读取"],
            "scope": "各来源当前可见窗口；非CMES全历史；失败条目不导出旧数据冒充成功",
            "latest_official_publication_compared": False, "formal_published": False}


def export_snapshot(handoff, boundaries, target):
    """Export only the hash-checked successful iteration's complete snapshot."""
    bundle = Path(handoff["path"]).resolve()
    outbox = Path(boundaries["staging_root"]).resolve() / "outbox"
    require(bundle.parent == outbox, "交接包不属于已验证的隔离暂存目录")
    manifest = read(bundle / "manifest.json")
    require(manifest["bundle_id"] == handoff["bundle_id"], "交接包身份不匹配")
    require(manifest["menu_rows"] == sorted(boundaries["rows"]), "交接包行范围不匹配")
    require(not manifest["production_published"], "手动入口不消费正式发布产物")
    for name, digest in manifest["files"].items():
        file = (bundle / name).resolve()
        require(file.is_relative_to(bundle) and sha(file) == digest, "交接包文件校验失败")
    snapshot = read(bundle / "snapshot.json")
    require(len(snapshot) == manifest["snapshot_records"], "快照数量不匹配")
    require(len({r["key"] for r in snapshot}) == len(snapshot), "快照含重复业务键")
    require({r["menu_row"] for r in snapshot} == set(boundaries["rows"]), "快照缺少验收行")
    # Validation of the whole bundle precedes all exports. Historical revisions
    # remain in staging; this export is the source-visible snapshot at receipt time.
    reports = []
    checks = {r["excel_row"]: r for r in manifest["checks"]}
    target.mkdir(exist_ok=True)
    for row in boundaries["rows"]:
        records = [r["data"] for r in snapshot if r["menu_row"] == row]
        fields = sorted({k for record in records for k in record})
        file = target / (str(row) + ".csv")
        with file.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for record in records:
                writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                                 for k, v in record.items()})
        reports.append({"excel_row": row, "records": len(records), "csv": str(file),
                        "sha256": sha(file), "observed_at": checks[row]["observed_at"],
                        "source_units": checks[row]["semantics"]["units"],
                        "source_latest_period": checks[row]["semantics"].get("latest_period")})
    return {"rows": reports, "records": len(snapshot), "bundle_id": manifest["bundle_id"],
            "scope": "各来源当前可见窗口；非CMES全历史；不将无新发布视为采集失败",
            "latest_official_publication_compared": False, "formal_published": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--boundaries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    args.output = args.output.resolve()
    invocation = args.output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    invocation.mkdir(parents=True)
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "execute": args.execute,
              "timer_installed": False, "formal_published": False, "invocation": str(invocation)}
    try:
        frozen, boundaries = preflight(args.runtime_manifest, args.boundaries)
        result.update(runtime_version=frozen["version"], checked_package_files=len(frozen["frozen_package_files"]))
        manual = boundaries.get("mode") == "manual-source-windows"
        command = [frozen["python"], "-I", "-m", "ashare_data.cli", "free-updates",
                   "run-manual" if manual else "run-scheduled", "--config", boundaries["config"]]
        command += ["--execute"] if args.execute else ([] if manual else ["--dry-run"])
        # Each worker owns its existing 75-second bound. Do not kill healthy
        # workers with an unrelated wrapper timeout or automatically retry here.
        child = subprocess.run(command, capture_output=True, text=True)
        (invocation / "collector.stdout.jsonl").write_text(child.stdout)
        (invocation / "collector.stderr.txt").write_text(child.stderr)
        messages = [json.loads(line) for line in child.stdout.splitlines() if line.strip()]
        summary = messages[-1] if messages else {}
        result.update(collector_exit_code=child.returncode, collector=summary)
        require(child.returncode == 0 or (manual and child.returncode == 2 and summary.get("items")),
                "本次未完成；详见采集日志。保留检查点，未导出旧数据冒充新结果")
        if args.execute:
            if manual:
                result["data"] = export_manual(summary, boundaries, invocation / "data")
            else:
                require(summary.get("all_requested_jobs_received"), "部分条目未取得数据；不生成成功导出")
                result["data"] = export_snapshot(summary["handoff"], boundaries, invocation / "data")
        code = child.returncode
        result["status"] = ("部分完成" if code else "手动更新完成") if args.execute else "预览通过，未采集"
    except (ValueError, KeyError, OSError) as exc:
        result.update(status="未完成", error=str(exc), automatic_retry=False)
        code = 2
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    output = invocation / "result.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    pointer = args.output / ".latest.next.json"
    pointer.write_text(json.dumps({"result": str(output), "status": result["status"]}, ensure_ascii=False) + "\n")
    os.replace(pointer, args.output / "latest.json")
    print(json.dumps({"status": result["status"], "result": str(output),
                      "error": result.get("error"), "timer_installed": False}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
