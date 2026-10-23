"""Finite, recoverable all-market update orchestration; never changes deployment."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import re
import signal
import time
from urllib.parse import urlencode, parse_qs, urlsplit
from zoneinfo import ZoneInfo

from ashare_data.market.increment import exact_number, normalize_values, sohu_capture
from ashare_data.providers.baostock_daily import DailySession
from ashare_data.providers.baostock_receipt.capture import utc_now
from ashare_data.providers.cmes import native_security
from ashare_data.providers.daily_increment import (
    universe, seed_files, equity_security, fetch_minute, assess_minutes, IncrementHTTP,
)
from ashare_data.providers.daily_sources import SourceBlocked
from ashare_data.storage.daily_increment import IncrementStore
from ashare_data.storage.market_release import atomic_write, canonical


def emit(event, **value):
    print(json.dumps({"at": utc_now(), "event": event, **value}, ensure_ascii=False), flush=True)


def load_bao(capture):
    body = gzip.decompress(Path(capture["capture_path"]).read_bytes())
    if hashlib.sha256(body).hexdigest() != capture["capture_sha256"]:
        raise ValueError("retained BaoStock capture changed")
    return dict(json.loads(body), **capture)


def bao_batches(store, day, *, retry=False):
    root = store.root / "runs" / day
    path = root / "baostock-index.json"
    index = json.loads(path.read_bytes()) if path.exists() else {}
    failure_path = root / "baostock-failure.json"
    failure = json.loads(failure_path.read_bytes()) if failure_path.exists() else None
    requests = {
        "query_stock_basic": {"code": "", "code_name": ""},
        "query_all_stock": {"day": day},
        "query_daily_history_k_AStock": {"date": day},
        "query_daily_adjust_factor": {"date": day},
        "query_trade_dates": {"start_date": (date.fromisoformat(day) - timedelta(days=store.config["retry_window_days"])).isoformat(), "end_date": day},
    }
    pending = {m: p for m, p in requests.items() if m not in index or
               index[m]["status"] != "ok" and time.time() >= index[m].get("retry_after_epoch", 0)}
    if pending and (not failure or time.time() >= failure["retry_after_epoch"]):
        try:
            with DailySession(store.config["sdk_path"], root / "baostock",
                              timeout=store.config["timeout"],
                              quota_path=store.root / "baostock-quota.sqlite3") as session:
                for method, params in pending.items():
                    value = session.query(method, params)
                    index[method] = {k: value[k] for k in ("capture_path", "capture_sha256", "status", "error", "fields")}
                    index[method]["retry_after_epoch"] = time.time() + 900 if value["status"] != "ok" else None
                    atomic_write(path, canonical(index))
                    emit("baostock_batch", method=method, state=value["status"], rows=len(value["rows"]), error=value["error"])
        except Exception as error:
            failure = dict(error=str(error), observed_at=utc_now(), retry_after_epoch=time.time() + 900)
            atomic_write(failure_path, canonical(failure))
            emit("baostock_unavailable", **failure)
    return {m: load_bao(v) for m, v in index.items()}


def prepare(store, day, http, *, retry_sources=False):
    path = store.root / "runs" / day / "universe.json"
    if path.exists():
        members = json.loads(path.read_bytes())
    else:
        members = universe(http)
        atomic_write(path, canonical(members))
        emit("universe", securities=len(members["rows"]),
             venues=dict(Counter("BJ" if r["security"].startswith("BJ.") else r["security"][-4:] for r in members["rows"])))
    inventory = seed_files(store.config["seed_directory"]) if store.config.get("seed_directory") else []
    store.seed(inventory)
    batches = bao_batches(store, day, retry=retry_sources)
    from ashare_data.market.increment_schedule import calendar_from_batches, load_base_calendar, historical_members
    calendar_from_batches(store, batches)
    load_base_calendar(store)
    basics = batches.get("query_stock_basic", {}).get("rows", [])
    dated = batches.get("query_all_stock", {})
    qualified = {r["code"] for r in basics if r.get("type") == "1"}
    selected = {r["security"]: r for r in members["rows"]}
    supplement = []
    for number, row in enumerate(dated.get("rows", [])):
        security = equity_security(row.get("code"))
        if security and security not in selected and row["code"] in qualified:
            value = dict(security=security, name=row.get("code_name"), source="baostock_dated_membership",
                         source_asset_id=dated["capture_sha256"], source_row=number,
                         observed_at=dated["observed_at"], membership_date=day)
            selected[security] = value
            supplement.append(value)
    if supplement:
        atomic_write(path.with_name("universe-supplement.json"), canonical(dict(day=day, rows=supplement)))
    if day < datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
        historical_members(store, day, list(selected.values()), batches)
    else:
        store.plan(day, list(selected.values()), basics)
    with store.db:
        for receipt in members["receipts"]:
            store.capture(dict(receipt, path=str(http.root / "raw" / (receipt["sha256"] + ".gz"))))
        for capture in batches.values():
            store.capture({k: v for k, v in capture.items() if k not in {"rows", "frames"}})
    apply_bao(store, day, batches)
    return batches


def apply_bao(store, day, batches):
    daily = batches.get("query_daily_history_k_AStock", {})
    by_security = {}
    accounting = {}
    daily_counts = Counter(input_rows=len(daily.get("rows", [])), adopted=0, equal_duplicate=0,
                           resolved_unused=len(daily.get("rows", [])), quarantined=0, format_rejected=0)
    for number, row in enumerate(daily.get("rows", [])):
        security = equity_security(row.get("code"))
        if security:
            by_security.setdefault(security, []).append((number, row))
    with store.db:
        for job in store.pending(day, "daily"):
            security = job["security"]
            native = by_security.get(security, [])
            if len(native) != 1:
                continue  # Explicit Sohu supplement or a visible missing task.
            number, row = native[0]
            daily_counts["resolved_unused"] -= 1
            try:
                if row.get("date") != day or row.get("adjustflag") != "3":
                    raise ValueError("BaoStock daily date/basis mismatch")
                values = normalize_values(row)
                values.update(preclose=exact_number(row["preclose"]) if row.get("preclose") else None,
                              tradestatus=row.get("tradestatus"), is_st=row.get("isST"))
                result = store.observe("baostock", "1d", security, day, values,
                                       daily["capture_sha256"], number, daily["observed_at"])
                state = "quality_gap" if result["conflict"] else "complete"
                store.cover(day, security, "daily", dict(state=state, observed_count=1,
                            source="baostock", unit_contract="raw CNY prices; shares; CNY amount",
                            tradestatus=values["tradestatus"], source_asset_id=daily["capture_sha256"],
                            input_rows=1, adopted=1, **result))
                store.finish(day, security, "daily")
                daily_counts["adopted" if result["added"] else "equal_duplicate"] += 1
            except (ValueError, KeyError) as error:
                daily_counts["quarantined"] += 1
                store.rejected(day, security, "daily", daily["capture_sha256"],
                               dict(source_row=number, native=row, reason=str(error)))
                store.cover(day, security, "daily", dict(state="quality_gap", observed_count=0, reason=str(error)))
                store.finish(day, security, "daily", error=str(error))
        factors = batches.get("query_daily_adjust_factor", {})
        factor_counts = Counter(input_rows=len(factors.get("rows", [])), adopted=0, equal_duplicate=0,
                                resolved_unused=len(factors.get("rows", [])), quarantined=0, format_rejected=0)
        grouped = {}
        for number, row in enumerate(factors.get("rows", [])):
            security = equity_security(row.get("code"))
            if security:
                grouped.setdefault(security, []).append((number, row))
        dated = {equity_security(r.get("code")) for r in batches.get("query_all_stock", {}).get("rows", [])}
        date_scoped = (factors.get("status") == "ok" and
                       set(factors.get("fields", [])) == {"code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"} and
                       all(r.get("dividOperateDate") == day for r in factors.get("rows", [])))
        for job in store.pending(day, "factor"):
            security = job["security"]
            if factors.get("status") != "ok":
                store.cover(day, security, "factor", dict(state="source_unavailable", observed_count=0))
                store.finish(day, security, "factor", error=factors.get("error") or "BaoStock factor batch unavailable")
                continue
            rows = grouped.get(security, [])
            for number, row in rows:
                record = dict(native=row, source="baostock", source_asset_id=factors["capture_sha256"],
                              source_row=number, observed_at=factors["observed_at"],
                              basis="provider native adjustment factors; no automatic CMES rebasing",
                              historical_available_at=None, compatible_with_cmes_unverified=True)
                digest = hashlib.sha256(canonical(row)).hexdigest()
                inserted = store.db.execute("INSERT OR IGNORE INTO factors VALUES(?,?,?,?,?)",
                                            (day, security, "baostock", digest, canonical(record).decode())).rowcount
                factor_counts["adopted" if inserted else "equal_duplicate"] += 1
                factor_counts["resolved_unused"] -= 1
            # Empty event batches are retained as observations, not fabricated unit factors.
            state = "complete" if rows else "no_factor_event" if date_scoped and security in dated else "source_unsupported" if security.startswith("BJ.") and security not in dated else "empty_unknown"
            store.cover(day, security, "factor", dict(state=state,
                                                      observed_count=len(rows), source="baostock",
                                                      source_asset_id=factors["capture_sha256"],
                                                      event_scope="date-scoped source factor feed only; not absence of corporate actions",
                                                      cmes_basis_bridge="unverified"))
            store.finish(day, security, "factor")
        for kind, batch, counts in (("daily", daily, daily_counts), ("factor", factors, factor_counts)):
            if batch.get("status") == "ok":
                accounting[kind] = dict(source_asset_id=batch["capture_sha256"], **counts,
                                        unused_reason="outside the frozen A-equity scope or already-completed security/day; no replacement")
        # Source-declared daily trading status is separate evidence, not a field filled into another price source.
        for security, native in by_security.items():
            if len(native) != 1:
                continue
            number, row = native[0]
            if row.get("date") != day or row.get("tradestatus") not in {"0", "1"}:
                continue
            current = store.db.execute("SELECT body FROM coverage WHERE day=? AND security=? AND kind='daily'", (day, security)).fetchone()
            if current:
                value = json.loads(current[0])
                value["trading_status_evidence"] = dict(source="baostock", source_asset_id=daily["capture_sha256"],
                                                       source_row=number, tradestatus=row["tradestatus"],
                                                       evidence_status="source_declared")
                store.cover(day, security, "daily", value)
                if row["tradestatus"] == "0" and not value.get("observed_count"):
                    store.finish(day, security, "daily")
    path = store.root / "runs" / day / "baostock-accounting.json"
    if accounting and not path.exists():
        atomic_write(path, canonical(accounting))


def collect_sohu(store, day, http, stopped):
    pending = store.pending(day, "daily")
    for start in range(0, len(pending), 20):
        if stopped():
            break
        jobs = pending[start:start + 20]
        securities = [j["security"] for j in jobs]
        codes = ["cn_" + native_security(s)[3:] for s in securities]
        url = "https://q.stock.sohu.com/hisHq?" + urlencode(dict(
            code=",".join(codes), start=day.replace("-", ""), end=day.replace("-", ""),
            stat="1", order="D", period="d", callback="historySearchHandler", rt="jsonp"))
        try:
            body, receipts = http.get(url, "https://q.stock.sohu.com/")
            match = re.fullmatch(r"\s*historySearchHandler\((.*)\);?\s*", body.decode("gb18030"), re.S)
            if not match:
                raise ValueError("invalid Sohu batch envelope")
            blocks = json.loads(match[1])
            blocks = blocks if isinstance(blocks, list) else [blocks]
            positions = {x.get("code"): n for n, x in enumerate(blocks) if x.get("code") in codes}
            if len(positions) != sum(x.get("code") in codes for x in blocks):
                raise ValueError("duplicate Sohu security blocks")
            receipt = receipts[-1]
            with store.db:
                asset = store.capture(dict(receipt, path=str(http.root / "raw" / (receipt["sha256"] + ".gz"))))
                for security, code in zip(securities, codes):
                    if code not in positions:
                        store.cover(day, security, "daily", dict(state="empty_unknown", observed_count=0,
                                                                 source="sohu", reason="omitted from batch", source_asset_id=asset))
                        store.finish(day, security, "daily")
                        continue
                    try:
                        symbol = native_security(security).replace(".", "").lower()
                        rows, native = sohu_capture(body, receipt, symbol, day, day, source_block=positions[code])
                        if len(rows) > 1 or any(r["date"] != day for r in rows):
                            raise ValueError("Sohu daily key mismatch")
                        counts = Counter()
                        for number, row in enumerate(rows):
                            values = normalize_values({k: str(row[k]) if row.get(k) is not None else None
                                                       for k in ("open", "high", "low", "close", "volume", "amount")})
                            counts.update(store.observe("sohu", "1d", security, day, values, asset,
                                                        positions[code] * 1000 + number, receipt["received_at"]))
                        state = "quality_gap" if counts["conflict"] else "complete" if rows else "empty_unknown"
                        store.cover(day, security, "daily", dict(state=state, observed_count=len(rows), source="sohu",
                                    unit_contract="source rounded lots*100; source 10k CNY*10000; no inferred status",
                                    source_asset_id=asset, source_block=positions[code], input_rows=len(native), **counts))
                        store.finish(day, security, "daily")
                    except (ValueError, RuntimeError, KeyError) as error:
                        store.cover(day, security, "daily", dict(state="quality_gap", observed_count=0, reason=str(error)))
                        store.finish(day, security, "daily", error=str(error))
        except Exception as error:
            with store.db:
                for security in securities:
                    store.cover(day, security, "daily", dict(state="source_unavailable", observed_count=0, reason=str(error), receipts=getattr(error, "receipts", [])))
                    store.finish(day, security, "daily", error=str(error))
            if isinstance(error, SourceBlocked) or "source cooling down" in str(error):
                break
        if start % 200 == 0:
            emit("daily_progress", processed=min(start + 20, len(pending)), scheduled=len(pending))


def collect_minutes(store, day, http, stopped, *, maximum=None):
    tasks = store.pending(day, "minute")
    if maximum is not None:
        tasks = tasks[:maximum]
    iterator = iter(tasks)
    completed = 0
    window = store.config["minute_window"]
    statuses = {}
    for security, body in store.db.execute("SELECT security,body FROM coverage WHERE day=? AND kind='daily'", (day,)):
        value = json.loads(body)
        statuses[security] = value.get("tradestatus") or value.get("trading_status_evidence", {}).get("tradestatus")
    retained = {}
    for job in tasks:
        if job.get("error") != "source_status_recheck":
            continue
        previous = store.db.execute("SELECT body FROM coverage WHERE day=? AND security=? AND kind='minute'",
                                    (day, job["security"])).fetchone()
        asset = json.loads(previous[0]).get("source_asset_id") if previous else None
        symbol = native_security(job["security"]).replace(".", "").lower()
        for (body,) in store.db.execute("SELECT body FROM capture_requests WHERE asset_id=?", (asset,)):
            receipt = json.loads(body)
            if parse_qs(urlsplit(receipt.get("url", "")).query).get("symbol") == [symbol] and receipt.get("path"):
                retained[job["security"]] = receipt
                break

    def fetch(job):
        if job.get("error") == "source_status_recheck":
            receipt = retained.get(job["security"])
            if receipt is None:
                raise ValueError("retained minute request missing for status-only recheck")
            body = gzip.decompress(Path(receipt["path"]).read_bytes())
            if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
                raise ValueError("retained minute capture changed")
        else:
            http.history.refresh = job.get("error") == "refresh_not_ready"
            try:
                body, receipt = fetch_minute(http, job["security"], window)
            finally:
                http.history.refresh = False
        return body, receipt, assess_minutes(body, day, window, trade_status=statuses.get(job["security"]))

    with ThreadPoolExecutor(max_workers=2) as pool:
        active = {}
        while active or not stopped():
            while len(active) < 2 and not stopped():
                job = next(iterator, None)
                if job is None:
                    break
                store.start(day, job["security"], "minute")
                active[pool.submit(fetch, job)] = job
            if not active:
                break
            done, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
            for future in done:
                job = active.pop(future)
                security = job["security"]
                try:
                    body, receipt, (rows, coverage) = future.result()
                    with store.db:
                        asset = store.capture(dict(receipt, path=receipt.get("path") or str(http.root / "raw" / (receipt["sha256"] + ".gz"))))
                        changes = Counter()
                        for row in rows:
                            if row["disposition"] == "adopted":
                                changes.update(store.observe("sina", "1m", security, day, row, asset,
                                                             row["source_row"], receipt["received_at"]))
                            elif row["disposition"] in {"quarantined", "format_rejected"}:
                                store.rejected(day, security, "minute", asset, row)
                        if changes["conflict"]:
                            coverage["state"] = "quality_gap"
                        coverage.update(source_asset_id=asset, normalization_changes=dict(changes))
                        store.cover(day, security, "minute", coverage)
                        store.finish(day, security, "minute")
                except Exception as error:
                    with store.db:
                        store.cover(day, security, "minute", dict(state="source_unavailable", observed_count=0, reason=str(error)))
                        store.finish(day, security, "minute", error=str(error))
                    if isinstance(error, SourceBlocked) or "source cooling down" in str(error):
                        stopped.blocked = True
                completed += 1
                if completed % 25 == 0 or not active:
                    emit("minute_progress", completed=completed, scheduled=len(tasks), security=security)
                if completed % 100 == 0:
                    store.report(day)
                    store.budget()
    return completed


def run(root, day, *, phase="all", max_minute_jobs=None, retry_sources=False, capture_day=None):
    store = IncrementStore(root)
    directory = store.root / "runs" / day
    directory.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if date.fromisoformat(day) > today:
        raise ValueError("future trading date")
    state = {"stop": False}
    def stopped():
        return state["stop"] or stopped.blocked or (store.root / "STOP").exists()
    stopped.blocked = False
    def halt(*args):
        state["stop"] = True
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in handlers:
        signal.signal(sig, halt)
    started = time.monotonic()
    try:
        with store.writer():
            store.register_request_cache(capture_day or day)
            atomic_write(directory / "process.json", canonical(dict(pid=__import__('os').getpid(), started_at=utc_now(), day=day, phase=phase)))
            store.budget()
            http = IncrementHTTP(store.root / "runs" / (capture_day or day) / "http", store.root,
                                 interval=store.config["interval"], timeout=20)
            from ashare_data.market.increment_schedule import retry_not_ready
            if phase in {"all", "prepare"}:
                prepare(store, day, http, retry_sources=retry_sources)
                if not stopped() and "sohu" in store.config["daily_sources"]:
                    collect_sohu(store, day, http, stopped)
            retry_not_ready(store, day)
            if phase in {"all", "minute"} and not stopped():
                if not store.db.execute("SELECT 1 FROM scopes WHERE day=?", (day,)).fetchone():
                    raise ValueError("prepare the all-market scope first")
                collect_minutes(store, day, http, stopped, maximum=max_minute_jobs)
            with store.db:
                store.event("run_finished", dict(day=day, phase=phase, elapsed_seconds=time.monotonic() - started, stopped=stopped()))
            result = store.report(day)
            result.update(elapsed_seconds=time.monotonic() - started, stopped=stopped())
            emit("run_finished", **result)
            return result
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        store.close()
