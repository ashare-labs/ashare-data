"""Replay the frozen six-factor batch offline; never acquire JQ labels."""

import argparse
from collections import Counter
from fractions import Fraction as F
import hashlib
import json
import math
from pathlib import Path
import statistics

from ashare_data import FactorResearchView
from compare_factor_panels import rank_metrics
from ashare_data.factors.joinquant.research_view import DEFINITION, FIELDS, LOOKBACK, MODE
from ashare_data.providers.factor_research_inputs import read_sources


def dump(path, value, *, immutable=False):
    content = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if immutable and path.exists() and path.read_bytes() != content:
        raise ValueError("immutable evidence changed: " + str(path))
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def distribution(values):
    values = sorted(float(x) for x in values if x is not None)
    return (
        {
            "p50": statistics.median(values),
            "p95": values[math.ceil(0.95 * len(values)) - 1],
            "max": max(values),
        }
        if values
        else None
    )


def compare(local, other, scope, days):
    """Exact-decimal numeric error; common-pool ranks and separate available pools."""
    stocks = [s["security"] for s in scope["securities"]]
    expected = {(s, d, f) for s in stocks for d in days for f in LOOKBACK}

    def index(rows):
        result = {}
        for row in rows:
            key = row["security"], row["date"], row["field"]
            if key not in expected:
                continue
            if key in result:
                raise ValueError("duplicate comparison cell")
            result[key] = None if row["value"] is None else F(row["value"])
        return result

    a, b = index(local), index(other)
    cells, panels, totals = [], [], {}
    rule, k = scope["rank_rule"], scope["rank_rule"]["top_k"]
    for field in LOOKBACK:
        errors, relative, numeric_pass = [], [], 0
        for s in stocks:
            for day in days:
                key = s, day, field
                row = {"security": s, "date": day, "field": field, "paired": False}
                if a.get(key) is not None and b.get(key) is not None:
                    error = abs(a[key] - b[key])
                    limit = F(str(scope["thresholds"][field]["atol"])) + F(
                        str(scope["thresholds"][field]["rtol"])
                    ) * abs(b[key])
                    numeric_pass += error <= limit
                    errors.append(error)
                    relative.append(error / abs(b[key]) if b[key] else None)
                    row.update(
                        paired=True,
                        absolute_error=float(error),
                        numeric_pass=error <= limit,
                        exact_decimal_equal=a[key] == b[key],
                    )
                cells.append(row)
        field_panels = []
        for day in days:
            common = [
                s
                for s in stocks
                if a.get((s, day, field)) is not None and b.get((s, day, field)) is not None
            ]
            panel = {"field": field, "date": day, "n": len(common), "rank_pass": None}
            if len(common) >= scope["minimum_panel_size"]:
                metrics = rank_metrics(
                    common,
                    [a[s, day, field] for s in common],
                    [b[s, day, field] for s in common],
                    k=k,
                )
                panel.update(metrics)
                panel["rank_pass"] = (
                    metrics["spearman"] is not None
                    and metrics["spearman"] >= rule["spearman_min"]
                    and metrics["pair_concordance"] is not None
                    and metrics["pair_concordance"] >= rule["pair_concordance_min"]
                    and metrics["top"]["overlap"] >= rule["top_overlap_min"]
                    and metrics["bottom"]["overlap"] >= rule["bottom_overlap_min"]
                )
            pools = {}
            for side, index_ in (("local", a), ("other", b)):
                available = [s for s in stocks if index_.get((s, day, field)) is not None]
                pools[side] = {"n": len(available)}
                for tail, sign in (("top", -1), ("bottom", 1)):
                    pools[side][tail] = sorted(
                        sorted(available, key=lambda s: (sign * index_[s, day, field], s))[:k]
                    )
            pools["complete_requested_pools"] = all(
                pools[s]["n"] == len(stocks) for s in ("local", "other")
            )
            for tail in ("top", "bottom"):
                pools[tail + "_replacement_count"] = k - len(
                    set(pools["local"][tail]) & set(pools["other"][tail])
                )
            panel["available_pools"] = pools
            field_panels.append(panel)
        panels.extend(field_panels)
        denominator = len(stocks) * len(days)
        totals[field] = {
            "requested": denominator,
            "paired": len(errors),
            "coverage": len(errors) / denominator,
            "numeric_passed": numeric_pass,
            "numeric_pass_rate": numeric_pass / len(errors) if errors else None,
            "absolute_error": distribution(errors),
            "relative_error": distribution(relative),
            "rank_panels_passed": sum(p["rank_pass"] is True for p in field_panels),
            "rank_panels_failed": sum(p["rank_pass"] is False for p in field_panels),
            "rank_panels_insufficient": sum(p["rank_pass"] is None for p in field_panels),
            "available_pool_replacements": sum(
                p["available_pools"][tail + "_replacement_count"]
                for p in field_panels
                for tail in ("top", "bottom")
            ),
            "source_control_pass": len(errors) / denominator >= scope["coverage_min"]
            and numeric_pass / max(1, len(errors)) >= scope["numeric_pass_min"]
            and all(
                p["rank_pass"] is True
                and all(
                    (k - p["available_pools"][tail + "_replacement_count"]) / k
                    >= rule[f"{tail}_overlap_min"]
                    for tail in ("top", "bottom")
                )
                for p in field_panels
            ),
        }
    return {
        "comparison": "Sina_vs_Tencent_raw_OHLC_shared_Sina_factors",
        "independent_adjustment_source": False,
        "jq_equivalent": False,
        "financial_comparison": "not_attempted_shared_financial_inputs",
        "summary": totals,
        "cells": cells,
        "panels": panels,
    }


