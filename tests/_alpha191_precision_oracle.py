"""Slow scalar Fraction/Decimal oracle; no production engine or rolling helpers.

Use for precision regressions in the rank/statistic/linear-window formulas only.
The caller supplies the formula; this is not a second formula source.
"""

import ast
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
import math
import operator


class PrecisionOracle:
    def __init__(self, arrays, pool, aliases, *, policy="strict"):
        self.arrays, self.pool, self.aliases, self.policy = arrays, pool, aliases, policy
        self.columns = pool.shape[1]

    @staticmethod
    def decimal(x):
        return Decimal(x.numerator) / Decimal(x.denominator) if isinstance(x, Fraction) else x

    @classmethod
    def operate(cls, a, b, op):
        if a is None or b is None:
            return None
        if isinstance(a, Decimal) or isinstance(b, Decimal):
            a, b = cls.decimal(a), cls.decimal(b)
        return op(a, b)

    def compute(self, expression, t, j):
        with localcontext() as context:
            context.prec = 80
            return self.value(self.parse(expression), t, j)

    @staticmethod
    @lru_cache(None)
    def parse(expression):
        return ast.parse(expression, mode="eval").body

    @lru_cache(None)
    def value(self, node, t, j):
        if t < 0:
            return None
        if isinstance(node, ast.Constant):
            return Fraction(node.value)
        if isinstance(node, ast.Name):
            if node.id in self.aliases:
                return self.value(self.parse(self.aliases[node.id]), t, j)
            x = float(self.arrays[node.id][t, j])
            return Fraction(x) if math.isfinite(x) else None
        if isinstance(node, ast.UnaryOp):
            x = self.value(node.operand, t, j)
            return -x if x is not None else None
        if isinstance(node, ast.Compare):
            a, b = [self.value(x, t, j) for x in [node.left, node.comparators[0]]]
            if a is None or b is None:
                return None
            op = {
                ast.Lt: operator.lt,
                ast.Gt: operator.gt,
                ast.Eq: operator.eq,
                ast.LtE: operator.le,
                ast.GtE: operator.ge,
            }[type(node.ops[0])]
            return Fraction(int(op(a, b)))
        if isinstance(node, ast.BinOp):
            a, b = (self.value(x, t, j) for x in [node.left, node.right])
            if a is None or b is None:
                return None
            if isinstance(node.op, ast.Pow) and (not isinstance(b, Fraction) or b.denominator != 1):
                if a < 0 or (a == 0 and b < 0):
                    return None
                return self.decimal(a) ** self.decimal(b)
            op = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
                ast.Pow: operator.pow,
            }[type(node.op)]
            if isinstance(node.op, ast.Div) and b == 0:
                return None
            return self.operate(a, b, op)
        assert isinstance(node, ast.Call)
        name, args = node.func.id, node.args
        if name in {"DELAY", "DELTA"}:
            lag = self.value(args[0], t - args[1].value, j)
            return (
                lag
                if name == "DELAY"
                else self.operate(self.value(args[0], t, j), lag, operator.sub)
            )
        if name == "ABS":
            x = self.value(args[0], t, j)
            return abs(x) if x is not None else None
        if name == "LOG":
            x = self.value(args[0], t, j)
            return self.decimal(x).ln() if x is not None and x > 0 else None
        if name == "R":
            if not self.pool[t, j]:
                return None
            pairs = [(k, self.value(args[0], t, k)) for k in range(self.columns) if self.pool[t, k]]
            if self.policy == "strict" and any(x is None for _, x in pairs):
                return None
            values = [x for _, x in pairs if x is not None]
            target = self.value(args[0], t, j)
            if target is None:
                return None
            return Fraction(
                2 * sum(x < target for x in values) + sum(x == target for x in values) + 1,
                2 * len(values),
            )
        if name in {"MIN", "MAX"}:
            a, b = [self.value(x, t, j) for x in args]
            if a is None or b is None:
                return None
            return min(a, b) if name == "MIN" else max(a, b)
        n = args[-1].value
        x = [self.value(args[0], k, j) for k in range(t - n + 1, t + 1)]
        if any(z is None for z in x):
            return None
        total = self.sum
        if name == "SUM":
            return total(x)
        if name == "MEAN":
            return total(x) / n
        if name == "STD":
            mean = total(x) / n
            squared = [self.operate(z, mean, operator.sub) ** 2 for z in x]
            return self.decimal(total(squared) / (n - 1)).sqrt()
        if name == "DECAY":
            return total(
                [self.operate(z, Fraction(k + 1), operator.mul) for k, z in enumerate(x)]
            ) / sum(range(1, n + 1))
        if name == "TSMIN":
            return min(x)
        if name == "TSMAX":
            return max(x)
        if name == "TSRANK":
            return Fraction(2 * sum(z < x[-1] for z in x) + sum(z == x[-1] for z in x) + 1, 2 * n)
        if name not in {"CORR", "COV"}:
            raise ValueError(name)
        y = [self.value(args[1], k, j) for k in range(t - n + 1, t + 1)]
        if any(z is None for z in y):
            return None
        sx, sy = total(x), total(y)
        numerator = n * total(
            [self.operate(a, b, operator.mul) for a, b in zip(x, y)]
        ) - self.operate(sx, sy, operator.mul)
        if name == "COV":
            return numerator / (n * (n - 1))
        xx = n * total([z * z for z in x]) - sx * sx
        yy = n * total([z * z for z in y]) - sy * sy
        if not xx or not yy:
            return None
        squared = numerator * numerator / (xx * yy)
        return (1 if numerator >= 0 else -1) * self.decimal(squared).sqrt()

    @classmethod
    def sum(cls, values):
        value = Fraction(0)
        for x in values:
            value = cls.operate(value, x, operator.add)
        return value
