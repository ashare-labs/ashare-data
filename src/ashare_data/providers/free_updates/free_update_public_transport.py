"""Ordinary system curl transport for a reviewed public endpoint.

Uses the same public User-Agent and current system proxy as urllib. It never
loads curlrc, follows redirects, generates cookies, relaxes TLS or retries.
Capture still owns admission, host cooldowns, receipts and response bounds.
"""
from email.parser import BytesParser
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import getproxies


def get(capture, url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "push2.eastmoney.com"
            or parsed.path != "/api/qt/clist/get" or parsed.username or parsed.password):
        raise ValueError("system curl is only reviewed for the public board member endpoint")
    proxy = getproxies().get("https")
    if proxy and (urlsplit(proxy).username or urlsplit(proxy).password):
        raise ValueError("credentialed proxy requires separate review")
    host = capture.before(url)
    with tempfile.TemporaryDirectory(prefix="ashare-public-http-") as directory:
        root = Path(directory)
        command = ["/usr/bin/curl", "--disable", "--silent", "--show-error", "--retry", "0",
                   "--proto", "=https", "--connect-timeout", "10", "--max-time", "20",
                   "--max-filesize", str(16 * 1024 * 1024),
                   "--user-agent", "ashare-data public-source research",
                   "--dump-header", str(root / "headers"), "--output", str(root / "body"),
                   "--write-out", "%{http_code}\n%{ssl_verify_result}\n"]
        if proxy:
            command += ["--proxy", proxy]
        command.append(url)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=25, check=False)
            status, verify = result.stdout.strip().splitlines()
            if result.returncode or verify != "0" or status == "000":
                raise OSError(f"system curl transport failed: exit={result.returncode}, HTTP={status}, TLS={verify}")
            blocks = (root / "headers").read_bytes().split(b"\r\n\r\n")
            http = [b for b in blocks if b.startswith(b"HTTP/")]
            if not http or int(http[-1].splitlines()[0].split()[1]) != int(status):
                raise OSError("system curl response status/header mismatch")
            headers = BytesParser().parsebytes(http[-1].split(b"\r\n", 1)[1])
            body = (root / "body").read_bytes()
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            capture.transport_failure(url, host, exc)
            raise
        capture.record(url, host, int(status), body, headers, request_method="GET")
        return body
