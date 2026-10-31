"""Issuer-period top holders with separate announcement and source-update dates.

These are the reports used by the reviewed Eastmoney holder-detail protocol.
Keep all native fields; only HOLD_NUM and the selected ownership ratio have a
normalized unit contract. A source date is not an intraday publication time.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import urlencode

from ashare_data.providers.free_updates import source_date

ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORTS = {
    "stock_gdfx_top_10_em": ("RPT_DMSK_HOLDERS", "RANK", "HOLD_RATIO"),
    "stock_gdfx_free_top_10_em": ("RPT_F10_EH_FREEHOLDERS", "HOLDER_RANK", "FREE_HOLDNUM_RATIO"),
}
KEY = ["SECUCODE", "END_DATE", "HOLDER_NAME", "SHARES_TYPE"]


def request_identity(params):
    symbol = params["symbol"]
    if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol):
        raise ValueError("holder request requires explicit market and six-digit security")
    period = source_date(params["date"])
    if not period or period[5:] not in {"03-31", "06-30", "09-30", "12-31"}:
        raise ValueError("holder request requires an explicit quarter end")
    return symbol[2:]+"."+symbol[:2].upper(), period


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid holder numeric field") from exc
    if not result.is_finite():
        raise ValueError("nonfinite holder numeric field")
    return result


def validate(rows, job, as_of):
    name = job["function"]
    if name not in REPORTS:
        return None
    security, period = request_identity(job["parameters"])
    _, rank_field, ratio_field = REPORTS[name]
    keys, announcements, updates, gaps = set(), [], [], []
    for row in rows:
        if (row.get("SECUCODE") != security or row.get("SECURITY_CODE") != security[:6]
                or source_date(row.get("END_DATE")) != period):
            raise ValueError("holder security, market or report period mismatch")
        key = tuple(row.get(k) for k in KEY)
        if any(v is None or v == "" for v in key) or not row.get("HOLDER_NAME"):
            raise ValueError("holder business identity missing")
        if key in keys:
            raise ValueError("duplicate holder business key")
        keys.add(key)
        rank, shares, ratio = (number(row.get(k)) for k in (rank_field, "HOLD_NUM", ratio_field))
        if rank != rank.to_integral_value() or not 1 <= rank <= 10:
            raise ValueError("holder rank outside top ten")
        if shares != shares.to_integral_value() or shares < 0 or not 0 <= ratio <= 100:
            raise ValueError("invalid holder shares or ownership percentage")
        notice, update = source_date(row.get("NOTICE_DATE")), source_date(row.get("UPDATE_DATE"))
        if period > as_of or (notice and not period <= notice <= as_of):
            raise ValueError("holder report or announcement date outside observed range")
        if row.get("NOTICE_DATE") not in (None, "") and not notice:
            raise ValueError("unparseable holder announcement date")
        if row.get("UPDATE_DATE") not in (None, "") and (not update or update > as_of):
            raise ValueError("unparseable or future holder source-update date")
        if row.get("报告期") != period or row.get("公告日期") != notice:
            raise ValueError("holder date aliases differ from original source fields")
        if notice:
            announcements.append(notice)
        else:
            gaps.append("源 NOTICE_DATE 缺失；UPDATE_DATE 不替代公告日")
        if update:
            updates.append(update)
    if not rows:
        raise ValueError("empty holder response does not prove publication or coverage")
    return {"contract_reviewed": True, "business_key": KEY, "unique_keys": len(keys),
        "units": {"HOLD_NUM": "shares", ratio_field: "percent"},
        "frequency": "issuer_report_publication_and_revisions", "latest_period": period,
        "latest_announcement": max(announcements, default=None),
        "latest_source_update": max(updates, default=None), "announcement_precision": "date",
        "intraday_publication_verified": False, "source_rows": len(rows), "gaps": sorted(set(gaps)),
        "limitations": ["公告日期来自 NOTICE_DATE；UPDATE_DATE 仅为源更新日，不推断盘中可见时刻",
            "部分个人股东缺 HOLDER_CODE；名称仅作同证券同报告期内业务键，不能识别跨公司同一人",
            "仅当前请求证券和报告期的源可见股东；全市场、全部报告期与首次公告版本需另验",
            "其他原生补充字段保留原值，未自动推断单位或将持股变动文字转成数值"]}


def collect(capture, name, params):
    security, period = request_identity(params)
    report, rank, _ = REPORTS[name]
    query = {"reportName": report, "columns": "ALL", "pageSize": "50", "sortColumns": rank,
        "sortTypes": "1", "source": "WEB", "client": "WEB",
        "filter": f'(SECURITY_CODE="{security[:6]}")(END_DATE=\'{period}\')'}
    records, expected = [], None
    for page in range(1, 5):
        body = json.loads(capture.get(ENDPOINT+"?"+urlencode(dict(query, pageNumber=str(page)))))
        result = body.get("result")
        if body.get("success") is False or not isinstance(result, dict):
            raise ValueError("holder report unavailable or source query rejected")
        count, pages = result.get("count"), result.get("pages")
        if (type(count) is not int or type(pages) is not int
                or not 1 <= count <= 100 or not 1 <= pages <= 4):
            raise ValueError("holder count/page budget invalid or empty")
        if expected is not None and (count, pages) != expected:
            raise ValueError("holder pagination totals changed during collection")
        expected = count, pages
        values = result.get("data")
        if not isinstance(values, list) or not values or len(values) > 50:
            raise ValueError("holder page missing or exceeds requested page size")
        for row in values:
            if not isinstance(row, dict):
                raise ValueError("holder page record malformed")
            records.append(dict(row, 报告期=source_date(row.get("END_DATE")),
                                公告日期=source_date(row.get("NOTICE_DATE"))))
        if page == pages:
            break
    if len(records) != expected[0]:
        raise ValueError("holder rows do not reconcile with source count")
    # Validate response filters and uniqueness before declaring the requested
    # page set complete. Actual observation-day validation follows at ingestion.
    validate(records, {"function": name, "parameters": params}, date.max.isoformat())
    capture.dataset_metadata = {"source": "Eastmoney datacenter", "report": report,
        "security": security, "report_period": period, "source_count": expected[0],
        "pages_received": expected[1], "complete_requested_issuer_period": True,"complete_returned_window":True,
        "complete_universe": False, "complete_history": False,
        "scope": "all source-visible rows for one explicit issuer and reporting period",
        "announcement_field": "NOTICE_DATE", "source_update_field": "UPDATE_DATE",
        "historical_first_publication_versions": False}
    return records
