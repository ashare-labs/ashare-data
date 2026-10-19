"""Resumable public source snapshots; no writes to the daily or unified database.

Only local research observations are produced. Provider current statements are
never promoted into FinancialLedger's official, historically visible reports.
"""
from __future__ import annotations

import argparse
import calendar
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from ashare_data.corporate_actions.sources import PARSERS

SCHEMA = "ad.financial-reference-batch.v1"
KINDS = ("sina_balance", "sina_income", "sina_cashflow", "sina_actions", "sina_float")
ALLOWED_KINDS = KINDS + ("sina_structure",)
STATEMENTS = {"sina_balance": "fzb", "sina_income": "lrb", "sina_cashflow": "llb"}
HOSTS = {"quotes.sina.cn", "vip.stock.finance.sina.com.cn", "stock.finance.sina.com.cn", "www.szse.cn"}
MAX_BODY = 16_000_000
NORMALIZER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
CORPORATE_PARSER_SHA256 = hashlib.sha256(
    (Path(__file__).parents[1] / "corporate_actions/sources.py").read_bytes()
).hexdigest()
TERMINAL = {"complete", "partial_source", "empty_unknown", "parse_failed", "failed_exhausted"}
METRICS = {
    "BIZTOTINCO": ("total_operating_revenue", "营业总收入"),
    "BIZINCO": ("operating_revenue", "营业收入"),
    "NETPROFIT": ("net_profit", "净利润"),
    "PARENETP": ("parent_net_profit", "归属于母公司所有者的净利润"),
    "MANANETR": ("net_operate_cash_flow", "经营活动产生的现金流量净额"),
    "TOTASSET": ("total_assets", "资产总计"),
    "TOTLIAB": ("total_liabilities", "负债合计"),
    "FINALCASHBALA": ("cash_equivalents_at_end", "期末现金及现金等价物余额"),
}


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, raw):
    """Same-directory atomic replace, cleaning the temporary on every outcome."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def immutable(path, raw):
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f"Refusing changed immutable file: {path}")
    else:
        atomic(path, raw)


def day(value):
    if value in (None, "", "--", "-", "00000000"):
        return None
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text).isoformat()


def numeric(value):
    if value is None or value in ("", "--", "-"):
        return None, "source_missing"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("nonfinite")
        return format(number, "f"), "source_observed"
    except (InvalidOperation, ValueError):
        return None, "invalid_source_numeric"


def security(symbol):
    if not re.fullmatch(r"(?:sh|sz)\d{6}", symbol):
        raise ValueError("Invalid source security")
    return symbol[2:] + (".XSHG" if symbol.startswith("sh") else ".XSHE")


def url_for(kind, symbol):
    security(symbol)
    if kind in STATEMENTS:
        return "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022?" + urlencode({
            "paperCode": symbol, "source": STATEMENTS[kind], "type": "0", "page": 1, "num": 1000,
        })
    if kind in ("sina_actions", "sina_structure"):
        page = "vISSUE_ShareBonus" if kind == "sina_actions" else "vCI_StockStructure"
        return f"https://vip.stock.finance.sina.com.cn/corp/go.php/{page}/stockid/{symbol[2:]}.phtml"
    if kind == "sina_float":
        return "https://stock.finance.sina.com.cn/stock/api/jsonp.php/" + (
            f"var%20KKE_ShareAmount_{symbol}=/StockService.getAmountBySymbol?symbol={symbol}"
        )
    raise ValueError("Unsupported source kind")


def quarters(start, end):
    return [f"{year}-{suffix}" for year in range(int(start[:4]), int(end[:4]) + 1)
            for suffix in ("03-31", "06-30", "09-30", "12-31")
            if start <= f"{year}-{suffix}" <= end]


def parse_financial(raw, task, start, end):
    data = json.loads(raw, parse_float=str)
    result = data.get("result", {})
    if result.get("status", {}).get("code") != 0:
        raise ValueError("Sina returned unsuccessful financial status")
    data = result.get("data")
    if not isinstance(data, dict):
        raise ValueError("Missing financial result object")
    count = int(data["report_count"])
    dates, reports = data["report_date"], data["report_list"]
    if count == 0 and dates == [] and reports in ({}, []):
        return [], {"source_status": "empty_unknown", "source_report_count": 0,
                    "pagination_complete": True, "absence_proved": False}
    if not isinstance(reports, dict):
        raise ValueError("Missing report map")
    keys = [entry["date_value"] for entry in dates]
    if count != len(keys) or len(keys) != len(set(keys)) or set(keys) != set(reports):
        raise ValueError("Incomplete or duplicate financial pagination")
    rows, all_periods = [], []
    for key in keys:
        period = day(key)
        all_periods.append(period)
        if not start <= period <= end:
            continue
        report = reports[key]
        fields, metrics, seen = [], {}, set()
        issues = []
        try:
            published = day(report.get("publish_date"))
        except (ValueError, TypeError):
            published = None
            issues.append("invalid_announcement_date")
        if published and published < period:
            issues.append("announcement_before_period")
        if not isinstance(report.get("data"), list):
            raise ValueError("Missing statement fields")
        for index, item in enumerate(report["data"]):
            if item.get("item_source") not in (None, STATEMENTS[task["kind"]]):
                raise ValueError("Statement field belongs to another report kind")
            field = item.get("item_field")
            title = item.get("item_title")
            value, state = numeric(item.get("item_value"))
            # Preserve duplicate IDs/titles by ordinal; do not silently deduplicate.
            fields.append({"source_field": field, "source_title": title, "ordinal": index,
                           "value": value, "state": state, "raw_value": item.get("item_value")})
            if field in METRICS:
                canonical, expected_title = METRICS[field]
                if field in seen or title != expected_title:
                    raise ValueError("Ambiguous or changed mapped financial field")
                seen.add(field)
                metrics[canonical] = {"value": value, "state": state, "source_ordinal": index,
                                      "source_field": field, "unit": "CNY" if report.get("rCurrency") == "CNY" else None}
        rtype = report.get("rType")
        outside_lifetime = bool(task.get("out") and period >= task["out"])
        rows.append({
            "schema": "ad.financial-source-report.v1", "security": security(task["symbol"]),
            "provider_security": task["symbol"], "source": "sina", "statement": STATEMENTS[task["kind"]],
            "period_end": period, "basis": "instant" if task["kind"] == "sina_balance" else "YTD",
            "period_start": None if task["kind"] == "sina_balance" else period[:4] + "-01-01",
            "scope": "consolidated" if rtype == "合并期末" else "source_unspecified",
            "currency": report.get("rCurrency"), "announcement_date": published,
            "unit_basis": "native_source_currency_amount_no_scale_conversion",
            "published_at": None, "historical_available_at": None,
            "announcement_time_precision": "date" if published else "unknown",
            "source_revision_id": digest(encoded(report)), "supersedes": None,
            "version_kind": "provider_snapshot_hash_not_official_revision_number",
            "source_updated_at_epoch": report.get("update_time"),
            "revision_status": "current_provider_snapshot_prior_versions_unknown",
            "source_metadata": {k: v for k, v in report.items() if k != "data"},
            "identity_status": "request_bound_not_independently_verified",
            "period_after_code_exit": outside_lifetime,
            "historical_pit_certified": False, "revision_history_complete": False,
            "first_publication_proven": False, "source_issues": issues,
            "fields": fields, "metrics": metrics,
            "source_locator": f"result/data/report_list/{key}",
        })
    periods = sorted(row["period_end"] for row in rows)
    # Missing quarters are diagnostic candidates, not proof of a filing obligation.
    lo = max(start, task.get("ipo") or start)
    hi = min(end, task.get("out") or end)
    return rows, {
        "source_status": "parsed_provider_statements", "source_report_count": count,
        "source_earliest_period": min(all_periods) if all_periods else None,
        "source_latest_period": max(all_periods) if all_periods else None,
        "target_earliest_period": min(periods) if periods else None,
        "target_latest_period": max(periods) if periods else None,
        "target_periods": periods, "target_report_count": len(rows),
        "quarters_without_observation": sorted(set(quarters(lo, hi)) - set(periods)),
        "pagination_complete": True, "absence_proved": False,
        "mapped_nonnull_values": sum(v["value"] is not None for r in rows for v in r["metrics"].values()),
        "after_code_exit_reports": sum(r["period_after_code_exit"] for r in rows),
    }


def parse_reference(raw, task, start, end):
    parsed = PARSERS[task["kind"]](raw, security(task["symbol"]))
    rows, all_dates, target_dates = [], [], []
    for category in ("actions", "shares", "nonimplemented", "unparsed_records"):
        for item in parsed.get(category, []):
            values = item.get("fields", {})
            quality = []
            if category == "shares" and values.get("total_shares") is not None:
                total = Decimal(values["total_shares"])
                components = [values.get(k) for k in ("float_a_shares", "float_b_shares", "float_h_shares")]
                if sum(Decimal(x) for x in components if x is not None) > total:
                    quality.append("source_share_components_exceed_total")
            dates = [v for k, v in values.items() if k.endswith("date") and isinstance(v, str)]
            if item.get("date"):
                dates.append(item["date"])
            dates = [v for v in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) and v > "1900-01-01"]
            all_dates.extend(dates)
            if dates and not any(start <= d <= end for d in dates):
                continue
            target_dates.extend(d for d in dates if start <= d <= end)
            rows.append({"schema": "ad.corporate-source-observation.v1", "category": category,
                         "security": security(task["symbol"]), "source": task["kind"],
                         "observation": item, "source_revision_id": digest(encoded(item)),
                         "historical_available_at": None, "historical_pit_certified": False,
                         "source_effective_date": item.get("date") if category == "shares" else None,
                         "source_date_precision": "date", "published_at": None,
                         "valid_to": None, "carry_forward_authorized": False,
                         "quality_flags": quality,
                         "absence_proved": False})
    return rows, {"source_status": parsed.get("source_status"),
                  "table_states": parsed.get("table_states"),
                  "source_earliest_date": min(all_dates) if all_dates else None,
                  "source_latest_date": max(all_dates) if all_dates else None,
                  "target_earliest_date": min(target_dates) if target_dates else None,
                  "target_latest_date": max(target_dates) if target_dates else None,
                  "counts": dict(Counter(r["category"] for r in rows)),
                  "absence_proved": False,
                  "full_history_proven": False}


def parse_calendar(raw, task, start, end):
    payload = json.loads(raw)
    values = payload.get("data")
    if not isinstance(values, list):
        raise ValueError("Missing official monthly calendar")
    if not values:
        return [], {"source_status": "empty_unknown", "absence_proved": False}
    year, month = map(int, task["month"].split("-"))
    expected = {f"{year:04}-{month:02}-{d:02}" for d in range(1, calendar.monthrange(year, month)[1] + 1)}
    dates = [v["jyrq"] for v in values]
    if len(dates) != len(set(dates)) or not set(dates) <= expected:
        raise ValueError("Official month has duplicate or escaped dates")
    missing = sorted(expected - set(dates))
    if any(v.get("jybz") not in {"0", "1"} for v in values):
        raise ValueError("Unknown official trading-day flag")
    rows = [{"schema": "ad.exchange-calendar-observation.v1", "exchange": "XSHE",
             "date": value["jyrq"], "is_open": value["jybz"] == "1", "raw_flag": value["jybz"],
             "source": "szse_monthList", "source_locator": f"data/{i}",
             "published_at": None, "historical_available_at": None,
             "historical_pit_certified": False, "security_tradability_proven": False}
            for i, value in enumerate(values) if start <= value["jyrq"] <= end]
    return rows, {"source_status": "partial_source_month" if missing else "complete_source_month",
                  "source_missing_dates": missing, "source_month_complete": not missing,
                  "source_month": task["month"],
                  "source_natural_days": len(values), "target_natural_days": len(rows),
                  "target_open_days": sum(r["is_open"] for r in rows),
                  "target_earliest_date": min(r["date"] for r in rows) if rows else None,
                  "target_latest_date": max(r["date"] for r in rows) if rows else None,
                  "exchange_scope": "XSHE_only", "absence_proved": False}


def prepare_calendar(root, start, end, reuse_root=None):
    root = Path(root).resolve()
    day(start), day(end)
    if start > end:
        raise ValueError("Invalid target window")
    root.mkdir(parents=True, exist_ok=True)
    if (root / "manifest.json").exists():
        _, manifest, tasks = load_batch(root)
        if (manifest["target_start"], manifest["target_end"]) != (start, end) or any(t["kind"] != "szse_calendar" for t in tasks):
            raise ValueError("Existing calendar batch changed")
        return manifest
    tasks = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        for month in range(1, 13):
            label = f"{year:04}-{month:02}"
            if start[:7] <= label <= end[:7]:
                tasks.append({"id": "szse_calendar-" + label, "kind": "szse_calendar", "symbol": None,
                              "month": label, "url": "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=" + label})
    baseline = encoded({"schema": "ad.exchange-calendar-scope.v1", "exchange": "XSHE",
                        "official_months_requested": [t["month"] for t in tasks]})
    task_raw = encoded(tasks)
    immutable(root / "universe.json", baseline)
    immutable(root / "tasks.json", task_raw)
    manifest = {"schema": SCHEMA, "created_at": now(), "target_start": start, "target_end": end,
                "baseline_codes": 0, "target_codes": 0, "planned_tasks": len(tasks),
                "source_families": ["szse_calendar"], "universe_sha256": digest(baseline),
                "tasks_sha256": digest(task_raw), "exchange": "XSHE", "unified_database_imported": False,
                "historical_pit_certified": False, "source_policy": "unauthenticated_local_research_no_redistribution"}
    immutable(root / "manifest.json", encoded(manifest))
    if reuse_root:
        reuse_root = Path(reuse_root).resolve()
        for task in tasks:
            body = reuse_root / ("szse-" + task["month"] + ".json")
            old_receipt = body.with_suffix(".json.receipt.json")
            if not body.exists() or not old_receipt.exists():
                continue
            old = json.loads(old_receipt.read_text())
            raw = body.read_bytes()
            if old.get("status") != 200 or old.get("sha256") != digest(raw) or old.get("url") != task["url"]:
                raise ValueError("Existing official calendar evidence changed")
            observed_at = old.get("captured_end") or old.get("received_at")
            if not observed_at:
                raise ValueError("Existing calendar evidence has no capture clock")
            receipt = {"task_id": task["id"], "state": "captured", "observed_at": observed_at,
                       "raw": artifact(root, "raw/" + task["id"] + ".json.gz", gzip.compress(raw, mtime=0)),
                       "attempts": [{"response_sha256": digest(raw), "http_status": 200,
                                     "received_at": observed_at, "local_replay": True,
                                     "original_path": str(body), "url": task["url"]}]}
            normalize_saved(root, manifest, task, receipt, raw)
            atomic(root / "receipts" / (task["id"] + ".json"), encoded(receipt))
    return manifest


def prepare(root, daily, start, end, kinds=None):
    root, daily = Path(root).resolve(), Path(daily).resolve()
    kinds = tuple(kinds or KINDS)
    if len(set(kinds)) != len(kinds) or not set(kinds) <= set(ALLOWED_KINDS):
        raise ValueError("Invalid source families")
    day(start), day(end)
    if start > end or root == daily.parent or daily.is_relative_to(root):
        raise ValueError("Invalid batch window or root")
    root.mkdir(parents=True, exist_ok=True)
    if (root / "manifest.json").exists():
        manifest = json.loads((root / "manifest.json").read_text())
        if (manifest["daily_database"], manifest["target_start"], manifest["target_end"]) != (str(daily), start, end):
            raise ValueError("Existing batch identity changed")
        existing_kinds = {t["kind"] for t in json.loads((root / "tasks.json").read_text())}
        if existing_kinds != set(kinds):
            raise ValueError("Existing source families changed")
        return manifest
    with sqlite3.connect(daily.as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        instruments = [dict(row) for row in con.execute("SELECT * FROM instruments ORDER BY symbol")]
        events = [dict(row) for row in con.execute("SELECT * FROM code_events ORDER BY id")]
        metadata = dict(con.execute("SELECT key,value FROM metadata"))
    tasks = []
    excluded = []
    for row in instruments:
        # Preserve the whole lineage baseline, and exclude only codes whose entire
        # known lifetime is outside this target window from new HTTP collection.
        if row["ipo"] > end or (row["out"] and row["out"] < start):
            excluded.append({"symbol": row["symbol"], "reason": "known_code_lifetime_outside_target"})
            continue
        for kind in kinds:
            tasks.append({"id": kind + "-" + row["symbol"], "kind": kind,
                          "symbol": row["symbol"], "ipo": row["ipo"], "out": row["out"],
                          "board": row["board"], "url": url_for(kind, row["symbol"])})
    # Start with both exchanges, banks/industry, exited codes and SME lineage.
    pilot = ["sh600600", "sh600000", "sh600656", "sz000001", "sz000002", "sh600519",
             "sz000858", "sh601318", "sz002001", "sh600849", "sh601313", "sz000022"]
    rank = {symbol: i for i, symbol in enumerate(pilot)}
    tasks.sort(key=lambda t: (rank.get(t["symbol"], 100), t["symbol"], kinds.index(t["kind"])))
    baseline = {"schema": "ad.expansion-universe.v1", "instruments": instruments,
                "code_events": events, "daily_metadata": metadata, "excluded_from_target": excluded,
                "names_are_historical": False, "main_board_before_sme_merger": False}
    raw = encoded(baseline)
    immutable(root / "universe.json", raw)
    task_raw = encoded(tasks)
    immutable(root / "tasks.json", task_raw)
    manifest = {"schema": SCHEMA, "created_at": now(), "daily_database": str(daily),
                "target_start": start, "target_end": end, "baseline_codes": len(instruments),
                "target_codes": len(instruments) - len(excluded), "planned_tasks": len(tasks),
                "source_families": list(kinds),
                "universe_sha256": digest(raw), "tasks_sha256": digest(task_raw),
                "source_policy": "unauthenticated_local_research_no_redistribution",
                "source_rate_limit": "not_published; one worker, minimum 2 seconds between requests",
                "daily_tables_reused_in_place": ["bars.tradestatus", "bars.is_st", "calendar", "instruments",
                                                  "listing_facts", "code_events", "factor_versions", "factors"],
                "unified_database_engine": "pending_user_review", "unified_database_imported": False,
                "historical_pit_certified": False, "source_revision_archive_complete": False,
                "blocked_families": {
                    "official_historical_price_limits": "no_verified_free_full_history_source",
                    "official_publication_times_and_revision_chain": "requires_announcement_index_and_original_reports",
                    "official_full_history_calendar": "existing_source_calendar_reused; exchange verification incomplete",
                }}
    immutable(root / "manifest.json", encoded(manifest))
    return manifest


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password:
            raise ValueError("Redirect outside explicit public sources")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password:
        raise ValueError("Unapproved public source")
    opener = build_opener(SafeRedirect())
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
    if parsed.hostname == "www.szse.cn":
        headers["Referer"] = "https://www.szse.cn/"
    req = Request(url, headers=headers)
    try:
        with opener.open(req, timeout=30) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise ValueError("Response exceeds body limit")
            return response.status, raw, dict(response.headers)
    except HTTPError as error:
        # Diagnostic bytes are hashed but not retained as HTML error-page archives.
        return error.code, error.read(4096), dict(error.headers)


def verify_receipt(root, receipt):
    root = Path(root).resolve()
    for field in ("raw", "normalized"):
        item = receipt.get(field)
        if item:
            path = (root / item["path"]).resolve()
            if not path.is_relative_to(root):
                raise ValueError("Artifact outside batch")
            content = path.read_bytes()
            if len(content) != item["bytes"] or digest(content) != item["sha256"]:
                raise ValueError("Artifact checksum mismatch: " + str(path))


def artifact(root, relative, raw):
    atomic(root / relative, raw)
    return {"path": relative, "bytes": len(raw), "sha256": digest(raw)}


def normalize_saved(root, manifest, task, receipt, raw=None):
    if raw is None:
        verify_receipt(root, receipt)
        raw = gzip.decompress((root / receipt["raw"]["path"]).read_bytes())
    if digest(raw) != receipt["attempts"][-1]["response_sha256"]:
        raise ValueError("Uncompressed response checksum mismatch")
    try:
        parser = (parse_calendar if task["kind"] == "szse_calendar" else
                  parse_financial if task["kind"] in STATEMENTS else parse_reference)
        rows, coverage = parser(raw, task, manifest["target_start"], manifest["target_end"])
        for row in rows:
            row["observed_at"] = receipt["observed_at"]
            row["response_sha256"] = receipt["attempts"][-1]["response_sha256"]
        payload = gzip.compress(b"".join(encoded(row) for row in rows), mtime=0)
        receipt["normalized"] = artifact(root, "normalized/" + task["id"] + ".jsonl.gz", payload)
        state = "complete" if rows else "empty_unknown"
        if str(coverage.get("source_status", "")).startswith("partial_"):
            state = "partial_source"
        receipt.update(state=state, row_count=len(rows), coverage=coverage)
        receipt.pop("parse_error", None)
    except Exception as error:
        receipt.update(state="parse_failed", parse_error=f"{type(error).__name__}: {error}"[:500])
    receipt["updated_at"] = now()
    receipt["normalizer_sha256"] = NORMALIZER_SHA256
    receipt["corporate_parser_sha256"] = CORPORATE_PARSER_SHA256
    return receipt


def replay(root, all_captured=False):
    """Rebuild local normalized outputs from their fixed originals, never network."""
    root, manifest, tasks = load_batch(root)
    with (root / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for task in tasks:
            path = root / "receipts" / (task["id"] + ".json")
            if not path.exists():
                continue
            receipt = json.loads(path.read_text())
            if receipt.get("raw") and (all_captured or receipt["state"] in {"parse_failed", "captured"}):
                normalize_saved(root, manifest, task, receipt)
                atomic(path, encoded(receipt))
        return audit(root, verify=True, write=True, _writer_owned=True)


def load_batch(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema") != SCHEMA:
        raise ValueError("Unknown batch schema")
    for name, key in (("universe.json", "universe_sha256"), ("tasks.json", "tasks_sha256")):
        if digest((root / name).read_bytes()) != manifest[key]:
            raise ValueError("Changed batch identity")
    return root, manifest, json.loads((root / "tasks.json").read_text())


def run(root, max_jobs=0, interval=2.0):
    root, manifest, tasks = load_batch(root)
    if interval < 2:
        raise ValueError("Minimum request interval is two seconds")
    stop = {"requested": False}
    def stop_signal(signum, frame):
        stop["requested"] = True
    attempted = 0
    with (root / "writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another expansion worker is active") from None
        previous = {s: signal.signal(s, stop_signal) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            # Only this collector's abandoned atomic temporaries are disposable.
            for sub in ("receipts", "raw", "normalized"):
                folder = root / sub
                folder.mkdir(exist_ok=True)
                for path in folder.glob("*.tmp"):
                    path.unlink()
            for path in root.glob("*.tmp"):
                path.unlink()
            state_path = root / "source-state.json"
            hosts = json.loads(state_path.read_text()) if state_path.exists() else {}
            last_request = 0.0
            for task in tasks:
                if stop["requested"] or (root / "STOP").exists() or (max_jobs and attempted >= max_jobs):
                    break
                host = urlparse(task["url"]).hostname
                host_state = hosts.setdefault(host, {})
                if host_state.get("blocked") or host_state.get("cooldown_until", 0) > time.time():
                    continue
                path = root / "receipts" / (task["id"] + ".json")
                receipt = json.loads(path.read_text()) if path.exists() else {"task_id": task["id"], "attempts": []}
                if receipt.get("state") in TERMINAL:
                    verify_receipt(root, receipt)
                    continue
                if receipt.get("retry_after", 0) > time.time():
                    continue
                if len(receipt["attempts"]) >= 3 and not receipt.get("raw"):
                    receipt["state"] = "failed_exhausted"
                    atomic(path, encoded(receipt))
                    continue
                # Success body from an interrupted parse/write is reused, never downloaded twice.
                raw = None
                if receipt.get("raw"):
                    verify_receipt(root, receipt)
                    raw = gzip.decompress((root / receipt["raw"]["path"]).read_bytes())
                if raw is None:
                    delay = max(interval, host_state.get("interval", interval)) - (time.monotonic() - last_request)
                    until = time.monotonic() + max(0, delay)
                    while time.monotonic() < until and not stop["requested"]:
                        time.sleep(min(1, until - time.monotonic()))
                    if stop["requested"]:
                        break
                    attempt = {"started_at": now(), "url": task["url"], "authenticated": False}
                    receipt["attempts"].append(attempt)
                    receipt["state"] = "running"
                    atomic(path, encoded(receipt))
                    attempted += 1
                    try:
                        status, raw, headers = fetch(task["url"])
                        attempt.update(http_status=status, received_at=now(), response_bytes=len(raw),
                                       response_sha256=digest(raw))
                        if status != 200:
                            raise RuntimeError(f"HTTP {status}")
                        receipt["raw"] = artifact(root, "raw/" + task["id"] + ".json.gz", gzip.compress(raw, mtime=0))
                        receipt["state"] = "captured"
                        receipt["observed_at"] = attempt["received_at"]
                        atomic(path, encoded(receipt))
                        host_state["consecutive_errors"] = 0
                    except Exception as error:
                        status = attempt.get("http_status")
                        attempt["error"] = f"{type(error).__name__}: {error}"[:500]
                        receipt["state"] = "failed_retryable"
                        failures = host_state.get("consecutive_errors", 0) + 1
                        host_state["consecutive_errors"] = failures
                        if status in (401, 402, 403):
                            host_state["blocked"] = f"HTTP {status}: access/payment approval required"
                        elif status == 429:
                            host_state["interval"] = max(10, host_state.get("interval", interval) * 2)
                            retry = headers.get("Retry-After", "3600")
                            cooldown = max(3600, int(retry)) if str(retry).isdigit() else 3600
                            host_state["cooldown_until"] = time.time() + cooldown
                        elif failures >= 3:
                            host_state["cooldown_until"] = time.time() + 1800
                        receipt["retry_after"] = max(time.time() + 60 * 2 ** len(receipt["attempts"]), host_state.get("cooldown_until", 0))
                        atomic(path, encoded(receipt))
                        atomic(state_path, encoded(hosts))
                        print(json.dumps({"task": task["id"], "state": receipt["state"], "error": attempt["error"]}), flush=True)
                        continue
                    finally:
                        last_request = time.monotonic()
                normalize_saved(root, manifest, task, receipt, raw)
                atomic(path, encoded(receipt))
                atomic(state_path, encoded(hosts))
                atomic(root / "progress.json", encoded({"updated_at": now(), "pid": os.getpid(),
                       "last_task": task["id"], "last_state": receipt["state"], "requests_this_run": attempted}))
                print(json.dumps({"task": task["id"], "state": receipt["state"], "rows": receipt.get("row_count")}), flush=True)
                if attempted and attempted % 50 == 0:
                    audit(root, verify=False, write=True, _writer_owned=True)
            return audit(root, verify=False, write=True, _writer_owned=True)
        finally:
            atomic(root / "worker-exit.json", encoded({"at": now(), "pid": os.getpid(), "requests": attempted,
                                                       "stop_requested": stop["requested"]}))
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def audit(root, verify=False, write=False, _writer_owned=False):
    root, manifest, tasks = load_batch(root)
    if write and not _writer_owned:
        with (root / "writer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return audit(root, verify, write, _writer_owned=True)
    states, families, errors = Counter(), defaultdict(lambda: Counter()), []
    rows_out = []
    for task in tasks:
        path = root / "receipts" / (task["id"] + ".json")
        receipt = json.loads(path.read_text()) if path.exists() else {"state": "pending"}
        state = receipt["state"]
        if verify:
            try:
                verify_receipt(root, receipt)
            except (ValueError, OSError) as error:
                errors.append({"task": task["id"], "error": str(error)})
        states[state] += 1
        families[task["kind"]][state] += 1
        families[task["kind"]]["observations"] += receipt.get("row_count", 0)
        rows_out.append({"task_id": task["id"], "security": security(task["symbol"]) if task["symbol"] else None, "kind": task["kind"],
                         "state": state, "row_count": receipt.get("row_count", 0),
                         "coverage": receipt.get("coverage"), "parse_error": receipt.get("parse_error")})
    result = {"schema": "ad.expansion-coverage.v1", "generated_at": now(),
              "baseline_codes": manifest["baseline_codes"], "target_codes": manifest["target_codes"],
              "planned_tasks": len(tasks), "task_states": dict(states),
              "families": dict(families), "checksums_verified": verify, "integrity_errors": errors,
              "pending_or_retryable": sum(states[s] for s in ("pending", "running", "captured", "failed_retryable")),
              "all_tasks_have_source_results": all(r["state"] in {"complete", "partial_source", "empty_unknown"} for r in rows_out),
              "full_history_complete": False, "historical_pit_certified": False,
              "unified_database_imported": False}
    if write:
        atomic(root / "coverage.json", encoded(result))
        atomic(root / "coverage-by-task.jsonl.gz", gzip.compress(b"".join(encoded(r) for r in rows_out), mtime=0))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--root", required=True, type=Path)
    prep.add_argument("--daily", required=True, type=Path)
    prep.add_argument("--start", default="2010-01-01")
    prep.add_argument("--end", required=True)
    prep.add_argument("--kinds", nargs="+", choices=ALLOWED_KINDS)
    cal = sub.add_parser("prepare-calendar")
    cal.add_argument("--root", required=True, type=Path)
    cal.add_argument("--start", default="2010-01-01")
    cal.add_argument("--end", required=True)
    cal.add_argument("--reuse-root", type=Path)
    worker = sub.add_parser("run")
    worker.add_argument("--root", required=True, type=Path)
    worker.add_argument("--max-jobs", type=int, default=0)
    worker.add_argument("--interval", type=float, default=2)
    check = sub.add_parser("audit")
    check.add_argument("--root", required=True, type=Path)
    check.add_argument("--verify", action="store_true")
    check.add_argument("--write", action="store_true")
    reparse = sub.add_parser("replay")
    reparse.add_argument("--root", required=True, type=Path)
    reparse.add_argument("--all-captured", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.root, args.daily, args.start, args.end, args.kinds)
    elif args.command == "prepare-calendar":
        result = prepare_calendar(args.root, args.start, args.end, args.reuse_root)
    elif args.command == "run":
        if args.max_jobs < 0:
            parser.error("max-jobs must be nonnegative")
        result = run(args.root, args.max_jobs, args.interval)
    elif args.command == "replay":
        result = replay(args.root, args.all_captured)
    else:
        result = audit(args.root, args.verify, args.write)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("integrity_errors"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
