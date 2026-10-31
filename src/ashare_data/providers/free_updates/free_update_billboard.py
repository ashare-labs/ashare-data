"""Native institution and broker disclosures, retaining source identities.

The provider's BUY/SELL reports are ranked disclosures, not an order book.
Post-event return statistics remain raw and must not be backdated to the event.
"""
from datetime import datetime
from decimal import Decimal
import json
import re
from urllib.parse import urlencode

from ashare_data.providers.free_updates import digest

FUNCTIONS = {"stock_lhb_jgmmtj_em", "stock_lhb_stock_detail_em"}
ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def query(name, params):
    if name == "stock_lhb_jgmmtj_em":
        if set(params) != {"start_date", "end_date"}:
            raise ValueError("institution report requires explicit date bounds")
        start, end = [datetime.strptime(params[k], "%Y%m%d").date() for k in ("start_date", "end_date")]
        if not 0 <= (end-start).days <= 31:
            raise ValueError("institution window exceeds 31 days")
        return {"sortColumns": "NET_BUY_AMT,TRADE_DATE,SECURITY_CODE", "sortTypes": "-1,-1,1",
                "pageSize": "500", "pageNumber": "1", "reportName": "RPT_ORGANIZATION_TRADE_DETAILS",
                "columns": "ALL", "source": "WEB", "client": "WEB",
                "filter": f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"}
    if name != "stock_lhb_stock_detail_em" or set(params) != {"symbol", "date", "flag"}:
        raise ValueError("broker report requires security, date and side")
    if not re.fullmatch(r"\d{6}", str(params["symbol"])) or params["flag"] not in {"买入", "卖出"}:
        raise ValueError("invalid broker security or side")
    day = datetime.strptime(params["date"], "%Y%m%d").date()
    side = "BUY" if params["flag"] == "买入" else "SELL"
    return {"reportName": "RPT_BILLBOARD_DAILYDETAILS"+side, "columns": "ALL",
            "filter": f'(TRADE_DATE=\'{day}\')(SECURITY_CODE="{params["symbol"]}")',
            "pageNumber": "1", "pageSize": "500", "sortTypes": "-1", "sortColumns": side,
            "source": "WEB", "client": "WEB"}


def collect(capture, name, params):
    request = query(name, params)
    rows, hashes, pages, count = [], set(), None, None
    for page in range(1, 41):
        request["pageNumber"] = str(page)
        response = json.loads(capture.get(ENDPOINT+"?"+urlencode(request)))
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValueError("billboard response is not a source data table")
        if pages is None:
            pages, count = int(result["pages"]), int(result["count"])
            if not 1 <= pages <= 40 or count <= 0:
                raise ValueError("billboard source window empty or too large")
        if (pages, count) != (int(result["pages"]), int(result["count"])):
            raise ValueError("billboard pagination changed during collection")
        identity = digest(result["data"])
        if identity in hashes:
            raise ValueError("billboard source repeated a page")
        hashes.add(identity)
        rows.extend(result["data"])
        if page == pages:
            break
    if len(rows) != count:
        raise ValueError("billboard source count differs from received rows")
    if name == "stock_lhb_stock_detail_em":
        # Anonymous institutional seats share OPERATEDEPT_CODE=0.  It is not
        # a seat identity. Preserve every disclosed row and its response order.
        rows = [dict(row, source_side=request["sortColumns"], source_row_number=i)
                for i, row in enumerate(rows, 1)]
    capture.dataset_metadata = {"report": request["reportName"], "pages_received": pages,
        "source_count": count, "complete_returned_window": True, "complete_history": False,
        "scope": "all institution disclosures in requested date window" if name == "stock_lhb_jgmmtj_em"
                 else "all published ranked broker rows for requested security, day and side",
        "native_fields_retained": True, "publication_at": None}
    return rows


def validate(rows, job, as_of, numeric, source_date):
    name, params = job["function"], job["parameters"]
    if name not in FUNCTIONS:
        return None
    query(name, params)
    institution = name == "stock_lhb_jgmmtj_em"
    fields = ["SECUCODE", "TRADE_DATE", "EXPLANATION"]
    if not institution:
        fields += ["source_side", "source_row_number"]
        positions = [row.get("source_row_number") for row in rows]
        if any(type(n) is not int for n in positions) or positions != list(range(1, len(rows)+1)):
            raise ValueError("broker response row positions missing, duplicate or out of order")
    amounts = ["BUY_AMT", "SELL_AMT", "NET_BUY_AMT", "ACCUM_AMOUNT"] if institution else ["BUY", "SELL", "NET", "ACCUM_AMOUNT"]
    units = dict.fromkeys(amounts, "CNY") | {"CLOSE_PRICE": "CNY/share", "CHANGE_RATE": "percent"}
    units |= {"RATIO": "percent", "TURNOVERRATE": "percent", "FREECAP": "source_native_scale_unconfirmed"} if institution else {
        "TOTAL_BUYRIO": "fraction", "TOTAL_SELLRIO": "fraction", "ACCUM_VOLUME": "shares"}
    dates, identities, issues = [], [], []
    for row in rows:
        security = row.get("SECUCODE", "")
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", security) or row.get("SECURITY_CODE") != security[:6]:
            raise ValueError("billboard security identity mismatch")
        day = source_date(row.get("TRADE_DATE"))
        start, end = ((source_date(params["start_date"]), source_date(params["end_date"])) if institution
                      else (source_date(params["date"]), source_date(params["date"])))
        if not day or not start <= day <= end <= as_of:
            raise ValueError("billboard trade date outside requested window")
        if not institution and (security[:6] != params["symbol"] or row.get("source_side") != ("BUY" if params["flag"] == "买入" else "SELL")):
            raise ValueError("broker response security or side differs")
        if any(row.get(k) in (None, "") for k in fields):
            raise ValueError("billboard disclosure identity missing")
        if not institution and row.get("OPERATEDEPT_CODE") in (None, ""):
            raise ValueError("broker native seat code absent")
        for k in units:
            if k not in row:
                raise ValueError("billboard native field absent: "+k)
            numeric(row[k])
        buy, sell, net, total = [numeric(row[k]) for k in amounts]
        required = (buy, sell, net, total) if institution else (
            buy if row["source_side"] == "BUY" else sell, total)
        if any(v is None for v in required) or any(v is not None and v < 0 for v in (buy, sell, total)):
            raise ValueError("billboard amount missing or invalid")
        if any(v is None for v in (buy, sell, net)):
            issues.append({"security": security, "date": day, "reason": row["EXPLANATION"],
                           "source_row_number": row["source_row_number"],
                           "net_identity_checked": False, "missing_fields": [
                               k for k in ("BUY", "SELL", "NET") if numeric(row[k]) is None],
                           "raw_values_retained": True})
        elif abs(buy-sell-net) > Decimal("0.02"):
            issues.append({"security": security, "date": day, "reason": row["EXPLANATION"],
                           "net_difference": str(buy-sell-net), "raw_values_retained": True})
        dates.append(day)
        identities.append(digest([row[k] for k in fields]))
    if len(identities) != len(set(identities)) or not rows:
        raise ValueError("duplicate or empty billboard disclosure identities")
    return {"contract_reviewed": True, "business_key": fields, "unique_keys": len(rows), "source_rows": len(rows),
        "units": units, "frequency": "exchange_trade_day_disclosure", "latest_period": max(dates),
        "latest_announcement": None, "gaps": [], "quality_issues": issues,
        "limitations": ["保留源证券、交易日、上榜原因及席位；source_side为请求榜单方向，不是逐笔成交方向",
            "source_row_number仅为本次分页合并后的响应行位置，不代表跨日稳定席位身份或原因组内名次；匿名机构可共享席位代码0",
            "单侧披露的另一侧空值原样保留，不补零；BUY/SELL不齐时不核算净额，源NET不等于已证实的实际净流入",
            "实际可见时间取采集回执；事后收益/胜率不能回填为上榜当时已知数据",
            "FREECAP保留原值且未确认尺度，不提供该列的市值换算；机构数量各源字段分别保留",
            "金额恒等式异常逐条标注，不改写原金额，也不删除其他有效记录"]}
