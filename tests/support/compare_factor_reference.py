"""Compare explicit offline factor panels, including changes to available selections."""

import argparse
from collections import Counter
from fractions import Fraction as F
import hashlib
import json
import math
from pathlib import Path
import statistics
import struct


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def distribution(values):
    ordered = sorted(float(v) for v in values if v is not None)
    if not ordered:
        return None
    return {
        "p50": statistics.median(ordered),
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max": ordered[-1],
    }


def average_ranks(values):
    ordered = sorted(values)
    return [
        F(sum(i + 1 for i, x in enumerate(ordered) if x == v), ordered.count(v)) for v in values
    ]


def choose(values, k, high):
    return sorted(sorted(values, key=lambda s: ((-1 if high else 1) * values[s], s))[:k])


def metrics(local, reference, k):
    names = sorted(set(local) & set(reference))
    a, b = [local[s] for s in names], [reference[s] for s in names]
    x, y = average_ranks(a), average_ranks(b)
    mean = F(len(names) + 1, 2)
    vx, vy = sum((v - mean) ** 2 for v in x), sum((v - mean) ** 2 for v in y)
    rho = (
        float(sum((u - mean) * (v - mean) for u, v in zip(x, y))) / math.sqrt(float(vx * vy))
        if vx * vy
        else None
    )
    correct = F(0)
    comparable = reference_ties = local_ties = 0
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            da, db = a[i] - a[j], b[i] - b[j]
            if db == 0:
                reference_ties += 1
                continue
            comparable += 1
            if da == 0:
                correct += F(1, 2)
                local_ties += 1
            elif (da > 0) == (db > 0):
                correct += 1
    result = {
        "n": len(names),
        "spearman": rho,
        "pair_concordance": float(correct / comparable) if comparable else None,
        "reference_tied_pairs": reference_ties,
        "local_ties_on_nontied_reference": local_ties,
    }
    for tail, high in (("top", True), ("bottom", False)):
        la, rb = (
            choose({s: local[s] for s in names}, k, high),
            choose({s: reference[s] for s in names}, k, high),
        )
        result[tail] = {
            "local": la,
            "reference": rb,
            "overlap": len(set(la) & set(rb)) / k,
            "replacement_count": k - len(set(la) & set(rb)),
        }
    return result


