"""Frozen, explicit twelve-slot research panels; never a JQ/PIT certificate.

This additive API separates source projections, numerical comparisons and
cross-sectional decisions. Existing archive-bound APIs and tests are unchanged.
"""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction as F
import math
import re
import statistics

from .f1_pv_semantic_contract import quantize

DEFINITION = "f1_twelve_slot_retrospective_batch.research.v1"
MODE = "dated_latest_snapshot_research"
FIELDS = (
    "ROC120",
    "Variance20",
    "ATR6",
    "ATR14",
    "EMA5",
    "VROC6",
    "TVMA6",
    "VOL5",
    "circulating_market_cap",
    "net_profit_to_total_operate_revenue_ttm",
    "net_profit_growth_rate",
    "operating_revenue_ttm",
    "net_operate_cashflow_growth_rate",
)
SLOTS = dict(
    zip(
        FIELDS,
        (
            "PV01",
            "PV02",
            "PV03",
            "PV03",
            "PV04",
            "PV05",
            "PV06",
            "SV01",
            "SV02",
            "FI01",
            "FI02",
            "FI03",
            "FI04",
        ),
        strict=True,
    )
)
MINIMUM = dict(zip(FIELDS[:9], (121, 21, 7, 15, 60, 6, 6, 5, 1), strict=True))
UNITS = {
    "raw_price": "CNY/share",
    "volume": "shares",
    "amount": "CNY",
    "shares": "A_circulating_shares",
    "financial": "consolidated_YTD_CNY",
}


class BatchInputUnavailable(ValueError):
    """A typed source/semantics gap, not a zero factor."""


def canonical_day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("canonical date required")
    return value


def value_text(value):
    with localcontext() as context:
        context.prec = 50
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _number(value):
    if value is None or isinstance(value, bool):
        raise BatchInputUnavailable("MISSING_NUMERIC_INPUT")
    return F(str(value))


def _nearest_nonmidpoint(value, places):
    scaled = abs(value) * 10**places
    if scaled.denominator == 2:
        raise BatchInputUnavailable("ROUNDING_MIDPOINT_OUTSIDE_FROZEN_SCOPE")
    return quantize(value, places)


def _effective(rows, day, key):
    selected = [row for row in rows if row["date"] <= day]
    if not selected:
        raise BatchInputUnavailable("PRIOR_EFFECTIVE_INPUT_MISSING")
    latest = max(row["date"] for row in selected)
    candidates = {_number(row[key]) for row in selected if row["date"] == latest}
    if len(candidates) != 1:
        raise BatchInputUnavailable("AMBIGUOUS_EFFECTIVE_INPUT")
    result = candidates.pop()
    if result <= 0:
        raise BatchInputUnavailable("NONPOSITIVE_EFFECTIVE_INPUT")
    return result


