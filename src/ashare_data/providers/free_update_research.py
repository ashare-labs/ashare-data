"""Native forecast snapshots preserve each company's actual/estimated years.

The public Eastmoney page labels the first EPS as earnings per share, not a
forecast; YEAR_MARK A/E is retained per record, never inferred from modal years.
The source list includes securities with no currently available forecast EPS;
counts of listed entries and usable forecasts are separate.
"""
import json
import math
import re
from urllib.parse import urlencode

from ashare_data.providers.free_updates import digest
from ashare_data.providers.free_update_dates import require_dates

COUNTS = ("RATING_ORG_NUM", "RATING_BUY_NUM", "RATING_ADD_NUM", "RATING_NEUTRAL_NUM", "RATING_REDUCE_NUM", "RATING_SALE_NUM")
UNITS = {**dict.fromkeys(COUNTS, "source report/rating count; missing is not zero"),
         **{f"EPS{i}": "CNY/share; native YEAR_MARK A=actual, E=estimated" for i in range(1, 5)},
         "DEC_AIMPRICEMAX": "CNY/share; source target price", "DEC_AIMPRICEMIN": "CNY/share; source target price"}


def collect(capture, params):
    if params:
        raise ValueError("forecast menu requires the complete unfiltered current list")
    query = dict(reportName="RPT_WEB_RESPREDICT", columns="WEB_RESPREDICT", pageNumber="1", pageSize="500",
                 sortTypes="1", sortColumns="SECUCODE", p="1", pageNo="1", pageNum="1")
    # Rating counts tie across many companies and source pages can reshuffle
    # those ties. Request a stable native security key; never dedupe an incomplete
    # page set and label it complete.
    rows, hashes, declared = [], set(), None
    for page in range(1, 41):
        query.update(dict.fromkeys(("pageNumber", "p", "pageNo", "pageNum"), str(page)))
        response = json.loads(capture.get("https://datacenter-web.eastmoney.com/api/data/v1/get?"+urlencode(query)))
        result = response.get("result")
        if response.get("success") is False or not isinstance(result, dict) or not result.get("data"):
            raise ValueError("forecast source response rejected or empty")
        current = (int(result["pages"]), int(result["count"]))
        declared = current if declared is None else declared
        if current != declared or not 1 <= current[0] <= 40 or current[0] != math.ceil(current[1]/500):
            raise ValueError("forecast native page/count changed or exceeds request budget")
        if digest(result["data"]) in hashes:
            raise ValueError("forecast source repeated a page")
        hashes.add(digest(result["data"]))
        rows.extend(result["data"])
        if page == declared[0]:
            break
    if len(rows) != declared[1] or len({r["SECUCODE"] for r in rows}) != len(rows):
        raise ValueError("forecast full-list count or security uniqueness failed")
    capture.dataset_metadata = {"report": "RPT_WEB_RESPREDICT", "source_count": declared[1], "pages_received": declared[0],
        "complete_returned_window": True, "complete_history": False, "complete_universe": True,
        "universe_definition": "all entries in the source's current research list, including entries with no estimated EPS",
        "scope": "current research source list, including SH/SZ/BJ; not every listed company, not all entries have forecast EPS, not past forecasts",
        "time_basis": "actual reception time; each YEARn is an individual fiscal year with its native A/E role",
        "source_page": "https://data.eastmoney.com/report/profitforecast.jshtml", "units": UNITS}
    return rows


def validate(rows, job, as_of, numeric):
    if job["function"] != "stock_profit_forecast_em":
        return None
    require_dates(rows, job, as_of)
    keys, marks, exchanges, missing = [], {}, {}, dict.fromkeys(UNITS, 0)
    forecast_available = 0
    for row in rows:
        code = row.get("SECUCODE", "")
        if not re.fullmatch(r"(?:(?:60|68)\d{4}\.SH|(?:00|30)\d{4}\.SZ|(?:43|83|87|92)\d{4}\.BJ)", code) or row.get("SECURITY_CODE") != code[:6] or not row.get("SECURITY_NAME_ABBR"):
            raise ValueError("forecast RMB A-share native security identity invalid")
        keys.append(code)
        exchanges[code[-2:]] = exchanges.get(code[-2:], 0)+1
        years = []
        for i in range(1, 5):
            if any(k not in row for k in (f"YEAR{i}", f"YEAR_MARK{i}", f"EPS{i}")):
                raise ValueError("forecast fiscal-year/EPS native field missing")
            years.append(row[f"YEAR{i}"])
            mark = str(row[f"YEAR{i}"])+row[f"YEAR_MARK{i}"]
            marks[mark] = marks.get(mark, 0)+1
        if years != sorted(set(years)):
            raise ValueError("forecast fiscal years repeated or reordered")
        for field in UNITS:
            if field not in row:
                raise ValueError("forecast measure absent: "+field)
            value = numeric(row[field])
            if value is None:
                missing[field] += 1
            elif field in COUNTS and (value < 0 or value != value.to_integral_value()):
                raise ValueError("forecast report/rating count is not a nonnegative integer")
        low, high = numeric(row["DEC_AIMPRICEMIN"]), numeric(row["DEC_AIMPRICEMAX"])
        if any(v is not None and v <= 0 for v in (low, high)) or low is not None and high is not None and low > high:
            raise ValueError("forecast target price range invalid")
        forecast_available += any(row[f"YEAR_MARK{i}"] == "E" and numeric(row[f"EPS{i}"]) is not None for i in range(1, 5))
    if not rows or len(keys) != len(set(keys)):
        raise ValueError("forecast empty or duplicate securities")
    return {"contract_reviewed": True, "business_key": ["SECUCODE", "observation_snapshot_date"], "unique_keys": len(keys),
        "source_rows": len(rows), "units": UNITS, "frequency": "current_research_consensus_snapshot",
        "latest_period": None, "latest_announcement": None, "gaps": [], "exchanges": exchanges,
        "native_fiscal_year_roles": marks, "missing_field_observations": missing,
        "forecast_eps_available_securities": forecast_available,
        "forecast_eps_unavailable_securities": len(rows)-forecast_available,
        "limitations": ["预测值和实际值按原生A/E标记逐家公司区分，不用全表年份众数改写列名",
            "各字段null保留；评级分类数量不强制等于报告数量，不推断未分类或重复评级",
            "源未给每份研报发布时间，不能把接收快照当历史当时可见预测；全列表仅为源当前覆盖公司",
            "附加板块及源内部字段原样保存，未解释其内部编码"]}
