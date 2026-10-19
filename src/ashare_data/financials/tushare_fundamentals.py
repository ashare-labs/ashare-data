"""Ordinary-account financial versions, distributions and adjustment observations.

Date precision is not a publication timestamp. A report type/content projection is
not proof that the provider exposes every historical revision. Provider factors
are kept separate from Sina hfq and must not be concatenated into one series.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
import re

from ashare_data.financials import expansion as e

STATEMENTS = {"income": "lrb", "balancesheet": "fzb", "cashflow": "llb"}
METRICS = {
    "income": {"total_revenue": "total_operating_revenue", "revenue": "operating_revenue",
               "n_income": "net_profit", "n_income_attr_p": "parent_net_profit"},
    "balancesheet": {"total_assets": "total_assets", "total_liab": "total_liabilities"},
    "cashflow": {"n_cashflow_act": "net_operate_cash_flow", "c_cash_equ_end_period": "cash_equivalents_at_end"},
}
VERSION_TYPES = {"1": "latest_consolidated", "4": "adjusted_prior_comparable_consolidated",
                 "5": "pre_adjustment_consolidated"}
DOCS = {"income": 33, "balancesheet": 36, "cashflow": 44, "fina_indicator": 79, "dividend": 103, "adj_factor": 28}
# Only fina_indicator (100) and dividend (2000) document these source limits.
# The statement limit is a conservative local truncation guard, not a source claim.
ROW_GUARDS = {"income": 100, "balancesheet": 100, "cashflow": 100, "fina_indicator": 100, "dividend": 2000}
FINANCIAL_DATES = {"ann_date", "f_ann_date", "end_date"}
DIVIDEND_DATES = {"end_date", "ann_date", "record_date", "ex_date", "pay_date", "div_listdate", "imp_ann_date", "base_date"}


def required_day(value):
    result = e.day(value)
    if result is None:
        raise ValueError("Explicit valid date required")
    return result


def validate_params(api, params):
    if not re.fullmatch(r"\d{6}\.(SH|SZ)", params.get("ts_code", "")):
        raise ValueError("Ordinary endpoints require one explicit mainboard target code")
    allowed = {"ts_code", "ann_date", "start_date", "end_date", "period"}
    if api in STATEMENTS:
        allowed |= {"report_type", "comp_type"}
        if api != "balancesheet":
            allowed.add("f_ann_date")
        if params.get("report_type") not in VERSION_TYPES:
            raise ValueError("Explicit consolidated report type 1, 4 or 5 required")
    elif api == "dividend":
        allowed = {"ts_code", "ann_date", "record_date", "ex_date", "imp_ann_date"}
    elif api == "adj_factor":
        allowed = {"ts_code", "trade_date", "start_date", "end_date"}
    elif api != "fina_indicator":
        raise ValueError("Unapproved fundamentals endpoint")
    if not set(params) <= allowed:
        raise ValueError("Undocumented or unapproved request parameter")
    for key, value in params.items():
        if key.endswith("date") or key == "period":
            required_day(value)
    if ("start_date" in params) != ("end_date" in params):
        raise ValueError("Both range boundaries are required")
    if "start_date" in params:
        start, end = (date.fromisoformat(required_day(params[k])) for k in ("start_date", "end_date"))
        if end < start or (api == "adj_factor" and (end - start).days > 365):
            raise ValueError("Invalid or oversized date window")
    elif api != "dividend" and not set(params) & {"period", "ann_date", "f_ann_date", "trade_date"}:
        raise ValueError("Explicit bounded window or one date required")


def decimal_value(value):
    if value in (None, "", "--"):
        return None
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Nonfinite source number")
    return format(number, "f")


def plain(row):
    return {k: format(v, "f") if isinstance(v, Decimal) else v for k, v in row.items()}


def field(value, unit, **extra):
    value = decimal_value(value)
    return dict(value=value, state="source_observed" if value is not None else "source_null_or_missing", unit=unit, **extra)


def financial_row(row, api, task):
    from ashare_data.financials import tushare_reference as ts
    _, params = ts.request_identity(task["url"])
    period = required_day(row.get("end_date"))
    dates = {k: e.day(row.get(k)) for k in FINANCIAL_DATES}
    if params.get("period") and period != required_day(params["period"]):
        raise ValueError("Financial period mismatch")
    for key in ("ann_date", "f_ann_date"):
        if params.get(key) and dates[key] != required_day(params[key]):
            raise ValueError("Announcement filter mismatch")
    if "start_date" in params:
        # The indicator API uses report periods; statement APIs use announcement dates.
        selected = period if api == "fina_indicator" else dates["ann_date"]
        if selected is None or not required_day(params["start_date"]) <= selected <= required_day(params["end_date"]):
            raise ValueError("Financial date-window semantics mismatch")
    if api in STATEMENTS and str(row.get("report_type")) != params["report_type"]:
        raise ValueError("Consolidated report type mismatch")
    if str(row.get("update_flag")) not in {"0", "1", "None", ""}:
        raise ValueError("Unknown update_flag")
    selected = ts.request_fields(task["url"]).split(",")
    identity = set(ts.FINANCIAL_ID.split(","))
    values = {}
    for name in selected:
        if name in identity:
            continue
        unit = ("shares" if name == "total_share" else "ratio" if api == "fina_indicator"
                else "provider_native_amount_unit_unverified")
        if name in {"roe", "roa", "netprofit_margin", "debt_to_assets"}:
            unit = "percent"
        if name in {"basic_eps", "diluted_eps", "eps"}:
            unit = "CNY/share_source_documentation"
        values[name] = field(row.get(name), unit, canonical_field=METRICS.get(api, {}).get(name),
                             currency=None, scale_verified=False)
    return dict(schema="ad.tushare-financial-version.v1", statement=STATEMENTS.get(api, "indicators"),
                period_end=period, period_start=None if api == "balancesheet" else period[:4] + "-01-01",
                scope="consolidated" if api in STATEMENTS else "provider_indicator_scope",
                basis="instant" if api == "balancesheet" else "YTD" if api in STATEMENTS else "per_metric_definition",
                announcement_date=dates["ann_date"], actual_announcement_date=dates["f_ann_date"],
                announcement_time_precision="date" if dates["ann_date"] or dates["f_ann_date"] else "unknown",
                published_at=None, historical_available_at=None,
                report_type=str(row.get("report_type")) if api in STATEMENTS else None,
                version_kind=VERSION_TYPES.get(str(row.get("report_type")), "provider_indicator_snapshot"),
                source_update_flag=row.get("update_flag"), company_type=row.get("comp_type"), end_type=row.get("end_type"),
                revision_history_complete=False, supersedes=None, fields=values,
                period_after_code_exit=bool(task.get("out") and period >= required_day(task["out"])),
                cross_provider_publication_binding=False)


def dividend_row(row, task):
    from ashare_data.financials import tushare_reference as ts
    _, params = ts.request_identity(task["url"])
    dates = {k: e.day(row.get(k)) for k in DIVIDEND_DATES}
    for key in set(params) - {"ts_code"}:
        if dates[key] != required_day(params[key]):
            raise ValueError("Dividend event filter mismatch")
    values = {k: field(row.get(k), "CNY/eligible_share" if k.startswith("cash_") else "new_shares/eligible_share")
              for k in ("stk_div", "stk_bo_rate", "stk_co_rate", "cash_div", "cash_div_tax")}
    values["cash_div"]["tax_basis"] = "after_tax_source_claimed_not_investor_specific"
    values["cash_div_tax"]["tax_basis"] = "gross_pre_tax"
    base = decimal_value(row.get("base_share"))
    values["base_share"] = dict(native_value=base, native_unit="10000_shares",
        value=format(Decimal(base) * 10000, "f") if base is not None else None, unit="shares",
        state="source_observed" if base is not None else "source_null_or_missing")
    if any(Decimal(v["value"]) < 0 for v in values.values() if v["value"] is not None):
        raise ValueError("Negative dividend quantity")
    for name, value in dates.items():
        values[name] = dict(value=value, unit="date", state="source_observed" if value else "source_null_or_missing")
    values["div_proc"] = dict(value=row.get("div_proc"), unit="source_status",
                               state="source_observed" if row.get("div_proc") else "source_null_or_missing")
    return dict(schema="ad.tushare-dividend-version.v1", fields=values, source_status=row.get("div_proc"),
                implemented=row.get("div_proc") == "实施", absence_proved=False,
                period_after_code_exit=bool(task.get("out") and dates.get("ex_date") and dates["ex_date"] >= required_day(task["out"])),
                historical_available_at=None, revision_history_complete=False,
                announcement_semantics="ann_date_preplan; imp_ann_date_implementation", date_precision="date")


def factor_row(row, task):
    from ashare_data.financials import tushare_reference as ts
    _, params = ts.request_identity(task["url"])
    day = required_day(row.get("trade_date"))
    start, end = (required_day(params[k]) for k in ("start_date", "end_date")) if "start_date" in params else (required_day(params["trade_date"]),) * 2
    if not start <= day <= end:
        raise ValueError("Factor date mismatch")
    value = decimal_value(row.get("adj_factor"))
    if value is None or Decimal(value) <= 0:
        raise ValueError("Factor must be positive and finite")
    return dict(schema="ad.tushare-factor-observation.v1", trade_date=day,
                fields={"adj_factor": dict(value=value, unit="dimensionless", state="source_observed")},
                basis="tushare_dividend_reinvestment_provider_factor", compatible_with_sina_hfq=False,
                carry_forward_authorized=False, historical_available_at=None)


def observations(raw, task):
    from ashare_data.financials import tushare_reference as ts
    api, params = ts.request_identity(task["url"])
    source, rows, periods, seen_days = ts.table(raw), [], [], set()
    for index, row in enumerate(source):
        if row.get("ts_code") != params["ts_code"]:
            raise ValueError("Response target code mismatch")
        if api in STATEMENTS or api == "fina_indicator":
            result = financial_row(row, api, task)
            period = result["period_end"]
            periods.append(period)
            if not task["target_start"] <= period <= task["target_end"]:
                continue
        elif api == "dividend":
            result = dividend_row(row, task)
            event_day = result["fields"]["ex_date"]["value"] or result["fields"]["ann_date"]["value"]
            if event_day:
                periods.append(event_day)
                if not task["target_start"] <= event_day <= task["target_end"]:
                    continue
        elif api == "adj_factor":
            result = factor_row(row, task)
            if result["trade_date"] in seen_days:
                raise ValueError("Duplicate daily factor")
            seen_days.add(result["trade_date"])
        else:
            raise ValueError("Fundamentals task required")
        result.update(source="tushare", api=api, security=e.security(task["symbol"]),
                      identity_status="request_bound_not_independently_verified",
                      source_locator=f"data/items/{index}", source_ordinal=index,
                      source_revision_id=e.digest(e.encoded(plain(row))),
                      revision_identity_basis="requested_field_projection_hash_and_source_locator",
                      source_documentation=f"https://tushare.pro/document/2?doc_id={DOCS[api]}")
        rows.append(result)
    cap = ROW_GUARDS.get(api)
    has_more = json.loads(raw).get("data", {}).get("has_more") is True
    coverage = dict(source_rows=len(source), target_rows=len(rows), source_earliest_period=min(periods) if periods else None,
                    source_latest_period=max(periods) if periods else None,
                    row_cap_reached=bool((cap and len(source) >= cap) or has_more), row_guard=cap,
                    guard_is_documented_source_limit=api in {"fina_indicator", "dividend"},
                    date_window_semantics="report_period" if api == "fina_indicator" else "announcement_date" if api in STATEMENTS else "event_or_trading_date",
                    absence_proved=False, revision_history_complete=False, full_history_complete=False)
    if api == "adj_factor":
        coverage.update(missing_evidenced_sessions=sorted(set(task.get("expected_sessions", [])) - seen_days),
                        expected_sessions_bound="expected_sessions" in task,
                        source_earliest_date=min(seen_days) if seen_days else None,
                        source_latest_date=max(seen_days) if seen_days else None)
    return rows, coverage


def split_task(task):
    """Bisect only the documented date dimension; never assume offset pagination."""
    from ashare_data.financials import tushare_reference as ts
    api, params = ts.request_identity(task["url"])
    if api not in {*STATEMENTS, "fina_indicator"} or "start_date" not in params:
        return []
    a, b = (date.fromisoformat(required_day(params[k])) for k in ("start_date", "end_date"))
    if a == b:
        return []
    mid = a + (b - a) // 2
    children = []
    for start, end in ((a, mid), (mid + timedelta(days=1), b)):
        new = params | dict(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        url = ts.request_url(api, new, ts.request_fields(task["url"]))
        children.append(task | dict(id="tushare_" + api + "-" + e.digest(url.encode())[:24], url=url,
                                    start=start.isoformat(), end=end.isoformat(), split_parent=task["id"],
                                    gap_group=task.get("gap_group", task["id"])))
    return children