def market_value(item, sessions, day, field, *, provider="sina"):
    """Shared exact arithmetic over separately identified raw OHLCV sources.

    Tencent comparisons share Sina factors/shares, so only the market-input
    layer is independent. VROC's V/F hypothesis remains explicitly uncertified.
    """
    canonical_day(day)
    if field not in MINIMUM or provider not in {"sina", "tencent"}:
        raise ValueError("explicit supported field/provider required")
    if sessions != sorted(set(sessions)) or day not in sessions:
        raise BatchInputUnavailable("INVALID_OR_MISSING_SESSION_GRID")
    pos, needed = sessions.index(day), MINIMUM[field]
    if pos + 1 < needed:
        raise BatchInputUnavailable("WARMUP_INSUFFICIENT")
    window = sessions[pos + 1 - needed : pos + 1]
    bars = item[provider]
    if any(d not in bars for d in window):
        raise BatchInputUnavailable("SOURCE_SESSION_MISSING")
    raw = [bars[d] for d in window]
    for row in raw:
        if _number(row["volume"]) <= 0 or _number(row["amount"]) <= 0:
            raise BatchInputUnavailable("NORMAL_TRADING_ONLY")
        low, high, close, opening = (_number(row[k]) for k in ("low", "high", "close", "open"))
        if low <= 0 or not low <= min(close, opening) <= max(close, opening) <= high:
            raise ValueError("invalid source OHLC")
    if field in {"ROC120", "Variance20", "ATR6", "ATR14", "EMA5", "VROC6"}:
        factors = [_effective(item["factors"], d, "value") for d in window]
        adjusted = [
            _number(row["close"]) * factor for row, factor in zip(raw, factors, strict=True)
        ]
        close = [quantize(v, 2) for v in adjusted]
    if field == "ROC120":
        return (adjusted[-1] / adjusted[0] - 1) * 100, window
    if field == "Variance20":
        returns = [b / a - 1 for a, b in zip(close[:-1], close[1:], strict=True)]
        mean = sum(returns, F()) / 20
        return quantize(sum(((v - mean) ** 2 for v in returns), F()) * F(250, 19), 6), window
    if field in {"ATR6", "ATR14"}:
        tr = []
        for i in range(1, needed):
            high = quantize(_number(raw[i]["high"]) * factors[i], 2)
            low = quantize(_number(raw[i]["low"]) * factors[i], 2)
            tr.append(max(high - low, abs(high - close[i - 1]), abs(low - close[i - 1])))
        return quantize(sum(tr, F()) / len(tr), 6), window
    if field == "EMA5":
        state = close[0]
        for current in close[1:]:
            state = (state * 2 + current) / 3
        return quantize(state / close[-1], 6), window
    if field == "VROC6":
        volume = [
            quantize(_number(row["volume"]) / factor, 0)
            for row, factor in zip(raw, factors, strict=True)
        ]
        if volume[0] <= 0:
            raise BatchInputUnavailable("ZERO_RESEARCH_ADJUSTED_VOLUME_BASE")
        return (volume[-1] / volume[0] - 1) * 100, window
    if field == "TVMA6":
        return sum((_number(row["amount"]) for row in raw), F()) / 6, window
    shares = [_effective(item["shares"], d, "value") for d in window]
    if any(v.denominator != 1 for v in shares):
        raise BatchInputUnavailable("NONINTEGER_SHARE_DENOMINATOR")
    if field == "VOL5":
        daily = [
            _nearest_nonmidpoint(_number(row["volume"]) * 100 / share, 4)
            for row, share in zip(raw, shares, strict=True)
        ]
        return sum(daily, F()) / 5, window
    cap = _number(raw[-1]["close"]) * shares[-1]
    # Preserve the existing bounded SV02 conversion path, including its old ULP failure.
    return F(float(_nearest_nonmidpoint(cap / 100_000_000, 4)) * 1e8), window


class SnapshotFinancials:
    """Current public YTD snapshot projected by reported publication dates.

    No claim that a currently returned revised number existed on its notice date.
    Exact four-quarter arithmetic checks the input chain, not historical visibility.
    """

    def __init__(self, rows):
        self.rows = rows
        seen = set()
        for row in rows:
            key = row["kind"], canonical_day(row["period"])
            if key in seen:
                raise ValueError("ambiguous financial period in source snapshot")
            seen.add(key)
            canonical_day(row["notice_date"])
            if row["notice_date"] < row["period"] or row["currency"] != "CNY":
                raise ValueError("invalid financial identity/unit/date")

    def select(self, field, period, cutoff):
        kind = "cash" if field == "NETCASH_OPERATE" else "income"
        selected = [
            row
            for row in self.rows
            if row["kind"] == kind and row["period"] == period and row["notice_date"] < cutoff
        ]
        if len(selected) != 1 or selected[0]["values"].get(field) is None:
            raise BatchInputUnavailable(f"FINANCIAL_VISIBLE_COMPONENT_MISSING:{field}:{period}")
        return selected[0]

    def latest(self, field, cutoff):
        kind = "cash" if field == "NETCASH_OPERATE" else "income"
        eligible = [
            row["period"]
            for row in self.rows
            if row["kind"] == kind
            and row["notice_date"] < cutoff
            and row["values"].get(field) is not None
        ]
        if not eligible:
            raise BatchInputUnavailable("FINANCIAL_VISIBLE_PERIOD_MISSING")
        return max(eligible)

    def ttm(self, field, period, cutoff):
        ends = ("03-31", "06-30", "09-30", "12-31")
        if period[5:] not in ends:
            raise ValueError("calendar quarter required")
        index = int(period[:4]) * 4 + ends.index(period[5:])
        values, sources = [], {}
        for i in range(index - 3, index + 1):
            year, quarter = divmod(i, 4)
            p = f"{year}-{ends[quarter]}"
            current = self.select(field, p, cutoff)
            sources[p] = current
            v = _number(current["values"][field])
            if quarter:
                previous_period = f"{year}-{ends[quarter - 1]}"
                previous = self.select(field, previous_period, cutoff)
                sources[previous_period] = previous
                v -= _number(previous["values"][field])
            values.append(v)
        # Structurally different annual bridge oracle, under the same snapshot.
        bridge = _number(self.select(field, period, cutoff)["values"][field])
        if period[5:] != "12-31":
            previous_year = int(period[:4]) - 1
            bridge += _number(self.select(field, f"{previous_year}-12-31", cutoff)["values"][field])
            bridge -= _number(
                self.select(field, f"{previous_year}{period[4:]}", cutoff)["values"][field]
            )
        total = sum(values, F())
        if total != bridge:
            raise ValueError("four quarters disagree with annual bridge")
        return total, list(sources.values())

    def factor(self, day, field, *, mode):
        if mode != MODE:
            raise BatchInputUnavailable("STRICT_PIT_UNPROVEN_CURRENT_SNAPSHOT")
        canonical_day(day)
        mapping = {
            "net_profit_to_total_operate_revenue_ttm": "NETPROFIT",
            "net_profit_growth_rate": "NETPROFIT",
            "operating_revenue_ttm": "OPERATE_INCOME",
            "net_operate_cashflow_growth_rate": "NETCASH_OPERATE",
        }
        source_field = mapping[field]
        period = self.latest(source_field, day)
        current, sources = self.ttm(source_field, period, day)
        if field == "operating_revenue_ttm":
            return current, sources, period
        if field == "net_profit_to_total_operate_revenue_ttm":
            base, more = self.ttm("TOTAL_OPERATE_INCOME", period, day)
            numerator = current
        else:
            prior_period = f"{int(period[:4]) - 1}{period[4:]}"
            prior_cutoff = day
            if field == "net_profit_growth_rate":
                today = date.fromisoformat(day)
                try:
                    prior_cutoff = today.replace(year=today.year - 1).isoformat()
                except ValueError:
                    prior_cutoff = today.replace(year=today.year - 1, day=28).isoformat()
            base, more = self.ttm(source_field, prior_period, prior_cutoff)
            numerator = current - base
        if base <= 0:
            raise BatchInputUnavailable("NONPOSITIVE_FINANCIAL_BASE_OUTSIDE_FROZEN_SCOPE")
        return _nearest_nonmidpoint(numerator / base, 6), sources + more, period


