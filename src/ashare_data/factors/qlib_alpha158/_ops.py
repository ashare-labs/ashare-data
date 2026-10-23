"""Qlib v0.9.7 operators used by its fixed Alpha158 expressions only.

Derived from Microsoft Qlib data/ops.py and data/_libs/rolling.pyx, MIT;
see qlib-license.txt. AD's strict full-window Alpha191 semantics do not apply.
"""

import ast
from collections import deque
import operator

import numpy as np
import pandas as pd

from ashare_data.factors.alpha191._engine import parse


def regression(series, window, kind):
    # Preserve the official sequential double-precision recurrence, including
    # missing time coordinates and cancellation; no different regression fit.
    bars = deque([np.nan] * window)
    count = x = xx = y = yy = xy = np.float64(0)
    out = []
    for value in series.to_numpy(dtype=np.float64):
        xy = xy - y
        xx = xx + count - 2 * x
        x = x - count
        old = bars.popleft()
        bars.append(value)
        if not np.isnan(old):
            count -= 1
            y -= old
            yy -= old * old
        if not np.isnan(value):
            count += 1
            x += window
            xx += window * window
            y += value
            yy += value * value
            xy += window * value
        numerator = count * xy - x * y
        denominator = count * xx - x * x
        if kind == "Rsquare":
            r = numerator / np.sqrt(denominator * (count * yy - y * y))
            result = r * r
        else:
            slope = numerator / denominator
            result = (
                slope
                if kind == "Slope"
                else value - (slope * window + (y / count - slope * (x / count)))
            )
        out.append(result)
    result = pd.Series(out, index=series.index)
    if kind == "Rsquare":
        result.loc[np.isclose(series.rolling(window, min_periods=1).std(), 0, atol=2e-5)] = np.nan
    return result


class Operators:
    """Evaluate only the packaged, fixed expression trees; no external formula API."""

    def __init__(self, fields):
        self.fields = fields
        self.cache = {}

    def evaluate(self, expression):
        return self.node(parse(expression.replace("$", "")))

    def node(self, node):
        key = ast.dump(node)
        if key in self.cache:
            return self.cache[key]
        if isinstance(node, ast.Name):
            value = self.fields[node.id]
        elif isinstance(node, ast.Constant):
            value = node.value
        elif isinstance(node, ast.BinOp):
            op = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
            }[type(node.op)]
            value = op(self.node(node.left), self.node(node.right))
        elif isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = {ast.Gt: np.greater, ast.Lt: np.less}[type(node.ops[0])]
            value = op(self.node(node.left), self.node(node.comparators[0]))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            value = self.call(node.func.id, *(self.node(a) for a in node.args))
        else:
            raise ValueError("Unsupported packaged Alpha158 expression")
        self.cache[key] = value
        return value

    @staticmethod
    def call(name, *args):
        if name in {"Greater", "Less"}:
            return (np.maximum if name == "Greater" else np.minimum)(*args)
        if name in {"Abs", "Log"}:
            return (np.abs if name == "Abs" else np.log)(args[0])
        if name == "Ref":
            return args[0].shift(args[1])
        if name == "Corr":
            left, right, window = args
            a, b = left.rolling(window, min_periods=1), right.rolling(window, min_periods=1)
            result = a.corr(right)
            result.loc[np.isclose(a.std(), 0, atol=2e-5) | np.isclose(b.std(), 0, atol=2e-5)] = (
                np.nan
            )
            return result
        series, window, *rest = args
        if name in {"Slope", "Rsquare", "Resi"}:
            return regression(series, window, name)
        roll = series.rolling(window, min_periods=1)
        if name == "Rank":
            return roll.rank(pct=True)
        if name in {"IdxMax", "IdxMin"}:
            op = np.argmax if name == "IdxMax" else np.argmin
            return roll.apply(lambda x: op(x) + 1, raw=True)
        if name == "Quantile":
            return roll.quantile(rest[0])
        return getattr(
            roll, {"Mean": "mean", "Sum": "sum", "Std": "std", "Max": "max", "Min": "min"}[name]
        )()
