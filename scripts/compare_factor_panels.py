"""Offline comparisons of explicitly supplied factor panels and tolerances."""

from collections import Counter, defaultdict
import math
import statistics
from ashare_data.factors.joinquant.panel import SLOTS


def _ranks(values):
    ordered = sorted(range(len(values)), key=values.__getitem__)
    ranks, start = [0.0] * len(values), 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[ordered[end]] == values[ordered[start]]:
            end += 1
        for i in ordered[start:end]:
            ranks[i] = (start + 1 + end) / 2
        start = end
    return ranks


def rank_metrics(securities, local, reference, *, k):
    """Average tied ranks; deterministic security-order tie breaking for selection."""
    if len(set(securities)) != len(securities) or not (
        len(local) == len(reference) == len(securities)
    ):
        raise ValueError("unique aligned panel identities required")
    if len(local) <= k or k < 1:
        raise ValueError("panel must be larger than top K")
    x, y = _ranks(local), _ranks(reference)
    spearman = statistics.correlation(x, y) if len(set(x)) > 1 and len(set(y)) > 1 else None
    correct, comparable, reference_ties, local_ties = 0.0, 0, 0, 0
    for i in range(len(local)):
        for j in range(i + 1, len(local)):
            a, b = local[i] - local[j], reference[i] - reference[j]
            if b == 0:
                reference_ties += 1
                continue
            comparable += 1
            if a == 0:
                local_ties += 1
                correct += 0.5
            elif (a > 0) == (b > 0):
                correct += 1

    def chosen(values, high):
        return sorted(
            securities[i]
            for i in sorted(
                range(len(values)), key=lambda i: ((-1 if high else 1) * values[i], securities[i])
            )[:k]
        )

    tails = {}
    for side, high in (("top", True), ("bottom", False)):
        a, b = chosen(local, high), chosen(reference, high)
        tails[side] = {
            "local": a,
            "reference": b,
            "overlap": len(set(a) & set(b)) / k,
            "replacement_count": k - len(set(a) & set(b)),
        }
    return {
        "n": len(local),
        "spearman": spearman,
        "pair_concordance": correct / comparable if comparable else None,
        "reference_tied_pairs": reference_ties,
        "local_ties_on_nontied_reference": local_ties,
        **tails,
    }


def compare_panels(local, reference, scope, *, comparison_kind):
    """Frozen tolerances and full-denominator coverage; never silently drop a gap."""
    expected = {
        (s["security"], d, f)
        for s in scope["securities"]
        for d in scope["reference_dates"]
        for f in scope["fields"]
    }

    def keyed(rows):
        result = {}
        for row in rows:
            key = row["security"], row["date"], row["field"]
            if key not in expected:
                continue
            if key in result:
                raise ValueError("duplicate comparison key")
            if row["value"] is not None and not math.isfinite(float(row["value"])):
                raise ValueError("nonfinite comparison value; use explicit missing")
            result[key] = row
        return result

    a, b = keyed(local), keyed(reference)
    cells, panels, totals = [], [], {}
    for field in scope["fields"]:
        threshold = scope["thresholds"][field]
        errors, relative_errors, passed = [], [], 0
        for security, day, name in sorted(expected):
            if name != field:
                continue
            key = security, day, name
            row = {"security": security, "date": day, "field": name, "paired": False}
            if (
                key in a
                and key in b
                and a[key]["value"] is not None
                and b[key]["value"] is not None
            ):
                av, bv = float(a[key]["value"]), float(b[key]["value"])
                absolute = abs(av - bv)
                tolerance = threshold["atol"] + threshold["rtol"] * abs(bv)
                okay = absolute <= tolerance
                passed += okay
                errors.append(absolute)
                relative_errors.append(absolute / abs(bv) if bv else None)
                row.update(
                    paired=True,
                    local=av,
                    reference=bv,
                    absolute_error=absolute,
                    relative_error=relative_errors[-1],
                    tolerance=tolerance,
                    numeric_pass=okay,
                    float64_equal=av == bv,
                )
            cells.append(row)
        field_panels = []
        for day in scope["reference_dates"]:
            paired = [r for r in cells if r["field"] == field and r["date"] == day and r["paired"]]
            panel = {"field": field, "date": day, "n": len(paired), "rank_pass": None}
            if len(paired) >= scope["minimum_panel_size"]:
                metrics = rank_metrics(
                    [r["security"] for r in paired],
                    [r["local"] for r in paired],
                    [r["reference"] for r in paired],
                    k=scope["rank_rule"]["top_k"],
                )
                rule = scope["rank_rule"]
                panel.update(metrics)
                panel["rank_pass"] = (
                    metrics["spearman"] is not None
                    and metrics["spearman"] >= rule["spearman_min"]
                    and metrics["pair_concordance"] is not None
                    and metrics["pair_concordance"] >= rule["pair_concordance_min"]
                    and metrics["top"]["overlap"] >= rule["top_overlap_min"]
                    and metrics["bottom"]["overlap"] >= rule["bottom_overlap_min"]
                )
            field_panels.append(panel)
        panels.extend(field_panels)
        denominator = len(scope["securities"]) * len(scope["reference_dates"])
        coverage = len(errors) / denominator

        def distribution(values):
            values = sorted(v for v in values if v is not None)
            return (
                {
                    "p50": statistics.median(values),
                    "p95": values[math.ceil(0.95 * len(values)) - 1],
                    "max": max(values),
                }
                if values
                else None
            )

        totals[field] = {
            "slot": SLOTS[field],
            "expected": denominator,
            "paired": len(errors),
            "coverage": coverage,
            "numeric_passed": passed,
            "numeric_pass_rate": passed / len(errors) if errors else None,
            "absolute_error": distribution(errors),
            "relative_error": distribution(relative_errors),
            "numeric_usable": coverage >= scope["coverage_min"]
            and passed / max(1, len(errors)) >= 0.95,
            "rank_usable": coverage >= scope["coverage_min"]
            and all(p["rank_pass"] is True for p in field_panels),
            "reference_sufficient": coverage >= scope["coverage_min"],
        }
    return {
        "comparison_kind": comparison_kind,
        "fields": totals,
        "panels": panels,
        "cells": cells,
        "original_exact_alignment_unchanged": True,
        "production_eligible": False,
    }


def coverage_summary(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["field"]].append(row)
    return {
        field: {
            "requested": len(group),
            "computed": sum(r["value"] is not None for r in group),
            "missing_reasons": dict(Counter(r.get("reason") for r in group if r["value"] is None)),
            "later_updated_component_cells": sum(
                r.get("component_updates_after_cutoff", 0) > 0 for r in group
            ),
        }
        for field, group in grouped.items()
    }