def compute_panel(dataset, dates, *, definition, mode=MODE, provider="sina"):
    """Return every requested cell, with explicit missingness and provenance flags."""
    if definition != DEFINITION or mode != MODE:
        raise ValueError("explicit research definition and snapshot mode required")
    if (
        dataset.get("schema") != "ad.f1-batch-inputs.v1"
        or dataset.get("units") != UNITS
        or dataset.get("raw_price_adjustment") != "none"
        or dataset.get("input_contains_JQ_values") is not False
    ):
        raise ValueError("explicit independent input schema, units and raw price basis required")
    if not dates or dates != sorted(set(dates)):
        raise ValueError("ordered unique output dates required")
    for day in dates + dataset["sessions"]:
        canonical_day(day)
    identities = [item["security"] for item in dataset["items"]]
    if len(set(identities)) != len(identities) or any(
        not re.fullmatch(r"\d{6}\.X(SHG|SHE)", s) for s in identities
    ):
        raise ValueError("unique canonical securities required")
    results = []
    for item in dataset["items"]:
        finance = SnapshotFinancials(item["financials"])
        for day in dates:
            for field in FIELDS:
                row = {
                    "security": item["security"],
                    "date": day,
                    "field": field,
                    "slot": SLOTS[field],
                    "value": None,
                    "status": "missing",
                    "provider": provider,
                    "definition": definition,
                    "production_eligible": False,
                    "jq_equivalent": False,
                    "historical_pit_certified": False,
                }
                try:
                    if field in MINIMUM:
                        value, window = market_value(
                            item, dataset["sessions"], day, field, provider=provider
                        )
                        row["window_start"] = window[0]
                        row["input_quality"] = (
                            "source_claimed_share_history"
                            if field in {"VOL5", "circulating_market_cap"}
                            else "retrospective_market_snapshot"
                        )
                        if field == "VROC6":
                            row["input_quality"] = "unproven_volume_adjustment_hypothesis"
                    else:
                        value, sources, period = finance.factor(day, field, mode=mode)
                        row["period_end"] = period
                        row["input_quality"] = MODE
                        row["component_updates_after_cutoff"] = sum(
                            s["update_date"] > day for s in sources
                        )
                        row["financial_sources"] = sorted(
                            {
                                (s["kind"], s["period"], s["notice_date"], s["update_date"])
                                for s in sources
                            }
                        )
                    row.update(value=value_text(value), status="computed")
                except BatchInputUnavailable as error:
                    row["reason"] = str(error)
                results.append(row)
    return results


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
