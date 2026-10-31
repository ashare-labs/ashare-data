"""Compare captured minute bars without inferring exchange trading timestamps.

Tests both left- and right-labelled buckets using exact Decimal OHLCV/hold.
Incomplete, missing and current-day buckets remain explicit exclusions. Empirical
agreement cannot establish the supplier's label convention or continuous roll.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path


FIELDS = ("open", "high", "low", "close", "volume", "hold")


def aggregate(rows):
    return dict(open=Decimal(rows[0]["open"]), high=max(Decimal(r["high"]) for r in rows),
                low=min(Decimal(r["low"]) for r in rows), close=Decimal(rows[-1]["close"]),
                volume=sum(Decimal(r["volume"]) for r in rows), hold=Decimal(rows[-1]["hold"]))


def compare(one, wider, period, closed_before):
    index = {datetime.fromisoformat(r["datetime"]): r for r in one}
    if len(index) != len(one) or not one:
        raise ValueError("one-minute labels empty or duplicated")
    results = {}
    for hypothesis in ("right_closed", "left_closed"):
        stats, mismatch_fields, examples, skipped = Counter(), Counter(), [], Counter()
        for bar in wider:
            stamp = datetime.fromisoformat(bar["datetime"])
            if stamp.date().isoformat() >= closed_before:
                skipped["current_or_later_calendar_day"] += 1
                continue
            offsets = range(-period + 1, 1) if hypothesis == "right_closed" else range(period)
            labels = [stamp + timedelta(minutes=offset) for offset in offsets]
            if labels[0] < min(index) or labels[-1] > max(index):
                skipped["outside_captured_one_minute_window"] += 1
                continue
            missing = [label for label in labels if label not in index]
            if missing:
                # May be a session break or an absent source label; never fill it.
                skipped["incomplete_bucket_including_session_breaks"] += 1
                continue
            calculated = aggregate([index[label] for label in labels])
            different = [field for field in FIELDS if calculated[field] != Decimal(bar[field])]
            stats["compared"] += 1
            stats["night_compared"] += int(stamp.hour >= 18 or stamp.hour < 6)
            stats["exact_matches"] += int(not different)
            if different:
                mismatch_fields.update(different)
                if len(examples) < 4:
                    examples.append({"source_label": bar["datetime"], "different_fields": different,
                                     "calculated": {k: str(calculated[k]) for k in different},
                                     "observed": {k: bar[k] for k in different}})
        results[hypothesis] = {**dict(stats), "mismatch_fields": dict(mismatch_fields),
                               "examples": examples, "excluded": dict(skipped)}
    return results


def audit(root, closed_before):
    from ashare_data.providers.free_updates.free_update_futures_minutes import parse
    captures, bindings = {}, []
    for file in sorted((root / "runs").glob("*.json")):
        value = json.loads(file.read_text())
        rows = value.get("records", [])
        if not rows or "source_period_minutes" not in rows[0]:
            continue
        symbol, period = rows[0]["source_symbol"], rows[0]["source_period_minutes"]
        key = (symbol, period)
        if key in captures:
            raise ValueError("multiple captures for context; choose an immutable input root")
        receipts = value["receipts"]
        if len(receipts) != 1:
            raise ValueError("unexpected minute receipt count")
        receipt = receipts[0]
        body = gzip.decompress((root / "raw" / (receipt["sha256"] + ".gz")).read_bytes())
        if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
            raise ValueError("raw hash mismatch")
        if parse(body, {"symbol": symbol, "period": str(period)}) != rows:
            raise ValueError("native projection differs from captured records")
        captures[key] = rows
        bindings.append({"run": str(file), "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                         "raw_sha256": receipt["sha256"], "symbol": symbol, "period": period})
    comparisons = []
    for (symbol, period), rows in captures.items():
        if period > 1 and (symbol, 1) in captures:
            comparisons.append({"symbol": symbol, "period": period,
                                "hypotheses": compare(captures[symbol, 1], rows, period, closed_before)})
    symbols = sorted({symbol for symbol, period in captures if period == 1})
    cross_symbol = []
    for left, right in zip(symbols, symbols[1:]):
        a = {r["datetime"]: r for r in captures[left, 1] if r["datetime"][:10] < closed_before}
        b = {r["datetime"]: r for r in captures[right, 1] if r["datetime"][:10] < closed_before}
        overlap = sorted(a.keys() & b.keys())
        differences = [{"source_label": label,
                        "fields": [f for f in FIELDS if Decimal(a[label][f]) != Decimal(b[label][f])]}
                       for label in overlap]
        differences = [r for r in differences if r["fields"]]
        cross_symbol.append({"left": left, "right": right, "overlapping_labels": len(overlap),
                             "different_bars": len(differences), "examples": differences[:5],
                             "only_left_labels": sorted(a.keys()-b.keys()),
                             "only_right_labels": sorted(b.keys()-a.keys())})
    return {"schema": "ad.futures-minute-alignment-audit.v1", "raw_hashes_and_projection_checked": True,
            "closed_before": closed_before, "bindings": bindings, "comparisons": comparisons,
            "cross_symbol_comparison": cross_symbol,
            "label_counts_by_calendar_day": {s: dict(Counter(r["datetime"][:10] for r in captures[s, 1])) for s in symbols},
            "trading_date_inferred": False, "timestamp_inferred": False, "acceptance_upgraded": False,
            "limitation": "Exact sample agreement is a consistency test, not a supplier label/timezone definition; no missing bars filled"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--closed-before", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.root.resolve(), args.closed_before)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"captures": len(result["bindings"]), "comparisons": len(result["comparisons"]),
                      "acceptance_upgraded": False}))


if __name__ == "__main__":
    main()
