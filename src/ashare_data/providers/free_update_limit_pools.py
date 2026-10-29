"""Named Eastmoney limit-pool snapshots, preserving vendor exclusions and dates."""

from datetime import date, time
from decimal import Decimal
import json
import re
from urllib.parse import urlencode

SPECS = {
    "stock_zt_pool_em": ("getTopicZTPool", "limit_up", "fbt:asc", 10000),
    "stock_zt_pool_dtgc_em": ("getTopicDTPool", "limit_down", "fund:asc", 10000),
    "stock_zt_pool_zbgc_em": ("getTopicZBPool", "broken_limit_up", "fbt:asc", 5000),
}
COMMON = {
    "c": "代码",
    "n": "名称",
    "p": "最新价",
    "zdp": "涨跌幅",
    "amount": "成交额",
    "ltsz": "流通市值",
    "tshare": "总市值",
    "hs": "换手率",
    "hybk": "所属行业",
}
EXTRA = {
    "limit_up": {
        "lbc": "连板数",
        "fbt": "首次封板时间",
        "lbt": "最后封板时间",
        "fund": "封板资金",
        "zbc": "炸板次数",
    },
    "limit_down": {
        "pe": "动态市盈率",
        "fund": "封单资金",
        "lbt": "最后封板时间",
        "fba": "板上成交额",
        "days": "连续跌停",
        "oc": "开板次数",
    },
    "broken_limit_up": {
        "ztp": "涨停价",
        "fbt": "首次封板时间",
        "zbc": "炸板次数",
        "zf": "振幅",
        "zs": "涨速",
    },
}
LIMITATIONS = [
    "来源专题排除ST股票，涨停池排除未中断连续一字涨停的新股；不是全A股无遗漏名单",
    "实测历史date请求收到不同qdate；qdate交易日语义未证实，不将请求日或接收日作为行情交易日",
    "每次重读保留源修订；涨停统计显式区分统计天数days与涨停次数ct，不沿用SDK含糊斜杠顺序",
    "未提供涨停规则的逐证券历史状态；仅验证来源池字段，不以统一10%或20%阈值重新判断涨停",
    "来源m=0同时用于深市和920北交所代码，保留原生m并按已核对的920号段识别；旧43/83/87代码未映射",
]


def security(code, market):
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        raise ValueError("limit pool security code malformed")
    suffix = (
        ".SH"
        if code.startswith("6")
        else ".SZ"
        if code.startswith(("00", "30"))
        else ".BJ"
        if code.startswith("920")
        else None
    )
    expected = 1 if suffix == ".SH" else 0
    if suffix is None or type(market) is not int or market != expected:
        raise ValueError("limit pool unreviewed market/security identity")
    return code + suffix


def clock(value):
    if type(value) is not int or not 0 <= value <= 235959:
        raise ValueError("limit pool clock must be native HHMMSS integer")
    text = str(value).zfill(6)
    try:
        time(int(text[:2]), int(text[2:4]), int(text[4:]))
    except ValueError as exc:
        raise ValueError("limit pool clock invalid") from exc
    if not ("091500" <= text <= "113000" or "130000" <= text <= "150000"):
        raise ValueError("limit pool clock outside reviewed Shanghai session")
    return text


def project(native, day, kind, reported_qdate=None):
    fields = COMMON | EXTRA[kind]
    if not set(fields) <= set(native) or "m" not in native:
        raise ValueError("limit pool native required fields changed")
    row = {label: native[key] for key, label in fields.items()}
    for key in ["p", "ztp"]:
        if key in fields:
            row[fields[key]] = str(Decimal(str(native[key])) / 1000)
    for key in ["fbt", "lbt"]:
        if key in fields:
            row[fields[key]] = clock(native[key])
    if kind != "limit_down":
        stats = native.get("zttj")
        if not isinstance(stats, dict) or set(stats) != {"days", "ct"}:
            raise ValueError("limit pool statistic schema changed")
        row.update(统计天数=stats["days"], 涨停次数=stats["ct"])
    row.update(
        请求日期=day,
        source_reported_qdate=reported_qdate,
        source_effective_trading_date=None,
        source_pool=kind,
        security=security(native["c"], native["m"]),
        source_native=native,
    )
    return row


