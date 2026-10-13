"""Bounded free-source capture for the frozen twelve-security experiment.

No JoinQuant, credentials, retries, overwrites, or execution of response scripts.
The caller explicitly enables network access; cached evidence is reused first.
"""

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


def run(root, scope, reuse):
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    requests = []
    for item in scope["securities"]:
        security = item["security"]
        code = ("sh" if security.endswith("XSHG") else "sz") + security[:6]
        for suffix, tail in [("daily.js", "hisdata_klc2/klc_kl.js"), ("hfq.js", "hfq.js")]:
            name = code + "-" + suffix
            requests.append((name, f"https://finance.sina.com.cn/realstock/company/{code}/{tail}"))
        requests.append(
            (
                code + "-shares.js",
                "https://stock.finance.sina.com.cn/stock/api/jsonp.php/"
                + f"var%20KKE_ShareAmount_{code}=/StockService.getAmountBySymbol?symbol={code}",
            )
        )
        for kind, report in [
            ("income", "RPT_F10_FINANCE_GINCOME"),
            ("cash", "RPT_F10_FINANCE_GCASHFLOW"),
        ]:
            query = {
                "reportName": report,
                "columns": "ALL",
                "filter": f"(SECUCODE=\"{security[:6]}.{code[:2].upper()}\")(REPORT_DATE>='2017-01-01')(REPORT_DATE<='2024-03-31')",
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
                "pageSize": 100,
                "pageNumber": 1,
                "source": "HSF10",
                "client": "PC",
            }
            requests.append(
                (
                    code + "-" + kind + ".json",
                    "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
                    + urlencode(query),
                )
            )
        for start, end in scope["input_windows"]:
            query = {"_var": "f1_batch", "param": f"{code},day,{start},{end},640,"}
            requests.append(
                (
                    f"{code}-tencent-{start}.js",
                    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?"
                    + urlencode(query),
                )
            )
    months = set()
    for start, end in scope["input_windows"]:
        year, month = map(int, start[:7].split("-"))
        while f"{year:04}-{month:02}" <= end[:7]:
            months.add(f"{year:04}-{month:02}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    for month in sorted(months):
        requests.append(
            (
                f"szse-{month}.json",
                "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month="
                + month,
            )
        )

    def capture(pair):
        name, url = pair
        target = raw / name
        if target.exists():
            return {"name": name, "status": "existing"}
        for folder in reuse:
            source = folder / name
            if source.is_file() and source.stat().st_size:
                shutil.copyfile(source, target)
                receipt = {
                    "name": name,
                    "url": url,
                    "reused_from": str(source),
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
                (raw / (name + ".receipt.json")).write_text(json.dumps(receipt, indent=2) + "\n")
                return {"name": name, "status": "reused"}
        started = datetime.now(timezone.utc).isoformat()
        environment = {
            **os.environ,
            "https_proxy": "http://127.0.0.1:7897",
            "http_proxy": "http://127.0.0.1:7897",
            "all_proxy": "socks5://127.0.0.1:7897",
        }
        process = subprocess.run(
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
            env=environment,
            capture_output=True,
            timeout=25,
        )
        content = target.read_bytes() if target.exists() else b""
        receipt = {
            "name": name,
            "url": url,
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "returncode": process.returncode,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "error": process.stderr.decode(errors="replace"),
            "authenticated": False,
            "retry_count": 0,
        }
        (raw / (name + ".receipt.json")).write_text(json.dumps(receipt, indent=2) + "\n")
        return {
            "name": name,
            "status": "captured" if process.returncode == 0 else "failed",
            "bytes": len(content),
        }

    # A small bounded worker pool only for independent public HTTP requests.
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(capture, requests):
            print(json.dumps(result), flush=True)

    # Eastmoney has different cash-flow tables for banks and insurers. Keep
    # the empty generic response and capture the explicitly identified schema.
    for item in scope["securities"]:
        security = item["security"]
        code = ("sh" if security.endswith("XSHG") else "sz") + security[:6]
        data = json.loads((raw / (code + "-income.json")).read_text())
        rows = (data.get("result") or {}).get("data", [])
        organization = rows[0]["ORG_TYPE"] if rows else None
        prefix = {"银行": "B", "保险": "I"}.get(organization)
        if prefix is None:
            continue
        query = {
            "reportName": f"RPT_F10_FINANCE_{prefix}CASHFLOW",
            "columns": "ALL",
            "filter": f"(SECUCODE=\"{security[:6]}.{code[:2].upper()}\")(REPORT_DATE>='2017-01-01')(REPORT_DATE<='2024-03-31')",
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "pageSize": 100,
            "pageNumber": 1,
            "source": "HSF10",
            "client": "PC",
        }
        print(
            json.dumps(
                capture(
                    (
                        code + "-cash-" + prefix + ".json",
                        "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
                        + urlencode(query),
                    )
                )
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--scope", type=Path, default=Path("docs/f1-batch-scope.json"))
    parser.add_argument("--reuse", type=Path, action="append", default=[])
    args = parser.parse_args()
    run(args.root, json.loads(args.scope.read_text()), args.reuse)
