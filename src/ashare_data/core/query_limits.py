"""Explicit local query budgets; these do not enlarge upstream data coverage."""

from dataclasses import asdict, dataclass

from ashare_data.core.model import require


@dataclass(frozen=True)
class QueryLimits:
    max_securities: int = 10
    max_count: int = 1000
    max_observation_rows: int = 100000

    def __post_init__(self):
        for name, value in asdict(self).items():
            require(
                type(value) is int and 0 < value <= 2**63 - 1,
                "INVALID_REQUEST",
                f"{name} 须为正整数本地查询预算",
            )

    def to_dict(self):
        return asdict(self)


# Partial results never override provenance, schema, conflict or guarantee checks.
PARTIAL_ERRORS = frozenset(
    {
        "NETWORK_ERROR",
        "SOURCE_NO_DATA",
        "NO_COMPLETED_BARS",
        "NO_VISIBLE_BARS",
        "CACHE_MISS",
        "COVERAGE_INCOMPLETE",
        "SOURCE_FIELD_MISSING",
        "SOURCE_HTTP_ERROR",
        "RATE_LIMITED",
        "SOURCE_ACCESS_DENIED",
    }
)
STOP_ERRORS = frozenset({"RATE_LIMITED", "SOURCE_ACCESS_DENIED"})
