"""Sequential anonymous BaoStock evidence for local daily maintenance.

The pinned SDK and independent wire decoder are shared with existing AD readers.
This collector archives evidence only; imports occur under the local store lock.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import gzip
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import tempfile
import time

from ashare_data.providers.baostock_receipt.capture import capture_query, utc_now, bh, sh
from ashare_data.providers.baostock_receipt.protocol import (
    decode_request,
    decode_response,
    check_identity,
)
from ashare_data.providers.baostock_worker import BoundedSocket, PinnedLoader, sdk_identity
from ashare_data.storage.local_daily import DEFAULT_ROOT, LocalDailyStore, contiguous, digest
from ashare_data.storage.immutable import immutable_write

FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST"


class RequestTimeout(BaseException):
    pass


def capture_path(root, request, refresh_day=None):
    directory = Path(root) / "raw" / "baostock"
    if refresh_day and request["method"] == "query_history_k_data_plus":
        return directory / (digest(dict(request=request, observed_date=refresh_day)) + ".json.gz")
    legacy = directory / (digest(request) + ".json.gz")
    if request["method"] != "query_stock_basic":
        return legacy
    # A current basic list must not become an eternal metadata cache. Historical
    # requests remain fixed; each new UTC observation day gets a separate basic snapshot.
    today = utc_now()[:10]
    if legacy.exists() and read_capture(legacy)["receipt"]["operation_completed_at"][:10] == today:
        return legacy
    return directory / (digest(dict(request=request, observed_date=today)) + ".json.gz")


def read_capture(path):
    return json.loads(gzip.decompress(Path(path).read_bytes()))


def verify_capture(envelope):
    """Re-decode saved wire bytes. Preserve the SDK's documented name whitespace loss."""
    receipt = envelope["receipt"]
    core = dict(receipt)
    rid = core.pop("receipt_id")
    if sh(core) != rid or receipt["request"] != envelope["request"]:
        raise ValueError("capture receipt identity mismatch")
    if not (
        receipt["sdk_error_code"] == "0"
        and not receipt["transport_faults"]
        and receipt["clock_order_valid"]
    ):
        raise ValueError("incomplete BaoStock query")
    rows = []
    page_lengths = []
    for index, (sent, raw, page) in enumerate(
        zip(envelope["raw_requests"], envelope["raw_responses"], receipt["pages"], strict=True), 1
    ):
        sent, raw = base64.b64decode(sent), base64.b64decode(raw)
        if bh(sent) != page["raw_request_sha256"] or bh(raw) != page["raw_response_sha256"]:
            raise ValueError("wire hash mismatch")
        decoded = decode_response(raw)
        if (
            decoded["error_code"] != "0"
            or check_identity(envelope["request"], decode_request(sent), decoded, index)["state"]
            != "matched"
        ):
            raise ValueError("wire identity mismatch")
        if decoded["rows"] != page["rows"]:
            raise ValueError("wire/receipt row mismatch")
        rows.extend(decoded["rows"])
        page_lengths.append(len(decoded["rows"]))
    if not page_lengths or any(n != 2000 for n in page_lengths[:-1]) or page_lengths[-1] >= 2000:
        raise ValueError("pagination has no complete terminal page")
    if not receipt["pages"]:
        raise ValueError("no source pages")
    if rows == envelope["sdk_rows"]:
        quality = "wire_rows_match_sdk"
    elif (
        envelope["request"]["method"] in {"query_stock_basic", "query_all_stock"}
        and [
            {k: "".join(v.split()) if k == "code_name" else v for k, v in row.items()}
            for row in rows
        ]
        == envelope["sdk_rows"]
    ):
        # Pinned ResultData.setData uses ''.join(receive_data.split()), also inside
        # JSON strings. Retain the independently decoded original names, not the lossy SDK name.
        quality = "wire_names_preserved_sdk_whitespace_loss"
    else:
        raise ValueError("unexplained SDK/wire mismatch")
    labels = [
        (r.get("code"), r.get("date", r.get("calendar_date", r.get("dividOperateDate"))))
        for r in rows
    ]
    if len(set(labels)) != len(labels):
        raise ValueError("duplicate wire labels")
    if receipt["query_status"] == "invalid_or_incomplete_frame":
        quality += ":revalidated_current_terminal_schema"
    return rows, quality


