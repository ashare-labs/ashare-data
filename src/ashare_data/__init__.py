"""A数达: direct public market data with optional caching and advanced offline snapshots."""

from ashare_data.core.model import DataError
from ashare_data.core.query_limits import QueryLimits
from ashare_data.reference.previous_close import (
    SourcePrecloseValue,
    SourcePrecloseRow,
    SourcePrecloseResult,
)
from ashare_data.reference.daily_status import DailyStatusFlag, DailyStatusRow, DailyStatusResult
from ashare_data.reference.calendar import (
    SourceCalendarFlag,
    SourceCalendarRow,
    SourceCalendarResult,
    SourceCalendarLink,
    SourceCalendarLinksResult,
)
from ashare_data.reference.listing import (
    ListingFactView,
    ListingFactDescriptor,
    ListingFactEvidence,
    ListingYearFact,
)
from ashare_data.core.coverage import CoverageContract
from ashare_data.market.turnover import reconcile_turnover
from ashare_data.market.turnover_diagnostics import reconcile_sina_day
from ashare_data.storage.immutable import Store
from ashare_data.research.datasets import ResearchResult, ResearchView
from ashare_data.research.minute_export import export_minute_research
from ashare_data.research.preflight import (
    ResearchInputRef,
    ResearchSecurityInputs,
    ResearchInputFact,
    ResearchInputRow,
    ResearchPreflightResult,
)
from ashare_data.providers.joinquant_observations import (
    JQObservationInput,
    JQObservationRead,
    JQObservationView,
)
from ashare_data.providers.joinquant_queries import JQQueryView
from ashare_data.providers.tdx_points import TDXPointInput, TDXPointView
from ashare_data.providers.tdx_bars import TDXBarSource, TDXBarView
from ashare_data.providers.neigezhu import NeigezhuMinuteView
from ashare_data.market.minute_session import TDXMinuteSessionView, TDXSessionSource
from ashare_data.reference.bounded_calendar import BoundedCalendarView
from ashare_data.research.independent_queries import IndependentQueryView
from ashare_data.reference.premarket import PremarketReferenceView
from ashare_data.research.daily import DailyResearchView
from ashare_data.research.execution_inputs import DailyExecutionView
from ashare_data.financials.pit import FinancialPIT
from ashare_data.factors.joinquant.research_view import FactorResearchView

from ashare_data.reference.historical_market import HistoricalMarketView
from ashare_data.corporate_actions.history import CorporateHistoryView
from ashare_data.providers.baostock import BaoStockSource, BaoStockView
from ashare_data.providers.zzshare import ZzshareSource, ZzshareView
from ashare_data.providers.zzshare_types import (
    ZzshareValue,
    SourceClaimedLimits,
    ZzshareDailyRow,
    ZzshareDailyResult,
)
from ashare_data.research.finite_facts import FiniteFacts, FiniteFactsView
from ashare_data.research.fact_types import FactReceipt, PreviousCloseCall, Evidence, FactRecord
from ashare_data.research.conditional import ConditionalView, ConditionalReceipt
from ashare_data.research.conditional_listing import (
    ConditionalListingContract,
    ConditionalListingProjection,
    ConditionalListingView,
)
from ashare_data.market.client import (
    Client,
    capabilities,
    get_all_securities,
    get_price,
    get_security_info,
    get_trade_days,
)
from ashare_data.market.minute_evidence import MinuteEvidenceView, seal_minute_evidence

from ashare_data.research.portable import PortableView, PortableUseView
from ashare_data.research.portable_types import (
    AdjustedHistoryCall,
    PortableConsumerBinding,
    PortableReadContext,
    PortableRead,
    PortableDocument,
)

__all__ = [
    "BaoStockSource",
    "BaoStockView",
    "ResearchResult",
    "ResearchView",
    "Store",
    "export_minute_research",
    "DataError",
    "Client",
    "get_price",
    "get_security_info",
    "get_trade_days",
    "get_all_securities",
    "capabilities",
    "CoverageContract",
    "reconcile_turnover",
    "reconcile_sina_day",
    "FiniteFacts",
    "FiniteFactsView",
    "FactReceipt",
    "PreviousCloseCall",
    "ConditionalView",
    "ConditionalReceipt",
]
__all__ += [
    "ConditionalListingContract",
    "ConditionalListingProjection",
    "ConditionalListingView",
    "QueryLimits",
]
__version__ = "0.10.0.dev9"

__all__ += [
    "PortableView",
    "PortableUseView",
    "AdjustedHistoryCall",
    "PortableConsumerBinding",
    "PortableReadContext",
    "PortableRead",
    "PortableDocument",
]


__all__ += ["ListingFactView", "ListingFactDescriptor", "ListingFactEvidence", "ListingYearFact"]

__all__ += ["Evidence", "FactRecord"]
__all__ += [
    "SourceCalendarFlag",
    "SourceCalendarRow",
    "SourceCalendarResult",
    "SourceCalendarLink",
    "SourceCalendarLinksResult",
]


__all__ += ["DailyStatusFlag", "DailyStatusRow", "DailyStatusResult"]

__all__ += ["SourcePrecloseValue", "SourcePrecloseRow", "SourcePrecloseResult"]

__all__ += [
    "ZzshareSource",
    "ZzshareView",
    "ZzshareValue",
    "SourceClaimedLimits",
    "ZzshareDailyRow",
    "ZzshareDailyResult",
]

__all__ += [
    "ResearchInputRef",
    "ResearchSecurityInputs",
    "ResearchInputFact",
    "ResearchInputRow",
    "ResearchPreflightResult",
]

__all__ += ["JQObservationInput", "JQObservationRead", "JQObservationView"]
__all__ += ["JQQueryView"]
__all__ += ["TDXPointInput", "TDXPointView"]
__all__ += ["TDXBarSource", "TDXBarView"]
__all__ += ["NeigezhuMinuteView"]
__all__ += ["TDXMinuteSessionView", "TDXSessionSource"]
__all__ += ["BoundedCalendarView"]
__all__ += ["IndependentQueryView"]
__all__ += ["PremarketReferenceView"]
__all__ += ["DailyResearchView"]

__all__ += ["DailyExecutionView"]
__all__ += ["FinancialPIT"]
__all__ += ["FactorResearchView"]

__all__ += ["HistoricalMarketView"]
__all__ += ["CorporateHistoryView"]

__all__ += ["MinuteEvidenceView", "seal_minute_evidence"]