def run(root, scope_path):
    raw = scope_path.read_bytes()
    frozen = scope_path.with_suffix(".sha256").read_text().split()[0]
    if hashlib.sha256(raw).hexdigest() != frozen:
        raise ValueError("frozen scope changed")
    scope = json.loads(raw)
    dataset = read_sources(root, scope)
    dataset_path = root / "derived/dataset.json"
    dataset_sha = dump(dataset_path, dataset, immutable=True)
    view = FactorResearchView.from_file(
        dataset_path, evidence_root=root, expected_sha256=dataset_sha
    )
    days = [d for d in dataset["sessions"] if any(a <= d <= b for a, b in scope["output_windows"])]
    stocks = [s["security"] for s in scope["securities"]]
    args = dict(dates=days, definition=DEFINITION, mode=MODE)
    local = view.query(stocks, list(FIELDS), **args)
    other = view.query(stocks, list(LOOKBACK), provider="tencent", **args)
    results = root / "results"
    results.mkdir(exist_ok=True)
    local_sha = dump(results / "local-panel.json", local, immutable=True)
    dump(results / "tencent-panel.json", other, immutable=True)
    source = compare(local["rows"], other["rows"], scope, days)
    dump(results / "source-comparison.json", source)
    coverage = {}
    for field in FIELDS:
        rows = [r for r in local["rows"] if r["field"] == field]
        coverage[field] = {
            "requested": len(rows),
            "computed": sum(r["status"] == "ok" for r in rows),
            "missing": dict(Counter(r["reason"] for r in rows if r["status"] != "ok")),
            "event_window_cells": sum(r.get("within_window_factor_changes", 0) > 0 for r in rows),
            "negative_profit_cells": sum(r.get("negative_denominator", False) for r in rows),
            "cells_with_later_source_updates": sum(
                r.get("component_updates_after_cutoff", 0) > 0 for r in rows
            ),
            "report_periods": dict(Counter(r["period_end"] for r in rows if "period_end" in r)),
        }
    gaps = [
        {
            "security": item["security"],
            "provider": provider,
            "missing_sessions": sorted(set(dataset["sessions"]) - set(item[provider])),
        }
        for item in dataset["items"]
        for provider in ("sina", "tencent")
        if set(dataset["sessions"]) - set(item[provider])
    ]
    summary = {
        "scope_sha256": frozen,
        "dataset_sha256": dataset_sha,
        "local_panel_sha256": local_sha,
        "securities": len(stocks),
        "output_sessions": len(days),
        "dates": days,
        "requested_cells": len(local["rows"]),
        "coverage": coverage,
        "raw_captures": sum(s["path"].startswith("raw/") for s in dataset["sources"]),
        "source_rows": {
            "sina": sum(len(i["sina"]) for i in dataset["items"]),
            "tencent": sum(len(i["tencent"]) for i in dataset["items"]),
            "financial_reports": sum(len(i["reports"]) for i in dataset["items"]),
        },
        "input_gaps": gaps,
        "source_comparison": source["summary"],
        "JQ_new_values_consumed": 0,
        "JQ_validation": "NOT_COMPARED",
        "production_eligible": False,
        "historical_pit_certified": False,
        "jq_equivalent": False,
    }
    dump(results / "summary.json", summary)
    dump(
        results / "reference-request.json",
        {
            "scope_sha256": frozen,
            "input_sha256": dataset_sha,
            "predictions_sha256": local_sha,
            **scope["proposed_JQ_reference"],
        },
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--scope", type=Path, required=True)
    opts = parser.parse_args()
    run(opts.evidence_root, opts.scope)