def publish_available(root):
    """Publish at checkpoints when the HTTP collector has released its writer lock."""
    with (Path(root) / "writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps(dict(at=utc_now(), publish="deferred_http_writer_active")), flush=True)
            return
        store = LocalDailyStore(root)
        try:
            result = import_captures(store)
            if result["captures"]:
                store.report()
            print(json.dumps(dict(at=utc_now(), published=result)), flush=True)
        finally:
            store.close()


def collect(
    root,
    requests,
    timeout=120,
    interval=1,
    publish_every=50,
    refresh=False,
    quarantine=None,
    archive_only=False,
):
    root = Path(root)
    directory = root / "raw" / "baostock"
    directory.mkdir(exist_ok=True)
    refresh_day = utc_now()[:10] if refresh else None
    quarantine = quarantine if quarantine is not None else {}
    requests = [
        r
        for r in requests
        if digest(r) not in quarantine and not capture_path(root, r, refresh_day).exists()
    ]
    if not requests:
        if not archive_only:
            publish_available(root)
        return dict(
            outcome="complete_with_quarantines" if quarantine else "complete",
            completed=0,
            quarantined=len(quarantine),
        )
    lock = Path(tempfile.gettempdir()) / f"ashare-baostock-{os.getuid()}.lock"
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        spec = importlib.util.find_spec("baostock")
        if spec is None:
            raise RuntimeError("Install the pinned optional baostock==0.9.4 SDK")
        sdk_root = Path(spec.origin).parent.parent
        identity = sdk_identity(sdk_root)
        sys.meta_path.insert(0, PinnedLoader(sdk_root, identity["files"]))
        import baostock as bs
        import baostock.common.context as ctx
        import baostock.common.contants as constants

        if hasattr(ctx, "apiKey") or constants.BAOSTOCK_SERVER_IP != "public-api.baostock.com":
            raise RuntimeError("anonymous public endpoint required")
        state = {"deadline": time.monotonic() + timeout, "completed": 0, "request": None}

        class TimedSocket(BoundedSocket):
            def _limit(self):
                left = state["deadline"] - time.monotonic()
                if left <= 0:
                    raise TimeoutError("BaoStock total request deadline")
                self.sock.settimeout(min(45, left))

        original_socket = socket.socket
        socket.socket = lambda *a, **kw: TimedSocket(original_socket(*a, **kw), 0)
        original_alarm = signal.getsignal(signal.SIGALRM)

        def expire(*_):
            error = RequestTimeout("BaoStock total request deadline")
            error.completed = state["completed"]
            error.request = state["request"]
            raise error

        signal.signal(signal.SIGALRM, expire)
        try:
            signal.setitimer(signal.ITIMER_REAL, timeout)
            login = bs.login()
            signal.setitimer(signal.ITIMER_REAL, 0)
            if login.error_code != "0":
                if login.error_code == constants.BSERR_RECVSOCK_FAIL:
                    # The pinned SDK maps a local receive timeout to 10002007;
                    # it is a transport error, not a credential/quota rejection.
                    raise ConnectionError("anonymous login receive failure: " + login.error_code)
                raise RuntimeError("anonymous login failed: " + login.error_code)
            print(
                json.dumps(dict(at=utc_now(), login="anonymous_success", pending=len(requests))),
                flush=True,
            )
            outcome = "complete"
            for index, request in enumerate(requests, 1):
                if (root / "STOP_BAOSTOCK").exists():
                    outcome = "stopped"
                    break
                started = utc_now()
                state["request"] = request
                state["deadline"] = time.monotonic() + timeout
                signal.setitimer(signal.ITIMER_REAL, timeout)
                result = capture_query(
                    bs,
                    ctx,
                    request,
                    task_started_at=started,
                    max_rows=30000,
                    max_pages=24,
                    allow_universe=True,
                )
                signal.setitimer(signal.ITIMER_REAL, 0)
                envelope = dict(
                    request=request,
                    receipt=result["receipt"],
                    sdk_rows=result["sdk_rows"],
                    raw_requests=[base64.b64encode(x).decode() for x in result["raw_requests"]],
                    raw_responses=[base64.b64encode(x).decode() for x in result["raw_responses"]],
                    sdk_identity=identity,
                )
                receipt = result["receipt"]
                try:
                    verified_rows, quality = verify_capture(envelope)
                    complete = True
                    envelope["collection_quality"] = quality
                except ValueError:
                    complete = False
                filename = capture_path(root, request, refresh_day)
                if not complete:
                    filename = filename.with_name(filename.name + ".failed-" + str(time.time_ns()))
                immutable_write(
                    filename,
                    gzip.compress(json.dumps(envelope, ensure_ascii=False).encode(), mtime=0),
                )
                if (
                    not complete
                    and not receipt["transport_faults"]
                    and receipt["sdk_error_code"] == "0"
                ):
                    # Reconnects do not repeatedly query the same unrecognised schema.
                    quarantine[digest(request)] = str(filename)
                print(
                    json.dumps(
                        dict(
                            at=utc_now(),
                            progress=index,
                            total=len(requests),
                            request=request,
                            rows=len(result["sdk_rows"]),
                            complete=complete,
                            status=receipt["query_status"],
                            quality=envelope.get("collection_quality"),
                            path=str(filename),
                        ),
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if not complete and (
                    receipt["transport_faults"] or receipt["sdk_error_code"] != "0"
                ):
                    # A transport/session failure can desynchronise the socket; stop this connection.
                    outcome = "transport_failed" if receipt["transport_faults"] else "source_error"
                    break
                # An independently quarantined schema failure with a completed SDK frame
                # does not discard other securities or hold the remaining batch hostage.
                if complete:
                    state["completed"] += 1
                if not archive_only and publish_every and index % publish_every == 0:
                    publish_available(root)
                time.sleep(interval)
            if not archive_only:
                publish_available(root)
            if outcome == "complete" and quarantine:
                outcome = "complete_with_quarantines"
            return dict(
                outcome=outcome,
                completed=state["completed"],
                quarantined=len(quarantine),
                failed_request=state["request"] if outcome == "transport_failed" else None,
            )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, original_alarm)
            socket.socket = original_socket
            with contextlib.suppress(Exception):
                ctx.default_socket.close()


def daily_requests(root, repair=False):
    """Plan only BaoStock intervals not already archived, or explicitly missing source dates."""
    store = LocalDailyStore(root, readonly=True)
    try:
        start, end = store.meta("start"), store.meta("end")
        days = [
            r[0]
            for r in store.db.execute(
                "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
            )
        ]
        archived = {}
        if not repair:
            for path in sorted((store.root / "raw" / "baostock").glob("*.json.gz")):
                envelope = read_capture(path)
                request = envelope["request"]
                if request["method"] != "query_history_k_data_plus":
                    continue
                verify_capture(envelope)
                p = request["params"]
                if (
                    p.get("fields") == FIELDS
                    and p.get("frequency") == "d"
                    and p.get("adjustflag") == "3"
                ):
                    archived.setdefault(p["code"].replace(".", ""), []).append(
                        (p["start_date"], p["end_date"])
                    )
        requests = []
        for (symbol,) in store.db.execute(
            "SELECT symbol FROM instruments ORDER BY out IS NULL,symbol"
        ):
            expected = store.expected(symbol, start, end, days)
            observed = {
                r[0]
                for r in store.db.execute(
                    "SELECT date FROM bars WHERE symbol=? AND source='baostock'", (symbol,)
                )
            }
            requested = archived.get(symbol, [])
            needed = [
                d
                for d in expected
                if d not in observed and not any(a <= d <= b for a, b in requested)
            ]
            for group in contiguous(needed, days):
                requests.append(
                    dict(
                        method="query_history_k_data_plus",
                        params=dict(
                            code=symbol[:2] + "." + symbol[2:],
                            fields=FIELDS,
                            start_date=group[0],
                            end_date=group[-1],
                            frequency="d",
                            adjustflag="3",
                        ),
                    )
                )
        return requests
    finally:
        store.close()


def history_requests(root, monthly=True, retired=True, all_bars=False, repair=False):
    db = sqlite3.connect((Path(root) / "daily.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        start = json.loads(db.execute("SELECT value FROM metadata WHERE key='start'").fetchone()[0])
        end = json.loads(db.execute("SELECT value FROM metadata WHERE key='end'").fetchone()[0])
        requests = [dict(method="query_stock_basic", params=dict(code="", code_name=""))]
        # All retained listing facts + monthly historical memberships provide independent discovery.
        group = "substr(date,1,7)" if monthly else "substr(date,1,4)"
        days = [
            r[0]
            for r in db.execute(
                f"SELECT min(date) FROM calendar WHERE date BETWEEN ? AND ? GROUP BY {group}",
                (start, end),
            )
        ]
        for day in days:
            requests.append(dict(method="query_all_stock", params=dict(day=day)))
        if retired and not all_bars:
            basic_path = capture_path(root, requests[0])
            if basic_path.exists():
                basics, _ = verify_capture(read_capture(basic_path))
                retired_rows = [
                    (r["code"].replace(".", ""), r["ipoDate"], r["outDate"])
                    for r in basics
                    if r["type"] == "1"
                    and r["code"].startswith(("sh.60", "sz.00"))
                    and r["outDate"]
                    and r["outDate"] > start
                    and r["ipoDate"] <= end
                ]
            else:
                retired_rows = db.execute(
                    "SELECT symbol,ipo,out FROM instruments WHERE out IS NOT NULL ORDER BY symbol"
                ).fetchall()
            daily = []
            for symbol, ipo, out in retired_rows:
                daily.append(
                    dict(
                        method="query_history_k_data_plus",
                        params=dict(
                            code=symbol[:2] + "." + symbol[2:],
                            fields=FIELDS,
                            start_date=max(start, ipo),
                            end_date=min(end, out),
                            frequency="d",
                            adjustflag="3",
                        ),
                    )
                )
            requests = requests[:1] + daily + requests[1:]
        if all_bars:
            requests.extend(daily_requests(root, repair=repair))
        # Retired requests can overlap the all-source repair pass; exact duplicate
        # requests never require a second network operation.
        return list({digest(r): r for r in requests}.values())
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Archive BaoStock historical memberships and raw daily supplements"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--annual", action="store_true", help="Use annual rather than monthly membership samples"
    )
    parser.add_argument("--no-retired-bars", action="store_true")
    parser.add_argument(
        "--all-bars",
        action="store_true",
        help="Supplement raw OHLCV/amount/status for every scoped security",
    )
    parser.add_argument(
        "--repair-gaps",
        action="store_true",
        help="Plan missing BaoStock dates even inside previously requested intervals",
    )
    parser.add_argument(
        "--publish-every",
        type=int,
        default=50,
        help="Publish archived captures at this many completed requests, when writer is free",
    )
    args = parser.parse_args(argv)
    if args.publish_every < 1:
        parser.error("--publish-every must be positive")
    requests = history_requests(
        args.root,
        monthly=not args.annual,
        retired=not args.no_retired_bars,
        all_bars=args.all_bars or args.repair_gaps,
        repair=args.repair_gaps,
    )
    failures_without_progress = 0
    quarantine = {}
    while True:
        try:
            result = collect(
                args.root,
                requests,
                publish_every=args.publish_every,
                refresh=args.repair_gaps,
                quarantine=quarantine,
            )
        except (RequestTimeout, TimeoutError, ConnectionError) as error:
            result = dict(
                outcome="transport_failed",
                completed=getattr(error, "completed", 0),
                failed_request=getattr(error, "request", None),
            )
            print(
                json.dumps(
                    dict(at=utc_now(), error=str(error), failed_request=result["failed_request"])
                ),
                flush=True,
            )
        if result["outcome"] != "transport_failed":
            print(json.dumps(dict(at=utc_now(), collection_outcome=result)), flush=True)
            return 0 if result["outcome"] in ("complete", "stopped") else 2
        if result["completed"]:
            failures_without_progress = 0
        failures_without_progress += 1
        if failures_without_progress >= 3:
            print(
                json.dumps(
                    dict(at=utc_now(), collection_outcome=result, retry_budget_exhausted=True)
                ),
                flush=True,
            )
            return 2
        delay = (10, 30)[failures_without_progress - 1]
        print(
            json.dumps(
                dict(
                    at=utc_now(),
                    reconnect_after_seconds=delay,
                    failures_without_progress=failures_without_progress,
                )
            ),
            flush=True,
        )
        time.sleep(delay)


def import_captures(store):
    """Merge archived evidence under the caller's writer lock; raw sources remain separate."""
    store.db.executescript("""
    CREATE TABLE IF NOT EXISTS source_captures (
      id TEXT PRIMARY KEY, method TEXT, request TEXT, observed_at TEXT, path TEXT, quality TEXT);
    CREATE TABLE IF NOT EXISTS universe_membership (
      date TEXT, symbol TEXT, name TEXT, trade_status TEXT, capture_id TEXT,
      PRIMARY KEY(date,symbol,capture_id)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS metadata_conflicts (
      symbol TEXT, field TEXT, exchange_value TEXT, baostock_value TEXT, capture_id TEXT,
      PRIMARY KEY(symbol,field,capture_id)) WITHOUT ROWID;
    """)
    statistics = dict(captures=0, new_symbols=[], bars=0, memberships=0, conflicts=0)
    start, end = store.meta("start"), store.meta("end")
    imported_paths = {r[0] for r in store.db.execute("SELECT path FROM source_captures")}
    for path in sorted((store.root / "raw" / "baostock").glob("*.json.gz")):
        if str(path) in imported_paths:
            continue
        envelope = read_capture(path)
        receipt, request = envelope["receipt"], envelope["request"]
        cid = receipt["receipt_id"]
        if store.db.execute("SELECT 1 FROM source_captures WHERE id=?", (cid,)).fetchone():
            continue
        rows, quality = verify_capture(envelope)
        method, params = request["method"], request["params"]
        observed = receipt["operation_completed_at"]
        if method == "query_stock_basic":
            additions = []
            for row in rows:
                symbol = row["code"].replace(".", "")
                if not (
                    row["type"] == "1"
                    and row["code"].startswith(("sh.60", "sz.00"))
                    and row["ipoDate"] <= end
                    and (not row["outDate"] or row["outDate"] >= start)
                ):
                    continue
                old = store.db.execute(
                    "SELECT * FROM instruments WHERE symbol=?", (symbol,)
                ).fetchone()
                if old and old["metadata_source"] != "baostock":
                    for field, new in [("ipo", row["ipoDate"]), ("out", row["outDate"] or None)]:
                        if old[field] != new:
                            store.db.execute(
                                "INSERT OR IGNORE INTO metadata_conflicts VALUES (?,?,?,?,?)",
                                (symbol, field, old[field], new, cid),
                            )
                            statistics["conflicts"] += 1
                else:
                    additions.append(
                        dict(
                            symbol=symbol,
                            name=row["code_name"],
                            ipo=row["ipoDate"],
                            out=row["outDate"] or None,
                            out_inclusive=True,
                            exit_boundary="BaoStock outDate; last-date inclusive; legal termination boundary not independently verified",
                            board="sme_until_2021-04-06"
                            if symbol.startswith(("sz002", "sz003"))
                            else "main",
                            source="baostock",
                            receipt=cid,
                            observed_at=observed,
                        )
                    )
                    if old is None:
                        statistics["new_symbols"].append(symbol)
            if additions:
                store.save_universe(additions, [], start, end)
        elif method == "query_all_stock":
            for row in rows:
                if not row["code"].startswith(("sh.60", "sz.00")):
                    continue
                symbol = row["code"].replace(".", "")
                store.db.execute(
                    "INSERT OR IGNORE INTO universe_membership VALUES (?,?,?,?,?)",
                    (params["day"], symbol, row["code_name"], row["tradeStatus"], cid),
                )
                statistics["memberships"] += 1
        elif method == "query_history_k_data_plus":
            symbol = params["code"].replace(".", "")
            if not store.db.execute(
                "SELECT 1 FROM instruments WHERE symbol=?", (symbol,)
            ).fetchone():
                continue
            from ashare_data.providers.daily_sources import decimal

            converted = []
            for row in rows:
                if (
                    row["adjustflag"] != "3"
                    or row["tradestatus"] not in ("0", "1")
                    or row["isST"] not in ("0", "1")
                ):
                    raise ValueError("unsupported BaoStock daily flags")
                converted.append(
                    dict(
                        date=row["date"],
                        **{
                            k: decimal(row[k])
                            for k in (
                                "open",
                                "high",
                                "low",
                                "close",
                                "preclose",
                                "volume",
                                "amount",
                            )
                        },
                        tradestatus=row["tradestatus"],
                        is_st=row["isST"],
                    )
                )
            from ashare_data.providers.daily_sources import validate_bars

            validate_bars(
                [r for r in converted if r["tradestatus"] == "1"],
                params["start_date"],
                params["end_date"],
            )
            # Job ID is distinct from HTTP acquisition, so a supplement never marks another source fetched.
            job = dict(
                id=digest(dict(bao=request)),
                kind="bars",
                symbol=symbol,
                start=params["start_date"],
                end=params["end_date"],
            )
            store.db.execute(
                "INSERT OR IGNORE INTO jobs (id,kind,symbol,start,end,updated_at,source) VALUES (?,?,?,?,?,?,'baostock')",
                (job["id"], "bars", symbol, job["start"], job["end"], observed),
            )
            raw_body = gzip.decompress(path.read_bytes())
            raw_sha = bh(raw_body)
            immutable_write(store.root / "raw" / (raw_sha + ".gz"), path.read_bytes())
            response = dict(
                url="baostock://public-api.baostock.com:10030/" + method,
                started_at=receipt["request_started_at"],
                received_at=observed,
                sha256=raw_sha,
                status=0,
                bytes=len(raw_body),
            )
            store.ingest(
                job, dict(rows=converted, source="baostock", receipts=[response], errors=[])
            )
            statistics["bars"] += len(rows)
        store.db.execute(
            "INSERT INTO source_captures VALUES (?,?,?,?,?,?)",
            (cid, method, json.dumps(request), observed, str(path), quality),
        )
        store.db.commit()
        statistics["captures"] += 1
    (store.root / "reports" / "bao-import.json").write_text(json.dumps(statistics, indent=2) + "\n")
    return statistics


if __name__ == "__main__":
    raise SystemExit(main())
