"""Native Eastmoney A-share valuations: identity, units and count reconciliation.

Retain every returned field, including security identity discarded by the SDK.
Valuation ratios are provider definitions, not recomputed accounting measures.
"""
import json
import re
from urllib.parse import urlencode

from ashare_data.providers.free_updates import digest, source_date

UNITS = {
    "CLOSE_PRICE": "CNY/share", "CHANGE_RATE": "percent",
    "TOTAL_MARKET_CAP": "CNY; provider total market capitalization",
    "NOTLIMITED_MARKETCAP_A": "CNY; unrestricted A shares",
    "TOTAL_SHARES": "shares; provider total issued shares", "FREE_SHARES_A": "shares; unrestricted A shares",
    **dict.fromkeys(("PE_TTM", "PE_LAR", "PB_MRQ", "PEG_CAR", "PCF_OCF_LAR", "PCF_OCF_TTM", "PS_TTM"), "source valuation ratio"),
}


def collect(capture, params):
    if set(params) != {"symbol"} or not re.fullmatch(r"\d{6}", params["symbol"]):
        raise ValueError("valuation requires an explicit six-digit security code")
    query = dict(sortColumns="TRADE_DATE", sortTypes="-1", pageSize="1000", pageNumber="1",
                 reportName="RPT_VALUEANALYSIS_DET", columns="ALL", quoteColumns="", source="WEB", client="WEB",
                 filter=f'(SECURITY_CODE="{params["symbol"]}")')
    rows, hashes, declared = [], set(), None
    for page in range(1, 41):
        query["pageNumber"] = str(page)
        data = json.loads(capture.get("https://datacenter-web.eastmoney.com/api/data/v1/get?"+urlencode(query)))
        result = data.get("result")
        if data.get("success") is False or not isinstance(result, dict) or not result.get("data"):
            raise ValueError("valuation response rejected or empty")
        current = (int(result["pages"]), int(result["count"]))
        if declared is None:
            declared = current
        if current != declared or not 1 <= current[0] <= 40 or not 1 <= current[1] <= 40000:
            raise ValueError("valuation page count changed or exceeds budget")
        if digest(result["data"]) in hashes:
            raise ValueError("valuation repeated page")
        hashes.add(digest(result["data"]))
        rows.extend(result["data"])
        if page == current[0]:
            break
    if len(rows) != declared[1]:
        raise ValueError("valuation native count mismatch")
    capture.dataset_metadata = {"report": "RPT_VALUEANALYSIS_DET", "symbol": params["symbol"],
        "source_count": declared[1], "pages_received": declared[0], "complete_requested_security_history": True,
        "complete_universe": False, "complete_history": False,
        "scope": "all currently source-visible dates for one explicit security, not the entire A-share market",
        "units": UNITS, "time_basis": "TRADE_DATE Asia/Shanghai; source valuation revisions observed at capture time"}
    return rows


def validate(rows, job, as_of, numeric):
    if job["function"] != "stock_value_em":
        return None
    dates, keys = [], []
    missing = dict.fromkeys(UNITS, 0)
    for row in rows:
        code = row.get("SECUCODE", "")
        if not re.fullmatch(re.escape(job["parameters"]["symbol"])+r"\.(?:SH|SZ)", code) or row.get("SECURITY_CODE") != code[:6] or not row.get("SECURITY_NAME_ABBR"):
            raise ValueError("valuation native security identity mismatch")
        day = source_date(row.get("TRADE_DATE"))
        if not day or day > as_of:
            raise ValueError("valuation trading date invalid")
        dates.append(day)
        keys.append((code, day))
        for field in UNITS:
            if field not in row:
                raise ValueError("valuation native measure absent: "+field)
            value = numeric(row[field])
            if value is None:
                missing[field] += 1
            elif field in {"CLOSE_PRICE", "TOTAL_MARKET_CAP", "NOTLIMITED_MARKETCAP_A", "TOTAL_SHARES", "FREE_SHARES_A"} and value < 0:
                raise ValueError("negative valuation price/capital/shares")
            if value is not None and field in {"TOTAL_SHARES", "FREE_SHARES_A"} and value != value.to_integral_value():
                raise ValueError("valuation shares not in native share units")
        price, shares, cap = (numeric(row[k]) for k in ("CLOSE_PRICE", "FREE_SHARES_A", "NOTLIMITED_MARKETCAP_A"))
        if all(v is not None for v in (price, shares, cap)) and abs(price*shares-cap) > abs(cap)*numeric("0.0001")+1:
            raise ValueError("valuation unrestricted A-share cap/price/share units do not reconcile")
    if not rows or len(keys) != len(set(keys)):
        raise ValueError("valuation duplicate or empty security/date keys")
    latest = rows[dates.index(max(dates))]
    return {"contract_reviewed": True, "business_key": ["SECUCODE", "TRADE_DATE"], "unique_keys": len(keys),
            "source_rows": len(rows), "units": UNITS, "frequency": "trading_day_close_and_provider_revisions",
            "latest_period": max(dates), "latest_announcement": None, "missing_field_observations": missing,
            "gaps": [k+" latest missing" for k in ("CLOSE_PRICE", "TOTAL_SHARES", "FREE_SHARES_A", "TOTAL_MARKET_CAP", "NOTLIMITED_MARKETCAP_A") if latest[k] is None],
            "limitations": ["估值比率保留源TTM/静态定义及负值，不冒充当时可见财务数据；修订留接收时间",
                "总市值口径可能含其他股类；只对流通A股价格×股数与市值作单位恒等核对",
                "已验显式证券全部来源可得日期；全市场目录、退市覆盖及日常启用另行验收"]}
