"""Build complete touched chunks from sealed recent responses; never scan old markets.

This consumes the existing collector journal and exact R2-style metadata. Every
captured bar is assigned one destination. A separate explicit apply publishes the
plan, and the prior release and all staging artifacts remain retained.
"""

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import time

import pandas as pd

from ashare_data.market.increment import minute_rows, normalize_values, MINUTE_SERIES, sohu_capture
from ashare_data.providers.daily_sources import tencent_daily
from ashare_data.storage.market_release import (
    MarketRelease,
    canonical,
    atomic_write,
    sha_file,
    connection,
    sql_literal,
    FIELDS,
    DISPOSITIONS,
)


def native_symbol(security):
    return (
        "bj" + security[3:]
        if security.startswith("BJ.")
        else ("sh" if security.endswith("XSHG") else "sz") + security[:6]
    )


def save_parquet(c, query, path):
    """Retain every input/output; compare all columns in both directions."""
    if not path.exists():
        c.execute(
            "COPY ("
            + query
            + ") TO "
            + sql_literal(path)
            + " (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)"
        )
    old = "SELECT * FROM read_parquet(" + sql_literal(path) + ")"
    projected = "SELECT * FROM (" + query + ")"
    count = c.execute(
        "SELECT count(*) FROM (("
        + projected
        + " EXCEPT ALL "
        + old
        + ") UNION ALL ("
        + old
        + " EXCEPT ALL "
        + projected
        + "))"
    ).fetchone()[0]
    assert count == 0, ("key/value mismatch", path, count)
    return {
        "path": str(path),
        "sha256": sha_file(path),
        "bytes": path.stat().st_size,
        "key_value_mismatches": 0,
    }


def all_jobs(root):
    versions = defaultdict(list)
    for path in (root / "jobs").glob("*.json"):
        value = json.loads(path.read_bytes())
        value["job_path"] = str(path)
        versions[value["job_key"]].append(value)
    chosen = {}
    for key, items in versions.items():
        ordered = sorted(items, key=lambda x: x["completed_at"])
        chosen[key] = next((x for x in reversed(ordered) if x["status"] == "ok"), ordered[-1])
    return chosen, versions


def response_raw(root, job):
    receipts = [r for r in job["receipts"] if r.get("sha256")]
    if not receipts:
        return None, None
    assert len(receipts) == 1, "one selected response per bounded job"
    receipt = receipts[0]
    path = root / "incoming/raw" / (receipt["sha256"] + ".gz")
    body = gzip.decompress(path.read_bytes())
    assert hashlib.sha256(body).hexdigest() == receipt["sha256"]
    return body, dict(
        asset_id="response:"
        + hashlib.sha256(
            canonical(
                receipt
                if "response_block" not in job
                else {
                    "receipt": receipt,
                    "security": job["security"],
                    "block": job["response_block"],
                }
            )
        ).hexdigest(),
        path=str(path),
        sha256=sha_file(path),
        content_sha256=receipt["sha256"],
        bytes=path.stat().st_size,
        source=job["source"],
        security=job["security"],
        record_scope={
            "native_block_index": job.get("response_block"),
            "requested_security": job["security"],
        },
        receipt=receipt,
        locator="JSON array row (Sina minute), hq array row (Sohu), day array row (Tencent)",
    )


def failed_record_count(body, source, security, source_block=None):
    """Error envelopes contain no bars; recognized rejected arrays are counted."""
    if body is None:
        return 0
    text = body.decode("gb18030" if source == "sohu" else "utf-8")
    if source == "sohu":
        import re

        m = re.fullmatch(r"\s*historySearchHandler\((.*)\);?\s*", text, re.S)
        if not m:
            raise ValueError("unclassified Sohu envelope; explicit review required")
        value = json.loads(m[1])
        items = value if isinstance(value, list) else [value]
        if source_block is not None:
            items = [items[source_block]]
        return sum(len(x.get("hq", [])) for x in items if isinstance(x, dict))
    value = json.loads(text)
    if source == "sina_minute":
        if value is None or isinstance(value, dict):
            return 0
        assert isinstance(value, list)
        return len(value)
    data = value.get("data", {})
    if not isinstance(data, dict):
        assert value.get("code") != 0, "successful Tencent envelope changed"
        return 0
    return len(data.get(native_symbol(security), {}).get("day", []))


