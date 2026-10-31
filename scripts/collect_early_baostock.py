#!/usr/bin/env python3
"""Archive anonymous early BaoStock responses; import only as diagnostics."""

import argparse
import base64
import gzip
import json
from pathlib import Path
import time

from ashare_data.providers.baostock_receipt.protocol import decode_response
from ashare_data.providers.baostock_receipt.baostock_collection import (
    collect,
    capture_path,
    read_capture,
    verify_capture,
    FIELDS,
    RequestTimeout,
)
from ashare_data.storage.local_daily import LocalDailyStore, DEFAULT_ROOT
from ashare_data.market.early_history import STAGES, write_json
from ashare_data.providers.daily_sources import decimal, now
from ashare_data.storage.immutable import immutable_write


def requests_for(store, stage):
    jobs = [
        dict(r)
        for r in store.db.execute(
            "SELECT j.* FROM jobs j JOIN early_jobs e USING(id) WHERE e.stage=? "
            "AND e.requested_source='baostock' AND j.state='pending' ORDER BY j.symbol",
            (stage,),
        )
    ]
    return [
        (
            job,
            dict(
                method="query_history_k_data_plus",
                params=dict(
                    code=job["symbol"][:2] + "." + job["symbol"][2:],
                    fields=FIELDS,
                    start_date=job["start"],
                    end_date=job["end"],
                    frequency="d",
                    adjustflag="3",
                ),
            ),
        )
        for job in jobs
    ]


def import_diagnostics(store, stage):
    container = store.root / "raw" / "early-baostock"
    counts = dict(jobs=0, rows=0, absent=0, errors=[])
    for job, request in requests_for(store, stage):
        path = capture_path(container, request)
        if not path.exists():
            counts["absent"] += 1
            store.db.execute(
                "UPDATE jobs SET state='failed',error=?,updated_at=? WHERE id=?",
                ("capture absent after bounded collection; no data claim", now(), job["id"]),
            )
            store.db.execute(
                "UPDATE early_jobs SET capture_path=?,quality='capture_absent' WHERE id=?",
                (str(path), job["id"]),
            )
            store.db.commit()
            continue
        try:
            envelope = read_capture(path)
            if envelope["request"] != request:
                raise ValueError("requested security/window differs from archived query")
            rows, wire_quality = verify_capture(envelope)
            integrity = sorted(
                {
                    decode_response(base64.b64decode(raw))["integrity"]
                    for raw in envelope["raw_responses"]
                }
            )
            receipt = envelope["receipt"]
            # Even SDK equality is not proof of the unverified outer integrity.
            # Keep all early BaoStock responses diagnostic pending unit/calendar review.
            quality = "diagnostic_only:" + ";".join(integrity) + ";" + wire_quality
            if "compressed_outer_integrity_unverified" in integrity:
                quality = "validation_unknown:" + quality
            raw = gzip.decompress(path.read_bytes())
            import hashlib

            sha = hashlib.sha256(raw).hexdigest()
            immutable_write(store.root / "raw" / (sha + ".gz"), path.read_bytes())
            rid = store.receipts(
                [
                    dict(
                        url="baostock://public-api.baostock.com:10030/query_history_k_data_plus",
                        started_at=receipt["request_started_at"],
                        received_at=receipt["operation_completed_at"],
                        sha256=sha,
                        status=0,
                        bytes=len(raw),
                    )
                ]
            )[0]
            for row in rows:
                if not job["start"] <= row["date"] <= job["end"] or row["date"] >= "2010-01-01":
                    raise ValueError("out of requested pre-2010 range")
                if row["code"] != request["params"]["code"] or row["adjustflag"] != "3":
                    raise ValueError("security/raw basis mismatch")
                converted = dict(
                    date=row["date"],
                    **{
                        k: decimal(row[k])
                        for k in ("open", "high", "low", "close", "preclose", "volume", "amount")
                    },
                    tradestatus=row["tradestatus"],
                    is_st=row["isST"],
                )
                body = json.dumps(converted, ensure_ascii=False)
                old = store.db.execute(
                    "SELECT body FROM early_diagnostics WHERE symbol=? AND date=? AND source='baostock'",
                    (job["symbol"], row["date"]),
                ).fetchone()
                if old and old[0] != body:
                    raise ValueError("diagnostic append conflict")
                store.db.execute(
                    "INSERT OR IGNORE INTO early_diagnostics VALUES(?,?,?,?,?,?)",
                    (job["symbol"], row["date"], "baostock", body, rid, quality),
                )
            store.db.execute(
                "UPDATE jobs SET state='diagnostic',source='early_baostock',nrows=?,response_ids=?,updated_at=? WHERE id=?",
                (len(rows), json.dumps([rid]), now(), job["id"]),
            )
            store.db.execute(
                "UPDATE early_jobs SET capture_path=?,quality=? WHERE id=?",
                (str(path), quality, job["id"]),
            )
            store.db.commit()
            counts["jobs"] += 1
            counts["rows"] += len(rows)
        except Exception as error:
            store.db.rollback()
            counts["errors"].append(dict(job=job["id"], error=str(error)))
            store.db.execute(
                "UPDATE jobs SET state='failed',error=?,updated_at=? WHERE id=?",
                ("capture verification failed: " + str(error), now(), job["id"]),
            )
            store.db.execute(
                "UPDATE early_jobs SET capture_path=?,quality='capture_verification_failed' WHERE id=?",
                (str(path), job["id"]),
            )
            store.db.commit()
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--import-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    store = LocalDailyStore(args.root, readonly=not args.import_only)
    try:
        if args.import_only:
            with store.writer():
                outcome = import_diagnostics(store, args.stage)
        else:
            requests = [request for _, request in requests_for(store, args.stage)]
            container = store.root / "raw" / "early-baostock"
            (container / "raw").mkdir(parents=True, exist_ok=True)
            results, quarantine = [], {}
            # Bounded reconnection, respecting the existing shared anonymous lock.
            # Every complete request is checkpointed and not refetched on resume.
            for attempt in range(3):
                try:
                    result = collect(
                        container,
                        requests,
                        timeout=60,
                        interval=1,
                        archive_only=True,
                        quarantine=quarantine,
                    )
                except (RequestTimeout, ConnectionError, OSError) as error:
                    result = dict(outcome="transport_failed", error=str(error))
                results.append(result)
                write_json(
                    args.output.with_suffix(".progress.json"),
                    dict(
                        stage=args.stage,
                        attempts=results,
                        at=now(),
                        diagnostic_only=True,
                        quarantines=quarantine,
                    ),
                )
                if result["outcome"] != "transport_failed":
                    break
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))
            outcome = dict(
                stage=args.stage, attempts=results, diagnostic_only=True, quarantines=quarantine
            )
        write_json(args.output, outcome)
        print(json.dumps(outcome, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
