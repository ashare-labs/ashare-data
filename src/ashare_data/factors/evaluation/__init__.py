"""Offline factor diagnostics using pinned Alphalens computation, never an execution engine.

Importing this module does not import the optional analytics/plotting dependencies.
See docs/factors.md for units, lag, missing values, universe and cost conventions.
"""

from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
import math

import numpy as np
import pandas as pd

from .config import EvaluationConfig
from ._inputs import fingerprint, forward_labels, stacked, validate

__all__ = ["EvaluationConfig", "evaluate"]
SCHEMA = "ad.factor-evaluation.v1"
ADAPTER_VERSION = "alphalens.daily-diagnostics.v1"
ALPHALENS_VERSION = "0.5.0"


def _backend():
    try:
        installed = version("alphalens-reloaded")
    except PackageNotFoundError as exc:
        raise ImportError("Install ashare-data[factor-evaluation] to evaluate factors") from exc
    if installed != ALPHALENS_VERSION:
        raise ImportError(f"Evaluation requires alphalens-reloaded=={ALPHALENS_VERSION}")
    from alphalens import performance, utils
    from scipy.stats import pearsonr
    return performance, utils, pearsonr


def _number(value):
    return float(value) if pd.notna(value) and math.isfinite(value) else None


def _moments(series):
    finite = series.dropna()
    mean, std = finite.mean(), finite.std(ddof=1)
    # Repeated identical correlations can acquire a ~1e-16 std from mean
    # reduction rounding. Such a denominator is not an enormous information ratio.
    resolution = 8 * np.finfo(float).eps * max(1.0, float(finite.abs().max()) if len(finite) else 1.0)
    return dict(mean=_number(mean), std=_number(std), n=len(finite),
                positive_fraction=_number((finite > 0).mean()) if len(finite) else None,
                mean_over_std=_number(mean / std) if std > resolution else None)


def _quantiles(factor, dates, config, utils):
    quantile = pd.Series(np.nan, index=factor.index, name="factor_quantile")
    rejected = []
    for day in dates:
        mask = factor.index.get_level_values("date") == day
        section = factor.loc[mask]
        reason = None
        if len(section) < max(config.min_assets, config.quantiles):
            reason = "insufficient_factor_cross_section"
        else:
            try:
                assigned = utils.quantize_factor(section.to_frame("factor"),
                                                 quantiles=config.quantiles, no_raise=False)
            except ValueError as exc:
                if "Bin edges must be unique" not in str(exc):
                    raise
                reason = "tied_quantile_edges"
            else:
                if set(assigned) != set(range(1, config.quantiles + 1)):
                    reason = "empty_quantile"
                else:
                    quantile.loc[assigned.index] = assigned
        if reason:
            rejected.append(dict(date=day.strftime("%Y-%m-%d"), reason=reason, n=len(section)))
    return quantile.dropna(), rejected


def _turnover(quantile, dates, config, performance):
    rows = []
    for q in range(1, config.quantiles + 1):
        counts = quantile[quantile == q].groupby(level="date").size().reindex(dates, fill_value=0)
        raw = performance.quantile_turnover(quantile, q, period=1) if len(quantile) else pd.Series(dtype=float)
        raw = raw.reindex(dates).where(counts.gt(0) & counts.shift(1).gt(0))
        for day in dates:
            rows.append(dict(date=day.strftime("%Y-%m-%d"), quantile=q,
                             value=_number(raw.at[day]), n=int(counts.at[day])))
    return rows


def _stability(metrics, horizon, window):
    rows = []
    for month, frame in metrics.groupby(metrics.index.strftime("%Y-%m")):
        rows.append(dict(kind="monthly", period=month, horizon=horizon,
                         ic_mean=_number(frame.ic.mean()), rank_ic_mean=_number(frame.rank_ic.mean()),
                         spread_mean=_number(frame.spread.mean()),
                         observations=int(frame.rank_ic.count()),
                         ic_observations=int(frame.ic.count()), spread_observations=int(frame.spread.count())))
    rolled = metrics.rolling(window, min_periods=window).mean()
    counts = metrics.rolling(window, min_periods=1).count()
    for day, row in rolled.iterrows():
        rows.append(dict(kind="rolling", period=day.strftime("%Y-%m-%d"), horizon=horizon,
                         window=window, ic_mean=_number(row.ic), rank_ic_mean=_number(row.rank_ic),
                         spread_mean=_number(row.spread), observations=int(counts.at[day, "rank_ic"]),
                         ic_observations=int(counts.at[day, "ic"]),
                         spread_observations=int(counts.at[day, "spread"])))
    return rows