def build(a):
    root = a.root
    scope = json.loads((root / "scope.json").read_bytes())
    r = MarketRelease(
        scope["parent"]["root"],
        scope["parent"]["release_id"],
        expected_manifest_sha256=scope["parent"]["manifest_sha256"],
    )
    assert (root / "collection-report.json").exists() or a.preview, "collection has not finished"
    selected, versions = all_jobs(root)
    scopes = scope["instruments"][: a.limit] if a.limit else scope["instruments"]
    out = root / (a.build_name or ("preview-build" if a.preview else "build"))
    out.mkdir(exist_ok=True)
    for name in ("chunks", "ledgers"):
        (out / name).mkdir(exist_ok=True)
    checkpoint = sqlite3.connect(out / "checkpoint.sqlite3")
    checkpoint.execute(
        "create table if not exists batches(instrument_id integer primary key,body text not null)"
    )
    settings = {
        "parent": r.sha256,
        "scope_sha256": sha_file(root / "scope.json"),
        "builder_sha256": sha_file(Path(__file__)),
        "normalizer_sha256": sha_file(
            Path(__import__("ashare_data.market.increment", fromlist=["x"]).__file__)
        ),
        "job_inventory_sha256": hashlib.sha256(
            canonical(
                [
                    (
                        key,
                        sorted(
                            (job["completed_at"], sha_file(job["job_path"])) for job in captures
                        ),
                    )
                    for key, captures in sorted(versions.items())
                ]
            )
        ).hexdigest(),
    }
    setting_path = out / "settings.json"
    if setting_path.exists():
        assert json.loads(setting_path.read_bytes()) == settings, (
            "build inputs changed; retain existing stage and use a named new build"
        )
    else:
        atomic_write(setting_path, canonical(settings))
    completed = {row[0] for row in checkpoint.execute("select instrument_id from batches")}
    if (
        completed == {item["instrument_id"] for item in scopes}
        and (out / "build-report.json").exists()
    ):
        report = json.loads((out / "build-report.json").read_bytes())
        assert report["preview"] == a.preview
        print("UNCHANGED: complete build retains its original report", flush=True)
        checkpoint.close()
        return
    old = {(f["table"], f["series_id"], f["instrument_id"]): f for f in r.files}
    after = {
        table: max(f["last_date"] for f in r.files if f["table"] == table)
        for table in ("bars_1d", "bars_1m")
    }
    with r.metadata() as db:
        base = (
            db.execute("select max(batch_id) from data_batches").fetchone()[0] // 100000 + 1
        ) * 100000
    c = connection(out / "spill", memory="1GB", threads=1)
    started = time.monotonic()
    for pos, item in enumerate(scopes):
        if pos % 100 == 0:
            assert shutil.disk_usage(root).free >= scope.get("free_reserve_bytes", 150 * 1024**3), (
                "free-space reserve reached; retained completed batches"
            )
            used = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
            assert used < scope.get("budget_bytes", 8 * 1024**3), (
                "increment budget reached; retained completed batches"
            )
        instrument = item["instrument_id"]
        security = item["security"]
        assert 0 < instrument < 10000, "batch allocation range requires an explicit new mapping"
        if checkpoint.execute(
            "select 1 from batches where instrument_id=?", (instrument,)
        ).fetchone():
            continue
        names = ["sohu"] if security.startswith("BJ.") else ["sohu", "tencent", "sina_minute"]
        if any(source + ":" + security not in selected for source in names):
            if a.preview:
                continue
            raise ValueError("unfinished source job " + security)
        watermarks = {
            table: max(
                (
                    f["last_date"]
                    for f in r.files
                    if f["table"] == table
                    and f["instrument_id"] == instrument
                    and f["series_id"] in ([1, 5] if table == "bars_1d" else [2, 4, 6])
                ),
                default="0001-01-01",
            )
            for table in ("bars_1d", "bars_1m")
        }
        prior_daily = {}
        supplement_days = set()
        prior_minute = {}
        for series in (1, 5, 6):
            table = "bars_1m" if series == 6 else "bars_1d"
            previous = old.get((table, series, instrument))
            if previous and previous["last_date"] >= scope["start"]:
                cursor = c.execute(
                    "select * from read_parquet(?) where trade_date between ? and ?",
                    [str(r.verify(previous)), scope["start"], scope["end"]],
                )
                columns = [x[0] for x in cursor.description]
                for values in cursor.fetchall():
                    rec = dict(zip(columns, values))
                    if series == 1:
                        prior_daily[str(rec["trade_date"])] = rec
                    elif series == 5:
                        supplement_days.add(str(rec["trade_date"]))
                    else:
                        prior_minute[rec["label_at"]] = rec
        minute_base = max(
            (
                f["last_date"]
                for f in r.files
                if f["table"] == "bars_1m"
                and f["instrument_id"] == instrument
                and f["series_id"] in (2, 4)
            ),
            default="0001-01-01",
        )
        ledger = []
        daily = []
        minute = []
        assets = []
        observations = []
        failures = []
        jobs = []
        for source in names:
            job = selected[source + ":" + security]
            jobs.append(
                {
                    k: job.get(k)
                    for k in (
                        "job_key",
                        "job_path",
                        "status",
                        "error",
                        "rows",
                        "first",
                        "last",
                        "completed_at",
                    )
                }
            )
            body, asset = response_raw(root, job)
            if asset:
                assets.append(asset)
            # Older failed observations remain independently accounted, never overwritten.
            for previous in versions[job["job_key"]]:
                if previous["completed_at"] == job["completed_at"]:
                    continue
                earlier, prior_asset = response_raw(root, previous)
                if prior_asset:
                    assets.append(prior_asset)
                n = failed_record_count(earlier, source, security, previous.get("response_block"))
                for index in range(n):
                    ledger.append(
                        dict(
                            source=source,
                            source_row=index,
                            asset_id=prior_asset["asset_id"],
                            disposition="resolved_unused",
                            reason="superseded_capture_retained",
                            trade_date=None,
                        )
                    )
            if job["status"] != "ok":
                failures.append(jobs[-1])
                n = failed_record_count(body, source, security, job.get("response_block"))
                for index in range(n):
                    ledger.append(
                        dict(
                            source=source,
                            source_row=index,
                            asset_id=asset["asset_id"],
                            disposition="format_rejected",
                            reason=job["error"],
                            trade_date=None,
                        )
                    )
                continue
            if source == "sina_minute":
                from ashare_data.market.increment import LEGACY_MINUTE_WINDOW
                window = scope.get("minute_window", LEGACY_MINUTE_WINDOW)
                parsed = minute_rows(body, after_date=minute_base, end_date=scope["end"], max_rows=window)
                native_array = json.loads(body) or []
                truncated_day = native_array[0]["day"][:10] if len(native_array) == window else None
                for rec in parsed:
                    if rec["disposition"] == "adopted":
                        previous = prior_minute.get(rec["label_at"])
                        if previous:
                            equal = all(previous[k] == rec[k] for k in FIELDS)
                            rec.update(
                                disposition="equal_duplicate" if equal else "quarantined",
                                reason="unchanged_published_source_value"
                                if equal
                                else "source_revision_needs_adjudication",
                            )
                        elif rec["trade_date"] < scope["start"] or (
                            rec["trade_date"] == truncated_day
                            and not any(
                                str(x["trade_date"]) == rec["trade_date"]
                                for x in prior_minute.values()
                            )
                        ):
                            rec.update(
                                disposition="quarantined",
                                reason="bounded_source_window_left_edge_not_complete",
                            )
                for rec in parsed:
                    ledger.append(
                        {
                            k: rec.get(k)
                            for k in (
                                "source_row",
                                "native_label",
                                "trade_date",
                                "disposition",
                                "reason",
                            )
                        }
                        | dict(source=source, asset_id=asset["asset_id"])
                    )
                    if rec["disposition"] == "adopted":
                        minute.append(rec)
            else:
                # Native response block and row positions remain explicit for bulk captures.
                if source == "sohu":
                    reparsed, native = sohu_capture(
                        body,
                        asset["receipt"],
                        native_symbol(security),
                        scope["start"],
                        scope["end"],
                        source_block=job.get("response_block"),
                    )
                else:

                    class FrozenHTTP:
                        def get(self, url, referer=None):
                            assert url == asset["receipt"]["url"]
                            return body, [asset["receipt"]]

                    reparsed, _ = tencent_daily(
                        FrozenHTTP(), native_symbol(security), scope["start"], scope["end"]
                    )
                    native = json.loads(body)["data"][native_symbol(security)]["day"]
                assert reparsed == job["normalized_rows"], (
                    "normalized checkpoint differs from source body"
                )
                locators = {row[0]: index for index, row in enumerate(native)}
                assert len(locators) == len(native) == len(reparsed)
                for raw in reparsed:
                    rec = dict(
                        source=source,
                        source_row=locators[raw["date"]],
                        asset_id=asset["asset_id"],
                        trade_date=raw["date"],
                        native_label=raw["date"],
                        native=raw,
                        response_id=hashlib.sha256(canonical(asset["receipt"])).hexdigest(),
                        observed_at=asset["receipt"]["received_at"],
                    )
                    try:
                        rec.update(normalize_values(raw))
                    except (ValueError, TypeError) as e:
                        rec.update(disposition="quarantined", reason=str(e))
                    else:
                        observations.append(
                            dict(
                                instrument_id=instrument,
                                symbol=native_symbol(security),
                                date=raw["date"],
                                source=source,
                                **{
                                    k: raw.get(k)
                                    for k in (
                                        "open",
                                        "high",
                                        "low",
                                        "close",
                                        "volume",
                                        "amount",
                                        "preclose",
                                        "tradestatus",
                                        "is_st",
                                    )
                                },
                                row_hash=hashlib.sha256(canonical(raw)).hexdigest(),
                                response_id=rec["response_id"],
                                observed_at=rec["observed_at"],
                            )
                        )
                        rec.update(disposition="resolved_unused", reason="before_increment_window")
                        if scope["start"] <= raw["date"] <= scope["end"]:
                            daily.append(rec)
                    ledger.append(rec)
        by_day = defaultdict(list)
        for rec in daily:
            by_day[rec["trade_date"]].append(rec)
        winners = []
        priority = {"baostock": 0, "eastmoney": 1, "sina": 2, **scope["source_priority"]}
        for day, candidates in sorted(by_day.items()):
            candidates.sort(key=lambda row: priority[row["source"]])
            winner = candidates[0]
            previous = prior_daily.get(day)
            if day in supplement_days:
                for row in candidates:
                    row.update(
                        disposition="resolved_unused", reason="existing_release_supplement_retained"
                    )
                continue
            if previous:
                old_rank = priority[previous["source"]]
                best_rank = priority[winner["source"]]
                equal = all(previous[k] == winner[k] for k in FIELDS)
                if equal or old_rank <= best_rank:
                    for row in candidates:
                        same = all(previous[k] == row[k] for k in FIELDS)
                        unresolved = priority[row["source"]] == old_rank and not same
                        row.update(
                            disposition="equal_duplicate"
                            if same
                            else "quarantined"
                            if unresolved
                            else "resolved_unused",
                            reason="unchanged_published_values"
                            if same
                            else "source_revision_needs_adjudication"
                            if unresolved
                            else "lower_frozen_daily_source_priority",
                        )
                    continue
            winner.update(
                disposition="adopted",
                reason="higher_priority_source_arrived"
                if previous
                else "frozen_daily_source_priority",
            )
            winners.append(winner)
            for other in candidates[1:]:
                equal = all(winner[k] == other[k] for k in FIELDS)
                other.update(
                    disposition="equal_duplicate" if equal else "resolved_unused",
                    reason="equal_daily_values" if equal else "lower_frozen_daily_source_priority",
                )
        chunks = []
        batches = []
        for table, series, records in [("bars_1d", 1, winners), ("bars_1m", MINUTE_SERIES, minute)]:
            if not records:
                continue
            batch = base + series * 10000 + instrument
            frame = []
            for row in records:
                value = dict(
                    series_id=series,
                    instrument_id=instrument,
                    trade_date=row["trade_date"],
                    batch_id=batch,
                    **{k: None if row[k] is None else str(row[k]) for k in FIELDS},
                )
                if table == "bars_1m":
                    value["label_at"] = row["label_at"]
                else:
                    value.update(
                        source_symbol=native_symbol(security),
                        source=row["source"],
                        row_hash=hashlib.sha256(canonical(row["native"])).hexdigest(),
                        response_id=row["response_id"],
                        observed_at=row["observed_at"],
                        preclose=None,
                        tradestatus=None,
                        is_st=None,
                    )
                frame.append(value)
            c.register("incoming", pd.DataFrame(frame))
            projection = [
                "series_id::USMALLINT series_id",
                "instrument_id::UINTEGER instrument_id",
                "trade_date::DATE trade_date",
            ]
            if table == "bars_1m":
                projection += ["label_at::TIMESTAMPTZ label_at"]
            projection += [
                k + "::" + ("BIGINT" if k == "volume_shares" else "DECIMAL(38,12)") + " " + k
                for k in FIELDS
            ]
            projection += ["batch_id::UINTEGER batch_id"]
            if table == "bars_1d":
                projection += [
                    '"' + k + '"::VARCHAR AS "' + k + '"'
                    for k in (
                        "source_symbol",
                        "source",
                        "row_hash",
                        "response_id",
                        "observed_at",
                        "preclose",
                        "tradestatus",
                        "is_st",
                    )
                ]
            new = "SELECT " + ",".join(projection) + " FROM incoming"
            previous = old.get((table, series, instrument))
            if previous:
                replaced_days = (
                    [row["trade_date"] for row in records if row["trade_date"] in prior_daily]
                    if table == "bars_1d"
                    else []
                )
                exclude = (
                    " WHERE trade_date NOT IN ("
                    + ",".join(sql_literal(x) for x in replaced_days)
                    + ")"
                    if replaced_days
                    else ""
                )
                query = (
                    "SELECT * FROM read_parquet("
                    + sql_literal(r.verify(previous))
                    + ")"
                    + exclude
                    + " UNION ALL "
                    + new
                )
            else:
                query = new
            query += " ORDER BY " + ("label_at" if table == "bars_1m" else "trade_date")
            output = out / "chunks" / f"{table}-{series}-{instrument}.parquet"
            receipt = save_parquet(c, query, output)
            key = "trade_date" + (",label_at" if table == "bars_1m" else "")
            assert (
                c.execute(
                    "select count(*) from (select "
                    + key
                    + ",count(*) n from read_parquet(?) group by "
                    + key
                    + " having n<>1)",
                    [str(output)],
                ).fetchone()[0]
                == 0
            )
            # All fresh input fields must round trip exactly, including nulls and Decimal values.
            actual = c.execute(
                "SELECT "
                + ",".join(FIELDS)
                + " FROM read_parquet(?) WHERE batch_id=? ORDER BY "
                + key,
                [str(output), batch],
            ).fetchall()
            expected = [
                tuple(row[k] for k in FIELDS)
                for row in sorted(records, key=lambda x: x.get("label_at", x["trade_date"]))
            ]
            assert actual == expected, "source-to-fact exact value mismatch"
            revisions = (
                sum(row["trade_date"] in prior_daily for row in records)
                if table == "bars_1d"
                else 0
            )
            inserted = len(records) - revisions
            chunks.append(
                dict(
                    table=table,
                    series_id=series,
                    instrument_id=instrument,
                    parquet=str(output),
                    sha256=receipt["sha256"],
                    new_rows=inserted,
                    changed_rows=len(records),
                    replaced_rows=revisions,
                    total_rows=inserted + (previous["rows"] if previous else 0),
                    bytes=receipt["bytes"],
                )
            )
            batch_assets = [
                x["asset_id"]
                for x in assets
                if (x["source"] == "sina_minute") == (table == "bars_1m")
            ]
            batches.append(
                dict(
                    batch_id=batch,
                    batch_key=f"increment:{scope['end']}:{r.sha256[:16]}:{series}:{instrument}",
                    assets=batch_assets,
                    table=table,
                    series_id=series,
                    instrument_id=instrument,
                    new_rows=inserted,
                    changed_rows=len(records),
                    replaced_rows=revisions,
                )
            )
            c.unregister("incoming")
        dispositions = Counter(row["disposition"] for row in ledger)
        assert len(ledger) == sum(dispositions.values())
        ledger_rows = [
            dict(
                source=x["source"],
                source_row=x["source_row"],
                asset_id=x.get("asset_id"),
                native_label=x.get("native_label"),
                trade_date=x.get("trade_date"),
                disposition=x["disposition"],
                reason=x["reason"],
            )
            for x in ledger
        ]
        path = out / "ledgers" / f"{instrument}.json.gz"
        compressed = gzip.compress(canonical(ledger_rows), mtime=0)
        if path.exists():
            assert path.read_bytes() == compressed
        else:
            path.write_bytes(compressed)
        result = dict(
            instrument_id=instrument,
            security=security,
            previous_watermarks=watermarks,
            chunks=chunks,
            batches=batches,
            assets=assets,
            jobs=jobs,
            failures=failures,
            observations=observations,
            ledger=dict(path=str(path), sha256=sha_file(path), bytes=path.stat().st_size),
            input_dispositions={
                "input_rows": len(ledger),
                **{k: dispositions[k] for k in DISPOSITIONS},
            },
            incoming_by_day={
                table: {
                    day: sum(1 for x in rows if x["trade_date"] == day)
                    for day in sorted({x["trade_date"] for x in rows})
                }
                for table, rows in [("bars_1d", winners), ("bars_1m", minute)]
            },
        )
        checkpoint.execute(
            "insert into batches values(?,?)", (instrument, canonical(result).decode())
        )
        checkpoint.commit()
        if pos % 100 == 0:
            print(
                "BUILT",
                pos + 1,
                len(scopes),
                security,
                "seconds",
                round(time.monotonic() - started, 1),
                flush=True,
            )
    results = [
        json.loads(row[0])
        for row in checkpoint.execute("select body from batches order by instrument_id")
    ]
    totals = Counter()
    for value in results:
        totals.update(value["input_dispositions"])
    request_outcomes = Counter(
        (items[-1]["source"], items[-1]["status"])
        for items in [
            sorted(captures, key=lambda job: job["completed_at"]) for captures in versions.values()
        ]
    )
    report = dict(
        parent=r.receipt(),
        preview=a.preview,
        scopes_built=len(results),
        scopes_requested=len(scopes),
        input_dispositions=totals,
        new_rows={
            table: sum(x["new_rows"] for v in results for x in v["chunks"] if x["table"] == table)
            for table in ("bars_1d", "bars_1m")
        },
        raw_source_errors=[x for v in results for x in v["failures"]],
        latest_capture_outcomes=[
            {"source": source, "status": status, "scopes": count}
            for (source, status), count in sorted(request_outcomes.items())
        ],
        after_date=after,
        cutoff=scope["end"],
        seconds=time.monotonic() - started,
        source_to_target_exact_values=True,
        old_market_scan=False,
        restoration_drill=False,
        deleted_artifacts=False,
    )
    atomic_write(out / "build-report.json", canonical(report))
    print("DONE", json.dumps(report, default=str), flush=True)
    c.close()
    checkpoint.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--preview", action="store_true")
    p.add_argument("--build-name")
    p.add_argument("--limit", type=int)
    a = p.parse_args()
    build(a)


if __name__ == "__main__":
    main()
