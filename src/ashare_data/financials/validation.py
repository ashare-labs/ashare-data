"""Shared finite-number and timezone validation for scoped FACTOR_PANEL research."""

from datetime import datetime
from decimal import Decimal, InvalidOperation


class AcceptanceError(ValueError):
    """A contract/evidence error; never a missing numeric value."""


def number(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AcceptanceError("finite numeric scalar required")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise AcceptanceError("invalid decimal") from exc
    if not result.is_finite():
        raise AcceptanceError("NaN and infinity must not masquerade as observations")
    return result


def instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise AcceptanceError("timestamp with timezone required") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise AcceptanceError("date-only/naive timestamp cannot establish strict visibility")
    return result
