"""Exchange aggregates with native precision and explicit effective dates."""

from datetime import date, timedelta
from decimal import Decimal
import json
import re
from urllib.parse import urlencode

from ashare_data.providers.free_updates.free_update_dates import reviewed_date

MARGIN = "szse_margin_summary"
LATEST_MARGIN = "szse_margin_latest"
MARGIN_COLUMNS = {
    "jrrzmr": "融资买入额<br>(亿元)",
    "jrrzye": "融资余额<br>(亿元)",
    "jrrjmc": "融券卖出量<br>(亿股/亿份)",
    "jrrjyl": "融券余量<br>(亿股/亿份)",
    "jrrjye": "融券余额<br>(亿元)",
    "jrrzrjye": "融资融券余额<br>(亿元)",
}
MARGIN_FIELDS = {key: label.split("<br>")[0] for key, label in MARGIN_COLUMNS.items()}
MARGIN_UNITS = {
    label: "100000000 shares_or_fund_units" if key in {"jrrjmc", "jrrjyl"} else "100000000 CNY"
    for key, label in MARGIN_FIELDS.items()
}
MARGIN_SOURCE = "https://www.szse.cn/api/report/ShowReport/data"


def explicit_date(params):
    if set(params) != {"date"} or not re.fullmatch(r"\d{8}|\d{4}-\d{2}-\d{2}", str(params["date"])):
        raise ValueError("margin summary needs an explicit full date")
    day = date.fromisoformat(params["date"])
    if not 1900 <= day.year <= 2200:
        raise ValueError("margin date outside reviewed range")
    return day.isoformat()


def margin_row(native, day):
    if set(native) != set(MARGIN_COLUMNS):
        raise ValueError("margin summary native fields changed")
    for value in native.values():
        # Retain two-place source precision. Removing grouping is not an
        # assertion that an unrounded CNY/share value has been recovered.
        if not isinstance(value, str) or not re.fullmatch(
            r"(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}", value
        ):
            raise ValueError("margin summary precision/grouping changed or unavailable")
    return {MARGIN_FIELDS[k]: value.replace(",", "") for k, value in native.items()} | {
        "日期": day,
        "exchange": "SZSE",
        "source_native": native,
        "source_units": MARGIN_UNITS,
        "source_decimal_places": 2,
        "quantity_scope": "mixed shares and fund units; not stock shares alone",
        "publication_at": None,
    }


class UnpublishedDate(ValueError):
    """The named source date has no published rows; it is not a zero balance."""


def collect(capture, params):
    day = explicit_date(params)
    query = {
        "SHOWTYPE": "JSON",
        "CATALOGID": "1837_xxpl",
        "txtDate": day,
        "tab1PAGENO": 1,
        "random": "0.7425245522795993",
    }
    body = json.loads(capture.get(MARGIN_SOURCE + "?" + urlencode(query)))
    if not isinstance(body, list):
        raise ValueError("margin summary response is not a list of named tables")
    selected = [
        t for t in body if isinstance(t, dict) and t.get("metadata", {}).get("tabkey") == "tab1"
    ]
    if len(selected) != 1:
        raise ValueError("margin summary table missing or duplicated")
    table = selected[0]
    meta, native = table.get("metadata", {}), table.get("data")
    if (
        table.get("error") is not None
        or meta.get("name") != "融资融券交易总量"
        or meta.get("catalogid") != "1837_xxpl"
        or (meta.get("subname") != day and not (native == [] and meta.get("subname") == ""))
        or meta.get("cols") != MARGIN_COLUMNS
        or meta.get("pagesize") != -1
        or meta.get("pagecount") != 0
        or not isinstance(native, list)
        or len(native) > 1
    ):
        raise ValueError(
            "margin summary date/unit/table mismatch or unavailable; no empty-day inference"
        )
    if not native:
        raise UnpublishedDate("margin report has no published rows for " + day)
    rows = [margin_row(native[0], day)]
    capture.dataset_metadata = {
        "source": MARGIN_SOURCE,
        "source_table": meta["name"],
        "source_count": 1,
        "pages_received": 1,
        "complete_requested_window": True,
        "complete_returned_window": True,
        "complete_history": False,
        "complete_security_details": False,
        "scope": "one official exchange-wide aggregate for the explicit source date; tab2 details are not collected",
        "source_effective_date": day,
        "source_columns": meta["cols"],
        "source_footer": meta.get("footer"),
        "source_decimal_places": 2,
        "source_rounding_method": "not specified; no reconstruction of unrounded amounts",
        "units": MARGIN_UNITS,
        "publication_at": None,
        "limitation": "preserve native hundred-million units and displayed precision; not equivalent precision to historical Jin10 yuan values",
    }
    return rows


def collect_latest(capture, params):
    if set(params) != {"as_of"}:
        raise ValueError("latest margin needs an explicit observation date")
    latest = date.fromisoformat(explicit_date({"date": params["as_of"]}))
    absent = []
    # Publication may lag the trading day. Query explicit source dates, never
    # interpret the undated default response's 2010 header as a latest date.
    for offset in range(8):
        day = (latest - timedelta(days=offset)).isoformat()
        try:
            rows = collect(capture, {"date": day})
        except UnpublishedDate:
            absent.append(day)
            continue
        capture.dataset_metadata.update(requested_as_of=latest.isoformat(),
            dates_without_published_rows=absent, lookup_requests=offset + 1,
            scope="latest published official aggregate within eight explicit calendar dates; no holiday or zero-balance inference")
        return rows
    raise UnpublishedDate("no published report in eight explicit dates; retain prior snapshot")


