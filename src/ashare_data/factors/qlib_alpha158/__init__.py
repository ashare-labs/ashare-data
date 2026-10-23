"""Pinned official default Qlib Alpha158 features over frozen explicit panels.

This is feature calculation only: no labels, fitted processors, models or I/O.
Expressions and numerical semantics derive from Microsoft Qlib (MIT).
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
import hashlib
import json
import re

import numpy as np
import pandas as pd

from ashare_data.factors.alpha191 import _axes, _numeric, UNITS
from ashare_data.factors.qlib_alpha158._ops import Operators

VERSION = "qlib.alpha158.v0.9.7"
ENGINE_VERSION = "qlib-alpha158.python.v1"
QLIB_COMMIT = "da920b7f954f48ab1bb64117c976710de198373e"
NAMESPACE = "qlib_alpha158"
MARKET_FIELDS = {"open", "high", "low", "close", "volume", "vwap", "amount"}
STATUS = {
    0: "OK",
    1: "QLIB_NAN",
    2: "POSITIVE_INFINITY",
    3: "NEGATIVE_INFINITY",
    4: "OUT_OF_UNIVERSE",
    5: "MISSING_DEPENDENCY",
}
__all__ = ["Alpha158", "Alpha158Result", "coverage", "VERSION", "ENGINE_VERSION", "QLIB_COMMIT"]


def coverage():
    """Official names/order, expressions and context sizes; no full-window mask."""
    snapshot = json.loads(files(__package__).joinpath("features.json").read_text())
    rows = []
    for feature in snapshot["features"]:
        name, expression = feature["name"], feature["expression"]
        window = re.search(r"(\d+)$", name)
        rows.append(
            dict(
                factor=name,
                library=NAMESPACE,
                namespace=NAMESPACE,
                library_label="Qlib Alpha158",
                qualified_name=f"{NAMESPACE}:{name}",
                definition=VERSION,
                expression=expression,
                dependencies=tuple(sorted(set(re.findall(r"\$([a-z]+)", expression)))),
                window=int(window[1]) if window else 0,
                lookback=feature["lookback"],
                lookahead=feature["lookahead"],
                full_window_rows=feature["lookback"] + 1,
                warmup_policy="Qlib min_periods=1; context length is not a validity mask",
                recursive_or_event_history=False,
                cross_section_rank=False,
                availability="IMPLEMENTED",
                api_fields=(name,),
                entrypoint="ashare_data.factors.qlib_alpha158.Alpha158.compute",
                documentation="docs/factors.md",
                source=snapshot["source"],
                upstream_commit=QLIB_COMMIT,
                license="MIT",
                historical_comparison="Pinned official float32 expressions; no provider/PIT equivalence",
                note="158 engineered price/volume features, not 158 effective strategies. "
                "Explicit VWAP and volume basis; no fitted normalization or fill.",
            )
        )
    return pd.DataFrame(rows).set_index("factor")


@dataclass
class Alpha158Result:
    values: pd.DataFrame
    status: pd.DataFrame
    report: pd.DataFrame
    metadata: dict

    def status_labels(self):
        return self.status.map(STATUS.__getitem__)

    def between(self, start=None, end=None):
        dates = self.values.index.get_level_values("date")
        keep = np.ones(len(dates), dtype=bool)
        if start is not None:
            keep &= dates >= pd.Timestamp(start)
        if end is not None:
            keep &= dates <= pd.Timestamp(end)
        return _result(self.values.loc[keep], self.status.loc[keep], self.metadata)


def _result(values, status, metadata):
    report = coverage().loc[values.columns].copy()
    report["valid_cells"] = (status == 0).sum()
    report["total_cells"] = len(values)
    report["status_counts"] = [
        {STATUS[int(k)]: int(v) for k, v in status[c].value_counts().items()} for c in values
    ]
    return Alpha158Result(values, status, report, deepcopy(metadata))


class Alpha158:
    """Same date × security panel convention as Alpha191, with Qlib semantics.

    All numeric inputs are explicitly quantized to float32 as in Qlib local
    binary storage; expressions keep upstream intermediate dtypes and final
    results are float32. NaN and signed infinity in outputs are retained.
    ``adjustment`` identifies raw or adjusted:<snapshot> prices. ``volume_basis``
    and ``vwap_basis`` must describe actual supplied quantities, not an assumed
    vendor convention. VWAP is never synthesized here. No input is filled.

    Optional instrument_ranges maps EVERY security to inclusive (start,end)
    session labels inside the supplied grid. This is data lifetime, independent
    of membership; missing rows inside a lifetime retain their calendar slots.
    Without ranges, each security has the full supplied history.
    """

    def __init__(
        self,
        data,
        *,
        universe,
        adjustment,
        volume_basis,
        vwap_basis,
        input_id=None,
        instrument_ranges=None,
        units=None,
        version=VERSION,
    ):
        if version != VERSION:
            raise ValueError("unsupported Qlib Alpha158 definition version")
        _axes(universe)
        if universe.isna().any().any() or any(
            not pd.api.types.is_bool_dtype(t) for t in universe.dtypes
        ):
            raise ValueError("universe requires known Boolean membership")
        if not isinstance(adjustment, str) or not (
            adjustment == "raw" or (adjustment.startswith("adjusted:") and len(adjustment) > 9)
        ):
            raise ValueError("explicit raw or adjusted:<snapshot> basis required")
        if any(not isinstance(v, str) or not v.strip() for v in (volume_basis, vwap_basis)):
            raise ValueError("explicit volume_basis and vwap_basis required")
        if units is not None and dict(units) != UNITS:
            raise ValueError("convert explicitly to CNY/share prices, share volume and CNY amount")
        if not isinstance(data, Mapping) or not data or set(data) - MARKET_FIELDS:
            raise ValueError("data must map supported market fields to frames")
        self.index, self.columns = universe.index.copy(), universe.columns.copy()
        self.membership = universe.to_numpy(dtype=bool, copy=True)
        self.fields = {}
        digest = hashlib.sha256()
        digest.update(self.index.asi8.tobytes())
        digest.update(json.dumps(self.columns.tolist()).encode())
        digest.update(self.membership.tobytes())
        for name in sorted(data):
            array = _numeric(data[name], self.index, self.columns, name)
            if np.any(np.abs(array[np.isfinite(array)]) > np.finfo(np.float32).max):
                raise ValueError(f"{name}: input overflows Qlib float32 storage")
            values = array.astype("<f4")
            self.fields[name] = pd.DataFrame(values, index=self.index, columns=self.columns)
            digest.update(name.encode())
            digest.update(values.tobytes())
        if instrument_ranges is None:
            instrument_ranges = {s: (self.index[0], self.index[-1]) for s in self.columns}
        if not isinstance(instrument_ranges, Mapping) or set(instrument_ranges) != set(
            self.columns
        ):
            raise ValueError("instrument_ranges must name every security")
        self.ranges = {}
        for sid, bounds in instrument_ranges.items():
            if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
                raise ValueError("instrument_ranges require (start,end)")
            start, end = map(pd.Timestamp, bounds)
            if start not in self.index or end not in self.index or start > end:
                raise ValueError("instrument range must be ordered and inside supplied sessions")
            self.ranges[sid] = (start, end)
        contracts = dict(
            version=VERSION,
            engine_version=ENGINE_VERSION,
            library=NAMESPACE,
            namespace=NAMESPACE,
            qlib_commit=QLIB_COMMIT,
            adjustment=adjustment,
            volume_basis=volume_basis,
            vwap_basis=vwap_basis,
            units=dict(UNITS),
            instrument_ranges={
                s: [str(d.date()) for d in bounds] for s, bounds in self.ranges.items()
            },
            input_dtype="float32",
            output_dtype="float32",
            processors=[],
        )
        digest.update(json.dumps(contracts, sort_keys=True).encode())
        self.metadata = dict(
            **contracts,
            input_id=input_id,
            input_sha256=digest.hexdigest(),
            history_start=str(self.index[0].date()),
            history_end=str(self.index[-1].date()),
            status_codes=STATUS,
            data_access="offline",
            point_in_time_certified=False,
            provider_equivalence=False,
        )

    def compute(self, fields=None):
        definitions = coverage()
        selected = (
            list(definitions.index)
            if fields is None
            else ([fields] if isinstance(fields, str) else list(fields))
        )
        if (
            not selected
            or len(set(selected)) != len(selected)
            or set(selected) - set(definitions.index)
        ):
            raise ValueError("select unique official Alpha158 field names")
        shape = (len(self.index), len(self.columns), len(selected))
        values = np.full(shape, np.nan, dtype=np.float32)
        status = np.full(shape, 4, dtype=np.uint8)
        missing = {}
        with np.errstate(all="ignore"):
            for j, sid in enumerate(self.columns):
                start, end = self.ranges[sid]
                positions = np.flatnonzero((self.index >= start) & (self.index <= end))
                ops = Operators({k: frame[sid].iloc[positions] for k, frame in self.fields.items()})
                for k, name in enumerate(selected):
                    row = definitions.loc[name]
                    absent = sorted(set(row.dependencies) - set(self.fields))
                    if absent:
                        missing[name] = absent
                        status[positions, j, k] = 5
                        continue
                    result = ops.evaluate(row.expression).to_numpy(dtype=np.float32)
                    values[positions, j, k] = result
                    status[positions, j, k] = np.select(
                        [np.isnan(result), np.isposinf(result), np.isneginf(result)], [1, 2, 3], 0
                    )
        values[~self.membership] = np.nan
        status[~self.membership] = 4
        index = pd.MultiIndex.from_product([self.index, self.columns], names=["date", "security"])
        return _result(
            pd.DataFrame(values.reshape(-1, len(selected)), index=index, columns=selected),
            pd.DataFrame(status.reshape(-1, len(selected)), index=index, columns=selected),
            {**self.metadata, "missing_dependencies": missing},
        )
