"""Read the maintained daily library and publish bounded, immutable products."""

from ashare_data.product.daily import LocalDailyProduct
from ashare_data.product.validation import validate_covered_input

__all__ = ["LocalDailyProduct", "validate_covered_input"]
