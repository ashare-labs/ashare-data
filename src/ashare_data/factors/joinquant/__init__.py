"""Explicit factor calculations over supplied, validated research inputs."""

from ashare_data.financials.ledger import AcceptanceError
from ashare_data.financials.ledger import (
    FinancialLedger,
    InputUnavailable as FinancialInputUnavailable,
    RESEARCH_MODE,
    VisibilityUnproven,
    financial_factor_candidate,
    verify_pack_files,
)
from .share_financial import SHARE_MODE, net_profit_growth_candidate, share_metrics
from .amount_inputs import InputUnavailable as MoneyInputUnavailable, RESEARCH_ROLE
from .price_volume import DEFINITION as PV_RESEARCH_DEFINITION, ContractError as PriceContractError
from .source_factors import (
    REVENUE_RESEARCH_DEFINITION,
    ROC120_RESEARCH_DEFINITION,
    reported_operating_revenue_ttm,
    roc120_native_candidate,
)
from .amount import bounded_prediction
from .amount_comparison import (
    compare_reference,
    compare_composite_reference,
    summarize_composite_comparisons,
)
from .panel import (
    DEFINITION as BATCH_RESEARCH_DEFINITION,
    MODE as BATCH_RESEARCH_MODE,
    compute_panel,
)
from .research_view import FactorResearchView

__all__ = [
    "AcceptanceError",
    "FinancialLedger",
    "FinancialInputUnavailable",
    "MoneyInputUnavailable",
    "RESEARCH_MODE",
    "RESEARCH_ROLE",
    "VisibilityUnproven",
    "financial_factor_candidate",
    "verify_pack_files",
    "bounded_prediction",
    "compare_reference",
    "compare_composite_reference",
    "summarize_composite_comparisons",
    "SHARE_MODE",
    "net_profit_growth_candidate",
    "share_metrics",
    "PV_RESEARCH_DEFINITION",
    "PriceContractError",
    "REVENUE_RESEARCH_DEFINITION",
    "ROC120_RESEARCH_DEFINITION",
    "reported_operating_revenue_ttm",
    "roc120_native_candidate",
    "BATCH_RESEARCH_DEFINITION",
    "BATCH_RESEARCH_MODE",
    "compute_panel",
    "FactorResearchView",
]
