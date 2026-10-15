#!/usr/bin/env python3
"""Count early source observations, proxy gaps, conflicts and factor versions."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from ashare_data.local_daily import DEFAULT_ROOT, LocalDailyStore, BAR_FIELDS
from ashare_data.local_early import STAGES, LIMITATIONS, write_json
from ashare_data.local_sources import now


def writer(path, fields):
    handle = path.open("w")
    out = csv.DictWriter(handle, fieldnames=fields)
    out.writeheader()
    return handle, out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    store = LocalDailyStore(args.root, readonly=True)
    store.db.execute("BEGIN")
    handles = []

    def table(name, fields):
        handle, out = writer(args.output / (name + ".csv"), fields)
        handles.append(handle)
        return out

    coverage = table(
        "coverage",
        [
            "stage",
            "symbol",
            "candidate_ipo",
            "candidate_out",
            "early_exit",
            "http_rows",
            "http_unique_days",
            "inside_episode_observed_days",
            "first",
            "last",
            "weekend_days",
            "proxy_expected_days",
            "proxy_gap_days",
            "bao_diagnostic_rows",
            "invalid_http_diagnostic_rows",
            "amount_present_days",
            "factor_versions",
        ],
    )
    gaps = table("proxy-gaps", ["stage", "symbol", "date", "classification", "calendar_scope"])
    source_ranges = table(
        "source-ranges",
        [
            "stage",
            "symbol",
            "source",
            "rows",
            "first",
            "last",
            "weekend_rows",
            "amount_missing",
            "http_rows",
            "diagnostic_rows",
            "status",
        ],
    )
    conflicts = table(
        "source-conflicts",
        [
            "symbol",
            "date",
            "left_source",
            "right_source",
            "fields",
            "close_ratio_left_right",
            "volume_ratio_left_right",
            "bao_diagnostic",
            "left_quality",
            "right_quality",
            "price_difference",
            "volume_abs_difference",
        ],
    )
    problems = table(
        "value-problems",
        [
            "symbol",
            "date",
            "source",
            "issue",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "quality",
        ],
    )
    factors = table(
        "factor-ranges",
        [
            "symbol",
            "version",
            "source",
            "response_id",
            "observed_at",
            "baseline_date",
            "pre2010_dated_entries",
            "first_dated_entry",
            "last_dated_entry",
            "pre2010_change_entries",
            "first_change",
            "last_change",
            "basis",
        ],
    )
    boundaries = table("outside-episode", ["symbol", "date", "source", "reason"])
    jobs = table(
        "jobs",
        [
            "stage",
            "symbol",
            "requested_source",
            "start",
            "end",
            "state",
            "attempts",
            "nrows",
            "error",
            "quality",
            "replayed",
            "capture_path",
            "response_ids",
        ],
    )
    for row in store.db.execute(
        "SELECT e.stage,j.symbol,e.requested_source,j.start,j.end,j.state,j.attempts,"
        "j.nrows,j.error,e.quality,e.replayed,e.capture_path,j.response_ids "
        "FROM early_jobs e JOIN jobs j USING(id) ORDER BY e.stage,j.symbol,e.requested_source,j.start"
    ):
        jobs.writerow(dict(row))
    aggregate = {stage: Counter() for stage in STAGES}
    global_counts = Counter()
    price_ratios, volume_ratios = Counter(), Counter()
    early_exits, empty_exits = [], []
    symbols = [
        r[0] for r in store.db.execute("SELECT DISTINCT symbol FROM early_episodes ORDER BY symbol")
    ]
    calendar = [
        r[0]
        for r in store.db.execute(
            "SELECT date FROM early_calendar_observations "
            "WHERE exchange='SH' AND source='sina_trading_dates_sh' ORDER BY date"
        )
    ]
    total_source = Counter()
    diagnostic_source = Counter()
    for index, symbol in enumerate(symbols, 1):
        episodes = list(
            store.db.execute("SELECT ipo,out FROM early_episodes WHERE symbol=?", (symbol,))
        )
        early_exit = bool(episodes and all(out and out < "2010-01-01" for ipo, out in episodes))
        if early_exit:
            early_exits.append(symbol)

        def inside(day):
            return any(ipo <= day and (not out or day < out) for ipo, out in episodes)

        bars = [
            dict(r)
            for r in store.db.execute(
                "SELECT * FROM bars WHERE symbol=? AND date<'2010-01-01' ORDER BY date,source",
                (symbol,),
            )
        ]
        diagnostic = [
            dict(json.loads(r["body"]), source=r["source"], quality=r["quality"])
            for r in store.db.execute(
                "SELECT * FROM early_diagnostics WHERE symbol=? ORDER BY date,source", (symbol,)
            )
        ]
        if early_exit and not bars:
            empty_exits.append(symbol)
        versions = list(
            store.db.execute(
                "SELECT * FROM factor_versions WHERE symbol=? ORDER BY source,first_observed_at",
                (symbol,),
            )
        )
        for version in versions:
            entries = list(
                store.db.execute(
                    "SELECT date,multiplier FROM factors WHERE version=? AND date<'2010-01-01' ORDER BY date",
                    (version["id"],),
                )
            )
            dates = [r[0] for r in entries]
            events = [d for d in dates if d > "1900-01-01"]
            changes = [
                entries[i][0]
                for i in range(1, len(entries))
                if Decimal(entries[i][1]) != Decimal(entries[i - 1][1])
            ]
            factors.writerow(
                dict(
                    symbol=symbol,
                    version=version["id"],
                    source=version["source"],
                    response_id=version["response_id"],
                    observed_at=version["last_observed_at"],
                    baseline_date=dates[0] if dates else None,
                    pre2010_dated_entries=len(events),
                    first_dated_entry=events[0] if events else None,
                    last_dated_entry=events[-1] if events else None,
                    pre2010_change_entries=len(changes),
                    first_change=changes[0] if changes else None,
                    last_change=changes[-1] if changes else None,
                    basis=version["basis"],
                )
            )
            global_counts["pre2010_factor_rows"] += len(dates)
            global_counts["pre2010_factor_change_entries_not_certified_actions"] += len(changes)
        for stage, (start, end) in STAGES.items():
            selected = [r for r in bars if start <= r["date"] <= end]
            diag = [r for r in diagnostic if start <= r["date"] <= end]
            bao = [r for r in diag if r["source"] == "baostock"]
            invalid_http = [r for r in diag if r["source"] != "baostock"]
            expected = {d for d in calendar if start <= d <= end and inside(d)}
            observed = {r["date"] for r in selected}
            if not expected and not selected and not diag:
                continue
            missing = expected - observed
            bao_by_date = {r["date"]: r for r in bao}
            invalid_days = {r["date"] for r in invalid_http}
            for day in sorted(missing):
                b = bao_by_date.get(day)
                kind = "no_http_observation"
                if day in invalid_days:
                    kind = "invalid_http_row_quarantined"
                if b:
                    kind = (
                        "unverified_baostock_halt_claim"
                        if b.get("tradestatus") == "0"
                        else "unverified_baostock_activity_claim"
                    )
                gaps.writerow(
                    dict(
                        stage=stage,
                        symbol=symbol,
                        date=day,
                        classification=kind,
                        calendar_scope="Shanghai_provider_proxy; Shenzhen_early_weekends_and_PT_eligibility_unverified",
                    )
                )
            amount_days = {r["date"] for r in selected if r.get("amount") is not None}
            weekend = {d for d in observed if date.fromisoformat(d).weekday() >= 5}
            coverage.writerow(
                dict(
                    stage=stage,
                    symbol=symbol,
                    candidate_ipo=min(r[0] for r in episodes),
                    candidate_out=max((r[1] or "") for r in episodes),
                    early_exit=early_exit,
                    http_rows=len(selected),
                    http_unique_days=len(observed),
                    inside_episode_observed_days=sum(inside(d) for d in observed),
                    first=min(observed) if observed else None,
                    last=max(observed) if observed else None,
                    weekend_days=len(weekend),
                    proxy_expected_days=len(expected),
                    proxy_gap_days=len(missing),
                    bao_diagnostic_rows=len(bao),
                    invalid_http_diagnostic_rows=len(invalid_http),
                    amount_present_days=len(amount_days),
                    factor_versions=len(versions),
                )
            )
            for source in sorted({r["source"] for r in selected + diag}):
                source_rows = [r for r in selected + diag if r["source"] == source]
                diagnostic_rows = sum(r["source"] == source for r in diag)
                days = [r["date"] for r in source_rows]
                source_ranges.writerow(
                    dict(
                        stage=stage,
                        symbol=symbol,
                        source=source,
                        rows=len(source_rows),
                        first=min(days),
                        last=max(days),
                        weekend_rows=sum(date.fromisoformat(d).weekday() >= 5 for d in days),
                        amount_missing=sum(r.get("amount") is None for r in source_rows),
                        http_rows=len(source_rows) - diagnostic_rows,
                        diagnostic_rows=diagnostic_rows,
                        status="diagnostic_validation_unknown"
                        if source == "baostock"
                        else (
                            "schema_parsed_with_invalid_rows_quarantined"
                            if diagnostic_rows
                            else "schema_parsed_research"
                        ),
                    )
                )
                total_source[(stage, source)] += len(source_rows)
                diagnostic_source[(stage, source)] += diagnostic_rows
            c = aggregate[stage]
            c.update(
                dict(
                    candidate_symbols=1,
                    symbols_with_http=bool(selected),
                    http_rows=len(selected),
                    http_unique_security_days=len(observed),
                    inside_episode_observed_days=sum(inside(d) for d in observed),
                    proxy_expected_days=len(expected),
                    proxy_gap_days=len(missing),
                    bao_diagnostic_rows=len(bao),
                    invalid_http_diagnostic_rows=len(invalid_http),
                    weekend_security_days=len(weekend),
                    amount_present_security_days=len(amount_days),
                    early_exit_symbols_with_http=bool(early_exit and selected),
                )
            )
        byday = defaultdict(list)
        for row in bars + diagnostic:
            day, source = row["date"], row["source"]
            byday[day].append(row)
            if not inside(day):
                boundaries.writerow(
                    dict(
                        symbol=symbol,
                        date=day,
                        source=source,
                        reason="outside_candidate_episode_identity_or_stage_unknown",
                    )
                )
                global_counts["outside_episode_source_rows"] += 1
            issues = []
            nums = {
                k: Decimal(row[k])
                for k in ("open", "high", "low", "close", "volume", "amount")
                if row.get(k) is not None
            }
            if all(k in nums for k in ("open", "high", "low", "close")):
                if (
                    not 0
                    < nums["low"]
                    <= min(nums["open"], nums["close"])
                    <= max(nums["open"], nums["close"])
                    <= nums["high"]
                ):
                    issues.append("invalid_or_placeholder_ohlc")
            else:
                issues.append("missing_ohlc")
            if nums.get("amount", 0) < 0:
                issues.append("negative_source_amount_quarantined")
            if nums.get("volume", 0) < 0:
                issues.append("negative_source_volume_quarantined")
            if all(k in nums for k in ("low", "high", "volume", "amount")) and nums["volume"] > 0:
                average = nums["amount"] / nums["volume"]
                # Tolerance describes display precision only, never modifies data.
                if not nums["low"] - Decimal("0.01") <= average <= nums["high"] + Decimal("0.01"):
                    issues.append("amount_volume_average_outside_ohlc")
            for issue in issues:
                problems.writerow(
                    dict(
                        symbol=symbol,
                        date=day,
                        source=source,
                        issue=issue,
                        **{
                            k: row.get(k)
                            for k in ("open", "high", "low", "close", "volume", "amount")
                        },
                        quality=row.get("quality", "http_schema_parsed"),
                    )
                )
                global_counts[issue + ":" + source] += 1
        for day, rows in byday.items():
            for i, left in enumerate(rows):
                for right in rows[i + 1 :]:
                    different = [
                        k
                        for k in BAR_FIELDS
                        if left.get(k) is not None
                        and right.get(k) is not None
                        and Decimal(left[k]) != Decimal(right[k])
                    ]
                    if not different:
                        continue

                    def ratio(key):
                        if (
                            left.get(key) is None
                            or right.get(key) is None
                            or Decimal(right[key]) == 0
                        ):
                            return None
                        return Decimal(left[key]) / Decimal(right[key])

                    cr, vr = ratio("close"), ratio("volume")
                    price_difference = any(k in different for k in ("open", "high", "low", "close"))
                    volume_difference = (
                        abs(Decimal(left["volume"]) - Decimal(right["volume"]))
                        if left.get("volume") is not None and right.get("volume") is not None
                        else None
                    )
                    conflicts.writerow(
                        dict(
                            symbol=symbol,
                            date=day,
                            left_source=left["source"],
                            right_source=right["source"],
                            fields="|".join(different),
                            close_ratio_left_right=str(cr) if cr is not None else None,
                            volume_ratio_left_right=str(vr) if vr is not None else None,
                            bao_diagnostic="baostock" in (left["source"], right["source"]),
                            left_quality=left.get("quality", "http_schema_parsed_research"),
                            right_quality=right.get("quality", "http_schema_parsed_research"),
                            price_difference=price_difference,
                            volume_abs_difference=str(volume_difference)
                            if volume_difference is not None
                            else None,
                        )
                    )
                    global_counts["conflicting_source_pairs"] += 1
                    global_counts[
                        "pairs_with_price_difference"
                        if price_difference
                        else "pairs_only_nonprice_difference"
                    ] += 1
                    global_counts["difference_fields:" + "|".join(different)] += 1
                    if volume_difference is not None and 0 < volume_difference < 100:
                        global_counts[
                            "volume_difference_below_100_display_units_not_a_validation_rule"
                        ] += 1
                    if cr in (Decimal(10), Decimal("0.1")):
                        price_ratios[
                            (symbol, day[:4], left["source"], right["source"], str(cr))
                        ] += 1
                    if vr in (Decimal(5), Decimal("0.2")):
                        volume_ratios[
                            (symbol, day[:4], left["source"], right["source"], str(vr))
                        ] += 1
        if index % 100 == 0:
            print(json.dumps(dict(at=now(), progress=index, total=len(symbols))), flush=True)
    for handle in handles:
        handle.close()
    states = [
        dict(r)
        for r in store.db.execute(
            "SELECT e.stage,e.requested_source,j.state,count(*) jobs,sum(j.nrows) rows "
            "FROM early_jobs e JOIN jobs j USING(id) GROUP BY 1,2,3 ORDER BY 1,2,3"
        )
    ]
    summary = dict(
        at=now(),
        limitations=LIMITATIONS,
        stages=aggregate,
        global_counts=global_counts,
        source_rows=[
            dict(
                stage=a,
                source=b,
                rows=n,
                http_rows=n - diagnostic_source[(a, b)],
                diagnostic_rows=diagnostic_source[(a, b)],
            )
            for (a, b), n in sorted(total_source.items())
        ],
        candidate_symbols=len(symbols),
        early_exits=len(early_exits),
        early_exit_symbols=early_exits,
        early_exits_without_http=empty_exits,
        jobs=states,
        exact_10x_close=[
            dict(symbol=a, year=b, left=c, right=d, ratio=e, days=n)
            for (a, b, c, d, e), n in sorted(price_ratios.items())
        ],
        exact_5x_volume=[
            dict(symbol=a, year=b, left=c, right=d, ratio=e, days=n)
            for (a, b, c, d, e), n in sorted(volume_ratios.items())
        ],
    )
    store.db.rollback()
    store.close()
    summary["files"] = {
        p.name: dict(
            bytes=p.stat().st_size, sha256=hashlib.file_digest(p.open("rb"), "sha256").hexdigest()
        )
        for p in args.output.glob("*.csv")
    }
    write_json(args.output / "summary.json", summary)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "at",
                    "candidate_symbols",
                    "early_exits",
                    "early_exits_without_http",
                    "stages",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
