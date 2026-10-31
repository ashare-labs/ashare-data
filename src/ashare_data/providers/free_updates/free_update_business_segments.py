"""Issuer-bound business segments: native currency, ratios and report periods."""

from collections import Counter
from decimal import Decimal
import json
import re
from urllib.parse import urlencode

from ashare_data.providers.free_updates.free_update_dates import reviewed_date

FUNCTION = "stock_zygc_em"
FIELDS = {
    "SECURITY_CODE": "股票代码",
    "REPORT_DATE": "报告日期",
    "ITEM_NAME": "主营构成",
    "MAIN_BUSINESS_INCOME": "主营收入",
    "MBI_RATIO": "收入比例",
    "MAIN_BUSINESS_COST": "主营成本",
    "MBC_RATIO": "成本比例",
    "MAIN_BUSINESS_RPOFIT": "主营利润",
    "MBR_RATIO": "利润比例",
    "GROSS_RPOFIT_RATIO": "毛利率",
}
TYPES = {"1": "按行业分类", "2": "按产品分类", "3": "按地区分类"}
UNIT_SOURCE = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/Index"


def identity(params):
    if set(params) != {"symbol"} or not re.fullmatch(
        r"(?:SH6\d{5}|SZ(?:00|30)\d{4})", params["symbol"]
    ):
        raise ValueError("business segments require explicit reviewed A-share market/code")
    return params["symbol"][2:] + "." + params["symbol"][:2]


def project(native, code):
    if (
        not set(FIELDS) <= set(native)
        or native.get("SECUCODE") != code
        or native["SECURITY_CODE"] != code[:6]
        or native.get("MAINOP_TYPE") not in TYPES
    ):
        raise ValueError("business segment native fields, issuer or classification changed")
    return {
        **{v: native[k] for k, v in FIELDS.items()},
        "分类类型": TYPES[native["MAINOP_TYPE"]],
        "security": code,
        "source_native": native,
        "publication_at": None,
        "visibility_basis": "first actual receipt; report period is not announcement availability",
    }


def collect(capture, params):
    code = identity(params)
    body = json.loads(
        capture.get(
            "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax?"
            + urlencode({"code": params["symbol"]})
        )
    )
    native = body.get("zygcfx")
    if not isinstance(native, list) or not 0 < len(native) <= 20000:
        raise ValueError("business segment response empty/oversize")
    rows = [project(r, code) for r in native]
    capture.dataset_metadata = {
        "source_count": len(rows),
        "pages_received": 1,
        "complete_returned_window": True,
        "complete_history": False,
        "scope": "all business segments in current selected issuer PageAjax response, not all issuer history or all securities",
        "source_report_periods": sorted({r["报告日期"] for r in rows}),
        "unit_source": UNIT_SOURCE,
        "source_publication_time": None,
        "limitations": [
            "分类口径分开，不把产品/行业/地区行重复加总",
            "各报告期允许源未披露成本或毛利字段为空；不推算填充",
            "公告时刻未提供，按首次实际取得时点可见，不支持当时已知回测假设",
        ],
    }
    return rows


def validate(rows, job, as_of, numeric):
    if job["function"] != FUNCTION:
        return None
    code = identity(job["parameters"])
    keys = []
    days = []
    missing = Counter()
    gaps = []
    for row in rows:
        if row != project(row["source_native"], code):
            raise ValueError("business segment native projection differs")
        day = reviewed_date(row["报告日期"])
        if not day or day > as_of or not row["主营构成"]:
            raise ValueError("business segment period/name missing or future")
        values = {
            k: numeric(row[k])
            for k in [
                "主营收入",
                "主营成本",
                "主营利润",
                "收入比例",
                "成本比例",
                "利润比例",
                "毛利率",
            ]
        }
        for k, v in values.items():
            if v is None:
                missing[k] += 1
        income, cost, profit = [values[k] for k in ["主营收入", "主营成本", "主营利润"]]
        if income is None:
            gaps.append("存在主营收入未披露的构成行，保留空值")
        if all(v is not None for v in (income, cost, profit)):
            # Native rounding differs across reports; test only supported precision.
            tolerance = max(
                Decimal("0.02"), sum(abs(v) for v in [income, cost, profit]) * Decimal("0.000001")
            )
            if abs(income - cost - profit) > tolerance:
                gaps.append("源收入-成本与主营利润存在不一致，不猜测行业公式")
        margin = values["毛利率"]
        if (
            margin is not None
            and income not in (None, 0)
            and profit is not None
            and abs(profit / income - margin) > Decimal("0.000001")
        ):
            gaps.append("源毛利率与原始金额关系不一致；不重算覆盖")
        keys.append((code, day, row["分类类型"], row["主营构成"]))
        days.append(day)
    if not rows or len(keys) != len(set(keys)):
        raise ValueError("business segment empty or duplicate period/classification/item")
    return {
        "contract_reviewed": True,
        "gaps": sorted(set(gaps)),
        "business_key": ["security", "报告日期", "分类类型", "主营构成"],
        "unique_keys": len(keys),
        "source_rows": len(rows),
        "latest_period": max(days),
        "latest_announcement": None,
        "units": {
            **dict.fromkeys(["主营收入", "主营成本", "主营利润"], "CNY"),
            **dict.fromkeys(
                ["收入比例", "成本比例", "利润比例", "毛利率"],
                "fraction; source UI multiplies by 100 for percent",
            ),
        },
        "field_missing_counts": dict(missing),
        "frequency": "issuer reporting periods and source revisions",
        "limitations": [
            "只核原生已披露字段，不把银行或保险未披露字段按一般企业猜补",
            "负数及抵消项原样保留；跨分类不加总，分类内也不强制凑100%",
            "按实际接收时间留存；原始公告时间缺失，不以报告期代替可见时点",
            "当前响应可见期，不保证发行人全历史或全市场覆盖",
        ],
    }
