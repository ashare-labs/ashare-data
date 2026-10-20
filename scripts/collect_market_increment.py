"""Resume a release-bounded, recent-window collection without mutating old assets.

This collector only writes raw responses and a small journal. Admission and
publication are separate. No historic-market scan, source sync, or daemon runs.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import re
from pathlib import Path
import shutil
import sqlite3
import time
from urllib.parse import urlencode

from ashare_data.providers.daily_sources import PublicHTTP, sohu_daily, tencent_daily
from ashare_data.market.increment import sohu_capture
from ashare_data.storage.market_release import MarketRelease, atomic_write, canonical, sha_file


def needs_request(previous, retry_errors):
    if previous is None:
        return True
    denied = previous["status"] == "access_denied" or any(
        r.get("status") in (401, 403, 429)
        for r in previous.get("pause_evidence", {}).get("receipts", [])
    )
    return retry_errors and previous["status"] != "ok" and not denied


def sohu_groups(jobs, successful_keys, size):
    """Keep unresolved codes out of batches that the native endpoint can reject."""
    pending = []
    for job in jobs:
        if job[0] not in successful_keys:
            if pending:
                yield pending
                pending = []
            yield [job]
            continue
        pending.append(job)
        if len(pending) == size:
            yield pending
            pending = []
    if pending:
        yield pending


def batch_sohu(http, jobs, start, end, producer):
    """One native HTTP response, with a separate exact block locator per security."""
    codes = [
        "cn_"
        + (item["security"][3:] if item["security"].startswith("BJ.") else item["security"][:6])
        for _, _, item in jobs
    ]
    assert len(codes) == len(set(codes)) <= 20
    url = "https://q.stock.sohu.com/hisHq?" + urlencode(
        dict(
            code=",".join(codes),
            start=start.replace("-", ""),
            end=end.replace("-", ""),
            stat="1",
            order="D",
            period="d",
            callback="historySearchHandler",
            rt="jsonp",
        )
    )
    http.history.refresh = True
    http.history.receipts = []
    http.history.cache_scope = start + ":" + end
    began = datetime.now(timezone.utc).isoformat()
    try:
        body, receipts = http.get(url, "https://q.stock.sohu.com/")
    except Exception as error:
        return [
            dict(
                job_key=key,
                source=source,
                security=item["security"],
                instrument_id=item["instrument_id"],
                status="access_denied" if type(error).__name__ == "SourceBlocked" else "error",
                error=type(error).__name__ + ": " + str(error),
                receipts=getattr(error, "receipts", []),
                started_at=began,
                completed_at=datetime.now(timezone.utc).isoformat(),
                collector_sha256=producer,
            )
            for key, source, item in jobs
        ]
    match = re.fullmatch(r"\s*historySearchHandler\((.*)\);?\s*", body.decode("gb18030"), re.S)
    assert match, "unclassified batch envelope retained; do not publish"
    envelope = json.loads(match[1])
    blocks = envelope if isinstance(envelope, list) else [envelope]
    positions = {}
    for index, block in enumerate(blocks):
        assert isinstance(block, dict), "invalid batch member"
        code = block.get("code")
        assert code in codes or not block.get("hq"), (
            "unrequested source rows require explicit accounting"
        )
        if code in codes:
            assert code not in positions, "duplicate batch security"
            positions[code] = index
    values = []
    for (key, source, item), code in zip(jobs, codes, strict=True):
        value = dict(
            job_key=key,
            source=source,
            security=item["security"],
            instrument_id=item["instrument_id"],
            started_at=began,
            collector_sha256=producer,
        )
        index = positions.get(code)
        if index is None:
            # Other members' bars must not be counted again for an omitted security.
            value.update(
                status="error",
                error="requested security omitted from native Sohu batch response",
                receipts=[],
                batch_response_evidence=receipts,
            )
        else:
            value.update(receipts=receipts, response_block=index)
            symbol = (
                "bj" + item["security"][3:]
                if item["security"].startswith("BJ.")
                else ("sh" if item["security"].endswith("XSHG") else "sz") + item["security"][:6]
            )
            try:
                rows, _ = sohu_capture(body, receipts[-1], symbol, start, end, source_block=index)
                value.update(
                    status="ok",
                    rows=len(rows),
                    first=min((r["date"] for r in rows), default=None),
                    last=max((r["date"] for r in rows), default=None),
                    normalized_rows=rows,
                )
            except (ValueError, RuntimeError, KeyError) as error:
                value.update(status="error", error=type(error).__name__ + ": " + str(error))
        value["completed_at"] = datetime.now(timezone.utc).isoformat()
        values.append(value)
    return values


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--market", type=Path, required=True)
    p.add_argument("--release-id", type=int, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--active-since", required=True)
    p.add_argument("--limit", type=int)
    p.add_argument("--retry-errors", action="store_true")
    p.add_argument(
        "--refresh-sohu",
        action="store_true",
        help="Refresh only Sohu scopes not yet reaching the frozen cutoff, using native batches",
    )
    p.add_argument("--sohu-batch-size", type=int, default=20, choices=range(1, 21))
    a = p.parse_args()
    assert datetime.fromisoformat(a.start) <= datetime.fromisoformat(a.end)
    assert (datetime.fromisoformat(a.end) - datetime.fromisoformat(a.start)).days <= 7
    a.root.mkdir(parents=True, exist_ok=True)
    lock = (a.root / "collector.lock").open("a+b")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    r = MarketRelease(a.market, a.release_id)
    scope_path = a.root / "scope.json"
    if scope_path.exists():
        scope = json.loads(scope_path.read_bytes())
        assert scope["parent"]["manifest_sha256"] == r.sha256
        assert (scope["start"], scope["end"], scope["active_since"]) == (
            a.start,
            a.end,
            a.active_since,
        )
    else:
        active = {
            f["instrument_id"]
            for f in r.files
            if f["table"] == "bars_1d" and f["last_date"] >= a.active_since
        }
        with r.metadata() as db:
            identifiers = [
                dict(x)
                for x in db.execute("select * from release_identifiers")
                if x["instrument_id"] in active
                and (x["valid_from"] is None or x["valid_from"] <= a.end)
                and (x["valid_to"] is None or x["valid_to"] > a.end)
            ]
        assert len(identifiers) == len(active) == len({x["instrument_id"] for x in identifiers}), (
            "ambiguous active alias"
        )
        identifiers.sort(key=lambda x: (x["security"].startswith("BJ."), x["security"]))
        scope = dict(
            schema="ad-market-increment-scope.v1",
            parent=r.receipt(),
            start=a.start,
            end=a.end,
            active_since=a.active_since,
            instruments=identifiers,
            basis="Prior release observed daily scopes with last_date>=active_since; not a complete listed-market claim",
            minute_scope="Existing SH/SZ identifiers only; Sina bounded latest 1023 labels; no arbitrary historical paging",
            source_priority={"sohu": 3, "tencent": 4},
            budget_bytes=8 * 1024**3,
            free_reserve_bytes=150 * 1024**3,
            collector_sha256=sha_file(Path(__file__)),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        atomic_write(scope_path, canonical(scope))
    producer = sha_file(Path(__file__))
    producer_path = a.root / "evidence" / ("collector-" + producer + ".py")
    if not producer_path.exists():
        atomic_write(producer_path, Path(__file__).read_bytes())
    db = sqlite3.connect(a.root / "collection.sqlite3")
    db.executescript(
        "CREATE TABLE IF NOT EXISTS jobs(job_key TEXT PRIMARY KEY,source TEXT,security TEXT,instrument_id INTEGER,body TEXT NOT NULL);"
    )
    # Recover completed per-job receipts before scheduling any network request.
    # A terminated run need not have reached its final journal merge.
    recovered = {}
    successful_sohu_keys = set()
    for saved in sorted((a.root / "jobs").glob("*.json")):
        value = json.loads(saved.read_bytes())
        if value["source"] == "sohu" and value["status"] == "ok" and value.get("rows", 0) > 0:
            successful_sohu_keys.add(value["job_key"])
        previous = recovered.get(value["job_key"])
        if previous is None or value["completed_at"] > previous["completed_at"]:
            recovered[value["job_key"]] = value
    for value in recovered.values():
        db.execute(
            "insert or replace into jobs values(?,?,?,?,?)",
            (
                value["job_key"],
                value["source"],
                value["security"],
                value["instrument_id"],
                canonical(value).decode(),
            ),
        )
    db.commit()
    existing = {key: json.loads(body) for key, body in db.execute("select job_key,body from jobs")}
    http = PublicHTTP(a.root / "incoming", interval=1.0, timeout=15)
    started = time.monotonic()
    records = scope["instruments"][: a.limit] if a.limit else scope["instruments"]
    jobs = []
    for item in records:
        sources = (
            ["sohu"]
            if a.refresh_sohu or item["security"].startswith("BJ.")
            else ["sohu", "tencent", "sina_minute"]
        )
        for source in sources:
            key = source + ":" + item["security"]
            previous = existing.get(key)
            if (
                a.refresh_sohu
                and previous
                and previous.get("refresh_target_date") == a.end
                and not a.retry_errors
            ):
                continue
            stale_sohu = (
                a.refresh_sohu
                and previous
                and previous["status"] == "ok"
                and (previous.get("last") or "") < a.end
            )
            if not stale_sohu and not needs_request(previous, a.retry_errors or a.refresh_sohu):
                continue
            jobs.append((key, source, item))

    if not jobs and (a.root / "collection-report.json").exists():
        print("UNCHANGED: all requested scopes already have retained outcomes", flush=True)
        db.close()
        return

    # Separate sequential host streams maintain a one-second host pace without
    # thousands of in-flight requests. All persisted successes are skipped.
    def collect(job):
        key, source, item = job
        sid = item["security"]
        symbol = (
            "bj" + sid[3:]
            if sid.startswith("BJ.")
            else ("sh" if sid.endswith("XSHG") else "sz") + sid[:6]
        )
        value = dict(
            job_key=key,
            source=source,
            security=sid,
            instrument_id=item["instrument_id"],
            started_at=datetime.now(timezone.utc).isoformat(),
            collector_sha256=producer,
        )
        try:
            http.history.receipts = []
            http.history.refresh = bool(
                a.retry_errors and key in existing and existing[key]["status"] != "ok"
            )
            http.history.cache_scope = a.start + ":" + a.end
            if source == "sina_minute":
                url = (
                    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?"
                    + urlencode(dict(symbol=symbol, scale=1, ma="no", datalen=1023))
                )
                raw, receipts = http.get(url, "https://finance.sina.com.cn/")
                rows = json.loads(raw)
                assert rows is None or isinstance(rows, list), "unexpected minute envelope"
                rows = rows or []
                assert len(rows) <= 1023
                labels = [x["day"] for x in rows]
                value.update(
                    rows=len(rows),
                    first=min(labels) if labels else None,
                    last=max(labels) if labels else None,
                )
            else:
                fn = sohu_daily if source == "sohu" else tencent_daily
                rows, receipts = fn(http, symbol, a.start, a.end)
                value.update(
                    rows=len(rows),
                    first=min((x["date"] for x in rows), default=None),
                    last=max((x["date"] for x in rows), default=None),
                    normalized_rows=rows,
                )
            value.update(status="ok", receipts=receipts)
        except Exception as e:
            value.update(
                status="access_denied" if type(e).__name__ == "SourceBlocked" else "error",
                error=type(e).__name__ + ": " + str(e),
                receipts=getattr(e, "receipts", getattr(http.history, "receipts", [])),
            )
        value["completed_at"] = datetime.now(timezone.utc).isoformat()
        return value

    def stream(source):
        if a.refresh_sohu and source == "sohu":
            source_jobs = [job for job in jobs if job[1] == source]
            paused = None
            for group in sohu_groups(source_jobs, successful_sohu_keys, a.sohu_batch_size):
                if paused is not None:
                    values = [
                        dict(
                            job_key=key,
                            source=source,
                            security=item["security"],
                            instrument_id=item["instrument_id"],
                            status="deferred",
                            error="batch source paused; no request sent",
                            receipts=[],
                            pause_evidence=paused,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                            collector_sha256=producer,
                        )
                        for key, _, item in group
                    ]
                else:
                    values = batch_sohu(http, group, a.start, a.end, producer)
                for value in values:
                    value["refresh_target_date"] = a.end
                    statuses = [r.get("status") for r in value.get("receipts", [])]
                    if value["status"] == "access_denied" or (
                        value["status"] == "error"
                        and statuses
                        and all(isinstance(v, int) and v >= 500 for v in statuses)
                    ):
                        paused = {
                            "job_key": value["job_key"],
                            "error": value.get("error"),
                            "receipts": value.get("receipts", []),
                            "at": value["completed_at"],
                        }
                    path = (
                        a.root
                        / "jobs"
                        / (
                            hashlib.sha256(value["job_key"].encode()).hexdigest()
                            + "-"
                            + hashlib.sha256(canonical(value)).hexdigest()
                            + ".json"
                        )
                    )
                    atomic_write(path, canonical(value))
                    yield value
            return
        paused = None
        for job in jobs:
            if job[1] != source:
                continue
            if paused is None:
                value = collect(job)
                statuses = [r.get("status") for r in value.get("receipts", [])]
                server_unavailable = (
                    value["status"] == "error"
                    and statuses
                    and all(isinstance(s, int) and s >= 500 for s in statuses)
                )
                if value["status"] == "access_denied" or server_unavailable:
                    paused = {
                        "job_key": value["job_key"],
                        "error": value.get("error"),
                        "receipts": value.get("receipts", []),
                        "at": value["completed_at"],
                    }
            else:
                key, _, item = job
                value = dict(
                    job_key=key,
                    source=source,
                    security=item["security"],
                    instrument_id=item["instrument_id"],
                    status="deferred",
                    error="source paused after bounded failed requests; no request sent",
                    receipts=[],
                    pause_evidence=paused,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    collector_sha256=producer,
                )
            path = (
                a.root
                / "jobs"
                / (
                    hashlib.sha256(job[0].encode()).hexdigest()
                    + "-"
                    + hashlib.sha256(canonical(value)).hexdigest()
                    + ".json"
                )
            )
            atomic_write(path, canonical(value))
            yield value

    def run_source(source):
        count = 0
        attempted = 0
        deferred = 0
        for value in stream(source):
            count += 1
            deferred += int(value["status"] == "deferred")
            attempted += int(value["status"] != "deferred")
            if count % 100 == 0:
                print(source, count, "last", value["security"], value.get("last"), flush=True)
            assert shutil.disk_usage(a.root).free > scope["free_reserve_bytes"], (
                "free reserve reached"
            )
            if count % 200 == 0:
                used = sum(x.stat().st_size for x in a.root.rglob("*") if x.is_file())
                assert used < scope["budget_bytes"], "increment budget reached; retained partials"
        return dict(
            source=source,
            processed=count,
            attempted=attempted,
            deferred=deferred,
            stopped="complete",
        )

    print(
        "START",
        json.dumps(
            dict(
                parent=r.receipt(),
                instruments=len(records),
                pending_jobs=len(jobs),
                root=str(a.root),
            )
        ),
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_source, s) for s in ("sohu", "tencent", "sina_minute")]
        for f in as_completed(futures):
            print("STREAM", json.dumps(f.result()), flush=True)
    # Journal can always be rebuilt from immutable-at-completion job files.
    for path in sorted(
        (a.root / "jobs").glob("*.json"), key=lambda p: json.loads(p.read_bytes())["completed_at"]
    ):
        value = json.loads(path.read_bytes())
        db.execute(
            "insert or replace into jobs values(?,?,?,?,?)",
            (
                value["job_key"],
                value["source"],
                value["security"],
                value["instrument_id"],
                canonical(value).decode(),
            ),
        )
    db.commit()
    result = dict(
        parent=r.receipt(),
        seconds=time.monotonic() - started,
        counts=[
            list(x)
            for x in db.execute(
                "select source,json_extract(body,'$.status'),count(*),max(json_extract(body,'$.last')) from jobs group by 1,2"
            )
        ],
        scope_sha256=sha_file(scope_path),
    )
    prior_report = a.root / "collection-report.json"
    if prior_report.exists():
        retained = a.root / "evidence" / ("collection-report-" + sha_file(prior_report) + ".json")
        if not retained.exists():
            atomic_write(retained, prior_report.read_bytes())
    atomic_write(prior_report, canonical(result))
    print("DONE", json.dumps(result), flush=True)
    db.close()


if __name__ == "__main__":
    main()
