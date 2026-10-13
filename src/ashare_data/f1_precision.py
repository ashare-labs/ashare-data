"""Exact final six-decimal candidates; midpoint behavior remains unsupported."""

from decimal import Decimal
from fractions import Fraction
import struct

from .f1_acceptance import AcceptanceError


def require(condition, message):
    if not condition:
        raise AcceptanceError(message)


def six_decimal_profiles(value: Fraction) -> dict:
    """Exact-rational final quantization; no intermediate float/quarter rounding."""
    scaled = abs(value) * 1_000_000
    units, remainder = divmod(scaled.numerator, scaled.denominator)
    require(2 * remainder != scaled.denominator, "midpoint excluded from this experiment")
    sign = -1 if value < 0 else 1
    result = {}
    for name, magnitude in (
        ("nearest6_nonmidpoint", units + (2 * remainder > scaled.denominator)),
        ("truncate6_toward_zero", units),
    ):
        decimal_text = format(Decimal(sign * magnitude).scaleb(-6), ".6f")
        f64 = float(decimal_text)
        result[name] = {
            "decimal": decimal_text,
            "float64_17g": format(f64, ".17g"),
            "float64_hex": f64.hex(),
            "float64_bits_be": struct.pack(">d", f64).hex(),
        }
    return result
