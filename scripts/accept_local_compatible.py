#!/usr/bin/env python3
"""Replay the retained real gap inventory through the public local query API.

Run against the existing library in read-only/query-only mode. All output goes to
--output, never to the library. No network, imports, acquisitions or price edits.
"""

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import socket
import time

import ashare_data
from ashare_data.local_daily import DEFAULT_ROOT, LocalDailyStore, digest


def security(symbol):
    return symbol[2:] + (".XSHG" if symbol.startswith("sh") else ".XSHE")


def run(root, capture, output):
    if output.is_relative_to(root):
        raise ValueError("acceptance output must be outside the read-only library")
    started = time.monotonic()
    failures, checks = [], 0

    def check(condition, message):
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(message)

    database = root / "daily.sqlite3"
    before = (database.stat().st_size, database.stat().st_mtime_ns)
    inputs = {}
    for name in (
        "acceptance-amount-gap-dispositions.csv",
        "acceptance-gap-dispositions.csv",
        "alias-query-smoke.json",
        "trade-status-followup.json",
        "source-status-conflict-002647.json",
    ):
        inputs[name] = hashlib.sha256((root / "reports" / name).read_bytes()).hexdigest()
    store = LocalDailyStore(root, readonly=True)
    store.db.execute("PRAGMA query_only=ON")
    store.db.execute("BEGIN")
    rows, classes, amount_fills = [], Counter(), []
    amount_summary, gap_summary = Counter(), Counter()
    observations_before, observations_after = hashlib.sha256(), hashlib.sha256()
    try:
        for filename, kind in (
            ("acceptance-amount-gap-dispositions.csv", "amount"),
            ("acceptance-gap-dispositions.csv", "day"),
        ):
            with (root / "reports" / filename).open() as handle:
                groups = list(csv.DictReader(handle))
            for group in groups:
                symbol, start, end = (group[k] for k in ("symbol", "start", "end"))
                calendar = [
                    r[0]
                    for r in store.db.execute(
                        "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
                        (start, end),
                    )
                ]
                dates = store.expected(symbol, start, end, calendar)
                check(len(dates) == int(group["security_days"]), "inventory dates: " + str(group))
                sql = "SELECT * FROM bars WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date,source"
                params = (symbol, start, end)
                raw = [dict(r) for r in store.db.execute(sql, params)]
                observations_before.update(digest(raw).encode())
                single = store.get_price(security(symbol), start, end)
                frame = store.get_price(
                    security(symbol),
                    start,
                    end,
                    mode="compatible",
                    alias_captures=[capture] if symbol == "sh601313" else [],
                )
                observations_after.update(
                    digest([dict(r) for r in store.db.execute(sql, params)]).encode()
                )
                previous = {r["date"]: r for r in single.to_dict("records")}
                current = {r["date"]: r for r in frame.to_dict("records")}
                missing = {r["date"]: r for r in frame.attrs["unfilled_days"]}
                for day in dates:
                    a, b = previous.get(day), current.get(day)
                    diag = b or missing[day]
                    entry = dict(
                        kind=kind,
                        symbol=symbol,
                        date=day,
                        inventory_kind=group["kind"],
                        before_present=a is not None,
                        after_present=b is not None,
                        before_amount=a.get("amount") if a else None,
                        after_amount=b.get("amount") if b else None,
                        trade_state=diag["trade_state"],
                        filled_fields=";".join(b["filled_fields"]) if b else "",
                        conflict_count=len(diag["conflicts"]),
                        evidence_kinds=";".join(
                            sorted({e["evidence_kind"] for e in diag["status_evidence"]})
                        ),
                        amount_lineage=json.dumps(b["field_sources"]["amount"], sort_keys=True)
                        if b
                        else "null",
                    )
                    rows.append(entry)
                    if kind == "amount":
                        check(
                            not any(r["date"] == day and r["amount"] is not None for r in raw),
                            "inventory no longer missing amount: " + symbol + day,
                        )
                        classes[group["kind"]] += 1
                        amount_summary["security_days"] += 1
                        amount_summary["before_missing"] += a is None or a["amount"] is None
                        amount_summary["after_missing"] += b is None or b["amount"] is None
                        if b and b["amount"] is not None:
                            amount_fills.append(
                                dict(
                                    symbol=symbol,
                                    date=day,
                                    amount=b["amount"],
                                    lineage=b["field_sources"]["amount"],
                                )
                            )
                        if group["kind"] == "explicit_source_halt_without_amount":
                            check(
                                b is not None and b["amount"] is None and not b["filled_fields"],
                                "suspension incorrectly filled: " + symbol + day,
                            )
                    else:
                        gap_summary["security_days"] += 1
                        gap_summary["before_missing"] += a is None
                        gap_summary["after_missing"] += b is None
                        check(
                            a is None and b is None and diag["status_evidence"],
                            "native gap converted to quote or lacking evidence: " + symbol + day,
                        )
                        check(
                            any(
                                r["source"] == "baostock_alias" and r["tradestatus"] == "0"
                                for r in diag["observations"]
                            ),
                            "missing alias halt: " + day,
                        )
        targets = json.loads((root / "reports/alias-query-smoke.json").read_text())[
            "amount_targets"
        ]
        check(
            {(r["symbol"], r["date"], r["amount"]) for r in amount_fills}
            == {(r["symbol"], r["date"], r["amount"]) for r in targets},
            "three exact alias amount fills",
        )
        for symbol, day in (("sz002647", "2025-03-12"), ("sh600656", "2016-05-12")):
            row = store.get_price(security(symbol), day, day, mode="compatible").iloc[0]
            check(
                row.amount is None
                and row.volume is None
                and row.trade_state == "status_conflict"
                and row.status_evidence
                and not row.filled_fields,
                "status counterexample: " + symbol,
            )
        # One real native-field fill and one real per-day fallback use the same public API.
        ordinary = store.get_price("600656.XSHG", "2016-05-10", "2016-05-11", source="sina")
        completed = store.get_price(
            "600656.XSHG", "2016-05-10", "2016-05-11", source="sina", mode="compatible"
        )
        ordinary_fill = dict(
            before_amount_present=int(ordinary.amount.notna().sum()),
            after_amount_present=int(completed.amount.notna().sum()),
            coverage=completed.attrs["coverage"],
        )
        check(
            ordinary_fill["after_amount_present"] > ordinary_fill["before_amount_present"]
            and ordinary_fill["coverage"]["filled_fields"].get("amount") == 2,
            "real native-source field completion",
        )
        fallback_before = store.get_price("600000.XSHG", "2026-09-01", "2026-09-30", source="sina")
        fallback_after = store.get_price(
            "600000.XSHG", "2026-09-01", "2026-09-30", source="sina", mode="compatible"
        )
        day_fallback = dict(
            before_rows=len(fallback_before),
            after_rows=len(fallback_after),
            coverage=fallback_after.attrs["coverage"],
        )
        check(
            day_fallback["before_rows"] == 0
            and day_fallback["after_rows"] == 21
            and day_fallback["coverage"]["completion_counts"] == {"fallback_day": 21},
            "real native-source per-day fallback",
        )
        whole = store.get_price(
            "601313.XSHG", "2012-01-16", "2018-02-27", mode="compatible", alias_captures=[capture]
        )
        whole_single = store.get_price("601313.XSHG", "2012-01-16", "2018-02-27")
        whole_comparison = dict(
            before_source=whole_single.attrs["source"],
            before_rows=len(whole_single),
            after_rows=len(whole),
            before_amount_present=int(whole_single.amount.notna().sum()),
            after_amount_present=int(whole.amount.notna().sum()),
        )
        check(len(whole.attrs["unfilled_days"]) == 118, "full alias interval native halt gaps")
        # The known 2015-12-03 close difference must remain a conflict, not a splice.
        difference = whole[whole.date == "2015-12-03"].iloc[0]
        check(
            any(
                c.get("field") == "close" and c["kind"] == "value_conflict"
                for c in difference.conflicts
            ),
            "real alias/native close disagreement disclosed",
        )
        check(
            all(
                p is None or p["source"] != "baostock_alias"
                for p in difference.field_sources.values()
            ),
            "real alias/native close disagreement prevents alias field splice",
        )
        boundary = store.get_price(
            "601313.XSHG", "2018-02-28", "2018-03-01", mode="compatible", alias_captures=[capture]
        )
        check(boundary.empty, "old-code exclusive validity boundary")
        check(
            observations_before.hexdigest() == observations_after.hexdigest(), "raw rows unchanged"
        )
        check(store.db.total_changes == 0, "zero database writes")
        result = dict(
            passed=not failures,
            checks=checks,
            failures=failures,
            root=str(root),
            package_file=ashare_data.__file__,
            input_sha256=inputs,
            amount_inventory=dict(amount_summary),
            amount_classes=dict(classes),
            amount_fills=amount_fills,
            day_inventory=dict(gap_summary),
            ordinary_native_completion=ordinary_fill,
            ordinary_native_day_fallback=day_fallback,
            full_old_code_coverage=whole.attrs["coverage"],
            full_old_code_query_comparison=whole_comparison,
            source_row_digest_before=observations_before.hexdigest(),
            source_row_digest_after=observations_after.hexdigest(),
            database_changes=store.db.total_changes,
            network_requests=0,
            policy_version=whole.attrs["policy_version"],
            duration_seconds=round(time.monotonic() - started, 3),
        )
    finally:
        store.close()
    check(
        before == (database.stat().st_size, database.stat().st_mtime_ns), "database file unchanged"
    )
    result.update(
        passed=not failures,
        checks=checks,
        database_file_unchanged=before == (database.stat().st_size, database.stat().st_mtime_ns),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output.with_suffix(".csv")
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    result["detail_csv"] = str(csv_path)
    result["detail_csv_sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--alias-capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    def no_network(*args, **kwargs):
        raise RuntimeError("acceptance is offline; network access forbidden")

    socket.socket.connect = no_network
    socket.create_connection = no_network
    result = run(
        args.root.expanduser().resolve(),
        args.alias_capture.expanduser().resolve(),
        args.output.expanduser().resolve(),
    )
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "passed",
                    "checks",
                    "failures",
                    "amount_inventory",
                    "day_inventory",
                    "duration_seconds",
                )
            },
            indent=2,
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
