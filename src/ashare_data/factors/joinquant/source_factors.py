"""Existing ROC120, fixed VROC6 and reported-revenue TTM research arithmetic.

Named JoinQuant alignment remains unresolved. New ports are prepared for review,
not retrospectively certified by their regression tests or prior background work.
"""

from decimal import Decimal, localcontext
from pathlib import Path

from ashare_data.financials.ledger import AcceptanceError, number
from ashare_data.financials.ledger import FinancialLedger, RESEARCH_MODE
from ashare_data.factors.joinquant.amount_inputs import (
    NativeSeries,
    InputUnavailable,
    RESEARCH_ROLE,
    _day,
    verify_pack,
)

ROC120_RESEARCH_DEFINITION = "catalog_roc120_native_sina_hfq.research.v1"
REVENUE_RESEARCH_DEFINITION = "reported_operating_revenue_ttm.research.v1"


def _decimal(value):
    if type(value) not in (str, int, Decimal):
        raise AcceptanceError("exact source decimal text or integer required")
    return number(value)


class _ROCSeries(NativeSeries):
    def roc120(self, output_date: str) -> Decimal:
        if self.item.get("price_unit") != "CNY_per_share":
            raise AcceptanceError("explicit raw price unit required")
        if self.item.get("price_policy") != "raw_times_native_hfq_no_rounding":
            raise AcceptanceError("explicit source-specific post-price policy required")
        window = self.window(output_date, 121)
        factors = self.item.get("factors")
        if not isinstance(factors, list) or not factors:
            raise InputUnavailable("NATIVE_FACTOR_UNAVAILABLE", "source_missing")
        labels = [_day(f["date"]) for f in factors]
        if labels != sorted(set(labels)):
            raise AcceptanceError("factor effective dates duplicate or unordered")
        numbers = [_decimal(f["value"]) for f in factors]
        if any(f <= 0 for f in numbers):
            raise AcceptanceError("nonpositive factor")
        prices = []
        with localcontext() as ctx:
            ctx.prec = 50
            for row in window:
                if row.get("close") is None:
                    raise InputUnavailable("RAW_CLOSE_MISSING", "source_missing")
                raw = _decimal(row["close"])
                if raw <= 0:
                    raise InputUnavailable("NONPOSITIVE_CLOSE")
                matches = [f for d, f in zip(labels, numbers, strict=True) if d <= row["date"]]
                if not matches:
                    raise InputUnavailable("NO_PRIOR_NATIVE_FACTOR", "source_missing")
                prices.append(raw * matches[-1])
            return 100 * (prices[-1] - prices[0]) / prices[0]


def roc120_native_candidate(
    root, security, output_dates, *, expected_inputs_sha256, definition, role
):
    """Read a verified native pack and evaluate the unchanged 121-session candidate.

    Raw decimal close times the date-effective native Sina factor; no price or
    output quantization. All dates and source files must be explicitly supplied.
    Typed missing/insufficient inputs remain failed rows, never filled prices.
    """
    if definition != ROC120_RESEARCH_DEFINITION or role != RESEARCH_ROLE:
        raise AcceptanceError("explicit ROC120 research definition and role required")
    if (
        not isinstance(output_dates, list)
        or not output_dates
        or output_dates != sorted(set(output_dates))
    ):
        raise AcceptanceError("ordered explicit output dates required")
    pack = verify_pack(Path(root), expected_inputs_sha256)
    items = [item for item in pack["items"] if item["security"] == security]
    if len(items) != 1:
        raise AcceptanceError("one explicit security in the native input pack required")
    series, rows = _ROCSeries(items[0]), []
    for day in output_dates:
        try:
            if any(row["trading_state"] != "observed_trade" for row in series.window(day, 121)):
                raise InputUnavailable("NORMAL_TRADING_ONLY")
            value = series.roc120(day)
            row = {"state": "valid", "value": str(value), "reason": None}
        except InputUnavailable as exc:
            row = {"state": exc.state, "value": None, "reason": exc.reason}
        rows.append({"security": series.security, "date": day, **row})
    return {
        "definition_version": definition,
        "unit": "percent_points",
        "adjustment": "native_sina_hfq_exact",
        "as_of_policy": "retrospective_native_snapshot",
        "slot": "ROC120",
        "catalog_candidate": "ROC120",
        "formal_id": None,
        "role": role,
        "rows": rows,
        "input_manifest_sha256": expected_inputs_sha256,
        "formula_core_officially_confirmed": True,
        "jq_equivalent": False,
        "historical_pit_certified": False,
        "strategy_visible": False,
        "production_eligible": False,
        "integration_independent_review_completed": False,
        "alignment_status": "not_passed",
        "source_issue": "ADJ-INPUT-001",
        "remaining_gap": "Actual factor endpoint dates/prices, snapshot, adjustment basis and processing contract are unresolved.",
    }


def reported_operating_revenue_ttm(
    ledger: FinancialLedger, security: str, as_of: str, *, definition: str, mode: str
) -> dict:
    """Return the existing unquantized consolidated operating-revenue TTM in CNY.

    Uses four complete quarters and the annual bridge at one explicit cutoff.
    Caller verifies source files with verify_pack_files before creating the ledger.
    This is operating revenue, not total operating revenue or a vendor TTM alias.
    """
    if definition != REVENUE_RESEARCH_DEFINITION or mode != RESEARCH_MODE:
        raise AcceptanceError(
            "explicit reported-revenue definition and research visibility required"
        )
    field = "operating_revenue"
    period = ledger.latest_period(security, field, as_of=as_of, mode=mode)
    component = ledger.quarter_ttm(security, field, period, as_of=as_of, mode=mode)
    return {
        "definition_version": definition,
        "slot": "operating_revenue_ttm",
        "catalog_candidate": "operating_revenue_ttm",
        "formal_id": None,
        "security": security,
        "as_of": as_of,
        "period_end": period,
        "value": component["value"],
        "unit": "CNY",
        "components": {field: component},
        "version_policy": "latest_visible_YTD_at_same_cutoff",
        "precision_status": "unquantized_decimal50_reported_amount",
        "historical_pit_certified": False,
        "jq_equivalent": False,
        "strategy_visible": False,
        "production_eligible": False,
        "integration_independent_review_completed": False,
        "alignment_status": "not_passed",
        "remaining_gap": "The built-in materialized TTM component versions and processing/storage path remain unidentified.",
    }
