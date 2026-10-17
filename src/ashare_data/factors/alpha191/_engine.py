"""Small, private vector evaluator for the static, versioned formula registry.

It accepts no user formulas and executes no source-language code. Missing and
undefined arithmetic carry reason codes, including through Boolean conditions.
"""

from dataclasses import dataclass
import ast
from functools import lru_cache
import operator

import numpy as np
import pandas as pd

from ashare_data.factors.alpha191._formulas import ALIASES

OK, WARMUP, MISSING, DOMAIN, EXCLUDED, RANK_INCOMPLETE, AMBIGUOUS, DEPENDENCY = range(8)
STATUS = {
    OK: "OK",
    WARMUP: "WARMUP",
    MISSING: "MISSING_INPUT",
    DOMAIN: "UNDEFINED_MATH",
    EXCLUDED: "OUT_OF_UNIVERSE",
    RANK_INCOMPLETE: "INCOMPLETE_RANK_POOL",
    AMBIGUOUS: "AMBIGUOUS_FORMULA",
    DEPENDENCY: "MISSING_DEPENDENCY",
}


@dataclass
class Rational:
    """Exact bounded integer provenance for ranks and their linear arithmetic."""

    numerator: np.ndarray
    denominator: np.ndarray
    known: np.ndarray


def rational(numerator, denominator, known=True):
    a, b, known = np.broadcast_arrays(
        np.asarray(numerator, dtype=np.int64), np.asarray(denominator, dtype=np.int64), known
    )
    known = known & (b > 0)
    a, b = np.where(known, a, 0), np.where(known, b, 1)
    gcd = np.gcd(a, b)
    return Rational(a // gcd, b // gcd, known)


def integer_windows(q, n):
    """Use each trailing window's denominator; future rows cannot select a path."""
    if q is None or len(q.numerator) < n:
        return None
    aw = np.lib.stride_tricks.sliding_window_view(q.numerator, n, axis=0)
    bw = np.lib.stride_tricks.sliding_window_view(q.denominator, n, axis=0)
    known = np.lib.stride_tricks.sliding_window_view(q.known, n, axis=0).all(axis=-1)
    scale = np.ones(known.shape, dtype=np.int64)
    # Limits are local overflow guards, not tolerances or a rank-rounding rule.
    for i in range(n):
        factor = bw[..., i] // np.gcd(scale, bw[..., i])
        known &= (factor <= 2**20) & (scale <= 2**20 // np.maximum(factor, 1))
        scale = np.where(known, scale, 1) * np.where(known, factor, 1)
    multiplier = scale[..., None] // bw
    known &= (np.abs(aw) <= 2**24 // np.maximum(multiplier, 1)).all(axis=-1)
    integers = np.where(known[..., None], aw, 0) * np.where(known[..., None], multiplier, 1)
    return integers, scale, known


def rational_binary(a, b, op):
    def as_ratio(x):
        if isinstance(x, Panel):
            return x.exact
        if isinstance(x, (int, float)) and np.isfinite(x) and x == int(x) and abs(x) < 2**24:
            return rational(int(x), 1)
        return None

    qa, qb = as_ratio(a), as_ratio(b)
    if (
        qa is None
        or qb is None
        or op not in {operator.add, operator.sub, operator.mul, operator.truediv}
    ):
        return None
    an, ad, bn, bd = np.broadcast_arrays(qa.numerator, qa.denominator, qb.numerator, qb.denominator)
    known = qa.known & qb.known
    for x in [an, ad, bn, bd]:
        known &= np.abs(x) <= 2**30
    if op == operator.truediv:
        known &= bn != 0
    an, ad, bn, bd = [
        np.where(known, x, fallback) for x, fallback in [(an, 0), (ad, 1), (bn, 1), (bd, 1)]
    ]
    if op in {operator.add, operator.sub}:
        common = np.gcd(ad, bd)
        left, right = an * (bd // common), bn * (ad // common)
        return rational(
            left + right if op == operator.add else left - right, ad * (bd // common), known
        )
    if op == operator.mul:
        return rational(an * bn, ad * bd, known)
    return rational(an * bd * np.sign(bn), ad * np.abs(bn), known)


def accurate_sum(values):
    """Neumaier compensated sum on the last axis, without cross-window state."""
    total = np.zeros(values.shape[:-1], dtype=float)
    correction = total.copy()
    for i in range(values.shape[-1]):
        value = values[..., i]
        updated = total + value
        correction += np.where(
            abs(total) >= abs(value), (total - updated) + value, (value - updated) + total
        )
        total = updated
    return total + correction


def centered_unit_window(values):
    """Remove the level before computing the mean, then bound the working scale.

    Identical representable differences must take identical arithmetic paths,
    independently of a security's price/volume level. This also avoids squaring
    huge or subnormal changes. The returned scale is mantissa * 2**exponent.
    """
    delta = values - values[..., :1]
    # A difference of opposite extreme finite values can overflow. A power-of-
    # two prescale is exact when normal; keep its exponent for covariance.
    halve = np.isfinite(values).all(axis=-1) & ~np.isfinite(delta).all(axis=-1)
    if halve.any():
        scaled = np.ldexp(values, -1)
        delta = np.where(halve[..., None], scaled - scaled[..., :1], delta)
    scale = np.max(np.abs(delta), axis=-1)
    unit = delta / np.where(scale > 0, scale, 1)[..., None]
    centered = unit - (accurate_sum(unit) / values.shape[-1])[..., None]
    mantissa, exponent = np.frexp(scale)
    return centered, mantissa, exponent + halve, scale == 0


@dataclass
class Panel:
    values: np.ndarray
    reasons: np.ndarray
    exact: Rational | None = None


def panel(values, reasons=OK, exact=None):
    a = np.array(values, dtype=float, copy=True)
    r = np.broadcast_to(np.asarray(reasons, dtype=np.uint8), a.shape).copy()
    r[(~np.isfinite(a)) & (r == OK)] = DOMAIN
    a[r != OK] = np.nan
    if exact is not None:
        exact = rational(exact.numerator, exact.denominator, exact.known & (r == OK))
    return Panel(a, r, exact)


def combine(a, b, op):
    av, ar = (a.values, a.reasons) if isinstance(a, Panel) else (a, OK)
    bv, br = (b.values, b.reasons) if isinstance(b, Panel) else (b, OK)
    with np.errstate(all="ignore"):
        v = op(av, bv)
    if not isinstance(a, Panel) and not isinstance(b, Panel):
        return v
    exact = rational_binary(a, b, op)
    if exact is not None:
        v = np.where(exact.known, exact.numerator / exact.denominator, v)
    return panel(v, np.maximum(ar, br), exact)


@lru_cache(maxsize=512)
def parse(expression):
    return ast.parse(expression, mode="eval").body


BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.BitAnd: np.logical_and,
    ast.BitOr: np.logical_or,
}
COMPARE = {
    ast.Lt: operator.lt,
    ast.Gt: operator.gt,
    ast.LtE: operator.le,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
}
FIELDS = {
    "O": "open",
    "C": "close",
    "H": "high",
    "L": "low",
    "V": "volume",
    "P": "vwap",
    "A": "amount",
    "BC": "benchmark_close",
    "BO": "benchmark_open",
    "MKT": "mkt",
    "SMB": "smb",
    "HML": "hml",
    "M146": "alpha146_m",
}


def characteristics(expression):
    """Return dependencies, conservative window length and state requirements."""

    def walk(node):
        if isinstance(node, ast.Constant):
            return set(), 1, False, False
        if isinstance(node, ast.Name):
            if node.id in ALIASES:
                return walk(parse(ALIASES[node.id]))
            return {FIELDS[node.id]}, 1, False, False
        children = (
            node.args
            if isinstance(node, ast.Call)
            else [child for child in ast.iter_child_nodes(node) if isinstance(child, (ast.expr,))]
        )
        parts = [walk(child) for child in children]
        deps = set().union(*(p[0] for p in parts))
        window = max((p[1] for p in parts), default=1)
        state = any(p[2] for p in parts)
        rank = any(p[3] for p in parts)
        if isinstance(node, ast.Call):
            name = node.func.id
            rank |= name == "R"
            state |= name in {"SMA", "SELF143", "FILTERBETA"}
            if name in {"DELAY", "DELTA"}:
                window += node.args[1].value
            if name in {
                "SUM",
                "MEAN",
                "STD",
                "TSMIN",
                "TSMAX",
                "TSRANK",
                "DECAY",
                "WMA",
                "SLOPE",
                "LOWDAY",
                "HIGHDAY",
                "CORR",
                "COV",
                "RESID",
                "CUMRANGE",
            }:
                window += node.args[-1].value - 1
        return deps, window, state, rank

    return walk(parse(expression))


class Engine:
    def __init__(self, fields, membership, rank_policy, seed):
        self.fields = fields
        self.membership = membership
        self.rank_policy = rank_policy
        self.seed = seed
        self.cache = {}

    def evaluate(self, expression):
        return self._eval(parse(expression))

    def _eval(self, node):
        key = ast.dump(node)
        if key in self.cache:
            return self.cache[key]
        if isinstance(node, ast.Constant):
            result = node.value
        elif isinstance(node, ast.Name):
            result = (
                self.evaluate(ALIASES[node.id])
                if node.id in ALIASES
                else self.fields[FIELDS[node.id]]
            )
        elif isinstance(node, ast.BinOp):
            result = combine(self._eval(node.left), self._eval(node.right), BINARY[type(node.op)])
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            result = combine(0, self._eval(node.operand), operator.sub)
        elif isinstance(node, ast.Compare) and len(node.ops) == 1:
            result = combine(
                self._eval(node.left), self._eval(node.comparators[0]), COMPARE[type(node.ops[0])]
            )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            result = self.call(node.func.id, *(self._eval(x) for x in node.args))
        else:
            raise ValueError("invalid internal formula node: " + ast.dump(node))
        self.cache[key] = result
        return result

    @staticmethod
    def _window_reason(x, n):
        r = pd.DataFrame(x.reasons).rolling(n, min_periods=1).max().to_numpy(dtype=np.uint8)
        r[: n - 1] = np.maximum(r[: n - 1], WARMUP)
        return r

    def rolling(self, x, n, kind):
        r = self._window_reason(x, n)
        roll = pd.DataFrame(x.values).rolling(n, min_periods=n)
        exact = None
        if kind == "STD":
            v = roll.std(ddof=1).to_numpy()
        elif kind in {"SUM", "MEAN"}:
            v = np.full_like(x.values, np.nan)
            if len(v) >= n:
                w = np.lib.stride_tricks.sliding_window_view(x.values, n, axis=0)
                v[n - 1 :] = accurate_sum(w) / (n if kind == "MEAN" else 1)
        elif kind in {"TSMIN", "TSMAX"}:
            name = {"TSMIN": "min", "TSMAX": "max"}[kind]
            v = getattr(roll, name)().to_numpy()
        elif kind == "TSRANK":
            ranks = roll.rank(method="average", pct=False).to_numpy()
            exact = rational(np.nan_to_num(2 * ranks).astype(np.int64), 2 * n)
            v = exact.numerator / exact.denominator
        else:
            v = np.full_like(x.values, np.nan)
            if len(v) >= n:
                windows = np.lib.stride_tricks.sliding_window_view(x.values, n, axis=0)
                if kind == "CUMRANGE":
                    # Prefix sums restart at the oldest observation in each
                    # trailing window. Exclude an artificial zero prefix.
                    cumulative = np.cumsum(windows, axis=-1)
                    values = cumulative.max(axis=-1) - cumulative.min(axis=-1)
                elif kind in {"DECAY", "WMA", "SLOPE"}:
                    weights = np.arange(1, n + 1, dtype=float)
                    if kind == "WMA":
                        weights = 0.9 ** np.arange(n - 1, -1, -1)
                    if kind == "SLOPE":
                        weights -= weights.mean()
                        weights /= np.dot(weights, weights)
                        # Center response to reduce cancellation for high price levels.
                        values = np.sum(
                            (windows - windows.mean(axis=-1, keepdims=True)) * weights, axis=-1
                        )
                    else:
                        values = np.sum(windows * (weights / weights.sum()), axis=-1)
                else:
                    # Most recent extreme wins ties; today has lag 0.
                    operation = np.argmin if kind == "LOWDAY" else np.argmax
                    values = operation(windows[..., ::-1], axis=-1)
                v[n - 1 :] = values
        grid = integer_windows(x.exact, n) if kind in {"SUM", "MEAN", "DECAY"} else None
        if grid is not None:
            integers, denominator, known = grid
            weights = np.arange(1, n + 1) if kind == "DECAY" else np.ones(n, dtype=np.int64)
            total_weight = int(weights.sum())
            known &= np.max(np.abs(integers), axis=-1) <= 2**53 // total_weight
            numerator = np.zeros(x.values.shape, dtype=np.int64)
            denominators = np.ones_like(numerator)
            eligible = np.zeros(x.values.shape, dtype=bool)
            numerator[n - 1 :] = np.sum(integers * weights, axis=-1)
            denominators[n - 1 :] = denominator * (total_weight if kind != "SUM" else 1)
            eligible[n - 1 :] = known
            exact = rational(numerator, denominators, eligible)
            v = np.where(exact.known, exact.numerator / exact.denominator, v)
        return panel(v, r, exact)

    def call(self, name, *args):
        if name in {"MIN", "MAX"}:
            return combine(*args, np.minimum if name == "MIN" else np.maximum)
        if name in {"ABS", "LOG", "SIGN"}:
            x = args[0]
            with np.errstate(all="ignore"):
                return panel(
                    {"ABS": np.abs, "LOG": np.log, "SIGN": np.sign}[name](x.values), x.reasons
                )
        if name in {"DELAY", "DELTA"}:
            x, n = args
            v = np.full_like(x.values, np.nan)
            r = np.full_like(x.reasons, WARMUP)
            if n < len(v):
                v[n:], r[n:] = x.values[:-n], x.reasons[:-n]
            lag = panel(v, r)
            return lag if name == "DELAY" else combine(x, lag, operator.sub)
        if name == "IF":
            c, a, b = args
            av, ar = (a.values, a.reasons) if isinstance(a, Panel) else (a, OK)
            bv, br = (b.values, b.reasons) if isinstance(b, Panel) else (b, OK)
            yes = c.values != 0
            return panel(np.where(yes, av, bv), np.maximum(c.reasons, np.where(yes, ar, br)))
        if name == "R":
            x = args[0]
            a, r = x.values.copy(), x.reasons.copy()
            a[~self.membership] = np.nan
            ranks = pd.DataFrame(a).rank(axis=1, method="average", pct=False).to_numpy()
            count = np.isfinite(a).sum(axis=1, keepdims=True)
            exact = rational(np.nan_to_num(2 * ranks).astype(np.int64), 2 * np.maximum(count, 1))
            v = exact.numerator / exact.denominator
            if self.rank_policy == "strict":
                incomplete = (self.membership & ~np.isfinite(a)).any(axis=1)
                any_valid = (self.membership & np.isfinite(a)).any(axis=1)
                row_reason = np.max(np.where(self.membership, r, OK), axis=1)
                row_reason[incomplete & any_valid] = RANK_INCOMPLETE
                r[incomplete] = row_reason[incomplete, None]
            r[~self.membership] = EXCLUDED
            return panel(v, r, exact)
        if name in {"CORR", "COV"}:
            return self.paired_statistic(*args, name)
        if name == "SMA":
            x, n, m = args
            v = np.full_like(x.values, np.nan)
            previous = np.full(x.values.shape[1], np.nan)
            for i, row in enumerate(x.values):
                previous = np.where(np.isnan(previous), row, (m * row + (n - m) * previous) / n)
                v[i] = previous
            return panel(v, x.reasons)
        if name == "RESID":
            return self.residual(*args)
        if name == "SELF143":
            x = args[0]
            v = np.full_like(x.values, np.nan)
            r = np.zeros_like(x.reasons)
            previous = np.array(self.seed, dtype=float, copy=True)
            previous_reason = x.reasons[0].copy()
            v[0], r[0] = previous, previous_reason
            for i in range(1, len(v)):
                previous_reason = np.maximum(
                    previous_reason, np.maximum(x.reasons[i], x.reasons[i - 1])
                )
                ret = x.values[i] / x.values[i - 1] - 1
                previous = np.where(ret > 0, ret * previous, previous)
                v[i], r[i] = previous, previous_reason
            return panel(v, r)
        if name == "FILTERBETA":
            return self.filtered_beta(*args)
        if name in {
            "SUM",
            "MEAN",
            "STD",
            "TSMIN",
            "TSMAX",
            "TSRANK",
            "DECAY",
            "WMA",
            "SLOPE",
            "LOWDAY",
            "HIGHDAY",
            "CUMRANGE",
        }:
            return self.rolling(args[0], args[1], name)
        raise ValueError("unknown internal operator: " + name)

    def paired_statistic(self, a, b, n, name):
        """Shifted/scaled full-window moments, with exact degenerate identities.

        E[XY]-E[X]E[Y] rolling cancellation can become a material error after
        RANK. Even unshifted x-mean(x) is sensitive to the price/volume level;
        subtract an anchor before means, normalize, then use compensated sums.
        Two nonconstant observations correlate at exactly +/-1; a constant input
        has exactly zero covariance. Neither is a rank-rounding policy.
        Work in bounded time batches to avoid whole-panel n-window temporaries.
        """
        r = np.maximum(self._window_reason(a, n), self._window_reason(b, n))
        v = np.full_like(a.values, np.nan)
        if len(v) < n:
            return panel(v, r)
        aw = np.lib.stride_tricks.sliding_window_view(a.values, n, axis=0)
        bw = np.lib.stride_tricks.sliding_window_view(b.values, n, axis=0)
        batch = max(1, 262144 // (a.values.shape[1] * n))
        with np.errstate(all="ignore"):
            for start in range(0, len(aw), batch):
                end = start + batch
                x, y = aw[start:end], bw[start:end]
                dx, ax, ex, cx = centered_unit_window(x)
                dy, ay, ey, cy = centered_unit_window(y)
                constant = cx | cy
                if name == "CORR" and n == 2:
                    # Multiplying the differences may underflow; multiply signs.
                    value = np.sign(x[..., -1] - x[..., 0]) * np.sign(y[..., -1] - y[..., 0])
                else:
                    numerator = accurate_sum(dx * dy)
                    if name == "COV":
                        value = np.ldexp(numerator / (n - 1) * ax * ay, ex + ey)
                    else:
                        value = numerator / np.sqrt(accurate_sum(dx * dx) * accurate_sum(dy * dy))
                        value = np.clip(value, -1, 1)
                value[constant] = np.nan if name == "CORR" else 0
                v[start + n - 1 : min(end + n - 1, len(v))] = value
        ag, bg = integer_windows(a.exact, n), integer_windows(b.exact, n)
        exact = None
        if ag is not None and bg is not None:
            xw, ad, ak = ag
            yw, bd, bk = bg
            known = ak & bk
            if name == "CORR":
                # Primitive integer differences make correlation independent of
                # the denominator or affine scale used to represent the ranks.
                xw = xw - xw[..., :1]
                yw = yw - yw[..., :1]
                xw //= np.maximum(np.gcd.reduce(np.abs(xw), axis=-1)[..., None], 1)
                yw //= np.maximum(np.gcd.reduce(np.abs(yw), axis=-1)[..., None], 1)
            limit = np.maximum(np.max(np.abs(xw), axis=-1), np.max(np.abs(yw), axis=-1))
            known &= limit <= int((2**52 // (n * n)) ** 0.5)
            xw = np.where(known[..., None], xw, 0)
            yw = np.where(known[..., None], yw, 0)
            sx, sy = xw.sum(axis=-1), yw.sum(axis=-1)
            num = n * (xw * yw).sum(axis=-1) - sx * sy
            if name == "COV":
                numerator = np.zeros(a.values.shape, dtype=np.int64)
                denominator = np.ones_like(numerator)
                eligible = np.zeros(a.values.shape, dtype=bool)
                numerator[n - 1 :] = num
                denominator[n - 1 :] = n * (n - 1) * ad * bd
                eligible[n - 1 :] = known
                exact = rational(numerator, denominator, eligible)
                v = np.where(exact.known, exact.numerator / exact.denominator, v)
            else:
                xx = n * (xw * xw).sum(axis=-1) - sx * sx
                yy = n * (yw * yw).sum(axis=-1) - sy * sy
                # Canonical squared fraction preserves algebraically equal
                # correlations with different sample shapes; sqrt only once.
                small = (np.abs(num) < 2**31) & (xx < 2**31) & (yy < 2**31)
                safe_num, safe_xx, safe_yy = [np.where(small, z, 0) for z in [num, xx, yy]]
                squared = rational(
                    safe_num * safe_num, safe_xx * safe_yy, known & small & (xx > 0) & (yy > 0)
                )
                value = np.sign(num) * np.sqrt(squared.numerator / squared.denominator)
                v[n - 1 :] = np.where(squared.known, value, v[n - 1 :])
                v[n - 1 :][known & ((xx == 0) | (yy == 0))] = np.nan
        return panel(v, r, exact)

    def residual(self, y, mkt, smb, hml, n):
        predictors = [mkt, smb, hml]
        r = self._window_reason(y, n)
        for x in predictors:
            r = np.maximum(r, self._window_reason(x, n))
        v = np.full_like(y.values, np.nan)
        for i in range(n - 1, len(v)):
            # Factors are common across securities, enforced by the public API.
            design = np.column_stack(
                [np.ones(n)] + [x.values[i + 1 - n : i + 1, 0] for x in predictors]
            )
            eligible = r[i] == OK
            if not eligible.any():
                continue
            target = y.values[i + 1 - n : i + 1, eligible]
            beta, _, rank, _ = np.linalg.lstsq(design, target, rcond=None)
            if rank < 4:
                r[i, eligible] = DOMAIN
            else:
                v[i, eligible] = target[-1] - design[-1] @ beta
        return panel(v, r)

    def filtered_beta(self, y, x, condition, n):
        v = np.full_like(y.values, np.nan)
        r = np.full_like(y.reasons, WARMUP)
        selected = []
        for i in range(1, len(v)):
            # Unknown selection cannot be skipped: keep a placeholder in the event
            # window, until n known down-market events have displaced it.
            if condition.reasons[i, 0] != OK or condition.values[i, 0] != 0:
                selected.append(i)
                selected = selected[-n:]
            if len(selected) < n:
                continue
            rr = np.maximum(y.reasons[selected], x.reasons[selected]).max(axis=0)
            if np.any(condition.reasons[selected, 0] != OK):
                rr = np.maximum(rr, MISSING)
            xx, yy = x.values[selected], y.values[selected]
            dx, dy = xx - xx.mean(axis=0), yy - yy.mean(axis=0)
            with np.errstate(all="ignore"):
                v[i] = (dx * dy).sum(axis=0) / (dx * dx).sum(axis=0)
            r[i] = rr
        return panel(v, r)
