"""Shared date-role audit: future plans must not become observed actuals.

Exceptions belong to reviewed source fields, not a caller-supplied forecast flag.
Nothing is silently removed. Reviewed adapters may explicitly quarantine an
unidentifiable native row with its raw payload, reason and reconciled count.
Every returned observation is still checked here. Failed batches retain their
raw receipt and original worker result; no observation is committed.
"""
from datetime import date, datetime
from functools import lru_cache
import json
import re

from ashare_data.providers.free_updates import DATE_FIELDS, catalog, source_date

EXTRA_DATES = {"NOTICE_DATE", "END_DATE", "UPDATE_DATE", "publication_at", "announcement_at",
               "成立日期", "DELIST_DATE", "上市日期", "申购日期", "中签公告日", "中签缴款日", "摇号结果公告日", "周期结束",
               "上市时间", "中签号发布日", "原股东配售-股权登记日", "announcement_date", "dim_date", "DIM_DATE", "opendate",
               "统计月份", "公告日期", "生效时间"}
IPO_SCHEDULE = {"上市日期", "申购日期", "中签公告日", "中签缴款日", "摇号结果公告日"}
BOND_SCHEDULE = {"上市时间", "申购日期", "中签号发布日", "原股东配售-股权登记日"}
MISSING = (None, "", "--", "-", "N/A", "NaT")


def reviewed_date(value):
    """Parse the entire date representation, never repair an invalid prefix."""
    text = str(value)
    # ISO timestamps can include time precision and offsets. Date-only labels
    # remain source calendar dates; this does not infer publication time.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T].+", text):
        try:
            stamp = datetime.fromisoformat(text)
            return stamp.date().isoformat() if 1900 <= stamp.year <= 2200 else None
        except ValueError:
            return None
    if not re.fullmatch(r"(?:\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|\d{4}年\d{1,2}月(?:份|\d{1,2}日)?|\d{4}|\d{6}|\d{8})", text):
        return None
    day = source_date(text)
    return day if day and 1900 <= int(day[:4]) <= 2200 else None


class DateContractError(ValueError):
    def __init__(self, audit):
        self.date_audit = audit
        detail = json.dumps(audit["violations"][:5], ensure_ascii=False)
        super().__init__(f"date-role contract rejected {audit['violation_count']} field(s): {detail}")


@lru_cache(maxsize=1)
def source_roles():
    from pathlib import Path
    calendar = {c["function"] for i in catalog()["items"] if i["mode"] == "calendar"
                for key in ("calls", "historical_candidate_calls") for c in i.get(key, [])}
    specs = json.loads(Path(__file__).with_name("public-macro-requests.json").read_text())["specs"]
    return calendar, {name for name, spec in specs.items() if spec.get("kind") == "jin10_latest"}


def role(row, field, job):
    name = job["function"]
    if name in {"ndrc_oil_adjustment", "shanghai_oil_prices"} and field == "生效时间":
        return "published_policy_effective_date"
    if name == 'macro_china_reserve_requirement_ratio' and field == 'TRADE_DATE':
        return 'published_policy_effective_date'
    if name in {"bond_zh_cov", "bond_zh_cov_info"}:
        if field == "DELIST_DATE":
            # In the native reference table this can equal maturity even when
            # redemption fields describe an earlier exit. It is not evidence
            # of completed or announced delisting and must stay separately named.
            return "source_bond_lifecycle_reference_not_execution"
        from ashare_data.providers.free_update_reference import BOND_DATES
        if field in BOND_DATES:
            return "published_bond_contract_or_schedule_not_execution"
    if name == "stock_profit_forecast_em" and re.fullmatch(r"YEAR[1-4]", field):
        mark = row.get("YEAR_MARK"+field[-1])
        return {"E": "estimated_fiscal_year", "A": "actual_fiscal_year"}.get(mark, "unknown_fiscal_year_mark")
    if name == "stock_new_ipo_cninfo" and field in IPO_SCHEDULE:
        return "published_ipo_schedule_not_confirmed_execution"
    if name == "bond_zh_cov" and field in BOND_SCHEDULE:
        return "published_bond_schedule_not_confirmed_execution"
    if name == "futures_resample" and field == "周期结束" and row.get("周期未结束") is True:
        return "calendar_bucket_end_not_market_observation"
    calendar_functions, releases = source_roles()
    if name in calendar_functions and field == "trade_date":
        return "published_trading_calendar"
    # The reviewed Jin10 latest schema explicitly distinguishes forecast from
    # actual, and binds every row to the requested indicator.
    if field == "日期" and row.get("indicator") == name and "今值" in row and row["今值"] in MISSING and "预测值" in row:
        if name in releases:
            return "scheduled_release_without_actual"
    return "observed_or_reported_date"


def audit_dates(rows, job, as_of):
    date.fromisoformat(as_of)
    allowed_roles = {"estimated_fiscal_year", "published_ipo_schedule_not_confirmed_execution",
                     "published_bond_schedule_not_confirmed_execution",
                     "calendar_bucket_end_not_market_observation", "published_trading_calendar",
                     "published_bond_contract_or_schedule_not_execution",
                     "source_bond_lifecycle_reference_not_execution",
                     "scheduled_release_without_actual", "published_policy_effective_date"}
    violations, future, unparsed = [], [], []
    checked = 0
    for index, row in enumerate(rows):
        fields = set(row) & (set(DATE_FIELDS) | EXTRA_DATES)
        if job["function"] in {"bond_zh_cov", "bond_zh_cov_info"}:
            from ashare_data.providers.free_update_reference import BOND_DATES
            fields |= set(row) & BOND_DATES
        if job["function"] == "stock_profit_forecast_em":
            fields |= set(row) & {"YEAR1", "YEAR2", "YEAR3", "YEAR4"}
        for field in sorted(fields):
            value = row[field]
            if value in MISSING:
                continue
            kind = role(row, field, job)
            checked += 1
            detail = {"row_index": index, "field": field, "source_value": value, "role": kind}
            if kind.endswith("fiscal_year") or kind == "unknown_fiscal_year_mark":
                if kind == "unknown_fiscal_year_mark" or isinstance(value, bool) or not re.fullmatch(r"\d{4}", str(value)) or not 1900 <= int(value) <= 2200:
                    violations.append(detail | {"reason": "fiscal year or native A/E marker invalid"})
                    continue
                day = str(value)+"-12-31"
            else:
                day = reviewed_date(value)
            if day is None:
                if re.match(r"^\d{4}[-/.年]\d", str(value)) or re.fullmatch(r"\d{6}|\d{8}", str(value)):
                    violations.append(detail | {"reason": "invalid source calendar date; no prefix repair"})
                else:
                    unparsed.append(detail | {"reason": "unreviewed date representation; not used as freshness evidence"})
                continue
            if day > as_of:
                detail |= {"normalized_date": day}
                if kind in allowed_roles:
                    future.append(detail | {"actual_observation": False})
                else:
                    violations.append(detail | {"reason": "future date has no reviewed forecast/schedule meaning"})
    return {"as_of": as_of, "checked_fields": checked, "violation_count": len(violations), "violations": violations,
            "allowed_future_count": len(future), "allowed_future_dates": future,
            "unparsed_count": len(unparsed), "unparsed_date_fields": unparsed,
            "policy": "retain every original row; reject whole batch on date violation; never filter future actuals",
            "scope": "reviewed date labels and explicit forecast-year fields; content text is not parsed as dates"}


def require_dates(rows, job, as_of):
    result = audit_dates(rows, job, as_of)
    if result["violations"]:
        raise DateContractError(result)
    return result
