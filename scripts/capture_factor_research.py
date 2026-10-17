"""Capture a bounded free-source FACTOR_RESEARCH matrix; no credentials or JoinQuant calls."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlencode


def run(root, scope_path, reuse, *, allow_public_network):
    scope = json.loads(scope_path.read_text())
    if (
        hashlib.sha256(scope_path.read_bytes()).hexdigest()
        != scope_path.with_suffix(".sha256").read_text().split()[0]
    ):
        raise ValueError("frozen scope changed")
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    requests = []
    old_windows = [("2019-07-01", "2020-01-31"), ("2022-12-01", "2024-04-30")]
    for item in scope["securities"]:
        security = item["security"]
        code = ("sh" if security.endswith("XSHG") else "sz") + security[:6]
        for name, tail in (("daily.js", "hisdata_klc2/klc_kl.js"), ("hfq.js", "hfq.js")):
            filename = code + "-" + name
            requests.append(
                (
                    filename,
                    "https://finance.sina.com.cn/realstock/company/" + code + "/" + tail,
                    filename,
                )
            )
        for start, end in scope["input_windows"]:
            filename = f"{code}-tencent-{start}.js"
            old_name = next(
                (f"{code}-tencent-{a}.js" for a, b in old_windows if a <= start and end <= b), ""
            )
            url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?" + urlencode(
                {"_var": "joinquant_batch", "param": f"{code},day,{start},{end},640,"}
            )
            requests.append((filename, url, old_name))
        for kind, table in (("income", "GINCOME"), ("cash", "GCASHFLOW"), ("balance", "GBALANCE")):
            query = {
                "reportName": "RPT_F10_FINANCE_" + table,
                "columns": "ALL",
                "filter": f"(SECUCODE=\"{security[:6]}.{code[:2].upper()}\")(REPORT_DATE>='2019-01-01')(REPORT_DATE<='2024-03-31')",
                "sortColumns": "REPORT_DATE",
                "sortTypes": -1,
                "pageSize": 100,
                "pageNumber": 1,
                "source": "HSF10",
                "client": "PC",
            }
            name = f"{code}-{kind}.json"
            requests.append(
                (
                    name,
                    "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
                    + urlencode(query),
                    name if kind != "balance" else "",
                )
            )
    months = set()
    for start, end in scope["input_windows"]:
        year, month = map(int, start[:7].split("-"))
        while f"{year:04}-{month:02}" <= end[:7]:
            months.add(f"{year:04}-{month:02}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    for month in sorted(months):
        name = f"szse-{month}.json"
        requests.append(
            (
                name,
                "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month="
                + month,
                name,
            )
        )

    def capture(request):
        name, url, reused_name = request
        target = raw / name
        if target.exists():
            receipt = json.loads(target.with_name(name + ".receipt.json").read_text())
            if (
                receipt["sha256"] != hashlib.sha256(target.read_bytes()).hexdigest()
                or receipt["returncode"]
            ):
                raise ValueError("existing capture is not a verified success: " + name)
            return {"name": name, "status": "existing"}
        source = reuse / "raw" / reused_name if reused_name else None
        if source and source.is_file():
            prior = json.loads(source.with_name(source.name + ".receipt.json").read_text())
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if digest != prior["sha256"] or prior.get("returncode", 0):
                raise ValueError("bad reused source: " + str(source))
            shutil.copyfile(source, target)
            receipt = {
                **prior,
                "name": name,
                "returncode": 0,
                "reused_from": str(source),
                "reused_sha256": digest,
                "requested_subset_url": url,
                "bytes": len(target.read_bytes()),
            }
            target.with_name(name + ".receipt.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
            return {"name": name, "status": "reused"}
        if not allow_public_network:
            raise ValueError("public network explicitly disabled: " + name)
        env = {
            **os.environ,
            "https_proxy": "http://127.0.0.1:7897",
            "http_proxy": "http://127.0.0.1:7897",
            "all_proxy": "socks5://127.0.0.1:7897",
        }
        started = datetime.now(timezone.utc).isoformat()
        proc = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--max-time",
                "20",
                "--connect-timeout",
                "8",
                "--max-filesize",
                "3000000",
                "--output",
                str(target),
                url,
            ],
            env=env,
            capture_output=True,
            timeout=25,
        )
        content = target.read_bytes() if target.exists() else b""
        receipt = {
            "name": name,
            "url": url,
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "returncode": proc.returncode,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "authenticated": False,
            "retry_count": 0,
            "error": proc.stderr.decode(errors="replace"),
        }
        target.with_name(name + ".receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return {
            "name": name,
            "status": "captured" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
        }

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(capture, requests))
    (root / "capture-results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(
        json.dumps(
            {
                state: sum(r["status"] == state for r in results)
                for state in {r["status"] for r in results}
            }
        )
    )
    if any(r["status"] == "failed" for r in results):
        raise SystemExit("Capture errors preserved; do not overwrite or retry blindly")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--reuse", type=Path, required=True)
    parser.add_argument("--allow-public-network", action="store_true")
    args = parser.parse_args()
    run(args.root, args.scope, args.reuse, allow_public_network=args.allow_public_network)
