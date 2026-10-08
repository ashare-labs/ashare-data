"""A数达: direct public market data with optional caching and advanced offline snapshots."""
from .model import DataError
from .contracts import CoverageContract
from .reconciliation import reconcile_turnover
from .storage import Store
from .research import ResearchResult, ResearchView
from .baostock import BaoStockSource, BaoStockView
from .d1 import D1Facts, D1View
from .d1_types import D1OwnerReceipt, D1PrevCloseCall
from .br1 import BR1View, BR1Receipt
from .live import Client, capabilities, get_all_securities, get_price, get_security_info, get_trade_days

__all__ = ["BaoStockSource", "BaoStockView", "ResearchResult", "ResearchView", "Store", "DataError", "Client", "get_price", "get_security_info", "get_trade_days",
           "get_all_securities", "capabilities", "CoverageContract", "reconcile_turnover",
           "D1Facts", "D1View", "D1OwnerReceipt", "D1PrevCloseCall", "BR1View", "BR1Receipt"]
__version__ = "0.6.0.dev2"
