"""Independent arithmetic identities and prior official-report source anchors.

These checks are software/source controls, never an independent review signoff
or a replacement for the pending named-factor reference matrix.
"""

import argparse
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction as F
import json
from pathlib import Path

from ashare_data.factors import joinquant as joinquant
from ashare_data.factors.joinquant.panel import MINIMUM, _effective
from ashare_data.factors.joinquant.source_factors import _ROCSeries


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {args.dataset.resolve(), args.panel.resolve()}:
        parser.error("output must differ from input files")
    panel = read(args.panel)
    rows = panel["rows"] if isinstance(panel, dict) else panel
    result = arithmetic(read(args.dataset), rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dump(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != "checks"}))
    if result["passed"] != result["total"]:
        raise SystemExit("arithmetic oracle mismatch")


if __name__ == "__main__":
    main()
