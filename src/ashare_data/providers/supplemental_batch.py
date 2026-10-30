"""Resumable current collection and projection for the separate 19-item batch.

Current member lists precede the deduplicated stock queue. Successfully received
scopes are reused on the same local day; a new day always opens a new checkpoint.
This uses the existing observation writer and immutable snapshots, never CURRENT.
"""

from datetime import datetime, timezone, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from ashare_data.providers.free_update_coverage import require
from ashare_data.providers.free_update_manual import read_snapshot, write_snapshot
from ashare_data.providers.free_update_worker import Capture
from ashare_data.providers.free_updates import digest, encoded, implementation_identity
from ashare_data.providers.supplemental_sources import CSI_CODES, collect, semantics, stock
from ashare_data.providers.supplemental_updates import FUNCTIONS, inputs, refresh
from ashare_data.storage.free_updates import writer

TZ = ZoneInfo("Asia/Shanghai")


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".next")
    pending.write_bytes(encoded(value))
    pending.replace(path)


def source_index(seed, roots):
    index = json.loads(Path(seed).read_text())
    paths = {e["snapshot"] for e in index["manual_snapshot_queries"] if e["function"] in FUNCTIONS}
    for root in roots:
        folder = Path(root) / "manual-snapshots"
        for pointer in folder.glob("latest-*.json"):
            identity = json.loads(pointer.read_text())["snapshot_id"]
            require(len(identity) == 64 and all(c in "0123456789abcdef" for c in identity),
                    "invalid upstream snapshot pointer")
            path = folder / identity
            manifest = json.loads((path / "manifest.json").read_text())
            if manifest["function"] in FUNCTIONS:
                paths.add(str(path.resolve()))
    entries = []
    for path in sorted(paths):
        m, _ = read_snapshot(path)
        entries.append(dict(function=m["function"], snapshot=path, excel_row=m["excel_row"]))
    return dict(manual_snapshot_queries=entries)


def cooling_hosts(roots):
    hosts = set()
    for root in {str(Path(p).resolve()) for p in roots}:
        path = Path(root) / "observations.sqlite"
        if path.exists():
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                hosts.update(r[0] for r in db.execute("SELECT host FROM cooldowns WHERE until_epoch>?", (time.time(),)))
        state = Path(root) / "source-state.json"
        if state.exists():
            hosts.update(h for h, s in json.loads(state.read_text()).items()
                         if s.get("blocked") or s.get("cooldown_until", 0) > time.time())
    if any(h == "sina" or h.endswith(("sina.com.cn", "sina.cn", "sinajs.cn")) for h in hosts):
        hosts.update({"hq.sinajs.cn", "finance.sina.com.cn", "quotes.sina.cn"})
    if any(h in {"tdx", "tdx-f10", "eltdx"} or h.endswith("tdx.com.cn") for h in hosts):
        hosts.add("static.tdx.com.cn")
    return hosts


def persist_source(db, root, function, params, records, coverage, receipts, as_of):
    require(records and receipts, "empty or unproven source response cannot replace a snapshot")
    for receipt in receipts:
        require(receipt["status"] == 200, "successful source snapshot includes failed HTTP response")
        body = gzip.decompress((root / "raw" / (receipt["sha256"] + ".gz")).read_bytes())
        require(hashlib.sha256(body).hexdigest() == receipt["sha256"], "raw response hash differs")
    semantic = semantics(function, records, params, as_of)
    require(semantic.get("contract_reviewed") and not semantic.get("gaps"), "source semantics unresolved")
    stable_params = {k: v for k, v in params.items() if k not in {"start_date", "end_date"}}
    job = dict(function=function, parameters=stable_params, implementation_sha256=implementation_identity())
    job["job_id"] = digest(job)
    run_id = digest([job, receipts])
    observed = max(r["received_at"] for r in receipts)
    check = dict(function=function, job_id=job["job_id"], run_id=run_id, observed_at=observed,
                 execution_kind="live_public_source", raw_protocol_reparsed=True,
                 records=len(records), receipts=receipts,
                 source_urls=sorted({r["url"] for r in receipts}), semantics=semantic,
                 coverage=dict(coverage, complete_history=False), source_parameters=params)
    row = 214 if function == "tdx_f10_hot_topics" else 51 if function == "sina_raw_daily" else 112
    result = write_snapshot(root, row, job, records, check)
    summary = dict(status="received", receipts=receipts, coverage=coverage, snapshot=result["snapshot"])
    with db:
        db.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (run_id, job["job_id"], as_of, min(r["received_at"] for r in receipts), observed,
                    "received", result["new_records"], len(records), encoded(job).decode(), encoded(summary).decode()))
    return result


