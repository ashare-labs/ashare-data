"""Bounded unauthenticated public captures, isolated from LocalDailyStore.

No proxy changes, credentials, paid API, response-code execution, or overwrites.
Re-running verifies and reuses successful receipts. Retry failures explicitly;
each retry appends a new attempt. Refreshes belong in a new collection directory.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from threading import Lock
import time
from urllib.parse import urlencode, urlparse

ALLOWED = {
    "vip.stock.finance.sina.com.cn",
    "stock.finance.sina.com.cn",
    "datacenter-web.eastmoney.com",
    "datacenter.eastmoney.com",
    "static.cninfo.com.cn",
    "www.cninfo.com.cn",
    "www.sse.com.cn",
    "www.szse.cn",
    "disc.static.szse.cn",
    "static.longi.com",
    "query.sse.com.cn",
}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def fixed(path, raw):
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f"Refusing to replace {path}")
    else:
        with path.open("xb") as f:
            f.write(raw)


def requests_for(scope):
    out = []
    for item in scope["securities"]:
        security = item["security"]
        code = security[:6]
        market_code = ("sh" if security.endswith("XSHG") else "sz") + code
        for kind, page in [
            ("sina_actions", "vISSUE_ShareBonus"),
            ("sina_structure", "vCI_StockStructure"),
        ]:
            out.append(
                dict(
                    id=f"{kind}-{code}",
                    kind=kind,
                    security=security,
                    url=f"https://vip.stock.finance.sina.com.cn/corp/go.php/{page}/stockid/{code}.phtml",
                )
            )
        out.append(
            dict(
                id=f"sina_float-{code}",
                kind="sina_float",
                security=security,
                url="https://stock.finance.sina.com.cn/stock/api/jsonp.php/"
                f"var%20KKE_ShareAmount_{market_code}=/StockService.getAmountBySymbol?symbol={market_code}",
            )
        )
        query = dict(
            reportName="RPT_SHAREBONUS_DET",
            columns="ALL",
            filter=f'(SECURITY_CODE="{code}")',
            pageSize=500,
            pageNumber=1,
            sortColumns="EX_DIVIDEND_DATE",
            sortTypes="-1",
            source="WEB",
            client="WEB",
        )
        out.append(
            dict(
                id=f"eastmoney_dividends-{code}",
                kind="eastmoney_dividends",
                security=security,
                url="https://datacenter-web.eastmoney.com/api/data/v1/get?" + urlencode(query),
            )
        )
    return out


def capture(root, request, retry_failed=False):
    root = Path(root)
    ident = request["id"]
    if not ident or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in ident
    ):
        raise ValueError("Invalid request ID")
    url = urlparse(request["url"])
    if url.scheme != "https" or url.hostname not in ALLOWED or url.username or url.password:
        raise ValueError("Only named unauthenticated HTTPS public sources are supported")
    attempts = sorted(root.glob(ident + ".attempt-*.receipt.json"))
    for path in attempts:
        receipt = json.loads(path.read_text())
        if receipt["request"] != request:
            raise ValueError("Request ID reused for different parameters")
        if receipt["ok"]:
            raw = (root / receipt["body"]).read_bytes()
            if len(raw) != receipt["bytes"] or hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
                raise ValueError("Cached response failed integrity verification")
            return dict(id=ident, status="reused", bytes=len(raw))
    if attempts and (not retry_failed or receipt["http_status"] in {"401", "402", "403", "429"}):
        return dict(id=ident, status="failed_cached", http=receipt["http_status"])
    # Preserve partial files left by an interrupted collection, too.
    used = [
        int(p.name.split(".attempt-")[1].split(".")[0]) for p in root.glob(ident + ".attempt-*.*")
    ]
    prefix = f"{ident}.attempt-{max(used, default=0) + 1:03}"
    body, header = root / (prefix + ".body"), root / (prefix + ".headers")
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--max-redirs",
        "3",
        "--connect-timeout",
        "10",
        "--max-time",
        "35",
        "--max-filesize",
        "15000000",
        "--user-agent",
        "Mozilla/5.0",
        "--dump-header",
        str(header),
        "--output",
        str(body),
        "--write-out",
        "%{http_code}",
    ]
    if request.get("form") is not None:
        command += ["--data", urlencode(request["form"])]
    if request.get("referer") is not None:
        command += ["--referer", request["referer"]]
    command += [request["url"]]
    started = datetime.now(timezone.utc).isoformat()
    try:
        p = subprocess.run(command, capture_output=True, timeout=40)
        status, returncode, error = (
            p.stdout.decode(errors="replace"),
            p.returncode,
            p.stderr.decode(errors="replace"),
        )
    except subprocess.TimeoutExpired:
        status, returncode, error = "", 124, "Public request timed out"
    raw = body.read_bytes() if body.exists() else b""
    valid = request["kind"] != "issuer_pdf" or raw.startswith(b"%PDF-")
    receipt = dict(
        request=request,
        started_at=started,
        received_at=datetime.now(timezone.utc).isoformat(),
        body=body.name,
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        http_status=status,
        returncode=returncode,
        ok=returncode == 0 and status == "200" and bool(raw) and valid,
        error=error or ("HTTP body is not PDF" if not valid else ""),
        authenticated=False,
        network_configuration_changed=False,
    )
    fixed(root / (prefix + ".receipt.json"), encoded(receipt))
    return dict(
        id=ident, status="captured" if receipt["ok"] else "failed", bytes=len(raw), http=status
    )


def run_batch(root, requests, *, workers=1, retry_failed=False):
    """Serialize each host; stop that host on permission/payment/rate responses."""
    if len(requests) > 300 or len({r["id"] for r in requests}) != len(requests):
        raise ValueError("Batch must have at most 300 distinct requests")
    locks = {urlparse(r["url"]).hostname: Lock() for r in requests}
    stopped = set()

    def paced(request):
        host = urlparse(request["url"]).hostname
        with locks[host]:
            if host in stopped:
                return dict(
                    id=request["id"],
                    status="host_stopped",
                    reason="permission_payment_or_rate_response",
                )
            result = capture(root, request, retry_failed)
            if result.get("http") in {"401", "402", "403", "429"}:
                stopped.add(host)
            if result["status"] == "captured":
                time.sleep(0.4)
            return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(paced, requests)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--scope", type=Path, default=Path("docs/action-history-scope.json"))
    p.add_argument("--requests", type=Path, help="Explicit additional public request list")
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--workers", type=int, choices=(1, 2, 3), default=1)
    args = p.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    requests = (
        json.loads(args.requests.read_text())
        if args.requests
        else requests_for(json.loads(args.scope.read_text()))
    )
    for row in run_batch(args.root, requests, workers=args.workers, retry_failed=args.retry_failed):
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
