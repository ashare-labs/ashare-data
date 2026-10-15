"""Public daily maintenance sources. No accounts, cookies, or implicit source mixing."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
import os
import tempfile
from bisect import bisect_left, bisect_right
from functools import lru_cache

from .storage import immutable_write


class SourceUnavailable(RuntimeError):
    pass


class SourceBlocked(SourceUnavailable):
    pass


HTTP_JOB_SOURCES = ("eastmoney", "sina", "sina_hfq", "sohu", "tencent")


def now():
    return datetime.now(timezone.utc).isoformat()


def decimal(value):
    if value is None or str(value) in ("", "-", "null", "None"):
        return None
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite source number")
    return format(result, "f")


class PublicHTTP:
    """Shared per-host pacing, finite backoff and immutable compressed response bodies."""

    def __init__(self, root, interval=1.0, timeout=30):
        self.root = Path(root)
        (self.root / "raw").mkdir(parents=True, exist_ok=True)
        self.interval, self.timeout = interval, timeout
        self.lock = threading.Lock()
        self.next_request = {}
        self.blocked = {}
        self.history = threading.local()
        self.cache_locks = {}
        self.network_failures = {}
        self.state_path = self.root / "source-state.json"
        self.pauses = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        (self.root / "raw" / "requests").mkdir(exist_ok=True)

    def get(self, url, referer=None):
        key = (url, getattr(self.history, "cache_scope", ""), now()[:10])
        with self.lock:
            cache_lock = self.cache_locks.setdefault(key, threading.Lock())
        # Concurrent gaps in one symbol share the same full-history URL. Wait
        # for the first request's atomic cache publication before looking again.
        with cache_lock:
            return self._get(url, referer)

    def _get(self, url, referer=None):
        cache = (
            self.root
            / "raw"
            / "requests"
            / (
                hashlib.sha256(
                    (url + "|" + getattr(self.history, "cache_scope", "")).encode()
                ).hexdigest()
                + ".json"
            )
        )
        if cache.exists() and not getattr(self.history, "refresh", False):
            saved = json.loads(cache.read_text())
            if saved["received_at"][:10] == now()[:10]:
                body = gzip.decompress((self.root / "raw" / (saved["sha256"] + ".gz")).read_bytes())
                if hashlib.sha256(body).hexdigest() != saved["sha256"]:
                    raise ValueError("raw cache hash mismatch")
                if hasattr(self.history, "receipts"):
                    self.history.receipts.append(saved)
                return body, [saved]
        host = urllib.parse.urlsplit(url).netloc
        receipts = []
        for attempt in range(3):
            with self.lock:
                if host in self.blocked:
                    raise SourceBlocked(self.blocked[host])
                pause = self.pauses.get(host, {})
                if pause.get("until_epoch", 0) > time.time():
                    error = SourceUnavailable(pause["reason"] + "; source cooling down")
                    error.receipts = receipts
                    raise error
                delay = max(0, self.next_request.get(host, 0) - time.monotonic())
                self.next_request[host] = time.monotonic() + delay + self.interval
            time.sleep(delay)
            with self.lock:
                if host in self.blocked:
                    error = SourceBlocked(self.blocked[host])
                    error.receipts = receipts
                    raise error
            started = now()
            try:
                headers = {"User-Agent": "ashare-data/0.9.1 (local public-data research)"}
                if referer:
                    headers["Referer"] = referer
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read(16 * 1024 * 1024 + 1)
                    status = response.status
                if len(body) > 16 * 1024 * 1024:
                    raise ValueError("response exceeds 16 MiB; split the request")
                with self.lock:
                    self.network_failures[host] = 0
                sha = hashlib.sha256(body).hexdigest()
                path = self.root / "raw" / (sha + ".gz")
                immutable_write(path, gzip.compress(body, mtime=0))
                receipts.append(
                    dict(
                        url=url,
                        started_at=started,
                        received_at=now(),
                        sha256=sha,
                        bytes=len(body),
                        status=status,
                    )
                )
                if hasattr(self.history, "receipts"):
                    self.history.receipts.extend(receipts)
                fd, temporary = tempfile.mkstemp(dir=cache.parent)
                with os.fdopen(fd, "w") as output:
                    json.dump(receipts[-1], output)
                os.replace(temporary, cache)
                return body, receipts
            except urllib.error.HTTPError as exc:
                receipts.append(
                    dict(
                        url=url,
                        started_at=started,
                        received_at=now(),
                        status=exc.code,
                        error=str(exc),
                    )
                )
                if exc.code in (401, 403, 429):
                    with self.lock:
                        self.blocked[host] = (
                            f"{host}: HTTP {exc.code}; host paused, no automatic retry"
                        )
                    error = SourceBlocked(self.blocked[host])
                    error.receipts = receipts
                    raise error from exc
                if exc.code == 404:
                    error = SourceUnavailable(f"{url}: HTTP 404")
                    error.receipts = receipts
                    raise error from exc
            except (OSError, TimeoutError) as exc:
                with self.lock:
                    self.network_failures[host] = self.network_failures.get(host, 0) + 1
                    if self.network_failures[host] >= 6:
                        self.pauses[host] = dict(
                            until_epoch=time.time() + 1800,
                            reason="repeated transport failures: " + str(exc),
                            at=now(),
                        )
                        fd, temporary = tempfile.mkstemp(dir=self.root)
                        with os.fdopen(fd, "w") as output:
                            json.dump(self.pauses, output)
                        os.replace(temporary, self.state_path)
                receipts.append(
                    dict(url=url, started_at=started, received_at=now(), error=str(exc))
                )
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
        error = SourceUnavailable(f"{url}: failed after 3 attempts")
        error.receipts = receipts
        raise error


def decode_sina(body, variable):
    """Decode only the quoted payload; never evaluate remote JavaScript."""
    import quickjs

    match = re.search(
        r"\bvar\s+" + re.escape(variable) + r'\s*=\s*("[^"\r\n]*")', body.decode("utf-8")
    )
    if not match:
        raise SourceUnavailable("Sina encoded payload absent")
    encoded = json.loads(match[1])
    ctx = quickjs.Context()
    ctx.set_memory_limit(64 * 1024 * 1024)
    ctx.set_time_limit(5)
    ctx.eval(Path(__file__).with_name("_sina_decode.js").read_text())
    result = json.loads(ctx.eval("JSON.stringify(d(" + json.dumps(encoded) + "))"))
    if not isinstance(result, list):
        raise ValueError("Sina decoder did not return a list")
    return result


def validate_bars(rows, start, end):
    seen = set()
    for row in rows:
        day = row["date"]
        if not start <= day <= end or day in seen:
            raise ValueError("duplicate or out-of-request source date: " + day)
        seen.add(day)
        values = [Decimal(row[k]) for k in ("open", "high", "low", "close")]
        opening, high, low, close = values
        if not (0 < low <= min(opening, close) <= max(opening, close) <= high):
            raise ValueError("invalid source OHLC on " + day)
        if row["volume"] is not None and Decimal(row["volume"]) < 0:
            raise ValueError("negative volume")
        if row["amount"] is not None and Decimal(row["amount"]) < 0:
            raise ValueError("negative amount")
    return sorted(rows, key=lambda r: r["date"])


def eastmoney(http, symbol, start, end):
    params = dict(
        secid=("1." if symbol.startswith("sh") else "0.") + symbol[2:],
        klt=101,
        fqt=0,
        beg=start.replace("-", ""),
        end=end.replace("-", ""),
        fields1="f1,f2,f3,f4,f5,f6",
        fields2="f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    )
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(params)
    body, receipts = http.get(url, "https://quote.eastmoney.com/")
    data = json.loads(body)
    if data.get("rc") != 0:
        raise SourceUnavailable("Eastmoney rc=" + str(data.get("rc")))
    data = data.get("data")
    if data is None:
        return [], receipts
    if data.get("code") != symbol[2:]:
        raise ValueError("Eastmoney security mismatch")
    rows = []
    for line in data.get("klines", []):
        a = line.split(",")
        if len(a) != 11:
            raise ValueError("Eastmoney daily field width changed")
        rows.append(
            dict(
                date=a[0],
                open=decimal(a[1]),
                close=decimal(a[2]),
                high=decimal(a[3]),
                low=decimal(a[4]),
                volume=decimal(Decimal(a[5]) * 100),
                amount=decimal(a[6]),
                tradestatus=None,
                is_st=None,
                preclose=None,
            )
        )
    return validate_bars(rows, start, end), receipts


@lru_cache(maxsize=16)
def _sina_history(body, symbol):
    # Cache immutable decoded values keyed by exact response bytes. Repairing many
    # gaps in one security must not repeatedly decode its entire 30-year history.
    source_fields = ("open", "high", "low", "close", "volume", "amount", "prevclose")
    rows = tuple(
        sorted(
            (
                (r["date"][:10], *(r.get(k) for k in source_fields))
                for r in decode_sina(body, "KLC_KL_" + symbol)
            ),
            key=lambda r: r[0],
        )
    )
    return tuple(r[0] for r in rows), rows


def sina_daily(http, symbol, start, end):
    url = f"https://finance.sina.com.cn/realstock/company/{symbol}/hisdata/klc_kl.js"
    body, receipts = http.get(url, "https://finance.sina.com.cn/")
    days, decoded = _sina_history(body, symbol)
    rows = []
    fields = ("open", "high", "low", "close", "volume", "amount", "preclose")
    for row in decoded[bisect_left(days, start) : bisect_right(days, end)]:
        rows.append(
            dict(
                date=row[0],
                **{k: decimal(v) for k, v in zip(fields, row[1:], strict=True)},
                tradestatus=None,
                is_st=None,
            )
        )
    return validate_bars(rows, start, end), receipts


def sina_factors(http, symbol):
    url = f"https://finance.sina.com.cn/realstock/company/{symbol}/hfq.js"
    body, receipts = http.get(url, "https://finance.sina.com.cn/")
    match = re.search(r"\bvar\s+" + symbol + r"hfq\s*=\s*(\{[^\r\n]*\})", body.decode("utf-8"))
    if not match:
        raise SourceUnavailable("Sina factor payload absent")
    data = json.loads(match[1])
    rows = [dict(date=r["d"], factor=decimal(r["f"])) for r in data.get("data", [])]
    if len(rows) != int(data["total"]) or len({r["date"] for r in rows}) != len(rows):
        raise ValueError("factor count/uniqueness mismatch")
    for row in rows:
        datetime.strptime(row["date"], "%Y-%m-%d")
        if Decimal(row["factor"]) <= 0:
            raise ValueError("nonpositive adjustment factor")
    return sorted(rows, key=lambda r: r["date"]), receipts


def sohu_daily(http, symbol, start, end):
    """Public history table: unadjusted CNY, rounded lots and ten-thousand CNY."""
    params = dict(
        code="cn_" + symbol[2:],
        start=start.replace("-", ""),
        end=end.replace("-", ""),
        stat="1",
        order="D",
        period="d",
        callback="historySearchHandler",
        rt="jsonp",
    )
    body, receipts = http.get(
        "https://q.stock.sohu.com/hisHq?" + urllib.parse.urlencode(params),
        "https://q.stock.sohu.com/",
    )
    match = re.fullmatch(r"\s*historySearchHandler\((.*)\);?\s*", body.decode("gb18030"), re.S)
    if not match:
        raise SourceUnavailable("Sohu expected JSONP envelope absent")
    data = json.loads(match[1])  # Parse the literal, never evaluate remote JavaScript.
    if data == []:
        return [], receipts
    if not isinstance(data, list) or len(data) != 1 or data[0].get("code") != params["code"]:
        raise ValueError("Sohu security mismatch")
    if data[0].get("status") != 0:
        raise SourceUnavailable("Sohu status=" + str(data[0].get("status")))
    rows = []
    for row in data[0]["hq"]:
        if len(row) < 10:
            raise ValueError("Sohu historical row schema")
        rows.append(
            dict(
                date=row[0],
                open=decimal(row[1]),
                close=decimal(row[2]),
                low=decimal(row[5]),
                high=decimal(row[6]),
                volume=decimal(Decimal(row[7]) * 100),
                amount=decimal(Decimal(row[8]) * 10000),
                preclose=None,
                tradestatus=None,
                is_st=None,
            )
        )
    return validate_bars(rows, start, end), receipts


def tencent_daily(http, symbol, start, end):
    """Raw daily OHLC and rounded lots; a calendar-year request fits the 640-row limit."""
    if start[:4] != end[:4]:
        raise ValueError("Tencent daily requests must be split by calendar year")
    params = {"param": f"{symbol},day,{start},{end},640,"}
    body, receipts = http.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode(params),
        "https://gu.qq.com/",
    )
    data = json.loads(body)
    if data.get("code") != 0:
        raise SourceUnavailable("Tencent code=" + str(data.get("code")))
    security = data.get("data", {}).get(symbol)
    if not isinstance(security, dict) or "day" not in security:
        raise SourceUnavailable("Tencent raw daily security payload absent")
    rows = []
    for row in security["day"]:
        # Ex-dividend rows may append one corporate-action annotation object.
        # Preserve it in the immutable raw response; the first six fields stay
        # raw OHLCV and are never changed by the annotation.
        if len(row) != 6 and not (len(row) == 7 and isinstance(row[6], dict)):
            raise ValueError("Tencent raw daily row schema")
        rows.append(
            dict(
                date=row[0],
                open=decimal(row[1]),
                close=decimal(row[2]),
                high=decimal(row[3]),
                low=decimal(row[4]),
                volume=decimal(Decimal(row[5]) * 100),
                amount=None,
                preclose=None,
                tradestatus=None,
                is_st=None,
            )
        )
    return validate_bars(rows, start, end), receipts


def fetch_job(http, job):
    receipts, errors = [], []
    http.history.receipts = []
    http.history.cache_scope = job.get("cache_scope", job["end"])
    try:
        if job.get("source") is not None and job["source"] not in HTTP_JOB_SOURCES:
            raise SourceUnavailable("Job source requires its dedicated collector: " + job["source"])
        if job["kind"] == "factor":
            rows, rec = sina_factors(http, job["symbol"])
            return dict(rows=rows, source="sina_hfq", receipts=rec, errors=[])
        supplement = {"sohu": sohu_daily, "tencent": tencent_daily}.get(job.get("source"))
        if supplement:
            rows, rec = supplement(http, job["symbol"], job["start"], job["end"])
            return dict(rows=rows, source=job["source"], receipts=rec, errors=[])
        try:
            rows, rec = eastmoney(http, job["symbol"], job["start"], job["end"])
            receipts.extend(rec)
            if rows:
                return dict(rows=rows, source="eastmoney", receipts=receipts, errors=errors)
            errors.append("eastmoney: successful empty response; no coverage inferred")
        except SourceBlocked:
            raise
        except SourceUnavailable as exc:
            receipts.extend(getattr(exc, "receipts", []))
            errors.append(str(exc))
        rows, rec = sina_daily(http, job["symbol"], job["start"], job["end"])
        receipts.extend(rec)
        return dict(rows=rows, source="sina", receipts=receipts, errors=errors)
    except Exception as exc:
        receipts = list(
            {
                json.dumps(r, sort_keys=True): r
                for r in receipts + http.history.receipts + getattr(exc, "receipts", [])
            }.values()
        )
        return dict(
            error=f"{type(exc).__name__}: {exc}",
            blocked=isinstance(exc, SourceBlocked),
            receipts=receipts,
            errors=errors,
        )


def exchange_universe(http):
    """Contemporary official listing/termination facts, not historical publication times."""
    import pandas as pd

    records, receipts = [], []
    params = dict(
        sqlId="COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        isPagination="true",
        STOCK_CODE="",
        CSRC_CODE="",
        REG_PROVINCE="",
        STOCK_TYPE="1",
        type="inParams",
    )
    for status in ("2,4,5,7,8", "3"):
        page, total, obtained = 1, None, 0
        while total is None or obtained < total:
            query = dict(params, COMPANY_STATUS=status)
            query.update(
                {
                    "pageHelp.pageSize": "2000",
                    "pageHelp.pageNo": str(page),
                    "pageHelp.beginPage": str(page),
                    "pageHelp.endPage": str(page),
                    "pageHelp.cacheSize": "1",
                }
            )
            url = "https://query.sse.com.cn/sseQuery/commonQuery.do?" + urllib.parse.urlencode(
                query
            )
            body, rec = http.get(url, "https://www.sse.com.cn/")
            receipts.extend(rec)
            data = json.loads(body)
            total = int(data["pageHelp"]["total"])
            items = data["result"]
            if not items and obtained < total:
                raise ValueError("SSE incomplete pagination")
            for row in items:
                code = row.get("A_STOCK_CODE")
                if not code or code == "-":
                    code = row.get("COMPANY_CODE", "")
                if not re.fullmatch(r"60\d{4}", code):
                    continue
                ipo = (
                    datetime.strptime(row["LIST_DATE"].replace("-", ""), "%Y%m%d")
                    .date()
                    .isoformat()
                )
                out = row.get("DELIST_DATE", "-")
                out = (
                    None
                    if out in ("-", "", None)
                    else datetime.strptime(out.replace("-", ""), "%Y%m%d").date().isoformat()
                )
                records.append(
                    dict(
                        symbol="sh" + code,
                        name=row["COMPANY_ABBR"],
                        ipo=ipo,
                        out=out,
                        source="sse",
                        board="main",
                        receipt=rec[-1]["sha256"],
                    )
                )
            obtained += len(items)
            page += 1
    for catalog, tab, retired in [("1110", "tab1", False), ("1793_ssgs", "tab2", True)]:
        url = f"https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID={catalog}&TABKEY={tab}"
        body, rec = http.get(url, "https://www.szse.cn/")
        receipts.extend(rec)
        frame = pd.read_excel(io.BytesIO(body), dtype=str).fillna("")
        for row in frame.to_dict("records"):
            code = row["证券代码" if retired else "A股代码"]
            if not re.fullmatch(r"00\d{4}", code):
                continue
            if not retired and row["板块"] != "主板":
                raise ValueError("SZSE board/prefix mismatch")
            records.append(
                dict(
                    symbol="sz" + code,
                    name=row["证券简称" if retired else "A股简称"],
                    ipo=row["上市日期" if retired else "A股上市日期"],
                    out=row["终止上市日期"] if retired else None,
                    source="szse",
                    board="sme_until_2021-04-06" if code.startswith(("002", "003")) else "main",
                    receipt=rec[-1]["sha256"],
                )
            )
    return records, receipts


def calendar(http, end):
    url = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"
    body, receipts = http.get(url, "https://finance.sina.com.cn/")
    days = [s[:10] for s in decode_sina(body, "datelist")]
    if not days or days[-1] < end or len(days) != len(set(days)):
        raise ValueError("calendar range/uniqueness invalid")
    return [d for d in days if "2010-01-01" <= d <= end], receipts
