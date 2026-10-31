"""Native Eastmoney oil adjustment records: tonnes and litres stay distinct.

Field/units were reviewed in the public oil_default HTML and its linked script.
SDK positional column assignment mislabels V95/V92 when native order changes;
this adapter retains native field names and validates the prior-price identity.
"""
import json
import math
from urllib.parse import urlencode

from ashare_data.providers.free_updates import digest, source_date

HISTORY_UNITS = dict.fromkeys(("VALUE", "CY_JG", "QY_FD", "CY_FD"), "CNY/tonne")
REGION_UNITS = {prefix+grade: "CNY/litre" for prefix in ("V", "ZDE", "QE") for grade in ("0", "89", "92", "95")}


def report(capture, kind, *, day=None):
    query = dict(reportName=kind, columns="ALL", sortColumns="cityname" if day else "dim_date",
                 sortTypes="1" if day else "-1", pageNumber="1", pageSize="1000", source="WEB")
    if day:
        query["filter"] = f"(dim_date='{day}')"
    rows, hashes, declared = [], set(), None
    for page in range(1, 21):
        query["pageNumber"] = str(page)
        response = json.loads(capture.get("https://datacenter-web.eastmoney.com/api/data/v1/get?"+urlencode(query)))
        result = response.get("result")
        if response.get("success") is False or not isinstance(result, dict) or not result.get("data"):
            raise ValueError("oil response rejected or empty")
        current = (int(result["pages"]), int(result["count"]))
        declared = current if declared is None else declared
        if current != declared or not 1 <= current[0] <= 20 or current[0] != math.ceil(current[1]/1000):
            raise ValueError("oil page/count changed or exceeds budget")
        if digest(result["data"]) in hashes:
            raise ValueError("oil source repeated page")
        hashes.add(digest(result["data"]))
        rows.extend(result["data"])
        if page == declared[0]:
            break
    if len(rows) != declared[1]:
        raise ValueError("oil source count not reconciled")
    return rows, declared[0]


def collect(capture, name, params):
    if params:
        raise ValueError("oil menu requests source history or current latest regional table")
    history, pages = report(capture, "RPTA_WEB_YJ_BD")
    dates = [source_date(r.get("DIM_DATE")) for r in history]
    if not all(dates) or len(set(dates)) != len(dates):
        raise ValueError("oil adjustment calendar invalid/duplicate")
    latest = max(dates)
    rows = history
    region_pages = 0
    if name == "energy_oil_detail_latest":
        rows, region_pages = report(capture, "RPTA_WEB_YJ_JH", day=latest)
        if any(source_date(r.get("DIM_DATE")) != latest for r in rows):
            raise ValueError("regional oil date differs from latest source adjustment")
    # Add a labelled native date for common freshness assessment; do not alter
    # native DIM_DATE or any value/order/null in the response.
    rows = [r | {"调整日期": source_date(r["DIM_DATE"])} for r in rows]
    capture.dataset_metadata = {"source": "Eastmoney oil_default public report", "source_count": len(rows),
        "pages_received": pages+region_pages, "latest_source_adjustment": latest,
        "complete_returned_window": True, "complete_history": name == "energy_oil_hist",
        "complete_universe": name == "energy_oil_detail_latest", "calendar_records": len(history),
        "scope": "all currently source-visible adjustment dates" if name == "energy_oil_hist" else "all regions at the source's latest available adjustment date; not regional history",
        "source_page": "https://data.eastmoney.com/cjsj/oil_default.html",
        "units": HISTORY_UNITS if name == "energy_oil_hist" else REGION_UNITS,
        "availability": "latest source date does not establish that every official pricing window is present"}
    return rows


def validate(rows, job, as_of, numeric):
    name = job["function"]
    if name not in {"energy_oil_hist", "energy_oil_detail_latest"}:
        return None
    region = name == "energy_oil_detail_latest"
    units = REGION_UNITS if region else HISTORY_UNITS
    dates, keys, missing, relations = [], [], dict.fromkeys(units, 0), 0
    for row in rows:
        day = source_date(row.get("DIM_DATE"))
        if not day or not "1990-01-01" <= day <= as_of or row.get("调整日期") != day:
            raise ValueError("oil adjustment date invalid/future")
        dates.append(day)
        if region and (not row.get("DIM_ID") or not row.get("CITYNAME")):
            raise ValueError("regional oil native identity missing")
        keys.append((row["DIM_ID"], day) if region else day)
        for field in units:
            if field not in row:
                raise ValueError("oil native field missing: "+field)
            value = numeric(row[field])
            if value is None:
                missing[field] += 1
            elif (region and field.startswith(("V", "QE")) or field in {"VALUE", "CY_JG"}) and value <= 0:
                raise ValueError("nonpositive oil price")
        required_prices = ("V0", "V92", "V95") if region else ("VALUE", "CY_JG")
        if any(numeric(row[k]) is None for k in required_prices):
            raise ValueError("oil required current prices missing; a date-only row is not valid price data")
        if region:
            for grade in ("0", "89", "92", "95"):
                price, old, delta = [numeric(row[k+grade]) for k in ("V", "QE", "ZDE")]
                if all(v is not None for v in (price, old, delta)):
                    if abs(price-old-delta) > numeric("0.011"):
                        raise ValueError("regional price/prior/change native fields do not reconcile")
                    relations += 1
    if not rows or len(keys) != len(set(keys)) or region and len(set(dates)) != 1:
        raise ValueError("oil identity/date duplicates or mixed latest regional dates")
    return {"contract_reviewed": True, "business_key": ["DIM_ID", "DIM_DATE"] if region else ["DIM_DATE"],
        "unique_keys": len(keys), "source_rows": len(rows), "units": units, "frequency": "official_oil_adjustment_events",
        "latest_period": max(dates), "latest_announcement": None, "gaps": [],
        "missing_field_observations": missing, "regional_price_change_checks": relations,
        "limitations": ["历史调价价格及涨跌为元/吨；地区各牌号价格、前价及涨跌为元/升；按原字段名映射，不按列位置",
            "各地区未供应牌号的null不填充；原调整日不冒充公告可见时刻",
            "完整覆盖仅指来源当前返回范围；每日启用前需继续核对与官方最新调价窗口的时效差"]}