def collect(capture, name, params):
    if name not in SPECS or set(params) != {"date"} or not re.fullmatch(r"\d{8}", params["date"]):
        raise ValueError("limit pool requires one explicit source request date")
    day = date.fromisoformat(params["date"]).isoformat()
    endpoint, kind, sort, limit = SPECS[name]
    query = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": limit,
        "sort": sort,
        "date": params["date"],
    }
    body = json.loads(
        capture.get("https://push2ex.eastmoney.com/" + endpoint + "?" + urlencode(query))
    )
    data = body.get("data")
    if (
        body.get("rc") != 0
        or not isinstance(data, dict)
        or type(data.get("tc")) is not int
        or not isinstance(data.get("pool"), list)
    ):
        raise ValueError("limit pool rejected, unavailable or changed schema")
    if (
        not 0 < data["tc"] < limit
        or data["tc"] != len(data["pool"])
    ):
        raise ValueError("limit pool count/page bound mismatch; empty is not data acceptance")
    reported = str(data.get("qdate"))
    if not re.fullmatch(r"\d{8}", reported):
        raise ValueError("limit pool qdate malformed")
    date.fromisoformat(reported)
    rows = [project(r, day, kind, reported) for r in data["pool"]]
    if len({r["security"] for r in rows}) != len(rows):
        raise ValueError("limit pool duplicate security")
    capture.dataset_metadata = {
        "source_count": len(rows),
        "pages_received": 1,
        "complete_returned_window": True,
        "complete_history": False,
        "scope": "complete source-visible pool observed at receipt; request date and source qdate retained independently, neither asserted as effective trading date",
        "trading_date": None,
        "requested_date": day,
        "reported_qdate": str(data["qdate"]),
        "requested_qdate_matches": reported == params["date"],
        "trading_date_verified": False,
        "source_pool": kind,
        "history_limit": "provider available dated snapshots only; no assumed lookback guarantee",
        "limitations": LIMITATIONS,
        "field_contract_source": "https://quote.eastmoney.com/ztb/newstatic/build/detail.js",
    }
    return rows


def validate(rows, job, as_of, numeric):
    if job["function"] not in SPECS:
        return None
    kind = SPECS[job["function"]][1]
    day = date.fromisoformat(job["parameters"]["date"]).isoformat()
    keys = []
    cap_discrepancies = []
    if day > as_of:
        raise ValueError("limit pool trading date is future")
    for row in rows:
        reported = row.get("source_reported_qdate")
        if not isinstance(reported, str) or not re.fullmatch(r"\d{8}", reported) or date.fromisoformat(reported).isoformat() > as_of:
            raise ValueError("limit pool source qdate malformed or future")
        if row != project(row["source_native"], day, kind, reported):
            raise ValueError("limit pool native projection/identity changed")
        if not row["名称"]:
            raise ValueError("limit pool missing name")
        for field in [
            "最新价",
            "成交额",
            "流通市值",
            "总市值",
            "换手率",
            "封板资金",
            "封单资金",
            "板上成交额",
            "涨停价",
        ]:
            if field not in row:
                continue
            value = numeric(row[field])
            if value is None or value < 0 or field in ["最新价", "涨停价"] and value <= 0:
                raise ValueError("limit pool price/amount missing or negative")
        if numeric(row["流通市值"]) > numeric(row["总市值"]):
            cap_discrepancies.append(
                {
                    "security": row["security"],
                    "float_cap_cny": row["流通市值"],
                    "total_cap_cny": row["总市值"],
                    "difference_cny": str(numeric(row["流通市值"]) - numeric(row["总市值"])),
                }
            )
        change = numeric(row["涨跌幅"])
        if (
            change is None
            or kind == "limit_up"
            and change <= 0
            or kind == "limit_down"
            and change >= 0
        ):
            raise ValueError("limit pool price-change sign conflicts with pool")
        for field in ["连板数", "炸板次数", "开板次数", "连续跌停", "统计天数", "涨停次数"]:
            if field in row and (type(row[field]) is not int or row[field] < 0):
                raise ValueError("limit pool count is not native nonnegative integer")
        minimum = 1 if kind == "limit_up" else 0
        if kind != "limit_down" and not minimum <= row["涨停次数"] <= row["统计天数"]:
            raise ValueError("limit pool ct/days inconsistent")
        if kind == "limit_up" and (row["连板数"] < 1 or row["首次封板时间"] > row["最后封板时间"]):
            raise ValueError("limit pool streak/clock inconsistent")
        if kind == "broken_limit_up" and numeric(row["最新价"]) > numeric(row["涨停价"]):
            raise ValueError("broken limit pool price above ceiling")
        keys.append((day, kind, row["security"]))
    if not rows or len(keys) != len(set(keys)):
        raise ValueError("empty or duplicate limit pool")
    units = {
        "最新价": "CNY/share; native p divided by 1000",
        "涨停价": "CNY/share; native ztp divided by 1000",
        "成交额": "CNY",
        "流通市值": "CNY",
        "总市值": "CNY",
        "封板资金": "CNY",
        "封单资金": "CNY",
        "板上成交额": "CNY",
        "涨跌幅": "percent",
        "换手率": "percent",
        "振幅": "percent",
        "涨速": "source intraday percent rate",
        "动态市盈率": "multiple",
        "连板数": "trading days",
        "统计天数": "source window days",
        "涨停次数": "count",
        "首次封板时间": "HHMMSS Asia/Shanghai",
        "最后封板时间": "HHMMSS Asia/Shanghai",
    }
    return {
        "contract_reviewed": True,
        "gaps": [],
        "market_cap_discrepancies": cap_discrepancies,
        "business_key": ["_snapshot_date", "source_pool", "security"],
        "business_key_limitation": "observed snapshot only; source qdate and requested date are not effective trading dates",
        "unique_keys": len(keys),
        "source_rows": len(rows),
        "units": {k: v for k, v in units.items() if any(k in r for r in rows)},
        "frequency": "current observed source snapshots and revisions",
        "latest_period": None,
        "latest_announcement": None,
        "source_exclusions": LIMITATIONS[0],
        "limitations": LIMITATIONS,
    }
