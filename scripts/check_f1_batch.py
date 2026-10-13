"""Independent arithmetic identities and prior official-report source anchors.

These checks are software/source controls, never an independent review signoff
or a replacement for the pending named-factor reference matrix.
"""

import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction as F
import hashlib
import json
from pathlib import Path

from ashare_data import f1
from ashare_data.f1_batch import MINIMUM, _effective, compare_panels
from ashare_data.f1_pv_semantic_contract import quantize
from ashare_data.f1_remaining import _ROCSeries


def read(path):
    return json.loads(path.read_text())


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def arithmetic(dataset, rows):
    items = {i["security"]: i for i in dataset["items"]}
    sessions = dataset["sessions"]
    checks = []
    with localcontext() as context:
        context.prec = 100
        for r in rows:
            if r["value"] is None or r["field"] not in MINIMUM:
                continue
            item, field = items[r["security"]], r["field"]
            index = sessions.index(r["date"])
            days = sessions[index + 1 - MINIMUM[field] : index + 1]
            bars = [item["sina"][d] for d in days]

            def d(value):
                value = F(value)
                return Decimal(value.numerator) / Decimal(value.denominator)

            factors = [d(_effective(item["factors"], day, "value")) for day in days]
            adjusted = [
                Decimal(b["close"]) * factor for b, factor in zip(bars, factors, strict=True)
            ]
            closes = [v.quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN) for v in adjusted]
            if field == "ROC120":
                expected = 100 * (adjusted[-1] - adjusted[0]) / adjusted[0]
                native = {
                    "security": item["security"],
                    "sessions": sessions,
                    "rows": [
                        {"date": day, **bar, "trading_state": "observed_trade"}
                        for day, bar in item["sina"].items()
                    ],
                    "raw_price_adjustment": "none",
                    "source_family": "Sina",
                    "currency": "CNY",
                    "price_unit": "CNY_per_share",
                    "price_policy": "raw_times_native_hfq_no_rounding",
                    "factors": item["factors"],
                }
                existing = _ROCSeries(native).roc120(r["date"])
                if abs(existing - expected) > Decimal("1e-45"):
                    raise AssertionError("existing ROC implementation differs")
            elif field == "Variance20":
                returns = [b / a - 1 for a, b in zip(closes[:-1], closes[1:], strict=True)]
                # E[x²]-E[x]² differs from the production centered-square sum.
                expected = (
                    (sum(v * v for v in returns) - sum(returns) ** 2 / 20) * Decimal(250) / 19
                )
            elif field in {"ATR6", "ATR14"}:
                expected = Decimal(0)
                for b, fac, prev in zip(bars[1:], factors[1:], closes[:-1], strict=True):
                    high = (Decimal(b["high"]) * fac).quantize(
                        Decimal(".01"), rounding=ROUND_HALF_EVEN
                    )
                    low = (Decimal(b["low"]) * fac).quantize(
                        Decimal(".01"), rounding=ROUND_HALF_EVEN
                    )
                    expected += max(high, prev) - min(low, prev)
                expected /= len(days) - 1
            elif field == "EMA5":
                beta = Decimal(2) / 3
                # Closed-form weighted sum differs from the recursive implementation.
                expected = (
                    closes[0] * beta**59
                    + sum(closes[k] * beta ** (59 - k) / 3 for k in range(1, 60))
                ) / closes[-1]
            elif field == "VROC6":
                first = (Decimal(bars[0]["volume"]) / factors[0]).quantize(
                    Decimal(1), rounding=ROUND_HALF_EVEN
                )
                last = (Decimal(bars[-1]["volume"]) / factors[-1]).quantize(
                    Decimal(1), rounding=ROUND_HALF_EVEN
                )
                expected = (last - first) / first * 100
            elif field == "TVMA6":
                expected = sum(Decimal(b["amount"]) for b in bars) / 6
            elif field == "VOL5":
                expected = (
                    sum(
                        (
                            Decimal(b["volume"]) * 100 / d(_effective(item["shares"], day, "value"))
                        ).quantize(Decimal(".0001"), rounding=ROUND_HALF_EVEN)
                        for day, b in zip(days, bars, strict=True)
                    )
                    / 5
                )
            else:
                cap = Decimal(bars[-1]["close"]) * d(_effective(item["shares"], days[-1], "value"))
                expected = Decimal.from_float(
                    float((cap / 100000000).quantize(Decimal(".0001"), rounding=ROUND_HALF_EVEN))
                    * 1e8
                )
            if field in {"Variance20", "ATR6", "ATR14", "EMA5"}:
                expected = expected.quantize(Decimal(".000001"), rounding=ROUND_HALF_EVEN)
            error = abs(Decimal(r["value"]) - expected)
            # Exact rational output text is emitted at 50 digits, so use a decimal
            # representation bound here, not a relaxed supplier-compatibility test.
            tolerance = max(abs(expected), Decimal(1)) * Decimal("1e-45")
            checks.append(
                {
                    "security": r["security"],
                    "date": r["date"],
                    "field": field,
                    "absolute_error": str(error),
                    "passed": error <= tolerance,
                }
            )
    return {
        "checks": checks,
        "passed": sum(r["passed"] for r in checks),
        "total": len(checks),
        "oracle": "Decimal100; variance second moments; closed-form EMA; interval-union TR; existing Decimal50 ROC",
    }


