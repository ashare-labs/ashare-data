"""Full bounded event windows from reviewed Eastmoney datacenter reports.

Keep source identifiers, announcement dates and raw measures. Source rows with
identical trade dimensions retain multiplicity; occurrence is not an exchange ID.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import json
import re
from urllib.parse import urlencode

from ashare_data.providers.free_updates import digest, encoded

ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORTS = {
    "dividends": ("RPT_SHAREBONUS_DET", "REPORT_DATE", "-1"),
    "block_trades": ("RPT_DATA_BLOCKTRADE", "TRADE_DATE,SECURITY_CODE", "-1,1"),
    "billboard": ("RPT_DAILYBILLBOARD_DETAILSNEW", "TRADE_DATE,SECURITY_CODE", "-1,1"),
}
TRADE_KEY = ["SECUCODE", "TRADE_DATE", "BUYER_CODE", "SELLER_CODE", "DEAL_PRICE", "DEAL_VOLUME"]


def preserve_trade_occurrences(rows):
    counts = defaultdict(int)
    result = []
    for row in sorted(rows, key=encoded):
        key = [row.get(k) for k in TRADE_KEY]
        if any(v is None or v == "" for v in key):
            raise ValueError("block-trade natural dimensions missing")
        key[-2:] = [str(Decimal(str(v)).normalize()) for v in key[-2:]]
        group = digest(key)
        counts[group] += 1
        result.append(dict(row, source_trade_group=group, source_occurrence=counts[group]))
    return result


def collect(capture, params):
    dataset = params["dataset"]
    report, order, direction = REPORTS[dataset]
    if dataset == "dividends":
        symbol = params["symbol"]
        if not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("dividend security must be a six-digit code")
        predicate = f'(SECURITY_CODE="{symbol}")'
    else:
        start, end = [datetime.strptime(params[k], "%Y%m%d").date().isoformat() for k in ("start_date", "end_date")]
        if start > end:
            raise ValueError("event date window is reversed")
        predicate = f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"
        if dataset == "block_trades":
            predicate = "(SECURITY_TYPE_WEB=1)" + predicate
    query = {"reportName": report, "columns": "ALL", "sortColumns": order, "sortTypes": direction,
             "pageNumber": 1, "pageSize": 500, "source": "WEB", "client": "WEB", "filter": predicate}
    output, pages, count, page_hashes = [], None, None, set()
    for page in range(1, 41):
        query["pageNumber"] = page
        response = json.loads(capture.get(ENDPOINT+"?"+urlencode(query)))
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValueError("event source response is not a data table")
        if pages is None:
            pages, count = int(result["pages"]), int(result["count"])
            if not 1 <= pages <= 40 or count <= 0:
                raise ValueError("event window empty or exceeds bounded pages; use smaller explicit date shards")
        if pages != int(result["pages"]) or count != int(result["count"]):
            raise ValueError("event window changed during pagination; retry a stable window")
        page_hash=digest(result["data"])
        if page_hash in page_hashes:
            raise ValueError("event pagination repeated the same page")
        page_hashes.add(page_hash)
        output.extend(result["data"])
        if page == pages:
            break
    if len(output) != count:
        raise ValueError("event source count does not match all returned pages")
    if dataset == "block_trades":
        output = preserve_trade_occurrences(output)
    elif dataset == "dividends":
        output = [dict(row, period_role="planned" if row.get("ASSIGN_PROGRESS") == "预披露" else "reported") for row in output]
    capture.dataset_metadata = {"report": report, "pages_received": pages, "source_count": count,
        "complete_requested_window": True, "complete_returned_window": True, "complete_history": dataset == "dividends",
        "scope": "one requested issuer history" if dataset == "dividends" else "all source A-share events in explicit date window",
        "event_id": "source dimension group plus occurrence, not exchange trade ID" if dataset == "block_trades" else "source security, period/date and event reason"}
    return output


def validate(rows, job, as_of, numeric, source_date):
    if job["function"] != "security_events_em":
        return None
    dataset, params = job["parameters"]["dataset"], job["parameters"]
    dates, notices, keys, gaps, quality_issues = [], [], [], [], []
    rounded_amount_rows = 0
    if dataset == "dividends":
        fields = ["SECUCODE", "REPORT_DATE"]
        units = {"PRETAX_BONUS_RMB": "CNY per 10 shares", "BONUS_RATIO": "bonus shares per 10 shares",
            "IT_RATIO": "converted shares per 10 shares", "BONUS_IT_RATIO": "shares per 10 shares",
            "TOTAL_SHARES": "shares", "DIVIDENT_RATIO": "fraction",
            **dict.fromkeys(["BASIC_EPS", "BVPS", "PER_CAPITAL_RESERVE", "PER_UNASSIGN_PROFIT"], "CNY/share"), "PNP_YOY_RATIO": "percent"}
    elif dataset == "block_trades":
        fields = ["source_trade_group", "source_occurrence"]
        units = {"DEAL_PRICE": "CNY/share", "CLOSE_PRICE": "CNY/share", "DEAL_VOLUME": "shares",
                 "DEAL_AMT": "CNY", "PREMIUM_RATIO": "fraction", "CHANGE_RATE": "percent", "TURNOVER_RATE": "percent"}
    else:
        fields = ["SECUCODE", "TRADE_DATE", "EXPLANATION"]
        units = {**dict.fromkeys(["BILLBOARD_NET_AMT", "BILLBOARD_BUY_AMT", "BILLBOARD_SELL_AMT", "BILLBOARD_DEAL_AMT", "ACCUM_AMOUNT", "FREE_MARKET_CAP"], "CNY"),
                 "CLOSE_PRICE": "CNY/share", **dict.fromkeys(["CHANGE_RATE", "DEAL_NET_RATIO", "DEAL_AMOUNT_RATIO", "TURNOVERRATE"], "percent")}
    for row in rows:
        security = row.get("SECUCODE", "")
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", security) or row.get("SECURITY_CODE") != security[:6]:
            raise ValueError("event security code/market identity mismatch")
        if dataset == "dividends":
            if row["SECURITY_CODE"] != params["symbol"]:
                raise ValueError("dividend response belongs to another issuer")
            day = source_date(row.get("REPORT_DATE"))
            notice = source_date(row.get("NOTICE_DATE"))
            planned = row.get("ASSIGN_PROGRESS") == "预披露"
            if row.get("period_role") != ("planned" if planned else "reported"):
                raise ValueError("dividend planned/reporting period distinction missing")
            if notice:
                if notice > as_of or (not planned and day and day > notice):
                    raise ValueError("dividend report period/announcement chronology invalid")
                notices.append(notice)
            else:
                gaps.append("部分分红记录缺公告日期，不能推断历史公告可见时间")
            if not row.get("ASSIGN_PROGRESS"):
                raise ValueError("dividend proposal/implementation status missing")
            # Scheduled future ex-dates are allowed, but never treated as an
            # already effective adjustment or as publication timestamps.
            for field in ["PLAN_NOTICE_DATE", "PUBLISH_DATE", "EQUITY_RECORD_DATE", "EX_DIVIDEND_DATE"]:
                if row.get(field) is not None and source_date(row[field]) is None:
                    raise ValueError("invalid source corporate-action date")
        else:
            day = source_date(row.get("TRADE_DATE"))
            if not day or not source_date(params["start_date"]) <= day <= source_date(params["end_date"]):
                raise ValueError("event trade date outside requested window")
        if not day or (day > as_of and not (dataset == "dividends" and planned)):
            raise ValueError("event actual statistical/trade date is in the future")
        if dataset != "dividends" or not planned:
            dates.append(day)
        for field in units:
            if field not in row:
                raise ValueError("event measure missing: "+field)
            numeric(row[field])
        if dataset == "block_trades":
            price, volume, amount = [numeric(row[k]) for k in ["DEAL_PRICE", "DEAL_VOLUME", "DEAL_AMT"]]
            if any(v is None or v <= 0 for v in [price, volume, amount]):
                raise ValueError("block-trade units or price-times-volume inconsistent")
            # The published page quotes 0.01 万股/万元, and two-decimal
            # prices. These are rounded source statistics, not exact ticks.
            # Check the propagated half-step bound without changing raw values.
            difference = abs(price*volume-amount)
            tolerance = price*Decimal(50)+volume*Decimal("0.005")+Decimal(50)
            if difference > tolerance:
                raise ValueError("block-trade amount exceeds stated source rounding envelope")
            rounded_amount_rows += difference > Decimal("0.02")
        if dataset == "billboard":
            buy, sell, net, total = [numeric(row[k]) for k in ["BILLBOARD_BUY_AMT", "BILLBOARD_SELL_AMT", "BILLBOARD_NET_AMT", "BILLBOARD_DEAL_AMT"]]
            if any(v is None for v in [buy,sell,net,total]):
                raise ValueError("billboard amount missing")
            if abs(buy-sell-net) > Decimal("0.02") or abs(buy+sell-total) > Decimal("0.02"):
                quality_issues.append({"security":security,"date":day,"reason":row["EXPLANATION"],
                    "net_difference":str(buy-sell-net),"total_difference":str(buy+sell-total)})
        if any(row.get(k) in (None, "") for k in fields):
            raise ValueError("event key missing")
        keys.append(digest([row[k] for k in fields]))
    if len(set(keys)) != len(rows):
        raise ValueError("duplicate event identities after explicit source multiplicity")
    return {"contract_reviewed": True, "business_key": fields, "unique_keys": len(keys),
        "units": units, "frequency": "issuer_announcement" if dataset == "dividends" else "exchange_trade_day_event_publication",
        "latest_period": max(dates,default=None), "latest_announcement": max(notices, default=None),
        "gaps": sorted(set(gaps)), "source_rows": len(rows), "quality_issues":quality_issues,
        "rounded_amount_rows":rounded_amount_rows,
        "limitations": ["报告期、交易日、预案/公告/登记/除权日分别保留；实际可见性使用接收时间",
            "龙虎榜金额汇总异常在quality_issues逐条保留；不假定所有上榜原因使用普通买卖前五席位加总，不改写或丢弃源金额",
            "预披露的计划归属期间可晚于公告日，period_role=planned；不当作已发布财务实际值或已实施分红",
            "来源后续收益字段是事后统计，不能在事件当天用于历史回测；交易记录无交易所唯一成交ID，保留原源重复次数",
            "大宗交易源展示万股/万元两位小数及元价格两位小数，按半精度传播界核对量价额，不当作逐笔精确成交"]}
