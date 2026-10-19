"""Read the maintained daily library and publish bounded, immutable products."""

from ashare_data.product.configuration import deployment_path
from ashare_data.product.daily import LocalDailyProduct
from ashare_data.product.minute import LocalMinuteProduct
from ashare_data.product.validation import validate_covered_input

__all__ = ["LocalDailyProduct", "LocalMinuteProduct", "validate_covered_input", "deployment_path"]
