"""A数达: direct public market data with optional caching and advanced offline snapshots."""
from .model import DataError
from .storage import Store
from .live import Client, capabilities, get_all_securities, get_price, get_security_info, get_trade_days

__all__ = ["Store", "DataError", "Client", "get_price", "get_security_info", "get_trade_days",
           "get_all_securities", "capabilities"]
__version__ = "0.2.0rc2"