def evaluate(values, prices, *, membership, config):
    """Return JSON-safe diagnostics for one factor on an explicit daily session grid.

    Signal t uses P[t+lag+h]/P[t+lag]-1. Quantiles use only t's membership and
    finite signed factor; all selected members need labels for a quantile mean.
    Pearson IC and Spearman RankIC use per-horizon observed pairs and disclose n.
    Config and factors must be chosen without inspecting this report's future labels.
    """
    if isinstance(config, Mapping):
        config = EvaluationConfig(**config)
    if not isinstance(config, EvaluationConfig):
        raise TypeError("config must be EvaluationConfig or its field mapping")
    values, prices, membership, dates = validate(values, prices, membership, config)
    performance, utils, pearsonr = _backend()
    signed = (values * config.direction).loc[dates].where(membership.loc[dates])
    factor = stacked(signed).rename("factor")
    quantile, rejected = _quantiles(factor, dates, config, utils)
    quantile_counts = quantile.groupby([quantile.index.get_level_values("date"), quantile]).size()
    membership_rows = [dict(date=d.strftime("%Y-%m-%d"), security=s, quantile=int(q))
                       for (d, s), q in quantile.items()]
    coverage = dict(
        signal_start=dates[0].strftime("%Y-%m-%d"), signal_end=dates[-1].strftime("%Y-%m-%d"),
        sessions=len(dates), securities=values.columns.tolist(),
        input_cells=len(dates) * len(values.columns),
        member_cells=int(membership.loc[dates].sum().sum()), finite_factor_cells=len(factor),
        missing_factor_cells=int((membership.loc[dates] & values.loc[dates].isna()).sum().sum()),
        excluded_member_cells=int((~membership.loc[dates]).sum().sum()),
        quantized_cells=len(quantile), rejected_quantile_dates=rejected, by_date=[], by_horizon=[],
    )
    for day in dates:
        coverage["by_date"].append(dict(
            date=day.strftime("%Y-%m-%d"), members=int(membership.loc[day].sum()),
            finite_factors=int(signed.loc[day].notna().sum()),
            quantized=int((quantile.index.get_level_values("date") == day).sum()),
        ))
    output = dict(schema=SCHEMA, config=config.to_dict(), engine=dict(
        library="alphalens-reloaded", version=ALPHALENS_VERSION, adapter=ADAPTER_VERSION,
        pandas=version("pandas"), numpy=version("numpy"), scipy=version("scipy"),
        input_sha256=fingerprint(values, prices, membership),
        functions=["utils.quantize_factor", "performance.factor_information_coefficient",
                   "performance.mean_return_by_quantile", "performance.compute_mean_returns_spread",
                   "performance.quantile_turnover", "scipy.stats.pearsonr"],
    ), summary=[], coverage=coverage, ic=[], rank_ic=[], quantile_returns=[], spread=[],
                  quantile_membership=membership_rows, turnover=_turnover(quantile, dates, config, performance),
                  stability=[], cost_sensitivity=[], warnings=[])
    for horizon in config.horizons:
        labels, reasons = forward_labels(prices, dates, config.lag, horizon)
        # Each horizon has its own pairwise sample; a missing 10-session label
        # must not remove an otherwise observed 1-session pair.
        pairs = factor.to_frame().join(stacked(labels).rename(f"{horizon}D")).dropna()
        column = f"{horizon}D"  # Alphalens column syntax; API unit remains supplied sessions.
        ic, rank_ic = pd.Series(np.nan, index=dates), pd.Series(np.nan, index=dates)
        pair_counts = pairs.groupby(level="date").size().reindex(dates, fill_value=0)
        valid_sections = []
        for day, section in pairs.groupby(level="date"):
            if (len(section) >= config.min_assets and section.factor.nunique() > 1
                    and section[column].nunique() > 1):
                ic.at[day] = pearsonr(section.factor, section[column]).statistic
                valid_sections.append(section)
        if valid_sections:
            rank_ic = performance.factor_information_coefficient(pd.concat(valid_sections))[column].reindex(dates)
        for day in dates:
            base = dict(date=day.strftime("%Y-%m-%d"), horizon=horizon, n=int(pair_counts.at[day]))
            output["ic"].append({**base, "value": _number(ic.at[day])})
            output["rank_ic"].append({**base, "value": _number(rank_ic.at[day])})
        reason_counts = reasons.where(signed.notna()).stack(future_stack=True).dropna().value_counts()
        coverage["by_horizon"].append(dict(horizon=horizon, eligible=len(factor), paired=len(pairs),
            excluded={str(k): int(v) for k, v in reason_counts.items() if k != "ok"},
            by_date=[dict(date=d.strftime("%Y-%m-%d"), paired=int(pair_counts.at[d]),
                          eligible=int(signed.loc[d].notna().sum())) for d in dates]))
        data = pairs.join(quantile, how="inner")
        full_index = pd.MultiIndex.from_product([range(1, config.quantiles + 1), dates],
                                               names=["factor_quantile", "date"])
        means = pd.DataFrame(np.nan, index=full_index, columns=[column])
        observed = pd.Series(0, index=full_index)
        if not data.empty:
            computed, _ = performance.mean_return_by_quantile(data, by_date=True,
                                                             demeaned=False, group_adjust=False)
            means = computed.reindex(full_index)
            observed = data.groupby(["factor_quantile", data.index.get_level_values("date")]).size().reindex(full_index, fill_value=0)
        for q, day in full_index:
            selected = int(quantile_counts.get((day, q), 0))
            n = int(observed.at[(q, day)])
            if not selected or n != selected:
                means.at[(q, day), column] = np.nan
            output["quantile_returns"].append(dict(date=day.strftime("%Y-%m-%d"), horizon=horizon,
                quantile=q, mean_return=_number(means.at[(q, day), column]), n=n, selected_n=selected))
        spread, _ = performance.compute_mean_returns_spread(means, config.quantiles, 1)
        # 0.5.0 normalizes the two legs to 100% gross; our declared statistic is
        # the raw high-minus-low difference (one unit each leg, 200% gross).
        spread = spread[column].mul(2).reindex(dates)
        for day in dates:
            output["spread"].append(dict(date=day.strftime("%Y-%m-%d"), horizon=horizon,
                                         value=_number(spread.at[day])))
        metrics = pd.DataFrame(dict(ic=ic, rank_ic=rank_ic, spread=spread))
        output["stability"].extend(_stability(metrics, horizon, config.rolling_window))
        top, bottom = means.xs(config.quantiles)[column], means.xs(1)[column]
        common = spread.notna()
        for bps in config.cost_bps:
            rate = bps / 10000
            output["cost_sensitivity"].append(dict(horizon=horizon, cost_bps=bps,
                top_mean=_number(top[common].mean() - 2 * rate),
                bottom_mean=_number(bottom[common].mean() - 2 * rate),
                spread_mean=_number(spread[common].mean() - 4 * rate), n=int(common.sum())))
        output["summary"].append(dict(horizon=horizon, ic=_moments(ic), rank_ic=_moments(rank_ic),
            spread=_moments(spread), quantile_means=[dict(quantile=q,
                **_moments(means.xs(q)[column])) for q in range(1, config.quantiles + 1)]))
    output["assumptions"] = dict(
        return_unit="decimal simple return over horizon trading sessions",
        signal_clock="factor known after session close; entry at t+lag close, exit at t+lag+h close",
        forward_return="P[t+lag+h]/P[t+lag]-1; complete observed entry-to-exit path required",
        quantiles="signed factor, equal-frequency qcut, ties not broken; signal-date membership only",
        missing_policy="IC uses observed pairs per horizon; quantile mean requires every selected label",
        stability="monthly/rolling descriptive means by signal date; available only after labels mature",
        ic_ratio="mean/sample_std, unannualized; std <= 8*float64_eps*max(1,max_abs_value) is undefined; no independent-sample t-stat",
        turnover="Alphalens entering names/current names between adjacent supplied sessions; not traded weight",
        cost_model="independent_cohort_full_round_trip",
        cost_basis="one-way bps on fixed entry notional, each long leg pays 2*bps; two-leg spread pays 4*bps",
        cost_sample="top/bottom/spread use common complete-label dates; no netting or borrow fee",
        spread="Qmax-Q1; diagnostic one-unit long and one-unit short, 200% gross; not a funded NAV",
        execution_simulated=False, historical_pit_verified=False,
    )
    output["warnings"] = [
        "Research diagnostics only: no T+1, halts, price limits, liquidity, lot size, borrow availability or fills are simulated.",
        "Input historical availability and universe provenance are caller declarations, not verified by this evaluator.",
        "Overlapping horizon returns are not a compounded strategy NAV; descriptive IC ratios are not significance tests.",
        "Missing future prices can bias observed-pair IC; per-horizon exclusions and incomplete quantiles remain visible.",
    ]
    if config.universe_mode == "fixed_sample":
        output["warnings"].append("Fixed selected universe; retrospective selection/survivorship bias is not removed.")
    if rejected:
        output["warnings"].append("Some signal dates cannot form all requested quantiles; see rejected_quantile_dates.")
    return output
