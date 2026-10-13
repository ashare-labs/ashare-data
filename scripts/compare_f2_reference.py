"""Offline F2 reference comparison. No queries, no changes to frozen predictions.

Pass --root a writable copy of the sealed F2 reference evidence. This command
writes three JSON reports there; never point it at an immutable original.
"""

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
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )


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
        F(sum(i + 1 for i, x in enumerate(ordered) if x == v), ordered.count(v))
        for v in values
    ]


def choose(values, k, high):
    return sorted(
        sorted(values, key=lambda s: ((-1 if high else 1) * values[s], s))[:k]
    )


def metrics(local, reference, k):
    names = sorted(set(local) & set(reference))
    a, b = [local[s] for s in names], [reference[s] for s in names]
    x, y = average_ranks(a), average_ranks(b)
    mean = F(len(names) + 1, 2)
    vx, vy = sum((v - mean) ** 2 for v in x), sum((v - mean) ** 2 for v in y)
    rho = (
        float(sum((u - mean) * (v - mean) for u, v in zip(x, y)))
        / math.sqrt(float(vx * vy))
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


def compare(repo, root):
    frozen = read(root / "frozen-protection-before.json")
    paths = {
        "scope": repo / "docs/f2-batch-scope.json",
        "inputs": repo / ".local-evidence/f2-batch/derived/dataset.json",
        "predictions": repo / ".local-evidence/f2-batch/results/local-panel.json",
    }
    assert {k: sha(p) for k, p in paths.items()} == frozen["hashes"], (
        "Frozen artifact changed"
    )
    scope, predictions = read(paths["scope"]), read(paths["predictions"])
    queries = scope["proposed_JQ_reference"]["queries"]
    expected = {
        (s, q["date"], f) for q in queries for s in q["securities"] for f in q["fields"]
    }
    assert len(expected) == 252 and len(predictions["rows"]) == 3096
    reference = []
    query_checks = []
    for i, q in enumerate(queries, 1):
        payload = read(root / "raw" / f"call-{i}.json")
        assert payload["request"] == q
        assert payload["scope_sha256"] == frozen["hashes"]["scope"]
        assert payload["local_predictions_sha256"] == frozen["hashes"]["predictions"]
        assert (
            payload["returned_cells"]
            == len(payload["rows"])
            == q["maximum_returned_values"]
        )
        output = (root / "raw" / f"call-{i}-output.txt").read_text()
        printed = json.loads(
            output.split("F2_252_JSON_BEGIN")[1].split("F2_252_JSON_END")[0]
        )
        assert printed == payload and f"F2_252_COMPLETE call={i}" in output
        assert payload["nan_cells"] == sum(
            r["missing_kind"] == "nan" for r in payload["rows"]
        )
        for row in payload["rows"]:
            if row["value"] is not None:
                assert math.isfinite(float(row["value"]))
                assert (
                    struct.pack(">d", float(row["value"])).hex()
                    == row["float64_bits_be"]
                )
        reference.extend(payload["rows"])
        query_checks.append(
            {
                "call": i,
                "cells": len(payload["rows"]),
                "nan": payload["nan_cells"],
                "raw_output_matches_json": True,
                "payload_sha256": sha(root / "raw" / f"call-{i}.json"),
            }
        )

    def keyed(rows, trim):
        result = {}
        for r in rows:
            key = r["security"], r["date"], r["field"]
            if trim and key not in expected:
                continue
            assert key in expected and key not in result
            result[key] = r
        assert set(result) == expected
        return result

    local, other = keyed(predictions["rows"], True), keyed(reference, False)
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
            b = {
                c["security"]: F(c["reference"])
                for c in day
                if c["reference"] is not None
            }
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
                (
                    p["pair_concordance"]
                    for p in fp
                    if p.get("pair_concordance") is not None
                ),
                default=None,
            ),
            "top_replacements": sum(
                p["available_pools"]["top"]["replacement_count"] for p in fp
            ),
            "bottom_replacements": sum(
                p["available_pools"]["bottom"]["replacement_count"] for p in fp
            ),
            "cells_with_later_component_updates": sum(
                c["local_metadata"].get("component_updates_after_cutoff", 0) > 0
                for c in fc
            ),
            "corporate_action_window_cells": sum(
                c["local_metadata"].get("within_window_factor_changes", 0) > 0
                for c in fc
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
        # Apply the same frozen overlap thresholds to each side's available pool.
        result["rank_usable"] = coverage >= scope["coverage_min"] and all(
            p["rank_pass"] is True
            and all(
                (k - p["available_pools"][tail]["replacement_count"]) / k
                >= rule[f"{tail}_overlap_min"]
                for tail in ("top", "bottom")
            )
            for p in fp
        )
        result["frozen_practical_acceptance"] = (
            result["numeric_usable"] and result["rank_usable"]
        )
        result["strict_float64_compatible_on_reference"] = result[
            "float64_bits_equal"
        ] == len(fc)
        summary[field] = result

    output = {
        "schema": "ad.f2-reference-comparison.v1",
        "frozen_commit": frozen["commit"],
        "frozen_hashes": frozen["hashes"],
        "reference_cells": len(reference),
        "matched_local_predictions": len(local),
        "full_frozen_local_predictions": 3096,
        "query_checks": query_checks,
        "thresholds": scope["thresholds"],
        "rank_rule": rule,
        "summary": summary,
        "panels": panels,
        "cells": cells,
        "historical_pit_certified": False,
        "production_eligible": False,
        "global_jq_equivalence_claimed": False,
        "original_exact_alignment_unchanged": True,
        "nonrequested_apr25_financial_cells_not_in_denominator": True,
    }
    (root / "results").mkdir(exist_ok=True)
    dump(root / "results/comparison.json", output)
    dump(
        root / "results/reference-panel.json",
        {
            "rows": reference,
            "source": "four captured research UI outputs",
            "cells_including_nan": len(reference),
        },
    )
    dump(
        root / "results/matched-frozen-predictions.json",
        {
            "source_sha256": frozen["hashes"]["predictions"],
            "rows": list(local.values()),
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--root", type=Path, required=True, help="Writable reference evidence copy")
    args = parser.parse_args()
    compare(args.repo, args.root)