def stock_statistics(rows, as_of, numeric):
    """Recognize incomplete current months without assigning undocumented units."""
    fields = "TOTAL_SHARES_SH TOTAL_SZARES_SZ TOTAL_MARKE_SH TOTAL_MARKE_SZ DEAL_AMOUNT_SH DEAL_AMOUNT_SZ VOLUME_SH VOLUME_SZ HIGH_INDEX_SH HIGH_INDEX_SZ LOW_INDEX_SH LOW_INDEX_SZ".split()
    periods, complete, incomplete = [], [], []
    for row in rows:
        day = reviewed_date(row.get("REPORT_DATE"))
        if not day or day[8:] != "01" or day > as_of or reviewed_date(row.get("TIME")) != day:
            raise ValueError("stock-statistics period label mismatch or future month")
        if not set(fields) <= set(row):
            raise ValueError("stock-statistics field schema changed")
        missing = [f for f in fields if numeric(row[f]) is None]
        for field in fields:
            value = numeric(row[field])
            if value is not None and value < 0:
                raise ValueError("negative stock-statistics amount/quantity/index")
        for market in ["SH", "SZ"]:
            lo, hi = numeric(row["LOW_INDEX_" + market]), numeric(row["HIGH_INDEX_" + market])
            if lo is not None and hi is not None and lo > hi:
                raise ValueError("stock-statistics high/low index reversed")
        periods.append(day)
        if missing:
            incomplete.append(
                {
                    "period": day,
                    "fields": missing,
                    "current_month_in_progress": day[:7] == as_of[:7],
                }
            )
        else:
            complete.append(day)
    if not rows or len(set(periods)) != len(periods):
        raise ValueError("empty or duplicate stock-statistics month")
    return {
        "contract_reviewed": True,
        "business_key": ["REPORT_DATE"],
        "unique_keys": len(periods),
        "frequency": "monthly_source_revision",
        "latest_period": max(periods),
        "latest_complete_period": max(complete, default=None),
        "incomplete_periods": incomplete,
        "units": {f: "source_index_points" if f.startswith(('HIGH_INDEX_','LOW_INDEX_')) else "source_native_scale_unverified" for f in fields},
        "field_latest_nonnull_periods": {f:max((reviewed_date(r['REPORT_DATE']) for r in rows if numeric(r[f]) is not None),default=None) for f in fields},
        "gaps": [] if any(numeric(r[f]) is not None for r in rows for f in fields if 'INDEX_' in f) else ["没有可用指数值"],
        "limitations": [
            "当月未结束的空值保留；完整历史月份与当月部分值分开，不填充",
            "统计月份不替代首次发布日期",
            "股本、金额和成交量仅保存原始数值，尺度未知，不换算为股或元；高低指数值单独可用",
        ],
    }


def validate(rows, job, as_of, numeric):
    if job["function"] == "macro_china_stock_market_cap":
        return stock_statistics(rows, as_of, numeric)
    if job["function"] not in {MARGIN, LATEST_MARGIN}:
        return None
    if job["function"] == LATEST_MARGIN:
        requested = explicit_date({"date": job["parameters"]["as_of"]})
        if not rows:
            raise ValueError("no published latest margin rows")
        day = rows[0]["日期"]
        if not 0 <= (date.fromisoformat(requested) - date.fromisoformat(day)).days <= 7 or requested > as_of:
            raise ValueError("margin source date outside lookup window")
    else:
        day = explicit_date(job["parameters"])
    if len(rows) != 1 or day > as_of or rows[0] != margin_row(rows[0]["source_native"], day):
        raise ValueError("margin summary source identity/date/projection mismatch")
    row = rows[0]
    if any(numeric(row[f]) is None or numeric(row[f]) < 0 for f in MARGIN_UNITS):
        raise ValueError("margin summary negative or unavailable measure")
    difference = abs(
        numeric(row["融资融券余额"]) - numeric(row["融资余额"]) - numeric(row["融券余额"])
    )
    # Three source values each display two decimal places. A discrepancy below
    # three display steps is possible without assuming a rounding convention.
    if difference >= Decimal("0.03"):
        raise ValueError("margin total differs from components beyond displayed precision")
    return {
        "contract_reviewed": True,
        "business_key": ["exchange", "日期"],
        "unique_keys": 1,
        "frequency": "exchange_daily_aggregate_and_revisions",
        "units": MARGIN_UNITS,
        "latest_period": day,
        "latest_announcement": None,
        "source_decimal_places": 2,
        "gaps": [],
        "balance_difference_native_units": str(difference),
        "limitations": [
            "源表的亿元、亿股/亿份和两位小数原样保留；不与旧金十元级精度混合",
            "来源只证明本日汇总，证券明细分页与全历史尚未验收；空日期不推定休市或无交易",
            "公告时刻未知，保留实际首次取得时点；融资余额包含调出标的名单的余额",
        ],
    }
