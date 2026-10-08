"""A数达: direct public market data with optional caching and advanced offline snapshots."""
from .model import DataError
from .contracts import CoverageContract
from .reconciliation import reconcile_turnover
from .storage import Store
from .research import ResearchResult, ResearchView
from .live import Client, capabilities, get_all_securities, get_price, get_security_info, get_trade_days

__all__ = ["ResearchResult", "ResearchView", "Store", "DataError", "Client", "get_price", "get_security_info", "get_trade_days",
           "get_all_securities", "capabilities", "CoverageContract", "reconcile_turnover"]
__version__ = "0.4.0.dev2"