def run(profile, *, execute=False, as_of=None, max_jobs=None):
    config = json.loads(Path(profile).read_text())
    require(config.get("schema") == "ad.supplemental-19-batch.v1", "unsupported second-batch profile")
    root = Path(config["root"]).resolve()
    require(not any((root / n).exists() for n in ("CURRENT", "CURRENT.json", "releases")),
            "production root forbidden")
    require(set(config["csi_indices"]) <= CSI_CODES, "index outside second-batch scope")
    require(len(set(config["topic_securities"])) == len(config["topic_securities"]), "duplicate topic issuer")
    for symbol in config["topic_securities"]:
        stock(symbol)
    for symbol in config.get("daily_securities", []):
        stock(symbol)
    day = as_of or datetime.now(TZ).date().isoformat()
    require(datetime.strptime(day, "%Y-%m-%d").date() <= datetime.now(TZ).date(), "future collection day")
    cap = max_jobs if max_jobs is not None else config.get("max_jobs", 40)
    require(1 <= cap <= 120, "invalid per-invocation job bound")
    require(1 <= config.get("attempts_per_day", 2) <= 2, "invalid same-day retry count")
    if not execute:
        return dict(dry_run=True, schema=config["schema"], root=str(root),
                    indices=config["csi_indices"] + ["399006"], topic_securities=config["topic_securities"],
                    batch_size=100, max_jobs=cap, first_batch_unchanged=True)
    require(day == datetime.now(TZ).date().isoformat(), "current-source collection must use today's checkpoint")
    started = time.monotonic()
    attempted, work = 0, []
    with writer(root) as db:
        state_path = root / "days" / day / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else dict(day=day, jobs={})
        require(state["day"] == day, "checkpoint day mismatch")
        index = source_index(config["source_index"], config["source_roots"] + [str(root)])
        sources = inputs(index)
        blocked = cooling_hosts(config["source_roots"] + config.get("cooldown_roots", []) + [str(root)])
        capture = Capture(root / "raw", blocked_hosts=blocked, interval=config.get("interval_seconds", 1),
                          request_limit=max(2 * cap, 1))

        def current(function, params):
            choices = []
            for m, records in sources:
                if m["function"] != function:
                    continue
                match = (all(r.get("指数代码") == params["symbol"] for r in records)
                         if function in {"index_stock_cons_csindex", "cni_current_members"}
                         else all(r.get("security") == params["symbol"] for r in records))
                if match and datetime.fromisoformat(m["observed_at"]).astimezone(TZ).date().isoformat() == day:
                    choices.append((m, records))
            return max(choices, key=lambda v: v[0]["observed_at"]) if choices else None

        def task(function, params):
            nonlocal attempted
            identity = digest([function, params])
            prior = state["jobs"].get(identity, {})
            if prior.get("received"):
                return True
            if (root / "STOP").exists():
                return False
            if attempted >= cap or prior.get("attempts", 0) >= config.get("attempts_per_day", 2):
                return False
            attempted += 1
            entry = dict(function=function, parameters=params, attempts=prior.get("attempts", 0) + 1,
                         received=False, started_at=datetime.now(timezone.utc).isoformat())
            state["jobs"][identity] = entry
            save(state_path, state)
            offset = len(capture.receipts)
            try:
                records, coverage = collect(capture, function, params, day)
                saved = persist_source(db, root, function, params, records, coverage,
                                       capture.receipts[offset:], day)
                m, rs = read_snapshot(saved["snapshot"])
                sources.append((m, rs))
                index["manual_snapshot_queries"].append(dict(function=function, snapshot=saved["snapshot"]))
                entry.update(received=True, snapshot=saved["snapshot"], records=len(records), coverage=coverage)
            except Exception as error:
                entry.update(error=type(error).__name__ + ": " + str(error), receipts=capture.receipts[offset:])
                # Retain exact receipt metadata for daily accounting and
                # propagate actual host refusal/transport cooldowns to peers.
                with db:
                    for receipt in capture.receipts[offset:]:
                        status = receipt["status"]
                        if status == "transport_error" or isinstance(status, int) and status >= 400:
                            until = time.time() + (3600 if status in {401, 403, 429} else 300)
                            if receipt.get("retry_after"):
                                try:
                                    until = max(until, time.time() + float(receipt["retry_after"]))
                                except ValueError:
                                    from email.utils import parsedate_to_datetime
                                    until = max(until, parsedate_to_datetime(receipt["retry_after"]).timestamp())
                            db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?,?)",
                                       (receipt["host"], until, f"HTTP {status}"))
                    db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)", (
                        uuid4().hex, identity, day, entry["started_at"], datetime.now(timezone.utc).isoformat(),
                        "failed", 0, 0, encoded(dict(function=function, parameters=params)).decode(),
                        encoded(dict(receipts=capture.receipts[offset:], error=entry["error"])).decode()))
            work.append(entry)
            save(state_path, state)
            return entry["received"]

        members_ready = True
        for code in config["csi_indices"]:
            params = dict(symbol=code)
            if not current("index_stock_cons_csindex", params):
                members_ready = task("index_stock_cons_csindex", params) and members_ready
        if not current("cni_current_members", dict(symbol="399006")):
            members_ready = task("cni_current_members", dict(symbol="399006")) and members_ready
        topics_ready = True
        for symbol in config["topic_securities"]:
            if not current("tdx_f10_hot_topics", dict(symbol=symbol)):
                topics_ready = task("tdx_f10_hot_topics", dict(symbol=symbol)) and topics_ready
        daily_ready = True
        for symbol in config.get("daily_securities", []):
            if not current("sina_raw_daily", dict(symbol=symbol)):
                end = datetime.strptime(day, "%Y-%m-%d")
                params = dict(symbol=symbol, asset_type="stock", start_date=(end-timedelta(days=45)).strftime("%Y%m%d"),
                              end_date=end.strftime("%Y%m%d"))
                daily_ready = task("sina_raw_daily", params) and daily_ready

        latest_members = {}
        quotes_today = set()
        for m, records in sources:
            if m["function"] in {"index_stock_cons_csindex", "cni_current_members"}:
                code = records[0]["指数代码"]
                if code not in latest_members or m["observed_at"] > latest_members[code][0]:
                    latest_members[code] = m["observed_at"], records
            elif (m["function"] == "sina_current_quotes"
                  and datetime.fromisoformat(m["observed_at"]).astimezone(TZ).date().isoformat() == day):
                quotes_today.update(r["security"] for r in records)
        universe = set()
        for _, records in latest_members.values():
            for r in records:
                exchange = {"上海证券交易所": "sh", "深圳证券交易所": "sz"}.get(r["交易所"])
                require(exchange, "unmapped index member exchange")
                universe.add(stock(exchange + r["成分券代码"]))
        pending = sorted(universe - quotes_today)
        quotes_ready = members_ready
        if members_ready:
            # Successful partial batches are subtracted from the next queue;
            # only absent names are retried, never all 1,800+ securities.
            for offset in range(0, len(pending), 100):
                quotes_ready = task("sina_current_quotes", dict(symbols=pending[offset:offset+100])) and quotes_ready
        received_today = {r["security"] for m, rs in sources if m["function"] == "sina_current_quotes"
                          and datetime.fromisoformat(m["observed_at"]).astimezone(TZ).date().isoformat() == day
                          for r in rs}
        quotes_ready = quotes_ready and bool(universe) and universe <= received_today
        path = root / "source-index.json"
        save(path, index)
        save(state_path, state)
        network_requests = len(capture.receipts)
        response_bytes = sum(r["bytes"] for r in capture.receipts)
    derived = refresh(path, root / "derived")
    result = dict(schema="ad.supplemental-19-batch-result.v1", day=day, attempted=attempted,
                  all_requested_jobs_received=members_ready and topics_ready and quotes_ready and daily_ready,
                  deferred=(root / "STOP").exists(), work=work, derived=derived, unique_members=len(universe),
                  network_requests=network_requests, response_bytes=response_bytes,
                  elapsed_seconds=time.monotonic()-started, first_batch_unchanged=True,
                  source_index=str(path), state=str(state_path))
    result["stop_requested"] = (root / "STOP").exists()
    save(root / "latest.json", result)
    return result
