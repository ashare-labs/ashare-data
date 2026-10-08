"""Exact decimal reconciliation; a mismatch is not an instruction to invent bars."""
from decimal import Decimal

from .model import day, number, require, symbol


def reconcile_turnover(rows, reference, *, bars_scope="unknown", coverage=None, amount_tolerance="0"):
    require(rows, "SOURCE_NO_DATA", "没有可对账记录")
    symbol(reference["security"])
    trade_date = day(reference["trade_date"])
    require(all(row["day"][:10] == trade_date for row in rows), "REFERENCE_DATE_MISMATCH", "不能跨日期混算对账")
    require(reference.get("volume_unit") == "share" and reference.get("amount_unit") == "CNY",
            "UNIT_UNKNOWN", "对账参考必须显式为股/元")
    require(all("amount" in row for row in rows), "SOURCE_FIELD_MISSING", "缺成交额不能合成或按零对账")
    volumes = [number(row["volume"]) for row in rows]
    require(all(v == v.to_integral_value() for v in volumes), "INVALID_VOLUME", "每条股数必须为整数")
    volume = sum(volumes, Decimal(0))
    amount = sum((number(row["amount"]) for row in rows), Decimal(0))
    require(volume == volume.to_integral_value(), "INVALID_VOLUME", "股数必须为整数")
    tolerance = number(amount_tolerance)
    require(tolerance <= Decimal("0.01"), "INVALID_REQUEST", "舍入容差不得大于一分；不能放宽到吸收统计差额")
    dv, da = number(reference["volume"]) - volume, number(reference["amount"]) - amount
    require(number(reference["volume"]) == number(reference["volume"]).to_integral_value(),
            "INVALID_VOLUME", "参考股数必须为整数")
    match = dv == 0 and abs(da) <= tolerance
    scope_known = bars_scope != "unknown" and bars_scope == reference.get("trading_scope") and bool(reference.get("scope_evidence"))
    coverage_ok = bool(coverage and coverage.get("complete") and coverage.get("contract_id"))
    reasons = []
    if not scope_known:
        reasons.append("SESSION_SCOPE_UNKNOWN")
    if not coverage_ok:
        reasons.append("COVERAGE_NOT_VERIFIED")
    if not match:
        reasons.append("TURNOVER_UNRECONCILED")
    return {"status": "failed" if not match else "unknown" if reasons else "passed",
            "accepted": not reasons, "comparison": "equal_within_declared_tolerance" if match else "different",
            "row_count": len(rows), "volume_shares": str(volume), "amount_cny": str(amount),
            "reference_volume_shares": str(reference["volume"]), "reference_amount_cny": str(reference["amount"]),
            "reference_minus_bars_volume": str(dv), "reference_minus_bars_amount": str(da),
            "amount_tolerance_cny": str(tolerance), "bars_scope": bars_scope,
            "reference_scope": reference.get("trading_scope", "unknown"), "reasons": reasons,
            "cause": "not established by totals alone", "fabricated_rows": 0,
            "independent_truth_verified": False}
