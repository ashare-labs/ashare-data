"""Resumable official report archive, independent of the live daily database.

Index dates, immutable capture clocks, document versions and candidate statement
cells remain separate. No date label or PDF creation metadata certifies strict
historical visibility. Full-text and revision searches are explicitly bounded.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import nullcontext
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import fcntl
import gzip
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ashare_data.financials.expansion import (
    PRESERVE_TEMPORARIES, atomic, digest, encoded, immutable, now,
    preserve_temporaries, security,
)
from ashare_data.financials import expansion as http_backoff

SCHEMA = "ad.official-financial-batch.v1"
NORMALIZER_SHA256 = digest(Path(__file__).read_bytes())
HOSTS = {"www.cninfo.com.cn", "static.cninfo.com.cn"}
CATEGORIES = ";".join("category_" + x + "_szsh" for x in ("ndbg", "bndbg", "yjdbg", "sjdbg")) + ";"
ORIGIN = "https://www.cninfo.com.cn"
PAGINATION_ERRORS = {
    "Index count changed while paging",
    "Index count disagrees with returned page rows",
    "Index total disagrees with distinct records",
    "Same-day index repair is incomplete or duplicated",
    "Recovery single-day source exceeds one complete page",
}
LABELS = {
    "total_assets": ("资产总计",),
    "total_liabilities": ("负债合计",),
    "operating_revenue": ("营业收入",),
    "total_operating_revenue": ("营业总收入",),
    "net_profit": ("净利润",),
    "parent_net_profit": ("归属于母公司所有者的净利润", "归属于母公司股东的净利润"),
    "net_operate_cash_flow": ("经营活动产生的现金流量净额",),
    "cash_equivalents_at_end": ("期末现金及现金等价物余额",),
}
MONEY = re.compile(r"(?<![\d.])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?!\d)")


class Deferred(Exception):
    pass


class StopRequested(Exception):
    pass


class SourceExhausted(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Official source redirected; manual source review required")


def prepare(root, source_batch, document_codes=120):
    root, source_batch = Path(root).resolve(), Path(source_batch).resolve()
    baseline = (source_batch / "universe.json").read_bytes()
    old = json.loads((source_batch / "manifest.json").read_text())
    if digest(baseline) != old["universe_sha256"]:
        raise ValueError("Changed baseline")
    instruments = json.loads(baseline)["instruments"]
    symbols = sorted({t["symbol"] for t in json.loads((source_batch / "tasks.json").read_text())})
    # Include both exchanges and one known correction history before ordinary codes.
    first = ["sz000637", "sh603169", "sh600000", "sz000001", "sh600068", "sh600176", "sh600318"]
    symbols.sort(key=lambda s: (first.index(s) if s in first else 100, s))
    if not 0 <= document_codes <= len(symbols):
        raise ValueError("Invalid document cohort")
    root.mkdir(parents=True, exist_ok=True)
    scope = {
        "symbols": symbols,
        "instruments": instruments,
        "document_symbols": symbols[:document_codes],
    }
    raw = encoded(scope)
    manifest = {
        "schema": SCHEMA,
        "source_batch": str(source_batch),
        "baseline_sha256": digest(baseline),
        "scope_sha256": digest(raw),
        "target_start": old["target_start"],
        "target_end": old["target_end"],
        "target_codes": len(symbols),
        "document_codes": document_codes,
        "document_policy": "earliest_middle_latest_full_Q1_for_each_cohort_code; all returned versions of selected periods",
        "index_policy": "four_periodic_categories_plus_separate_correction_and_revision_title_searches",
        "minimum_request_interval_seconds": 3,
        "source_policy": "public_unauthenticated_local_research_no_redistribution; numeric_quota_unpublished",
        "publication_policy": "current_index_date_only; never_infer_first_publication_clock",
        "historical_pit_certified": False,
        "unified_database_imported": False,
    }
    immutable(root / "scope.json", raw)
    immutable(root / "manifest.json", encoded(manifest))
    return manifest


class Client:
    def __init__(self, root, stopped=lambda: False, transport=None):
        self.root, self.stopped, self.transport = Path(root), stopped, transport
        self.state_path = self.root / "source-state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.calls, self.last = 0, 0.0

    def fetch(self, key, url, data=None):
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in HOSTS
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Unreviewed source")
        receipt_path = self.root / "receipts" / (key + ".json")
        identity = {"url": url, "form": data, "authenticated": False}
        receipt = (
            json.loads(receipt_path.read_text())
            if receipt_path.exists()
            else {"request": identity, "attempts": []}
        )
        if receipt["request"] != identity:
            raise ValueError("Request identity changed")
        if receipt.get("state") == "complete":
            raw = gzip.decompress((self.root / receipt["path"]).read_bytes())
            if digest(raw) != receipt["sha256"] or len(raw) != receipt["bytes"]:
                raise ValueError("Official source checksum mismatch")
            return raw, receipt
        host = self.state.setdefault(parsed.hostname, {})
        if host.get("blocked"):
            raise Deferred("source_blocked")
        if len(receipt["attempts"]) >= 3:
            raise SourceExhausted("request_exhausted_after_three_attempts")
        if max(host.get("cooldown_until", 0), receipt.get("retry_after", 0)) > time.time():
            raise Deferred("source_cooldown")
        delay = max(3, host.get("interval", 3)) - (time.monotonic() - self.last)
        while delay > 0:
            if self.stopped():
                raise StopRequested()
            time.sleep(min(1, delay))
            delay -= 1
        if self.stopped():
            raise StopRequested()
        attempt = {"started_at": now()}
        receipt["attempts"].append(attempt)
        receipt["state"] = "running"
        atomic(receipt_path, encoded(receipt))
        self.calls += 1
        status, headers = None, {}
        try:
            if self.transport:
                status, raw, headers = self.transport(url, data)
            else:
                req = Request(
                    url,
                    data=urlencode(data).encode() if data is not None else None,
                    headers={
                        "User-Agent": "ashare-local-research/1.0",
                        "Referer": ORIGIN + "/",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept-Encoding": "identity",
                    },
                )
                try:
                    with build_opener(NoRedirect()).open(req, timeout=30) as response:
                        status, headers = response.status, dict(response.headers)
                        raw = response.read(32_000_001)
                except HTTPError as error:
                    status, headers, raw = error.code, dict(error.headers), error.read(4096)
            attempt.update(status=status, received_at=now(), sha256=digest(raw), bytes=len(raw))
            attempt.update(http_backoff.diagnostics(status, raw, headers, time.time()))
            if status != 200:
                raise ValueError("HTTP " + str(status))
            if len(raw) > 32_000_000:
                raise ValueError("Body limit exceeded")
            path = "raw/" + key + ".gz"
            atomic(self.root / path, gzip.compress(raw, mtime=0))
            receipt.update(
                state="complete",
                path=path,
                sha256=digest(raw),
                bytes=len(raw),
                observed_at=attempt["received_at"],
            )
            http_backoff.success(host, headers, time.time(), 3)
            return raw, receipt
        except Exception as error:
            attempt["error"] = f"{type(error).__name__}: {error}"[:400]
            receipt["state"] = "failed_retryable"
            attempt["backoff"] = http_backoff.failure(host, status, headers, time.time(), 3)
            receipt["retry_after"] = max(
                time.time() + 60 * 2 ** len(receipt["attempts"]), host.get("cooldown_until", 0)
            )
            raise Deferred(attempt["error"]) from error
        finally:
            self.last = time.monotonic()
            atomic(receipt_path, encoded(receipt))
            atomic(self.state_path, encoded(self.state))


def report_identity(row):
    title = re.sub(r"<[^>]+>", "", row["announcementTitle"])
    clean = re.sub(r"\s", "", title)
    match = re.search(r"(20\d{2})年?(?:(?:第?一|1)季度|(?:第?三|3)季度|半年度|年度)(?:报告)", clean)
    period = None
    if match:
        part = match.group(0)
        suffix = (
            "03-31"
            if re.search(r"[一1]季度", part)
            else "09-30"
            if re.search(r"[三3]季度", part)
            else "06-30"
            if "半年度" in part
            else "12-31"
        )
        period = match.group(1) + "-" + suffix
    special = any(
        word in clean
        for word in (
            "摘要",
            "英文",
            "提示",
            "更正公告",
            "修订公告",
            "补充公告",
            "审阅",
            "核查",
            "专项",
            "独立董事",
            "监事会",
            "董事会",
            "审核意见",
        )
    )
    # CNINFO labels are civil dates in China. Historical epochs often encode
    # midnight Asia/Shanghai (16:00 UTC on the previous day), not UTC midnight.
    day = (
        datetime.fromtimestamp(row["announcementTime"] / 1000, timezone(timedelta(hours=8)))
        .date()
        .isoformat()
    )
    return {
        "announcement_id": str(row["announcementId"]),
        "title": title,
        "security_code": row["secCode"],
        "org_id": row["orgId"],
        "period_end": period,
        "published_date": day,
        "announcement_time_raw": row["announcementTime"],
        "published_at": None,
        "historical_available_at": None,
        "publication_precision": "date_only_in_current_official_index",
        "date_timezone": "Asia/Shanghai",
        "first_publication_proven": False,
        "revision_history_complete": False,
        "full_report_candidate": bool(
            period and not special and clean.endswith(("报告", "全文", "）", ")"))
        ),
        "revision_label": any(w in clean for w in ("修订", "更正", "更新")),
        "source_url": "https://static.cninfo.com.cn/" + row["adjunctUrl"],
        "historical_pit_certified": False,
    }


def index_form(symbol, issuer, manifest, label, page=1, *, start=None, end=None):
    """One shared request contract for collection and bounded recovery."""
    return dict(
        pageNum=page, pageSize=30, column="sse" if symbol.startswith("sh") else "szse",
        plate="sh" if symbol.startswith("sh") else "sz", tabName="fulltext",
        stock=symbol[2:] + "," + issuer["orgId"],
        searchkey={"periodic": "", "correction": "更正", "revision": "修订"}[label],
        secid="", category=CATEGORIES if label == "periodic" else "", trade="",
        seDate=(start or manifest["target_start"]) + "~" + (end or manifest["target_end"]),
        sortName="time", sortType="asc", isHLtitle="false",
    )


def index_security(client, symbol, manifest):
    root = client.root
    path = root / "indices" / (symbol + ".json.gz")
    if path.exists():
        return json.loads(gzip.decompress(path.read_bytes()))
    code = symbol[2:]
    body, ident_receipt = client.fetch(
        "identity-" + symbol,
        ORIGIN + "/new/information/topSearch/query",
        {"keyWord": code, "maxNum": 30},
    )
    identities = [
        r for r in json.loads(body) if r.get("code") == code and r.get("category") == "A股"
    ]
    if len(identities) != 1:
        raise ValueError("Official issuer identity missing or ambiguous")
    issuer = identities[0]
    found, evidence, queries = {}, ["identity-" + symbol], []
    for label in ("periodic", "correction", "revision"):
        total, seen, pages = None, set(), 0
        source_rows, duplicate_dates = 0, set()
        for page in range(1, 201):
            form = index_form(symbol, issuer, manifest, label, page)
            key = f"index-{symbol}-{label}-{page:03}"
            raw, receipt = client.fetch(key, ORIGIN + "/new/hisAnnouncement/query", form)
            payload = json.loads(raw)
            count = payload.get("totalAnnouncement")
            if not isinstance(count, int) or (total is not None and total != count):
                raise ValueError("Index count changed while paging")
            total = count
            rows = payload.get("announcements") or []
            source_rows += len(rows)
            if not rows and payload.get("hasMore"):
                raise ValueError("Empty index page claims more results")
            for row in rows:
                if (row["secCode"], row["orgId"]) != (code, issuer["orgId"]):
                    raise ValueError("Cross-issuer index row")
                item = report_identity(row)
                aid = item["announcement_id"]
                if aid in seen:
                    duplicate_dates.add(item["published_date"])
                seen.add(aid)
                item.update(index_receipt=key, index_observed_at=receipt["observed_at"])
                if aid in found:
                    prior = found[aid]
                    if any(
                        prior[k] != item[k]
                        for k in ("title", "source_url", "period_end", "published_date")
                    ):
                        raise ValueError("Conflicting announcement version")
                else:
                    found[aid] = item
            pages += 1
            evidence.append(key)
            if payload.get("hasMore") is False:
                break
        else:
            raise ValueError("Index exceeds reviewed pagination cap")
        if source_rows != total:
            raise ValueError("Index count disagrees with returned page rows")
        repairs = []
        # Time sorting can move same-day ties across page boundaries. Repair
        # only affected dates, retaining every response and checking the union.
        for day in sorted(duplicate_dates):
            repair_form = dict(form, pageNum=1, seDate=day + "~" + day)
            key = f"index-{symbol}-{label}-tie-{day}"
            raw, receipt = client.fetch(key, ORIGIN + "/new/hisAnnouncement/query", repair_form)
            payload = json.loads(raw)
            rows = payload.get("announcements") or []
            identities = {str(row["announcementId"]) for row in rows}
            if (
                payload.get("hasMore") is not False
                or payload.get("totalAnnouncement") != len(rows)
                or len(identities) != len(rows)
            ):
                raise ValueError("Same-day index repair is incomplete or duplicated")
            for row in rows:
                if (row["secCode"], row["orgId"]) != (code, issuer["orgId"]):
                    raise ValueError("Cross-issuer same-day index row")
                item = report_identity(row)
                if item["published_date"] != day:
                    raise ValueError("Same-day index repair returned a different date")
                aid = item["announcement_id"]
                item.update(index_receipt=key, index_observed_at=receipt["observed_at"])
                if aid in found and any(
                    found[aid][k] != item[k]
                    for k in ("title", "source_url", "period_end", "published_date")
                ):
                    raise ValueError("Conflicting announcement version during same-day repair")
                found.setdefault(aid, item)
                seen.add(aid)
            evidence.append(key)
            repairs.append({"date": day, "records": len(rows), "receipt": key})
        if len(seen) != total:
            raise ValueError("Index total disagrees with distinct records")
        queries.append(
            {
                "kind": label,
                "pages": pages,
                "records": total,
                "tie_date_repairs": repairs,
                "pagination_complete_for_query": True,
            }
        )
    result = {
        "schema": "ad.official-report-index.v1",
        "security": security(symbol),
        "issuer": issuer,
        "queries": queries,
        "sources": evidence,
        "announcements": list(found.values()),
        "captured_at": now(),
        "historical_revision_inventory_complete": False,
        "limitation": "Bounded current archive searches; withdrawn, renamed or differently classified revisions may be absent.",
    }
    atomic(path, gzip.compress(encoded(result), mtime=0))
    return result


def select_documents(index):
    eligible = [
        r
        for r in index["announcements"]
        if r["full_report_candidate"] and r["period_end"].endswith("03-31")
    ]
    periods = sorted({r["period_end"] for r in eligible})
    selected = (
        set(periods[i] for i in (0, len(periods) // 2, len(periods) - 1)) if periods else set()
    )
    return [r for r in eligible if r["period_end"] in selected]


def recover_pagination(client, symbol, manifest):
    """Resume pagination using disjoint date windows with exact distinct counts.

    Original responses remain immutable. Recovery has a separate deterministic
    receipt namespace and the same transport, throttle and retry limits. Related
    codes are still rejected. A single date may use bounded validated pages;
    neither strategy establishes issuer/code continuity.
    """
    code = symbol[2:]
    raw, _ = client.fetch(
        "identity-" + symbol, ORIGIN + "/new/information/topSearch/query",
        {"keyWord": code, "maxNum": 30},
    )
    identities = [
        r for r in json.loads(raw) if r.get("code") == code and r.get("category") == "A股"
    ]
    if len(identities) != 1:
        raise ValueError("Official issuer identity missing or ambiguous")
    issuer = identities[0]
    found, evidence, queries = {}, ["identity-" + symbol], []
    start, end = (date.fromisoformat(manifest[k]) for k in ("target_start", "target_end"))
    if end < start:
        raise ValueError("Invalid recovery date bounds")
    for label in ("periodic", "correction", "revision"):
        pending, leaves, seen, nodes = [(start, end)], [], set(), 0
        while pending:
            if client.stopped():
                raise StopRequested()
            a, b = pending.pop()
            nodes += 1
            if nodes > 511:
                raise ValueError("Recovery date partition exceeds reviewed request cap")
            key = f"recovery-{symbol}-{label}-{a.isoformat()}-{b.isoformat()}"
            form = index_form(symbol, issuer, manifest, label, start=a.isoformat(), end=b.isoformat())
            raw, receipt = client.fetch(key, ORIGIN + "/new/hisAnnouncement/query", form)
            payload = json.loads(raw)
            count, more, rows = (
                payload.get("totalAnnouncement"), payload.get("hasMore"),
                payload.get("announcements") or [],
            )
            if type(count) is not int or count < 0 or type(more) is not bool or not isinstance(rows, list):
                raise ValueError("Malformed recovery pagination envelope")
            evidence.append(key)
            atomic(client.root / "recovery-progress.json", encoded(dict(
                at=now(), pid=os.getpid(), symbol=symbol, query=label,
                date_start=a.isoformat(), date_end=b.isoformat(), declared_rows=count,
                requests_this_run=client.calls, state="partitioning_or_validating",
            )))
            page_receipts = [dict(receipt=key, sha256=receipt["sha256"], observed_at=receipt["observed_at"])]
            row_evidence = [(key, receipt)] * len(rows)
            if (count > 30 or more) and a != b:
                mid = a + (b - a) // 2
                pending.extend([(mid + timedelta(days=1), b), (a, mid)])
                continue
            if more:
                # A civil date cannot be bisected. Accept bounded additional
                # pages only when total, row count and distinct IDs all agree.
                for page in range(2, 201):
                    extra_key = key + f"-{page:03}"
                    extra_raw, extra_receipt = client.fetch(
                        extra_key, ORIGIN + "/new/hisAnnouncement/query",
                        index_form(symbol, issuer, manifest, label, page,
                                   start=a.isoformat(), end=b.isoformat()),
                    )
                    extra = json.loads(extra_raw)
                    extra_rows = extra.get("announcements") or []
                    if (type(extra.get("totalAnnouncement")) is not int
                            or extra["totalAnnouncement"] != count
                            or type(extra.get("hasMore")) is not bool
                            or not isinstance(extra_rows, list)):
                        raise ValueError("Recovery single-day pagination total changed or malformed")
                    rows.extend(extra_rows)
                    row_evidence.extend([(extra_key, extra_receipt)] * len(extra_rows))
                    evidence.append(extra_key)
                    nodes += 1
                    page_receipts.append(dict(receipt=extra_key, sha256=extra_receipt["sha256"],
                                              observed_at=extra_receipt["observed_at"]))
                    if nodes > 511 or (not extra_rows and extra["hasMore"]):
                        raise ValueError("Recovery single-day pagination has no bounded progress")
                    if extra["hasMore"] is False:
                        break
                else:
                    raise ValueError("Recovery single-day pagination exceeds reviewed cap")
            if len(rows) != count:
                raise ValueError("Recovery leaf count disagrees with source rows")
            local = set()
            for row, (row_key, row_receipt) in zip(rows, row_evidence, strict=True):
                if (row["secCode"], row["orgId"]) != (code, issuer["orgId"]):
                    raise ValueError("Cross-issuer index row")
                item = report_identity(row)
                if not a.isoformat() <= item["published_date"] <= b.isoformat():
                    raise ValueError("Recovery row outside requested date window")
                aid = item["announcement_id"]
                if aid in local or aid in seen:
                    raise ValueError("Recovery leaf has duplicate announcement identity")
                local.add(aid)
                item.update(index_receipt=row_key, index_observed_at=row_receipt["observed_at"])
                if aid in found and any(
                    found[aid][k] != item[k]
                    for k in ("title", "source_url", "period_end", "published_date")
                ):
                    raise ValueError("Conflicting announcement version during recovery")
                found.setdefault(aid, item)
            seen.update(local)
            leaves.append(dict(
                start=a.isoformat(), end=b.isoformat(), receipt=key, records=count,
                response_sha256=receipt["sha256"], observed_at=receipt["observed_at"],
                page_receipts=page_receipts,
            ))
        queries.append(dict(
            kind=label, pages=nodes, records=len(seen), date_windows=leaves,
            pagination_complete_for_query=True,
            completeness_basis="disjoint_date_windows_with_exact_counts_and_distinct_ids",
            snapshot_atomic_across_windows=False,
        ))
    result = dict(
        schema="ad.official-report-index.v1", security=security(symbol), issuer=issuer,
        queries=queries, sources=evidence, announcements=list(found.values()),
        captured_at=now(), recovery="bounded_date_partition_v1",
        historical_revision_inventory_complete=False,
        limitation="Current archive observations across bounded date windows; no historical PIT or code continuity inferred.",
    )
    atomic(client.root / "indices" / (symbol + ".json.gz"), gzip.compress(encoded(result), mtime=0))
    return result


def capture_index_remainders(client, symbol, manifest):
    """Capture missing original query pages; keep normalization/code checks separate."""
    raw, _ = client.fetch(
        "identity-" + symbol, ORIGIN + "/new/information/topSearch/query",
        dict(keyWord=symbol[2:], maxNum=30),
    )
    identities = [
        r for r in json.loads(raw)
        if r.get("code") == symbol[2:] and r.get("category") == "A股"
    ]
    if len(identities) != 1:
        raise ValueError("Official issuer identity missing or ambiguous")
    terminals = []
    for label in ("periodic", "correction", "revision"):
        for page in range(1, 201):
            raw, receipt = client.fetch(
                f"index-{symbol}-{label}-{page:03}", ORIGIN + "/new/hisAnnouncement/query",
                index_form(symbol, identities[0], manifest, label, page),
            )
            payload = json.loads(raw)
            count, more, rows = (
                payload.get("totalAnnouncement"), payload.get("hasMore"),
                payload.get("announcements") or [],
            )
            if type(count) is not int or count < 0 or type(more) is not bool or not isinstance(rows, list):
                raise ValueError("Malformed remainder pagination envelope")
            if more and not rows:
                raise ValueError("Empty remainder page claims more results")
            if more is False:
                terminals.append(dict(kind=label, terminal_page=page, observed_at=receipt["observed_at"]))
                break
        else:
            raise ValueError("Index exceeds reviewed pagination cap")
    # Reuse the hash-bound offline parser. It quarantines unknown identities and
    # exposes drift, duplicates and listing-lifetime gaps without merging codes.
    from ashare_data.financials.source_repair import recover_index
    return terminals, recover_index(client.root, symbol)


def run_index_remainders(root):
    """Finite continuation of the original related-code failures under its lock."""
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    scope_raw = (root / "scope.json").read_bytes()
    if manifest["schema"] != SCHEMA or digest(scope_raw) != manifest["scope_sha256"]:
        raise ValueError("Changed official batch identity")
    output = root / "supplement" / "related-code-remainders-v1"
    stop = {"value": False}
    previous = {s: signal.signal(s, lambda *_: stop.update(value=True)) for s in (signal.SIGINT, signal.SIGTERM)}

    def stopped():
        return stop["value"] or (root / "STOP").exists()

    try:
        with (root / "writer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            plan_path = output / "plan.json"
            if not plan_path.exists():
                selected, selection_evidence = [], {}
                for symbol in json.loads(scope_raw)["symbols"]:
                    status_raw = (root / "securities" / (symbol + ".json")).read_bytes()
                    status = json.loads(status_raw)
                    if status.get("reason") == "ValueError: Cross-issuer index row":
                        selected.append(symbol)
                        selection_evidence[symbol] = digest(status_raw)
                immutable(plan_path, encoded(dict(
                    source_manifest_sha256=digest((root / "manifest.json").read_bytes()),
                    scope_sha256=manifest["scope_sha256"], symbols=selected,
                    selection_evidence=selection_evidence,
                    canonical_indices_modified=False, code_intervals_merged=False,
                )))
            plan = json.loads(plan_path.read_text())
            if (plan["source_manifest_sha256"] != digest((root / "manifest.json").read_bytes())
                    or plan["scope_sha256"] != manifest["scope_sha256"]):
                raise ValueError("Changed remainder continuation plan")
            if (len(set(plan["symbols"])) != len(plan["symbols"])
                    or not set(plan["symbols"]) <= set(json.loads(scope_raw)["symbols"])):
                raise ValueError("Remainder targets escaped the frozen scope")
            for symbol in plan["symbols"]:
                if digest((root / "securities" / (symbol + ".json")).read_bytes()) != plan["selection_evidence"][symbol]:
                    raise ValueError("Remainder target status changed; review before continuing")
            client = Client(root, stopped)
            while not stopped():
                deferred = False
                for symbol in plan["symbols"]:
                    if stopped():
                        break
                    status_path = output / (symbol + ".json")
                    status = json.loads(status_path.read_text()) if status_path.exists() else {}
                    if status.get("state") == "captured_with_explicit_identity_gaps":
                        artifact = (root / status["recovered_index"]["path"]).resolve()
                        if (not artifact.is_relative_to(output)
                                or digest(artifact.read_bytes()) != status["recovered_index"]["sha256"]):
                            raise ValueError("Captured remainder artifact changed")
                    if status.get("state") in {"captured_with_explicit_identity_gaps", "source_exhausted", "parse_failed"}:
                        continue
                    try:
                        terminals, recovered = capture_index_remainders(client, symbol, manifest)
                        raw = gzip.compress(encoded(recovered), mtime=0)
                        artifact = output / (symbol + ".json.gz")
                        atomic(artifact, raw)
                        status = dict(
                            state="captured_with_explicit_identity_gaps", terminal_queries=terminals,
                            recovered_index=dict(path=str(artifact.relative_to(root)), sha256=digest(raw)),
                            exact_code_records=len(recovered["announcements"]),
                            related_code_records=len(recovered["related_code_announcements"]),
                            quarantined_rows=len(recovered["quarantined_rows"]),
                            query_set_complete=recovered["query_set_complete"],
                            issuer_continuity_proven=False, canonical_index_modified=False,
                        )
                    except Deferred as error:
                        deferred = True
                        status = dict(state="deferred", reason=str(error))
                    except SourceExhausted as error:
                        status = dict(state="source_exhausted", reason=str(error))
                    except StopRequested:
                        break
                    except Exception as error:
                        status = dict(state="parse_failed", reason=f"{type(error).__name__}: {error}"[:500])
                    status.update(at=now(), symbol=symbol, pid=os.getpid(), requests_this_run=client.calls)
                    atomic(status_path, encoded(status))
                    atomic(output / "progress.json", encoded(status))
                    print(json.dumps(status, ensure_ascii=False), flush=True)
                    host = client.state.get("www.cninfo.com.cn", {})
                    if host.get("blocked") or host.get("cooldown_until", 0) > time.time():
                        deferred = True
                        break
                if not deferred or any(s.get("blocked") for s in client.state.values()):
                    break
                wake = max([time.time() + 30] + [s.get("cooldown_until", 0) for s in client.state.values()])
                while time.time() < wake and not stopped():
                    time.sleep(min(1, wake - time.time()))
            states = Counter()
            for symbol in plan["symbols"]:
                path = output / (symbol + ".json")
                states[json.loads(path.read_text())["state"] if path.exists() else "pending"] += 1
            result = dict(
                at=now(), pid=os.getpid(), states=dict(states), requests=client.calls,
                stop_requested=stopped(), source_plan=str(plan_path),
                source_restrictions=client.state, published_release_modified=False,
            )
            atomic(output / "completion.json", encoded(result))
            return result
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def statement_cells(text, report):
    """Conservative Q1 native-text candidates; no unreviewed auto-PIT promotion.

    Only consolidated statement sections with explicit yuan units and a current
    column first are admitted. Every candidate retains the line and page. Annual,
    Q3 multi-period columns, scanned PDFs and ambiguous repeated fields are gaps.
    """
    if not report["period_end"].endswith("03-31"):
        return []
    rows = []
    section = None
    unit = False
    current_column = False
    start_page = 0
    for page in text["pages"]:
        lines = page["text"].splitlines()
        for ordinal, line in enumerate(lines):
            compact = re.sub(r"\s", "", line)
            if any(
                name in compact
                for name in ("母公司资产负债表", "母公司利润表", "母公司现金流量表", "财务报表附注")
            ) or compact in {"资产负债表", "利润表", "现金流量表"}:
                section = None
            for heading, kind in (
                ("合并资产负债表", "balance"),
                ("合并利润表", "income"),
                ("合并现金流量表", "cashflow"),
            ):
                if heading in compact and len(compact) < 40 and not re.search(r"\.{3}|…", compact):
                    section, unit, current_column, start_page = kind, False, False, page["page"]
            if section is None or page["page"] - start_page > 4:
                continue
            if "单位" in compact:
                unit = (
                    bool(re.search(r"单位[：:]?(?:人民币)?元(?:币种[:：]?人民币)?", compact))
                    and "万元" not in compact
                )
            if "项目" in compact or compact.startswith("本期发生额"):
                current_column = bool(
                    "本期发生额" in compact
                    or "本期金额" in compact
                    or report["period_end"][:4] in compact
                )
                years = re.findall(r"20\d{2}", compact)
                if years and years[0] != report["period_end"][:4]:
                    current_column = False
            amounts = list(MONEY.finditer(line))
            if not amounts or not unit or not current_column:
                continue
            prefix = re.sub(r"\s", "", line[: amounts[0].start()])
            prefix = re.sub(r"^[一二三四五六七八九十0-9（）()、.．]+", "", prefix)
            prefix = re.sub(r"^(?:其中|加|减)[：:]", "", prefix)
            prefix = re.sub(r"[（(].*$", "", prefix)
            # Footnote references after the label are deliberately not guessed.
            for field, labels in LABELS.items():
                if prefix not in labels:
                    continue
                kind = (
                    "balance"
                    if field in ("total_assets", "total_liabilities")
                    else "cashflow"
                    if field in ("net_operate_cash_flow", "cash_equivalents_at_end")
                    else "income"
                )
                if kind != section:
                    continue
                rows.append(
                    {
                        "schema": "ad.official-statement-cell-candidate.v1",
                        "field": field,
                        "value": str(
                            Decimal(amounts[0].group().replace(",", "").replace("−", "-"))
                        ),
                        "unit": "CNY",
                        "currency": "CNY",
                        "scope": "consolidated",
                        "statement": section,
                        "basis": "instant" if section == "balance" else "YTD",
                        "period_start": None
                        if section == "balance"
                        else report["period_end"][:4] + "-01-01",
                        "period_end": report["period_end"],
                        "page": page["page"],
                        "line": ordinal + 1,
                        "source_line": line,
                        "column": "first_current_period",
                        "review_status": "native_text_candidate_requires_layout_review",
                        "historical_pit_certified": False,
                    }
                )
    counts = Counter(r["field"] for r in rows)
    for row in rows:
        if counts[row["field"]] != 1:
            row["review_status"] = "ambiguous_duplicate_field"
    return rows


def collect_document(client, symbol, report, extractor, source_batch, force=False):
    root = client.root
    aid = report["announcement_id"]
    output = root / "documents" / (symbol + "-" + aid + ".json.gz")
    if output.exists() and not force:
        return json.loads(gzip.decompress(output.read_bytes()))
    raw, receipt = client.fetch("pdf-" + aid, report["source_url"])
    if not raw.startswith(b"%PDF"):
        raise ValueError("Official document is not a PDF")
    # An explicit retention mode keeps scratch even on failure; no cleanup is
    # allowed during collection sessions that prohibit permanent deletion.
    if PRESERVE_TEMPORARIES.get():
        scratch = client.root / "retained-temporaries"
        scratch.mkdir(exist_ok=True)
        context = nullcontext(tempfile.mkdtemp(prefix="ad-official-", dir=scratch))
    else:
        context = tempfile.TemporaryDirectory(prefix="ad-official-")
    with context as temp:
        pdf = Path(temp) / "report.pdf"
        pdf.write_bytes(raw)
        result = subprocess.run(
            [str(extractor), str(pdf)], capture_output=True, check=True, timeout=90
        )
        text = json.loads(result.stdout)
    candidates = statement_cells(text, report)
    provider = []
    for kind in ("sina_balance", "sina_income", "sina_cashflow"):
        path = source_batch / "normalized" / (kind + "-" + symbol + ".jsonl.gz")
        if path.exists():
            for line in gzip.decompress(path.read_bytes()).splitlines():
                row = json.loads(line)
                if row["period_end"] == report["period_end"]:
                    provider.append((kind + "-" + symbol, row))
    for candidate in candidates:
        candidate["security"] = security(symbol)
        matches = []
        for task, row in provider:
            metric = row["metrics"].get(candidate["field"])
            if not metric or metric["state"] != "source_observed":
                continue
            matches.append(
                {
                    "task_id": task,
                    "source_revision_id": row["source_revision_id"],
                    "response_sha256": row["response_sha256"],
                    "source_locator": row["source_locator"],
                    "value": metric["value"],
                    "amount_equal": Decimal(metric["value"]) == Decimal(candidate["value"]),
                    "scope_equal": all(
                        row.get(k) == candidate[k]
                        for k in ("scope", "currency", "basis", "period_start", "period_end")
                    ),
                }
            )
        candidate["provider_matches"] = matches
    useful_pages = {1, *(c["page"] for c in candidates)}
    excerpt = {
        **text,
        "pages": [p for p in text["pages"] if p["page"] in useful_pages],
        "full_text_retained": False,
    }
    data = {
        "schema": "ad.official-financial-document.v1",
        "security": security(symbol),
        "report": report,
        "document_sha256": digest(raw),
        "pdf_receipt": "pdf-" + aid,
        "observed_at": receipt["observed_at"],
        "text": excerpt,
        "cells": candidates,
        "normalizer_sha256": NORMALIZER_SHA256,
        "strict_pit_allowed": False,
        "blockers": [
            "historical_first_publication_clock_unproven",
            "complete_revision_chain_unproven",
            "layout_review_pending",
        ],
        "source_snapshot_is_official_version": False,
        "unified_database_imported": False,
    }
    atomic(output, gzip.compress(encoded(data), mtime=0))
    return data


def replay(root, extractor):
    """Rebuild cached derivatives while preserving exactly reviewed documents."""
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    with (root / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reviewed = set()
        review_path = root / "field-reviews.json"
        if review_path.exists():
            review = json.loads(review_path.read_text())
            if review.get("schema") != "ad.official-field-review.v1":
                raise ValueError("Unknown field review; replay stopped")
            for item in review["fields"]:
                name = item["symbol"] + "-" + item["announcement_id"] + ".json.gz"
                path = (root / "documents" / name).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("Reviewed document path escaped batch")
                if digest(path.read_bytes()) != item["document_derivative_sha256"]:
                    raise ValueError("Reviewed derivative changed before replay")
                reviewed.add(name)
        client = Client(
            root,
            transport=lambda *_: (_ for _ in ()).throw(
                ValueError("offline replay forbids network")
            ),
        )
        for path in (root / "indices").glob("*.json.gz"):
            index = json.loads(gzip.decompress(path.read_bytes()))
            for item in index["announcements"]:
                item["published_date"] = (
                    datetime.fromtimestamp(
                        item["announcement_time_raw"] / 1000, timezone(timedelta(hours=8))
                    )
                    .date()
                    .isoformat()
                )
                item["date_timezone"] = "Asia/Shanghai"
            index["normalizer_sha256"] = NORMALIZER_SHA256
            atomic(path, gzip.compress(encoded(index), mtime=0))
            symbol = path.name.removesuffix(".json.gz")
            for report in index["announcements"]:
                target = (
                    root / "documents" / (symbol + "-" + report["announcement_id"] + ".json.gz")
                )
                if not target.exists():
                    continue
                if target.name in reviewed:
                    continue
                receipt = json.loads(
                    (root / "receipts" / ("pdf-" + report["announcement_id"] + ".json")).read_text()
                )
                if receipt["state"] != "complete":
                    raise ValueError("Replay needs complete original PDF")
                collect_document(
                    client, symbol, report, extractor, Path(manifest["source_batch"]), force=True
                )
    return audit(root, True)


def audit(root, write=False):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    indices = list((root / "indices").glob("*.json.gz"))
    documents = list((root / "documents").glob("*.json.gz"))
    index_rows = 0
    dates = []
    cells = exact = 0
    states = Counter()
    for path in indices:
        data = json.loads(gzip.decompress(path.read_bytes()))
        index_rows += len(data["announcements"])
        dates += [r["period_end"] for r in data["announcements"] if r["full_report_candidate"]]
    for path in documents:
        data = json.loads(gzip.decompress(path.read_bytes()))
        cells += len(data["cells"])
        for row in data["cells"]:
            states[row["review_status"]] += 1
            exact += any(m["amount_equal"] and m["scope_equal"] for m in row["provider_matches"])
    security_states = Counter(
        json.loads(p.read_text())["state"] for p in (root / "securities").glob("*.json")
    )
    result = {
        "schema": "ad.official-expansion-coverage.v1",
        "updated_at": now(),
        "target_codes": manifest["target_codes"],
        "indexed_codes": len(indices),
        "indexed_announcements": index_rows,
        "selected_documents_captured": len(documents),
        "earliest_indexed_period": min(dates) if dates else None,
        "latest_indexed_period": max(dates) if dates else None,
        "native_text_cell_candidates": cells,
        "candidate_states": dict(states),
        "provider_exact_matches": exact,
        "security_states": dict(security_states),
        "real_strict_pit_records": 0,
        "unified_database_imported": False,
        "full_history_complete": False,
    }
    if write:
        atomic(root / "coverage.json", encoded(result))
    return result


def run(root, extractor, max_codes=0, retry_parse_failed=False, recover_paging=False):
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    scope_raw = (root / "scope.json").read_bytes()
    if manifest["schema"] != SCHEMA or digest(scope_raw) != manifest["scope_sha256"]:
        raise ValueError("Changed official batch identity")
    scope = json.loads(scope_raw)
    stopped = {"value": False}

    def stop(*_):
        stopped["value"] = True

    previous = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}

    def should_stop():
        return stopped["value"] or (root / "STOP").exists()

    with (root / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = Client(root, should_stop)
        if retry_parse_failed:
            for symbol in scope["symbols"]:
                status_path = root / "securities" / (symbol + ".json")
                if not status_path.exists():
                    continue
                status = json.loads(status_path.read_text())
                if status.get("state") != "parse_failed":
                    continue
                if recover_paging and status.get("reason", "").removeprefix("ValueError: ") not in PAGINATION_ERRORS:
                    continue
                review_path = root / "parse-reviews" / (symbol + ".json")
                reviews = json.loads(review_path.read_text()) if review_path.exists() else []
                reviews.append(
                    {
                        "at": now(),
                        "prior_status": status,
                        "normalizer_sha256": digest(Path(__file__).read_bytes()),
                        "action": (
                            "resume_pagination_with_existing_cache_and_bounded_date_windows"
                            if recover_paging else "retry_existing_source_cache_after_parser_review"
                        ),
                    }
                )
                atomic(review_path, encoded(reviews))
                atomic(status_path, encoded({"state": "pending", "symbol": symbol}))
        completed = 0
        try:
            while not should_stop():
                pending, changed = 0, False
                for symbol in scope["symbols"]:
                    if should_stop() or (max_codes and completed >= max_codes):
                        return audit(root, True)
                    status_path = root / "securities" / (symbol + ".json")
                    status = json.loads(status_path.read_text()) if status_path.exists() else {}
                    if status.get("state") in {"complete", "parse_failed", "source_exhausted"}:
                        continue
                    try:
                        try:
                            index = index_security(client, symbol, manifest)
                        except ValueError as error:
                            if not recover_paging or str(error) not in PAGINATION_ERRORS:
                                raise
                            index = recover_pagination(client, symbol, manifest)
                        docs = (
                            select_documents(index) if symbol in scope["document_symbols"] else []
                        )
                        for report in docs:
                            collect_document(
                                client, symbol, report, extractor, Path(manifest["source_batch"])
                            )
                        status = {
                            "state": "complete",
                            "announcements": len(index["announcements"]),
                            "selected_documents": len(docs),
                        }
                        completed += 1
                        changed = True
                    except Deferred as error:
                        pending += 1
                        status = {"state": "deferred", "reason": str(error)}
                    except StopRequested:
                        break
                    except SourceExhausted as error:
                        status = {"state": "source_exhausted", "reason": str(error)}
                    except Exception as error:
                        status = {
                            "state": "parse_failed",
                            "reason": f"{type(error).__name__}: {error}"[:500],
                        }
                    status.update(symbol=symbol, updated_at=now())
                    atomic(status_path, encoded(status))
                    atomic(
                        root / "progress.json",
                        encoded({"pid": os.getpid(), "requests_this_run": client.calls, **status}),
                    )
                    print(json.dumps(status, ensure_ascii=False), flush=True)
                    if changed and completed % 10 == 0:
                        audit(root, True)
                    # A blocked/cooling shared index host applies to every code.
                    host = client.state.get("www.cninfo.com.cn", {})
                    if host.get("blocked") or host.get("cooldown_until", 0) > time.time():
                        pending += 1
                        break
                audit(root, True)
                if not pending:
                    break
                if any(s.get("blocked") for s in client.state.values()):
                    break
                wake = max(
                    [time.time() + 30] + [s.get("cooldown_until", 0) for s in client.state.values()]
                )
                atomic(
                    root / "supervisor.json",
                    encoded(
                        {
                            "pid": os.getpid(),
                            "state": "cooldown_or_retry",
                            "wake_at_epoch": wake,
                            "at": now(),
                        }
                    ),
                )
                while time.time() < wake and not should_stop():
                    time.sleep(min(30, wake - time.time()))
        finally:
            atomic(
                root / "worker-exit.json",
                encoded(
                    {
                        "pid": os.getpid(),
                        "at": now(),
                        "stop_requested": should_stop(),
                        "requests": client.calls,
                    }
                ),
            )
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return audit(root, True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "audit", "replay", "capture-remainders"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-batch", type=Path)
    parser.add_argument("--document-codes", type=int, default=120)
    parser.add_argument("--extractor", type=Path)
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--retry-parse-failed", action="store_true")
    parser.add_argument("--recover-pagination", action="store_true")
    parser.add_argument("--retain-temporaries", action="store_true")
    args = parser.parse_args()
    with preserve_temporaries(args.retain_temporaries):
        if args.command == "prepare":
            result = prepare(args.root, args.source_batch, args.document_codes)
        elif args.command == "run":
            result = run(args.root, args.extractor, args.max_codes, args.retry_parse_failed, args.recover_pagination)
        elif args.command == "replay":
            result = replay(args.root, args.extractor)
        elif args.command == "capture-remainders":
            result = run_index_remainders(args.root)
        else:
            result = audit(args.root, True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
