"""Versioned, offline GTJA Alpha191 research formulas over explicit panel inputs.

See docs/alpha191.md for the numerical contract and remaining source ambiguities.
The 18 pre-existing F1/F2 factors and their JQ comparisons are independent APIs.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from copy import deepcopy
from importlib.resources import files
import hashlib
import json
import re

import numpy as np
import pandas as pd

from ._engine import (
    AMBIGUOUS,
    DEPENDENCY,
    EXCLUDED,
    MISSING,
    OK,
    STATUS,
    Engine,
    characteristics,
    panel,
)
from ._formulas import AMBIGUOUS as UNRESOLVED, FORMULAS, NOTES, EXPLICIT_FORMULAS, EXPLICIT_NOTES

VERSION = "gtja191.public.v1"
EXPLICIT_VERSION = "gtja191.explicit.v1"
ENGINE_VERSION = "alpha191.python.v3"
VERSIONS = (VERSION, EXPLICIT_VERSION)
UNITS = {"price": "CNY/share", "volume": "share", "amount": "CNY"}
MARKET_FIELDS = {"open", "high", "low", "close", "volume", "amount", "vwap"}
__all__ = [
    "Alpha191",
    "Alpha191Stream",
    "Alpha191Result",
    "VERSION",
    "EXPLICIT_VERSION",
    "ENGINE_VERSION",
    "VERSIONS",
    "STATUS",
    "coverage",
]


def _definitions(version):
    if version not in VERSIONS:
        raise ValueError(f"version must be one of {VERSIONS}")
    return FORMULAS | EXPLICIT_FORMULAS if version == EXPLICIT_VERSION else FORMULAS


def _ids(ids):
    if ids is None:
        return list(range(1, 192))
    if isinstance(ids, (int, str)):
        ids = [ids]
    out = []
    for value in ids:
        if isinstance(value, str) and re.fullmatch(r"alpha\d{3}", value):
            value = int(value[5:])
        if type(value) is not int or not 1 <= value <= 191:
            raise ValueError("factor IDs must be 1..191 or alpha001..alpha191")
        if value in out:
            raise ValueError("duplicate factor ID")
        out.append(value)
    if not out:
        raise ValueError("at least one factor ID required")
    return out


def coverage(*, version=VERSION):
    """One row for every ID; availability is independent of a particular dataset."""
    sources = json.loads(files(__package__).joinpath("sources.json").read_text())
    formulas = _definitions(version)
    rows = []
    for i in range(1, 192):
        expression = formulas.get(i)
        deps, rows_needed, state, rank = (
            characteristics(expression) if expression else (set(), None, False, False)
        )
        # Even blocked formulas disclose the fields they would need.
        if not expression:
            deps = set(sources["factors"][str(i)]["dependencies"])
        rows.append(
            {
                "factor": f"alpha{i:03d}",
                "library": "alpha191",
                "namespace": "alpha191",
                "library_label": "Alpha191",
                "qualified_name": f"alpha191:alpha{i:03d}",
                "version": version,
                "source_ambiguity": i in UNRESOLVED,
                "availability": (
                    "AMBIGUOUS_FORMULA"
                    if expression is None
                    else "REQUIRES_EXTERNAL_FACTORS"
                    if i == 30
                    else "REQUIRES_EXPLICIT_SEED"
                    if i == 143
                    else "REQUIRES_EXPLICIT_PARAMETER"
                    if i == 146
                    else "REQUIRES_BENCHMARK"
                    if i in {75, 149, 181, 182}
                    else "IMPLEMENTED"
                ),
                "dependencies": tuple(sorted(deps)),
                "full_window_rows": rows_needed,
                "recursive_or_event_history": state,
                "cross_section_rank": rank,
                "note": (
                    EXPLICIT_NOTES[i]
                    if version == EXPLICIT_VERSION and i in EXPLICIT_NOTES
                    else UNRESOLVED.get(i, NOTES.get(i, ""))
                ),
                "expression": expression,
            }
        )
    return pd.DataFrame(rows).set_index("factor")


def _axes(frame):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("nonempty DataFrame universe required")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("explicit DatetimeIndex trading-session grid required")
    if frame.index.tz is not None or not frame.index.equals(frame.index.normalize()):
        raise ValueError("session dates must be timezone-naive midnight labels")
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing or frame.index.hasnans:
        raise ValueError("session grid must be unique, finite and increasing")
    if not frame.columns.is_unique or any(not isinstance(x, str) or not x for x in frame.columns):
        raise ValueError("unique nonempty security names required")


def _numeric(frame, index, columns, name):
    if (
        not isinstance(frame, pd.DataFrame)
        or not frame.index.equals(index)
        or not frame.columns.equals(columns)
    ):
        raise ValueError(f"{name}: exact ordered date/security axes required; reindex explicitly")
    if any(
        not pd.api.types.is_numeric_dtype(t) or pd.api.types.is_bool_dtype(t) for t in frame.dtypes
    ):
        raise ValueError(f"{name}: numeric columns required")
    a = frame.to_numpy(dtype=float, na_value=np.nan)
    if np.isinf(a).any():
        raise ValueError(f"{name}: infinity is not an input value")
    return a


@dataclass
class Alpha191Result:
    values: pd.DataFrame
    status: pd.DataFrame
    report: pd.DataFrame
    metadata: dict

    def status_labels(self):
        """Human-readable per-cell status; status itself stores compact uint8 codes."""
        return self.status.map(STATUS.__getitem__)

    def between(self, start=None, end=None):
        """Slice outputs only; the calculation retains all supplied prehistory."""
        dates = self.values.index.get_level_values("date")
        mask = np.ones(len(dates), dtype=bool)
        if start is not None:
            mask &= dates >= pd.Timestamp(start)
        if end is not None:
            mask &= dates <= pd.Timestamp(end)
        return _result(self.values.loc[mask], self.status.loc[mask], self.metadata)


def _result(values, status, metadata):
    report = coverage(version=metadata["version"]).loc[values.columns].copy()
    report["valid_cells"] = (status == OK).sum(axis=0)
    report["total_cells"] = len(values)
    report["status_counts"] = [
        {STATUS[int(k)]: int(v) for k, v in status[c].value_counts().items()}
        for c in values.columns
    ]
    return Alpha191Result(values, status, report, deepcopy(metadata))


class Alpha191:
    """Freeze explicit date x security frames; compute with no network or backfill.

    ``universe`` is a Boolean membership frame on the complete caller-declared
    session grid. Each price/quantity frame must have those exact axes, including
    NaN rows for absent observations. It is never silently reindexed or filled.
    ``adjustment`` must be ``raw`` or ``adjusted:<immutable policy/snapshot id>``.
    Adjusted VWAP must already be on the same basis as adjusted OHLC; amount stays
    real CNY turnover and volume stays actual shares. No synthetic VWAP is inferred.
    """

    def __init__(
        self,
        data: Mapping[str, pd.DataFrame],
        *,
        universe: pd.DataFrame,
        adjustment: str,
        benchmark: pd.DataFrame | None = None,
        factors: pd.DataFrame | None = None,
        alpha143_seed=None,
        alpha146_m=None,
        version=VERSION,
        rank_policy="strict",
        units=None,
        input_id=None,
    ):
        self.formulas = _definitions(version)
        if alpha146_m is not None:
            if version != EXPLICIT_VERSION:
                raise ValueError("alpha146_m requires the explicit definition version")
            if type(alpha146_m) is not int or not 1 <= alpha146_m <= 60:
                raise ValueError("alpha146_m must be an explicit integer in 1..60")
        _axes(universe)
        if universe.isna().any().any() or any(
            not pd.api.types.is_bool_dtype(t) for t in universe.dtypes
        ):
            raise ValueError("universe must contain known Boolean membership on every cell")
        if rank_policy not in {"strict", "available"}:
            raise ValueError("rank_policy must be strict or available")
        if not isinstance(adjustment, str) or not (
            adjustment == "raw" or (adjustment.startswith("adjusted:") and len(adjustment) > 9)
        ):
            raise ValueError("explicit raw or adjusted:<policy/snapshot id> basis required")
        if units is not None and dict(units) != UNITS:
            raise ValueError(
                "only CNY/share prices, share volume and CNY amount accepted; convert explicitly"
            )
        if not isinstance(data, Mapping) or not data or set(data) - MARKET_FIELDS:
            raise ValueError("data must map supported market field names to frames")
        self.index, self.columns = universe.index.copy(), universe.columns.copy()
        self.membership = universe.to_numpy(dtype=bool, copy=True)
        arrays = {
            name: _numeric(frame, self.index, self.columns, name).copy()
            for name, frame in data.items()
        }
        for name, a in arrays.items():
            if np.any(a[np.isfinite(a)] < 0) or (
                name in {"open", "high", "low", "close", "vwap"} and np.any(a == 0)
            ):
                raise ValueError(f"{name}: prices must be positive; volume/amount nonnegative")
        if {"open", "high", "low", "close"} <= arrays.keys():
            o, h, low, c = (arrays[k] for k in ["open", "high", "low", "close"])
            if ((low > h) | (o < low) | (o > h) | (c < low) | (c > h)).any():
                raise ValueError("OHLC bounds violated")
        for extra, expected, rename in [
            (benchmark, {"open", "close"}, {"open": "benchmark_open", "close": "benchmark_close"}),
            (factors, {"mkt", "smb", "hml"}, {}),
        ]:
            if extra is None:
                continue
            if (
                not isinstance(extra, pd.DataFrame)
                or not extra.columns.is_unique
                or not len(extra.columns)
                or (
                    not set(extra.columns) <= expected if rename else set(extra.columns) != expected
                )
                or not extra.index.equals(self.index)
            ):
                raise ValueError("benchmark/factors require exact session index and named columns")
            for name in extra:
                tiled = pd.DataFrame(
                    np.repeat(extra[name].to_numpy()[:, None], len(self.columns), axis=1),
                    index=self.index,
                    columns=self.columns,
                )
                a = _numeric(tiled, self.index, self.columns, name)
                if rename and np.any(a[np.isfinite(a)] <= 0):
                    raise ValueError("benchmark prices must be positive")
                arrays[rename.get(name, name)] = a.copy()
        self.seed = None
        if alpha143_seed is not None:
            if isinstance(alpha143_seed, Mapping):
                if set(alpha143_seed) != set(self.columns):
                    raise ValueError("seed mapping must match every security")
                seed = [alpha143_seed[c] for c in self.columns]
            elif np.isscalar(alpha143_seed) and not isinstance(alpha143_seed, (str, bool)):
                seed = [alpha143_seed] * len(self.columns)
            else:
                raise ValueError("seed must be finite scalar or complete security mapping")
            self.seed = np.array(seed, dtype=float)
            if not np.isfinite(self.seed).all():
                raise ValueError("finite Alpha143 seed required")
        self.fields = {
            name: panel(a, np.where(np.isnan(a), MISSING, OK)) for name, a in arrays.items()
        }
        if alpha146_m is not None:
            self.fields["alpha146_m"] = alpha146_m
        self.rank_policy = rank_policy
        digest = hashlib.sha256()
        digest.update(self.index.asi8.tobytes())
        digest.update(json.dumps(self.columns.tolist(), ensure_ascii=False).encode())
        digest.update(self.membership.tobytes())
        for name in sorted(arrays):
            digest.update(name.encode())
            a = arrays[name].copy()
            a[np.isnan(a)] = np.nan
            digest.update(a.astype("<f8").tobytes())
        definition = {
            "version": version,
            "engine_version": ENGINE_VERSION,
            "adjustment": adjustment,
            "rank_policy": rank_policy,
            "alpha143_seed": None if self.seed is None else self.seed.tolist(),
            "units": dict(UNITS),
        }
        if version == EXPLICIT_VERSION:
            definition["alpha146_m"] = alpha146_m
        digest.update(json.dumps(definition, sort_keys=True).encode())
        self.metadata = {
            **definition,
            "library": "alpha191",
            "namespace": "alpha191",
            "input_id": input_id,
            "input_sha256": digest.hexdigest(),
            "history_start": str(self.index[0].date()),
            "history_end": str(self.index[-1].date()),
            "status_codes": dict(STATUS),
            "data_access": "offline",
            "point_in_time_certified": False,
            "joinquant_equivalence": False,
        }

    def compute(self, ids=None):
        selected = _ids(ids)
        engine = Engine(self.fields, self.membership, self.rank_policy, self.seed)
        values, statuses, missing_deps = {}, {}, {}
        shape = self.membership.shape
        for i in selected:
            name = f"alpha{i:03d}"
            if i not in self.formulas:
                result = panel(np.full(shape, np.nan), AMBIGUOUS)
            else:
                deps = characteristics(self.formulas[i])[0] - self.fields.keys()
                if i == 143 and self.seed is None:
                    deps.add("alpha143_seed")
                if deps:
                    missing_deps[name] = sorted(deps)
                    result = panel(np.full(shape, np.nan), DEPENDENCY)
                else:
                    engine.cache.clear()
                    result = engine.evaluate(self.formulas[i])
            a, r = result.values.copy(), result.reasons.copy()
            a[~self.membership], r[~self.membership] = np.nan, EXCLUDED
            values[name], statuses[name] = a.ravel(), r.ravel()
        index = pd.MultiIndex.from_product([self.index, self.columns], names=["date", "security"])
        return _result(
            pd.DataFrame(values, index=index),
            pd.DataFrame(statuses, index=index),
            {**self.metadata, "missing_dependencies": missing_deps},
        )


class Alpha191Stream:
    """Exact append-only block interface retaining prehistory, not a finite overlap.

    Each append recomputes the retained prefix with the same version and pool;
    this correctness-first interface is intended for bounded research, not tick
    streaming. Cost grows with retained history; bulk compute is faster.
    """

    def __init__(self, *, ids=None, **options):
        self.ids = _ids(ids)
        if set(options) & {"data", "universe", "benchmark", "factors"}:
            raise ValueError("pass data, universe, benchmark and factors to append")
        self.options = deepcopy(options)
        self._data = None
        self._universe = None
        self._benchmark = None
        self._factors = None

    def append(self, data, *, universe, benchmark=None, factors=None):
        _axes(universe)
        if self._universe is None:
            combined = {k: v.copy(deep=True) for k, v in data.items()}
            pool = universe.copy(deep=True)
            bench, ff = benchmark, factors
        else:
            if universe.index[0] <= self._universe.index[-1] or not universe.columns.equals(
                self._universe.columns
            ):
                raise ValueError("append requires later sessions and unchanged security axes")
            if set(data) != set(self._data):
                raise ValueError("append requires unchanged input fields")
            for old, new in [(self._benchmark, benchmark), (self._factors, factors)]:
                if (old is None) != (new is None):
                    raise ValueError("append requires unchanged external dependency presence")
                if old is not None and (
                    not isinstance(new, pd.DataFrame) or set(old.columns) != set(new.columns)
                ):
                    raise ValueError("append requires unchanged external dependency fields")
            # Validate block alignment BEFORE concatenation, which could otherwise
            # conceal malformed axes by aligning/filling them.
            Alpha191(data, universe=universe, benchmark=benchmark, factors=factors, **self.options)
            combined = {k: pd.concat([self._data[k], data[k]]) for k in data}
            pool = pd.concat([self._universe, universe])
            bench = None if benchmark is None else pd.concat([self._benchmark, benchmark])
            ff = None if factors is None else pd.concat([self._factors, factors])
        calculation = Alpha191(combined, universe=pool, benchmark=bench, factors=ff, **self.options)
        result = calculation.compute(self.ids).between(universe.index[0])
        # Commit state only after successful validation and calculation.
        self._data = {k: v.copy(deep=True) for k, v in combined.items()}
        self._universe = pool.copy(deep=True)
        self._benchmark = None if bench is None else bench.copy(deep=True)
        self._factors = None if ff is None else ff.copy(deep=True)
        return result
