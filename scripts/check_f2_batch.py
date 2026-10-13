"""Independent 100-digit arithmetic and rank oracle; no ashare_data imports.

Reopens captured JSON/decoded raw daily bars, rather than trusting the factor
input projection. This validates arithmetic, not independent financial sourcing
or a vendor's hidden implementation. Official PDF anchors are separately counted.
"""

import argparse
from collections import Counter
from decimal import Decimal as D, localcontext
import hashlib
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text(), parse_float=str)


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def source_oracle(root, scope, days):
    grid = sorted(
        {
            r["jyrq"]
            for p in (root / "raw").glob("szse-????-??.json")
            for r in load(p)["data"]
            if r["jybz"] == "1" and any(a <= r["jyrq"] <= b for a, b in scope["input_windows"])
        }
    )
    result = {}
    for stock in scope["securities"]:
        security = stock["security"]
        code = ("sh" if security.endswith("XSHG") else "sz") + security[:6]
        factors_text = (root / "raw" / (code + "-hfq.js")).read_text()
        factors = json.loads(
            factors_text[factors_text.index("{") : factors_text.rindex("}") + 1], parse_float=str
        )["data"]
        raw_daily = load(root / "derived" / (code + "-daily.json"))["rows"]
        prices = {}
        for r in raw_daily:
            d = r["date"][:10]
            f = D(max((v for v in factors if v["d"] <= d), key=lambda x: x["d"])["f"])
            prices[d] = [D(str(r[k])) * f for k in ("high", "low", "close")]
        statements = {
            kind: load(root / "raw" / (code + "-" + kind + ".json"))["result"]["data"]
            for kind in ("balance", "cash", "income")
        }
        for day in days:
            end = grid.index(day) + 1
            c = [prices[d][2] for d in grid[end - 5 : end]]
            result[security, day, "BIAS5"] = D(100) * (5 * c[-1] - sum(c)) / sum(c)
            # S=H+L+C. Scaling out /3 and /20 makes a distinct algebraic path.
            s = [sum(prices[d]) for d in grid[end - 20 : end]]
            total = sum(s)
            result[security, day, "CCI20"] = (
                D(20000) * (20 * s[-1] - total) / (15 * sum(abs(20 * x - total) for x in s))
            )
            changes = [prices[grid[i]][2] / prices[grid[i - 1]][2] for i in range(end - 12, end)]
            result[security, day, "PSY"] = D(sum(x > 1 for x in changes)) * 100 / 12
            visible = {
                kind: sorted(
                    (
                        r
                        for r in rows
                        if r["NOTICE_DATE"][:10] < day
                        and scope["financial_period_range"][0]
                        <= r["REPORT_DATE"][:10]
                        <= scope["financial_period_range"][1]
                    ),
                    key=lambda r: r["REPORT_DATE"],
                )
                for kind, rows in statements.items()
            }
            balance = visible["balance"][-1]
            result[security, day, "quick_ratio"] = (
                D(str(balance["TOTAL_CURRENT_ASSETS"])) - D(str(balance["INVENTORY"]))
            ) / D(str(balance["TOTAL_CURRENT_LIAB"]))
            result[security, day, "debt_to_asset_ratio"] = D(str(balance["TOTAL_LIABILITIES"])) / D(
                str(balance["TOTAL_ASSETS"])
            )
            latest = [visible[k][-1] for k in ("cash", "income")]
            assert latest[0]["REPORT_DATE"] == latest[1]["REPORT_DATE"]
            quarter_values = []
            for kind, field, row in zip(
                ("cash", "income"), ("NETCASH_OPERATE", "PARENT_NETPROFIT"), latest, strict=True
            ):
                period = row["REPORT_DATE"][:10]
                value = D(str(row[field]))
                if period[5:] != "03-31":
                    previous = (
                        period[:5]
                        + {"06-30": "03-31", "09-30": "06-30", "12-31": "09-30"}[period[5:]]
                    )
                    prior = [r for r in visible[kind] if r["REPORT_DATE"][:10] == previous]
                    assert len(prior) == 1
                    value -= D(str(prior[0][field]))
                quarter_values.append(value)
            result[security, day, "net_operating_cash_flow_coverage"] = (
                quarter_values[0] / quarter_values[1]
            )
    return result


def ordering(values):
    # Pair-count average ranks; independent of the implementation's sorted-run ranks.
    rank = {
        s: D(1)
        + sum(v < value for v in values.values())
        + D(sum(v == value for v in values.values()) - 1) / 2
        for s, value in values.items()
    }
    return {
        "ranks": {s: str(v) for s, v in rank.items()},
        "top3": sorted(sorted(values, key=lambda s: (-values[s], s))[:3]),
        "bottom3": sorted(sorted(values, key=lambda s: (values[s], s))[:3]),
    }