def official_anchors(archive_path, dataset, local):
    archive = read(archive_path)
    items = {i["security"]: i for i in dataset["items"]}
    mapping = {
        "net_profit": "NETPROFIT",
        "total_operating_revenue": "TOTAL_OPERATE_INCOME",
        "operating_revenue": "OPERATE_INCOME",
        "net_operate_cash_flow": "NETCASH_OPERATE",
    }
    components, predictions = [], []
    for security, pack in archive["financial_packs"].items():
        item = items[security]
        ledger = f1.FinancialLedger(pack)
        for record in pack["observations"]:
            if record["field"] not in mapping:
                continue
            field = mapping[record["field"]]
            matches = [
                row
                for row in item["financials"]
                if row["period"] == record["period_end"] and row["values"].get(field) is not None
            ]
            if len(matches) != 1:
                continue
            difference = F(str(matches[0]["values"][field])) - F(record["value"])
            components.append(
                {
                    "security": security,
                    "record_id": record["record_id"],
                    "source_field": field,
                    "exact_equal": difference == 0,
                    "difference_CNY": str(float(difference)),
                    "original_report": record["report_id"],
                }
            )
        for row in local:
            if row["security"] != security or row["field"] not in (
                "net_profit_to_total_operate_revenue_ttm",
                "net_profit_growth_rate",
                "operating_revenue_ttm",
                "net_operate_cashflow_growth_rate",
            ):
                continue
            result = {
                "security": security,
                "date": row["date"],
                "field": row["field"],
                "local": row["value"],
                "official_archive": None,
                "paired": False,
            }
            day = row["date"]
            try:
                if row["slot"] == "FI02":
                    value = F(
                        f1.fi02_lagged_candidate(ledger, security, day, mode=f1.RESEARCH_MODE)[
                            "prediction"
                        ]["decimal"]
                    )
                elif row["slot"] == "FI03":
                    period = ledger.latest_period(
                        security,
                        "operating_revenue",
                        as_of=day + "T15:00:00+08:00",
                        mode=f1.RESEARCH_MODE,
                    )
                    value = F(
                        ledger.quarter_ttm(
                            security,
                            "operating_revenue",
                            period,
                            as_of=day + "T15:00:00+08:00",
                            mode=f1.RESEARCH_MODE,
                        )["value"]
                    )
                else:
                    native = f1.financial_factor_candidate(
                        ledger,
                        row["slot"],
                        security,
                        day + "T15:00:00+08:00",
                        mode=f1.RESEARCH_MODE,
                    )
                    value = quantize(F(native["value"]), 6)
                result["official_archive"] = str(float(value))
                if row["value"] is not None:
                    result.update(
                        paired=True,
                        exact_equal=F(row["value"]) == value,
                        absolute_error=float(abs(F(row["value"]) - value)),
                    )
            except (f1.AcceptanceError, ValueError) as error:
                result["reason"] = str(error)
            predictions.append(result)
    return {
        "archive_input_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "components": components,
        "predictions": predictions,
        "component_exact": sum(r["exact_equal"] for r in components),
        "component_count": len(components),
        "prediction_pairs": sum(r["paired"] for r in predictions),
        "prediction_exact": sum(r.get("exact_equal", False) for r in predictions),
        "scope": "two previously reviewed issuer archives only; no broad PIT claim",
    }


def version_impact(anchors, local, scope):
    substituted = deepcopy(local)
    index = {(r["security"], r["date"], r["field"]): r for r in substituted}
    changed = set()
    for row in anchors["predictions"]:
        if row["paired"]:
            key = row["security"], row["date"], row["field"]
            index[key]["value"] = row["official_archive"]
            if not row["exact_equal"]:
                changed.add(key)
    scope = {**scope, "reference_dates": sorted({r["date"] for r in local})}
    comparison = compare_panels(
        local,
        substituted,
        scope,
        comparison_kind="two_official_archive_issuers_substituted_other_ten_fixed",
    )
    panels = [
        p
        for p in comparison["panels"]
        if any(day == p["date"] and field == p["field"] for _, day, field in changed)
    ]
    return {
        "schema": "ad.f1-financial-version-impact.v1",
        "changed_factor_cells": len(changed),
        "active_panels": panels,
        "full_breadth_independent_reference": False,
        "scope": "Only two archived issuers substituted; other ten fixed. Not independent financial panel validation.",
        "tail_replacements": sum(
            p["top"]["replacement_count"] + p["bottom"]["replacement_count"]
            for p in panels
            if "top" in p
        ),
        "insufficient_panels": sum(p["rank_pass"] is None for p in panels),
        "max_errors_by_field": {
            field: max(
                (
                    r["absolute_error"]
                    for r in anchors["predictions"]
                    if r["paired"] and r["field"] == field
                ),
                default=None,
            )
            for field in scope["fields"][9:]
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--archived-inputs", type=Path, required=True)
    parser.add_argument("--scope", type=Path, default=Path("docs/f1-batch-scope.json"))
    args = parser.parse_args()
    dataset, local = (
        read(args.root / "derived/dataset.json"),
        read(args.root / "results/local-panel.json"),
    )
    result = arithmetic(dataset, local)
    dump(args.root / "results/arithmetic-oracle.json", result)
    print("arithmetic", result["passed"], "/", result["total"])
    anchors = official_anchors(args.archived_inputs, dataset, local)
    dump(args.root / "results/official-report-anchors.json", anchors)
    dump(
        args.root / "results/financial-version-impact.json",
        version_impact(anchors, local, read(args.scope)),
    )
    print(
        "official source components",
        anchors["component_exact"],
        "/",
        anchors["component_count"],
        "factor predictions",
        anchors["prediction_exact"],
        "/",
        anchors["prediction_pairs"],
    )
    print("oracle field counts", dict(Counter(r["field"] for r in result["checks"])))
    if result["passed"] != result["total"]:
        raise AssertionError("arithmetic oracle mismatch")


if __name__ == "__main__":
    main()
