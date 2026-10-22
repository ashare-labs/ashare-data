"""Explicit conventions for offline, daily single-factor diagnostics."""

from dataclasses import asdict, dataclass
from datetime import date
from numbers import Real
import math


@dataclass(frozen=True)
class EvaluationConfig:
    input_id: str
    universe_label: str
    price_basis: str
    universe_mode: str = "fixed_sample"
    horizons: tuple[int, ...] = (1, 5, 10)
    quantiles: int = 5
    lag: int = 1
    direction: int = 1
    min_assets: int = 5
    cost_bps: tuple[float, ...] = (0, 5, 10, 20)
    rolling_window: int = 20
    start_date: str | None = None
    end_date: str | None = None

    def __post_init__(self):
        for name in ("input_id", "universe_label", "price_basis"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name}: explicit nonempty description required")
        if self.universe_mode not in {"fixed_sample", "point_in_time"}:
            raise ValueError("universe_mode must be fixed_sample or point_in_time")
        for name, lower, upper in (
            ("quantiles", 2, 20), ("lag", 1, 252),
            ("min_assets", 2, 100000), ("rolling_window", 2, 2520),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name}: integer in [{lower}, {upper}] required")
        if type(self.direction) is not int or self.direction not in (-1, 1):
            raise ValueError("direction must be +1 or -1, chosen before observing results")
        if not isinstance(self.horizons, (tuple, list)) or not self.horizons:
            raise ValueError("nonempty horizons required")
        if any(type(h) is not int or not 1 <= h <= 252 for h in self.horizons):
            raise ValueError("horizons must contain integers in [1, 252]")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("duplicate horizons")
        if not isinstance(self.cost_bps, (tuple, list)) or not self.cost_bps:
            raise ValueError("nonempty cost_bps required")
        if any(
            isinstance(c, bool) or not isinstance(c, Real)
            or not math.isfinite(c) or not 0 <= c <= 1000 for c in self.cost_bps
        ):
            raise ValueError("cost_bps must contain finite one-way rates in [0, 1000]")
        if len(set(self.cost_bps)) != len(self.cost_bps):
            raise ValueError("duplicate cost scenarios")
        object.__setattr__(self, "horizons", tuple(sorted(self.horizons)))
        object.__setattr__(self, "cost_bps", tuple(sorted(float(c) for c in self.cost_bps)))
        for name in ("start_date", "end_date"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or date.fromisoformat(value).isoformat() != value
            ):
                raise ValueError(f"{name}: canonical YYYY-MM-DD required")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not follow end_date")

    def to_dict(self):
        result = asdict(self)
        result["horizons"] = list(self.horizons)
        result["cost_bps"] = list(self.cost_bps)
        return result