def check_anchors(root, fixture, local):
    entries, ratios = [], []
    for anchor in load(fixture)["rows"]:
        path = root / anchor["pdf_path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == anchor["pdf_sha256"]
        receipt = load(path.with_name(path.name + ".receipt.json"))
        assert receipt["sha256"] == anchor["pdf_sha256"] and receipt["url"] == anchor["url"]
        s = anchor["security"]
        code = ("sh" if s.endswith("XSHG") else "sz") + s[:6]
        values = {}
        for field, amount in anchor["amounts"].items():
            kind = {"NETCASH_OPERATE": "cash", "PARENT_NETPROFIT": "income"}.get(field, "balance")
            candidates = [
                r
                for r in load(root / "raw" / (code + "-" + kind + ".json"))["result"]["data"]
                if r["REPORT_DATE"][:10] == anchor["period"]
            ]
            assert len(candidates) == 1
            value, reported = D(str(candidates[0][field])), D(amount["value"])
            entries.append(
                {
                    "security": s,
                    "field": field,
                    "physical_page": amount["physical_page"],
                    "official": str(reported),
                    "snapshot": str(value),
                    "match": value == reported,
                    "source_url": anchor["url"],
                    "pdf_sha256": anchor["pdf_sha256"],
                }
            )
            values[field] = reported
        answers = {
            "quick_ratio": (values["TOTAL_CURRENT_ASSETS"] - values["INVENTORY"])
            / values["TOTAL_CURRENT_LIAB"],
            "debt_to_asset_ratio": values["TOTAL_LIABILITIES"] / values["TOTAL_ASSETS"],
            "net_operating_cash_flow_coverage": values["NETCASH_OPERATE"]
            / values["PARENT_NETPROFIT"],
        }
        for f, v in answers.items():
            actual = local[s, anchor["date"], f]
            error = abs(actual - v)
            ratios.append(
                {
                    "security": s,
                    "date": anchor["date"],
                    "field": f,
                    "absolute_error": str(error),
                    "match_to_serialization": error <= D("1e-48") * max(abs(v), D(1)),
                }
            )
    return {
        "amounts": entries,
        "ratios": ratios,
        "amounts_matched": sum(e["match"] for e in entries),
        "ratios_matched": sum(r["match_to_serialization"] for r in ratios),
        "independent_preparer": False,
        "historical_pit_certified": False,
        "scope": "2 issuers x 1 quarter; no broad financial reference",
    }


def run(root, scope_path, anchors):
    scope = load(scope_path)
    digest = hashlib.sha256(scope_path.read_bytes()).hexdigest()
    assert digest == scope_path.with_suffix(".sha256").read_text().split()[0]
    summary = load(root / "results/summary.json")
    for filename, key in (
        ("results/local-panel.json", "local_panel_sha256"),
        ("derived/dataset.json", "dataset_sha256"),
    ):
        assert hashlib.sha256((root / filename).read_bytes()).hexdigest() == summary[key]
    days = summary["dates"]
    expected = {
        (s["security"], d, f) for s in scope["securities"] for d in days for f in scope["fields"]
    }
    rows = load(root / "results/local-panel.json")["rows"]
    local = {
        (r["security"], r["date"], r["field"]): D(r["value"]) for r in rows if r["status"] == "ok"
    }
    assert len(rows) == len(expected) and set(local) == expected
    with localcontext() as context:
        context.prec = 100
        oracle = source_oracle(root, scope, days)
        assert set(oracle) == expected
        max_errors, checked = {}, Counter()
        for key, expected_value in oracle.items():
            error = abs(expected_value - local[key])
            assert error <= D("1e-48") * max(abs(expected_value), D(1)), key
            max_errors[key[2]] = max(error, max_errors.get(key[2], D(0)))
            checked[key[2]] += 1
        panels = []
        for f in scope["fields"]:
            for day in days:
                a = ordering(
                    {s["security"]: local[s["security"], day, f] for s in scope["securities"]}
                )
                b = ordering(
                    {s["security"]: oracle[s["security"], day, f] for s in scope["securities"]}
                )
                assert a == b, (day, f)
                panels.append({"date": day, "field": f, "ranks_and_top_bottom_equal": True, **a})
        official = check_anchors(root, anchors, local)
    assert official["amounts_matched"] == 14 and official["ratios_matched"] == 6
    result = {
        "scope_sha256": digest,
        "checked_real_cells": len(local),
        "checked_by_field": dict(checked),
        "max_serialization_absolute_error": {k: str(v) for k, v in max_errors.items()},
        "rank_panels_checked": len(panels),
        "official_anchors": official,
        "oracle": "100-digit Decimal, algebraic alternatives and independent raw JSON traversal",
        "arithmetic_only_not_JQ_or_PIT_validation": True,
        "panels": panels,
    }
    dump(root / "results/oracle-check.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "panels"}, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--scope", type=Path, default=Path("docs/f2-batch-scope.json"))
    parser.add_argument(
        "--anchors", type=Path, required=True, help="Caller-supplied local official-report anchors JSON"
    )
    args = parser.parse_args()
    run(args.evidence_root, args.scope, args.anchors)