def compare_rows(local_rows, reference_rows, scope):
    """Compare rows over scope queries; missing results must have value=None."""
    queries = scope["queries"]
    expected = {(s, q["date"], f) for q in queries for s in q["securities"] for f in q["fields"]}
    if not expected or not set(scope["fields"]) == {f for _, _, f in expected}:
        raise ValueError("nonempty queries must cover exactly the requested fields")
    k = scope["rank_rule"]["top_k"]
    if type(k) is not int or k < 1 or scope["minimum_panel_size"] < k:
        raise ValueError("positive top_k must not exceed minimum_panel_size")
    reference_rows = [dict(row) for row in reference_rows]
    for row in reference_rows:
        value = row["value"]
        if value is not None and not math.isfinite(float(value)):
            raise ValueError("finite value or explicit None required")
        bits = struct.pack(">d", float(value)).hex() if value is not None else None
        if value is not None and row.get("float64_bits_be", bits) != bits:
            raise ValueError("reference binary64 bits do not match value")
        row.setdefault("float64_bits_be", bits)
        row.setdefault("missing_kind", "missing" if value is None else "none")
    for row in local_rows:
        if row["value"] is not None and not math.isfinite(float(row["value"])):
            raise ValueError("finite value or explicit None required")

    def keyed(rows, trim):
        result = {}
        for r in rows:
            key = r["security"], r["date"], r["field"]
            if trim and key not in expected:
                continue
            if key not in expected or key in result:
                raise ValueError("unexpected or duplicate comparison cell")
            result[key] = r
        if set(result) != expected:
            raise ValueError("all requested cells must be explicit, including missing values")
        return result

    local, other = keyed(local_rows, True), keyed(reference_rows, False)
    cells, panels, summary = [], [], {}
    rule, k = scope["rank_rule"], scope["rank_rule"]["top_k"]
    for key in sorted(expected):
        s, d, f = key
        a, b = local[key], other[key]
        paired = a["value"] is not None and b["value"] is not None
        cell = {
            "security": s,
            "date": d,
            "field": f,
            "local": a["value"],
            "reference": b["value"],
            "paired": paired,
            "local_metadata": {
                name: value
                for name, value in a.items()
                if name not in {"security", "date", "field", "value"}
            },
            "reference_float64_bits_be": b["float64_bits_be"],
            "reference_missing_kind": b["missing_kind"],
        }
        if paired:
            av, bv = F(a["value"]), F(b["value"])
            delta = av - bv
            limit = F(str(scope["thresholds"][f]["atol"])) + F(
                str(scope["thresholds"][f]["rtol"])
            ) * abs(bv)
            cell.update(
                absolute_error=float(abs(delta)),
                signed_error=float(delta),
                relative_error=float(abs(delta / bv)) if bv else None,
                tolerance=float(limit),
                numeric_pass=abs(delta) <= limit,
                exact_decimal_equal=av == bv,
                float64_bits_equal=struct.pack(">d", float(a["value"])).hex()
                == b["float64_bits_be"],
            )
        cells.append(cell)

    for field in scope["fields"]:
        fc = [c for c in cells if c["field"] == field]
        paired = [c for c in fc if c["paired"]]
        fp = []
        for d in sorted({c["date"] for c in fc}):
            day = [c for c in fc if c["date"] == d]
            a = {c["security"]: F(c["local"]) for c in day if c["local"] is not None}
            b = {c["security"]: F(c["reference"]) for c in day if c["reference"] is not None}
            common = set(a) & set(b)
            p = {
                "field": field,
                "date": d,
                "requested": len(day),
                "n": len(common),
                "rank_pass": None,
                "available_pools": {
                    "local_n": len(a),
                    "reference_n": len(b),
                    "complete_requested_pools": len(a) == len(b) == len(day),
                },
            }
            if len(common) >= scope["minimum_panel_size"]:
                p.update(metrics(a, b, k))
                p["rank_pass"] = (
                    p["spearman"] is not None
                    and p["spearman"] >= rule["spearman_min"]
                    and p["pair_concordance"] is not None
                    and p["pair_concordance"] >= rule["pair_concordance_min"]
                    and p["top"]["overlap"] >= rule["top_overlap_min"]
                    and p["bottom"]["overlap"] >= rule["bottom_overlap_min"]
                )
            for tail, high in (("top", True), ("bottom", False)):
                la, rb = choose(a, k, high), choose(b, k, high)
                p["available_pools"][tail] = {
                    "local": la,
                    "reference": rb,
                    "replacement_count": k - len(set(la) & set(rb)),
                }
            fp.append(p)
        panels.extend(fp)
        coverage = len(paired) / len(fc)
        passed = sum(c["numeric_pass"] for c in paired)
        result = {
            "requested": len(fc),
            "paired": len(paired),
            "coverage": coverage,
            "numeric_passed": passed,
            "numeric_pass_rate": passed / len(paired) if paired else None,
            "absolute_error": distribution(c["absolute_error"] for c in paired),
            "relative_error": distribution(c["relative_error"] for c in paired),
            "float64_bits_equal": sum(c["float64_bits_equal"] for c in paired),
            "exact_decimal_equal": sum(c["exact_decimal_equal"] for c in paired),
            "rank_panels_passed": sum(p["rank_pass"] is True for p in fp),
            "rank_panels_failed": sum(p["rank_pass"] is False for p in fp),
            "rank_panels_insufficient": sum(p["rank_pass"] is None for p in fp),
            "panel_count": len(fp),
            "minimum_spearman": min(
                (p["spearman"] for p in fp if p.get("spearman") is not None),
                default=None,
            ),
            "minimum_pair_concordance": min(
                (p["pair_concordance"] for p in fp if p.get("pair_concordance") is not None),
                default=None,
            ),
            "top_replacements": sum(p["available_pools"]["top"]["replacement_count"] for p in fp),
            "bottom_replacements": sum(
                p["available_pools"]["bottom"]["replacement_count"] for p in fp
            ),
            "cells_with_later_component_updates": sum(
                c["local_metadata"].get("component_updates_after_cutoff", 0) > 0 for c in fc
            ),
            "corporate_action_window_cells": sum(
                c["local_metadata"].get("within_window_factor_changes", 0) > 0 for c in fc
            ),
            "negative_denominator_cells": sum(
                c["local_metadata"].get("negative_denominator", False) for c in fc
            ),
            "report_periods": dict(
                Counter(
                    c["local_metadata"].get("period_end")
                    for c in fc
                    if "period_end" in c["local_metadata"]
                )
            ),
        }
        result["numeric_usable"] = (
            coverage >= scope["coverage_min"]
            and passed / max(1, len(paired)) >= scope["numeric_pass_min"]
        )
        # Common-pool ranks cannot see selections lost through missing values.
        # Apply the same overlap thresholds to each side's available pool.
        result["rank_usable"] = coverage >= scope["coverage_min"] and all(
            p["rank_pass"] is True
            and all(
                (k - p["available_pools"][tail]["replacement_count"]) / k
                >= rule[f"{tail}_overlap_min"]
                for tail in ("top", "bottom")
            )
            for p in fp
        )
        result["practical_acceptance"] = result["numeric_usable"] and result["rank_usable"]
        result["strict_float64_compatible_on_reference"] = result["float64_bits_equal"] == len(fc)
        summary[field] = result

    output = {
        "schema": "ad.factor-research-reference-comparison.v1",
        "reference_cells": len(reference_rows),
        "matched_local_predictions": len(local),
        "thresholds": scope["thresholds"],
        "rank_rule": rule,
        "summary": summary,
        "panels": panels,
        "cells": cells,
        "historical_pit_certified": False,
        "production_eligible": False,
        "global_jq_equivalence_claimed": False,
    }
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("scope", "local", "reference", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    inputs = {"scope": args.scope, "local": args.local, "reference": args.reference}
    if args.output.resolve() in {p.resolve() for p in inputs.values()}:
        parser.error("output must differ from input files")
    result = compare_rows(read(args.local)["rows"], read(args.reference)["rows"], read(args.scope))
    result["input_sha256"] = {name: sha(path) for name, path in inputs.items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dump(args.output, result)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
