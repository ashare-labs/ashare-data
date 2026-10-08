"""A数达: direct public market data with optional caching and advanced offline snapshots."""
from .model import DataError
from .contracts import CoverageContract
from .reconciliation import reconcile_turnover
from .storage import Store
from .live import Client, capabilities, get_all_securities, get_price, get_security_info, get_trade_days

__all__ = ["Store", "DataError", "Client", "get_price", "get_security_info", "get_trade_days",
           "get_all_securities", "capabilities", "CoverageContract", "reconcile_turnover"]
__version__ = "0.3.0.dev3"
