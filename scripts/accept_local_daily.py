#!/usr/bin/env python3
"""Reconcile security-day coverage and inspect stratified raw-data samples offline.

The denominator is re-derived from retained listing facts and code events without
calling the collection planner/coverage helper. No quote rows are changed. A
missing session is never treated as a halt merely because providers omit it.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
from itertools import combinations
import json
from pathlib import Path
import re

from ashare_data.storage.local_daily import DEFAULT_ROOT, LocalDailyStore
from ashare_data.providers.daily_sources import now
from ashare_data.reference.status_evidence import (
    issuer_status_evidence,
    exchange_republication_evidence,
)


SAMPLES = {
    "long_history_shanghai": ["sh600000", "sh600030"],
    "long_history_shenzhen": ["sz000001", "sz000004"],
    "merger_or_delisting": ["sh600003", "sh600005", "sz000033", "sz000787"],
    "relisted_new_code": ["sh600087", "sh601975", "sh601268", "sh601399"],
    "quote_code_change": [
        "sh600849",
        "sh601607",
        "sh601313",
        "sh601360",
        "sz000022",
        "sz001872",
        "sz000043",
        "sz001914",
    ],
    "long_suspension": ["sz000029", "sz000557", "sz000670", "sz000688"],
    "former_sme": ["sz002018", "sz002024", "sz003000"],
}
PRICE_FIELDS = ("open", "high", "low", "close")


def expected_dates(symbol, days, facts, events):
    """Independent interval-union calculation for the registered listing lifespan."""
    latest = {}
    for row in sorted(
        facts,
        key=lambda r: (
            json.loads(r["body"]).get("source") == "issuer_exit_notice",
            r["observed_at"],
            r["id"],
        ),
    ):
        latest[row["ipo"]] = row
    result = set()
    for row in latest.values():
        inclusive = json.loads(row["body"]).get("out_inclusive", False)
        result.update(
            d
            for d in days
            if row["ipo"] <= d
            and (not row["out"] or d < row["out"] or (inclusive and d == row["out"]))
        )
    for event in events:
        if event["new_symbol"] == symbol:
            result = {d for d in result if d >= event["effective_date"]}
        if event["old_symbol"] == symbol:
            result = {d for d in result if d < event["effective_date"]}
    return result


def date_intervals(values, calendar_index):
    groups = []
    for day in sorted(values):
        if groups and calendar_index[day] == calendar_index[groups[-1][-1]] + 1:
            groups[-1].append(day)
        else:
            groups.append([day])
    return [dict(start=g[0], end=g[-1], security_days=len(g)) for g in groups]


def compare_sources(symbol, by_source, allowed):
    result = []
    for left, right in combinations(sorted(by_source), 2):
        a, b = by_source[left], by_source[right]
        common = sorted(set(a) & set(b) & allowed)
        comparable = [
            d for d in common if a[d]["tradestatus"] != "0" and b[d]["tradestatus"] != "0"
        ]
        different = Counter()
        max_diffs = defaultdict(lambda: Decimal(0))
        examples = []
        for day in comparable:
            for field in (*PRICE_FIELDS, "volume", "amount"):
                av, bv = a[day][field], b[day][field]
                if av is None or bv is None:
                    continue
                delta = abs(Decimal(av) - Decimal(bv))
                tolerance = Decimal("0.000001") if field in PRICE_FIELDS else Decimal(100)
                max_diffs[field] = max(max_diffs[field], delta)
                if delta > tolerance:
                    different[field] += 1
                    if len(examples) < 12:
                        examples.append(
                            dict(
                                date=day,
                                field=field,
                                left=av,
                                right=bv,
                                absolute_difference=str(delta),
                            )
                        )
        result.append(
            dict(
                symbol=symbol,
                left=left,
                right=right,
                shared_expected_days=len(common),
                compared_nonhalt_days=len(comparable),
                differing_fields_over_tolerance=dict(different),
                max_absolute_differences={k: str(v) for k, v in max_diffs.items()},
                examples=examples,
            )
        )
    return result


def factor_audit(store, sample_symbols):
    errors, samples = [], []
    total, raw_checked = 0, 0
    code_ends = {e["old_symbol"]: e["effective_date"] for e in store.code_events()}
    alias_continuations = []
    for version in store.db.execute("SELECT * FROM factor_versions ORDER BY symbol,id"):
        rows = [
            dict(r)
            for r in store.db.execute(
                "SELECT date,multiplier FROM factors WHERE version=? ORDER BY date",
                (version["id"],),
            )
        ]
        total += len(rows)
        code_end = code_ends.get(version["symbol"])
        later = [r["date"] for r in rows if code_end and r["date"] >= code_end]
        if later:
            alias_continuations.append(
                dict(
                    symbol=version["symbol"],
                    version=version["id"],
                    code_valid_until_exclusive=code_end,
                    raw_source_dates_after_code_end=later,
                )
            )
        if len(rows) != version["nrows"]:
            errors.append(dict(symbol=version["symbol"], error="version row count mismatch"))
        for row in rows:
            try:
                date.fromisoformat(row["date"])
                value = Decimal(row["multiplier"])
                if not value.is_finite() or value <= 0:
                    raise ValueError("nonpositive or nonfinite factor")
            except (ValueError, InvalidOperation) as error:
                errors.append(dict(symbol=version["symbol"], date=row["date"], error=str(error)))
        version_body = dict(
            symbol=version["symbol"],
            source=version["source"],
            rows=[dict(date=r["date"], factor=r["multiplier"]) for r in rows],
        )
        version_hash = hashlib.sha256(
            json.dumps(
                version_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        if version_hash != version["id"]:
            errors.append(dict(symbol=version["symbol"], error="factor version identity mismatch"))
        receipt = store.db.execute(
            "SELECT * FROM responses WHERE id=?", (version["response_id"],)
        ).fetchone()
        item = dict(
            symbol=version["symbol"],
            version=version["id"],
            source=version["source"],
            observed_at=version["first_observed_at"],
            rows=len(rows),
            first_date=rows[0]["date"] if rows else None,
            last_date=rows[-1]["date"] if rows else None,
            source_url=receipt["url"] if receipt else None,
            raw_equals_stored=False,
            code_valid_until_exclusive=code_end,
            source_rows_after_code_end=len(later),
        )
        try:
            if not receipt or not receipt["sha256"]:
                raise ValueError("factor receipt absent")
            raw = gzip.decompress((store.root / "raw" / (receipt["sha256"] + ".gz")).read_bytes())
            if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
                raise ValueError("factor raw hash mismatch")
            text = raw.decode("utf-8")
            match = re.search(r"\bvar\s+" + version["symbol"] + r"hfq\s*=\s*", text)
            if not match:
                raise ValueError("factor source identity mismatch")
            parsed, _ = json.JSONDecoder().raw_decode(text[match.end() :])
            source = sorted((r["d"], Decimal(r["f"])) for r in parsed["data"])
            item["raw_equals_stored"] = int(parsed["total"]) == len(rows) and source == [
                (r["date"], Decimal(r["multiplier"])) for r in rows
            ]
            item["raw_sha256"] = receipt["sha256"]
            if not item["raw_equals_stored"]:
                raise ValueError("factor raw/stored mismatch")
            raw_checked += 1
        except (OSError, ValueError, InvalidOperation, KeyError) as error:
            errors.append(dict(symbol=version["symbol"], error=str(error)))
        if version["symbol"] in sample_symbols:
            samples.append(item)
    return dict(
        rows_checked=total,
        raw_snapshots_checked=raw_checked,
        provider_alias_continuations=alias_continuations,
        validation_errors=errors,
        samples=samples,
        interpretation="Positive/versioned source factors; raw identity is checked. This does not certify completeness of corporate-action events or historical publication times.",
    )


def audit(root):
    store = LocalDailyStore(root, readonly=True)
    try:
        store.db.execute("BEGIN")
        result = dict(at=now(), scope_start=store.meta("start"), scope_end=store.meta("end"))
        status_evidence, status_errors = issuer_status_evidence(store.root)
        republications, republication_errors = exchange_republication_evidence(store.root)
        status_evidence.extend(republications)
        batch_path = store.root / "batch-state.json"
        batch = json.loads(batch_path.read_text()) if batch_path.exists() else {}
        batch_reports_running = batch.get("outcome") == "running"
        days = [
            r[0]
            for r in store.db.execute(
                "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
                (result["scope_start"], result["scope_end"]),
            )
        ]
        instruments = [
            dict(r) for r in store.db.execute("SELECT * FROM instruments ORDER BY symbol")
        ]
        facts = defaultdict(list)
        for row in store.db.execute("SELECT * FROM listing_facts"):
            facts[row["symbol"]].append(dict(row))
        events = store.code_events()
        code_exits = {e["old_symbol"] for e in events}
        samples = dict(SAMPLES)
        samples["recent_ipo"] = [
            r["symbol"]
            for r in sorted(
                (i for i in instruments if not i["out"]), key=lambda r: r["ipo"], reverse=True
            )[:3]
        ]
        sample_symbols = {s for group in samples.values() for s in group}
        counts = Counter()
        comparisons, sample_details, gap_details, amount_gaps = [], [], [], []
        amount_dispositions, amount_classes = [], Counter()
        rows_by_source = Counter()
        gap_dispositions, gap_classes = [], Counter()
        calendar_index = {d: i for i, d in enumerate(days)}
        monthly_days = [
            r[0]
            for r in store.db.execute("SELECT DISTINCT date FROM universe_membership ORDER BY date")
        ]
        expected_monthly = {d: set() for d in monthly_days}
        for instrument in instruments:
            symbol = instrument["symbol"]
            expected = expected_dates(symbol, days, facts[symbol], events)
            main = {
                d
                for d in expected
                if not symbol.startswith(("sz002", "sz003")) or d >= "2021-04-06"
            }
            for day in monthly_days:
                if day in main:
                    expected_monthly[day].add(symbol)
            by_source = defaultdict(dict)
            for row in store.db.execute(
                "SELECT * FROM bars WHERE symbol=? ORDER BY date,source", (symbol,)
            ):
                by_source[row["source"]][row["date"]] = dict(row)
                rows_by_source[row["source"]] += 1
            observed = {d for source in by_source.values() for d in source}
            amount = {
                d
                for source in by_source.values()
                for d, row in source.items()
                if row["amount"] is not None
            }
            halted = {
                d
                for source in by_source.values()
                for d, row in source.items()
                if row["tradestatus"] == "0"
            }
            active = {
                d
                for source in by_source.values()
                for d, row in source.items()
                if row["tradestatus"] == "1"
            }
            reported_activity = active | {
                d
                for source in by_source.values()
                for d, row in source.items()
                if row["volume"] is not None and Decimal(row["volume"]) > 0
            }
            counts.update(
                dict(
                    raw_unique_security_days=len(observed),
                    lineage_expected=len(expected),
                    lineage_observed=len(expected & observed),
                    main_securities=bool(main),
                    main_delisted_securities=bool(
                        main and instrument["out"] and symbol not in code_exits
                    ),
                    main_expected=len(main),
                    main_observed=len(main & observed),
                    main_missing=len(main - observed),
                    main_amount_missing=len((main & observed) - amount),
                    main_explicit_halt_observations=len(main & halted),
                    main_active_flag_observations=len(main & active),
                    main_trade_status_conflicts=len(main & halted & active),
                )
            )
            missing = sorted(main - observed)
            missing_amount = (main & observed) - amount
            if missing_amount:
                amount_groups = {
                    "explicit_source_halt_without_amount": missing_amount
                    & (halted - reported_activity),
                    "reported_trading_activity_without_amount": missing_amount
                    & (reported_activity - halted),
                    "source_halt_activity_conflict_without_amount": missing_amount
                    & halted
                    & reported_activity,
                    "no_explicit_status_or_activity_without_amount": missing_amount
                    - (halted | reported_activity),
                }
                for kind, dates in amount_groups.items():
                    if dates:
                        amount_classes[kind] += len(dates)
                        amount_dispositions.extend(
                            dict(symbol=symbol, **interval, kind=kind)
                            for interval in date_intervals(dates, calendar_index)
                        )
                amount_gaps.append(
                    dict(
                        symbol=symbol,
                        days=len(missing_amount),
                        intervals=date_intervals(missing_amount, calendar_index),
                    )
                )
            if missing:
                jobs = [
                    dict(r)
                    for r in store.db.execute(
                        "SELECT source,state,start,end,error FROM jobs WHERE symbol=? AND kind='bars'",
                        (symbol,),
                    )
                ]
                gap_details.append(
                    dict(
                        symbol=symbol, name=instrument["name"], missing_days=missing, requests=jobs
                    )
                )
                by_disposition = defaultdict(list)
                for day in missing:
                    proof = [
                        e
                        for e in status_evidence
                        if e["symbol"] == symbol and e["start"] <= day < e["end_exclusive"]
                    ]
                    issuer_proof = [e for e in proof if e["evidence_kind"] == "issuer_notice"]
                    proof = issuer_proof or proof
                    covering = [j for j in jobs if j["start"] <= day <= j["end"]]
                    hashes = sorted({r["sha256"] for e in proof for r in e["receipts"]})
                    if proof:
                        kind = (
                            "issuer_confirmed_nontrading_without_bar"
                            if any(e["evidence_kind"] == "issuer_notice" for e in proof)
                            else "exchange_notice_republication_nontrading_without_bar"
                        )
                    elif any(j["state"] in ("pending", "running") for j in covering):
                        kind = "awaiting_collection"
                    elif batch_reports_running and not any(
                        j["source"] == "baostock" and j["state"] in ("done", "empty_unknown")
                        for j in covering
                    ):
                        kind = "awaiting_bao_collection_or_import"
                    elif any(j["state"] in ("done", "empty_unknown") for j in covering):
                        kind = "providers_returned_no_bar_reason_unconfirmed"
                    elif covering:
                        kind = "acquisition_failed_or_blocked"
                    else:
                        kind = "no_recorded_request"
                    by_disposition[(kind, ";".join(hashes))].append(day)
                for (kind, hashes), dates in by_disposition.items():
                    gap_classes[kind] += len(dates)
                    gap_dispositions.extend(
                        dict(
                            symbol=symbol,
                            **interval,
                            kind=kind,
                            evidence_sha256=hashes,
                            issuer_notice_sha256=(
                                hashes if kind == "issuer_confirmed_nontrading_without_bar" else ""
                            ),
                        )
                        for interval in date_intervals(dates, calendar_index)
                    )
            if symbol in sample_symbols:
                sample_details.append(
                    dict(
                        symbol=symbol,
                        name=instrument["name"],
                        ipo=instrument["ipo"],
                        out=instrument["out"],
                        metadata_source=instrument["metadata_source"],
                        metadata_receipt=instrument["receipt"],
                        expected_days=len(main),
                        observed_days=len(main & observed),
                        missing_days=missing,
                        explicit_halt_days=len(main & halted),
                        amount_missing_days=sorted((main & observed) - amount),
                        source_rows={k: len(v) for k, v in by_source.items()},
                    )
                )
                comparisons.extend(compare_sources(symbol, by_source, main))
        membership_checks = []
        for day in monthly_days:
            raw = {
                r[0]
                for r in store.db.execute(
                    "SELECT DISTINCT symbol FROM universe_membership WHERE date=?", (day,)
                )
                if not r[0].startswith(("sz002", "sz003")) or day >= "2021-04-06"
            }
            normalized = set(raw)
            mappings = []
            for event in events:
                if day < event["effective_date"] and event["new_symbol"] in normalized:
                    normalized.remove(event["new_symbol"])
                    normalized.add(event["old_symbol"])
                    mappings.append(
                        dict(
                            source_symbol=event["new_symbol"],
                            historical_symbol=event["old_symbol"],
                            issuer_notice_sha256=event["receipt"],
                        )
                    )
            expected = expected_monthly[day]
            membership_checks.append(
                dict(
                    date=day,
                    expected_members=len(expected),
                    raw_source_members=len(raw),
                    normalized_source_members=len(normalized),
                    expected_but_not_observed=sorted(expected - normalized),
                    observed_outside_listing_or_code_bounds=sorted(normalized - expected),
                    verified_code_mappings=mappings,
                )
            )
        unknown_members = [
            dict(r)
            for r in store.db.execute(
                "SELECT m.symbol,min(m.date) first_seen,max(m.date) last_seen,count(*) observations "
                "FROM universe_membership m LEFT JOIN instruments i ON i.symbol=m.symbol "
                "WHERE i.symbol IS NULL GROUP BY m.symbol"
            )
        ]
        result.update(
            denominator_definition=(
                "Union of observed-calendar open days within retained listing episodes and verified quote-code lifetimes, "
                "intersected with main-board membership at date (002/003 only from 2021-04-06). "
                "The denominator includes suspended sessions within the registered listing lifespan. "
                "Observed coverage counts each symbol/date once, including explicitly labelled source halt placeholders; "
                "it is not a count of traded days. Source rows remain separately counted."
            ),
            counts=dict(counts),
            source_rows=dict(rows_by_source),
            physical_source_rows=sum(rows_by_source.values()),
            sample_strata=samples,
            samples=sample_details,
            comparisons=comparisons,
            comparison_tolerances={
                "prices_CNY": "0.000001",
                "volume_shares": "100",
                "amount_CNY": "100",
            },
            comparison_note="Tolerance is a reporting threshold for source precision, not a correctness certificate. Halts are excluded from price comparisons.",
            gaps=gap_details,
            gap_dispositions=gap_dispositions,
            gap_classes=dict(gap_classes),
            issuer_evidence_errors=status_errors,
            exchange_republication_evidence_errors=republication_errors,
            batch_reported_outcome=batch.get("outcome"),
            amount_gaps=amount_gaps,
            amount_gap_classes=dict(amount_classes),
            amount_gap_dispositions=amount_dispositions,
            amount_gap_interpretation=(
                "Missing amount means no retained source reports a value for an observed expected date. "
                "Explicit source halt flags are separated from positive volume or active trade flags; "
                "conflicting signals remain separate. Source NULL values are preserved, never replaced with zero."
            ),
            historical_membership=dict(
                snapshot_dates=len(monthly_days),
                unregistered_symbols=unknown_members,
                checks=membership_checks,
                name_is_historical=False,
                publication_point_in_time_certified=False,
            ),
            factors=factor_audit(store, sample_symbols),
        )
        return result
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", default="acceptance.json")
    args = parser.parse_args()
    result = audit(args.root)
    output = args.root / "reports" / args.output
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    disposition_path = output.with_name(output.stem + "-gap-dispositions.csv")
    with disposition_path.open("w") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "symbol",
                "start",
                "end",
                "security_days",
                "kind",
                "evidence_sha256",
                "issuer_notice_sha256",
            ],
        )
        writer.writeheader()
        writer.writerows(result["gap_dispositions"])
    with output.with_name(output.stem + "-amount-gap-dispositions.csv").open("w") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["symbol", "start", "end", "security_days", "kind"]
        )
        writer.writeheader()
        writer.writerows(result["amount_gap_dispositions"])
    print(
        json.dumps(
            {
                "output": str(output),
                "at": result["at"],
                "counts": result["counts"],
                "physical_source_rows": result["physical_source_rows"],
                "factor_errors": result["factors"]["validation_errors"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(
        bool(
            result["factors"]["validation_errors"]
            or result["issuer_evidence_errors"]
            or result["exchange_republication_evidence_errors"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
