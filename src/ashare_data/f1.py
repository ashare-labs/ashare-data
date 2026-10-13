"""Explicit offline research API for the locally integrated F1 scope.

Calculations do not issue alignment certificates. The FI01/FI04 and conditional
TVMA6 observations are recorded in ``docs/f1-integration.json``; the later
FI02/SV scope is separate in ``docs/f1-fi-sv-round14.json``. No general JoinQuant
alias is registered, and the original SV02 zero-ULP failure remains unchanged.
PV round11 archive-bound candidates retain the failed EMA5 path and independent
source incompatibility; conditional arithmetic support is limited to one target.
"""

from .f1_acceptance import AcceptanceError
from .f1_financial import (
    FinancialLedger,
    InputUnavailable as FinancialInputUnavailable,
    RESEARCH_MODE,
    VisibilityUnproven,
    financial_factor_candidate,
    verify_pack_files,
)
from .f1_followup import confirmation_prediction
from .f1_fi_sv_scoped import (
    SHARE_MODE,
    fi02_lagged_candidate,
    observe_fi_sv_batch,
    sv_stage_candidate,
)
from .f1_price_volume import InputUnavailable as MoneyInputUnavailable, RESEARCH_ROLE
from .f1_pv_round11 import pv_round11_candidate, pv_round11_ema5_local_candidate
from .f1_pv_semantic_contract import DEFINITION as PV_RESEARCH_DEFINITION
from .f1_pv_semantic_contract import ContractError as PriceContractError
from .f1_remaining import (
    FI03_RESEARCH_DEFINITION,
    ROC120_RESEARCH_DEFINITION,
    reported_operating_revenue_ttm,
    roc120_native_candidate,
    vroc6_fixed_snapshot_candidate,
)
from .f1_vroc6_snapshot import DEFINITION as VROC6_RESEARCH_DEFINITION
from .f1_vroc6_snapshot import ROLE as VROC6_RESEARCH_ROLE
from .f1_tvma6_bounded import bounded_prediction
from .f1_tvma6_composite import compare_composite_reference, summarize_composite_comparisons
from .f1_batch import (
    DEFINITION as BATCH_RESEARCH_DEFINITION,
    MODE as BATCH_RESEARCH_MODE,
    compute_panel,
    compare_panels,
)

__all__ = [
    "AcceptanceError",
    "FinancialLedger",
    "FinancialInputUnavailable",
    "MoneyInputUnavailable",
    "RESEARCH_MODE",
    "RESEARCH_ROLE",
    "VisibilityUnproven",
    "financial_factor_candidate",
    "confirmation_prediction",
    "verify_pack_files",
    "bounded_prediction",
    "compare_composite_reference",
    "summarize_composite_comparisons",
    "SHARE_MODE",
    "fi02_lagged_candidate",
    "sv_stage_candidate",
    "observe_fi_sv_batch",
    "PV_RESEARCH_DEFINITION",
    "PriceContractError",
    "pv_round11_candidate",
    "pv_round11_ema5_local_candidate",
    "FI03_RESEARCH_DEFINITION",
    "ROC120_RESEARCH_DEFINITION",
    "VROC6_RESEARCH_DEFINITION",
    "VROC6_RESEARCH_ROLE",
    "reported_operating_revenue_ttm",
    "roc120_native_candidate",
    "vroc6_fixed_snapshot_candidate",
    "BATCH_RESEARCH_DEFINITION",
    "BATCH_RESEARCH_MODE",
    "compute_panel",
    "compare_panels",
]
