"""Validate explicit axes and align future labels without filling or filtering signals."""

import hashlib
import json

import numpy as np
import pandas as pd


def validate(values, prices, membership, config):
    if not isinstance(values, pd.DataFrame) or values.empty:
        raise ValueError("values: nonempty DataFrame required")
    index, columns = values.index, values.columns
    if not isinstance(index, pd.DatetimeIndex) or (
        index.tz is not None or index.hasnans or not index.is_unique
        or not index.is_monotonic_increasing or not index.equals(index.normalize())
    ):
        raise ValueError("explicit unique increasing timezone-naive midnight session grid required")
    if not columns.is_unique or any(not isinstance(s, str) or not s for s in columns):
        raise ValueError("unique nonempty security strings required")
    frames = []
    for name, frame in (("values", values), ("prices", prices), ("membership", membership)):
        if not isinstance(frame, pd.DataFrame) or (
            not frame.index.equals(index) or not frame.columns.equals(columns)
        ):
            raise ValueError(f"{name}: exact ordered date/security axes required")
        if name == "membership":
            if frame.isna().any().any() or not all(
                pd.api.types.is_bool_dtype(t) for t in frame.dtypes
            ):
                raise ValueError("membership: explicit Boolean cells, no missing values required")
            array = frame.to_numpy(dtype=bool, copy=True)
        else:
            if any(not pd.api.types.is_numeric_dtype(t) or pd.api.types.is_bool_dtype(t)
                   or pd.api.types.is_complex_dtype(t) for t in frame.dtypes):
                raise ValueError(f"{name}: real numeric cells required")
            array = frame.to_numpy(dtype=float, na_value=np.nan, copy=True)
            if np.isinf(array).any():
                raise ValueError(f"{name}: infinity is not a missing-value marker")
            if name == "prices" and np.any(array <= 0):
                raise ValueError("prices: finite observations must be positive; use NaN for absent")
        # Alphalens must not infer calendar days or bridge supplied session gaps.
        frames.append(pd.DataFrame(array, index=pd.DatetimeIndex(index.to_numpy(), name="date"),
                                   columns=pd.Index(columns, name="asset")))
    values, prices, membership = frames
    known = values.notna().any(axis=1)
    inferred = values.index[known]
    start = pd.Timestamp(config.start_date) if config.start_date else (
        inferred[0] if len(inferred) else values.index[0]
    )
    end = pd.Timestamp(config.end_date) if config.end_date else (
        inferred[-1] if len(inferred) else values.index[-1]
    )
    if start not in values.index or end not in values.index or start > end:
        raise ValueError("signal start/end must be ordered dates on the supplied session grid")
    dates = values.loc[start:end].index
    return values, prices, membership, dates


def fingerprint(values, prices, membership):
    digest = hashlib.sha256()
    digest.update(json.dumps({"dates": values.index.strftime("%Y-%m-%d").tolist(),
                              "securities": values.columns.tolist()},
                             sort_keys=True, separators=(",", ":")).encode())
    for name, frame in (("values", values), ("prices", prices), ("membership", membership)):
        array = np.array(frame, dtype="u1" if name == "membership" else "<f8", order="C")
        if name != "membership":
            array[np.isnan(array)] = np.nan  # canonical quiet NaN payload
            array[array == 0] = 0.0
        digest.update(name.encode())
        digest.update(array.tobytes())
    return digest.hexdigest()


def forward_labels(prices, dates, lag, horizon):
    """Strict complete observed path from entry to exit, inclusive; no imputation."""
    entry, exit_ = prices.shift(-lag), prices.shift(-(lag + horizon))
    # A missing interior price does not become an implicitly tradable interval.
    complete = prices.notna().rolling(horizon + 1, min_periods=horizon + 1).sum()
    complete = complete.shift(-(lag + horizon)).eq(horizon + 1)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        labels = exit_.div(entry).sub(1).where(complete)
    labels = labels.where(np.isfinite(labels))
    reasons = pd.DataFrame("ok", index=prices.index, columns=prices.columns)
    reasons[~complete] = "missing_path_price"
    reasons[exit_.isna()] = "missing_exit_price"
    reasons[entry.isna()] = "missing_entry_price"
    reasons[complete & labels.isna()] = "nonfinite_return"
    positions = np.arange(len(prices))
    reasons.loc[positions + lag + horizon >= len(prices)] = "insufficient_label_buffer"
    return labels.loc[dates], reasons.loc[dates]


def stacked(frame):
    return frame.stack(future_stack=True).dropna()
