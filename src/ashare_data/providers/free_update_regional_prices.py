"""Regional series with explicit source units, date roles and native missingness."""
import json
import re
from math import ceil
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ashare_data.providers.free_update_dates import reviewed_date

HB_URL = "https://www.hbets.cn/"
HOUSE = "macro_china_new_house_price"
HOUSE_FIELDS = {"FIRST_COMHOUSE_SAME": "新建商品住宅价格指数-同比", "FIRST_COMHOUSE_SEQUENTIAL": "新建商品住宅价格指数-环比",
                "FIRST_COMHOUSE_BASE": "新建商品住宅价格指数-定基", "SECOND_HOUSE_SAME": "二手住宅价格指数-同比",
                "SECOND_HOUSE_SEQUENTIAL": "二手住宅价格指数-环比", "SECOND_HOUSE_BASE": "二手住宅价格指数-定基"}
HOUSE_METHODOLOGY = ["https://www.stats.gov.cn/sj/zxfb/202303/t20230301_1919242.html",
    "https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899039.html",
    "https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1900997.html",
    "https://www.stats.gov.cn/sj/zxfb/202610/t20261019_1965609.html"]


def chart_number(value):
    """Remove only source-proved thousands separators, retaining the raw field."""
    if isinstance(value, str) and "," in value:
        if not re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", value):
            raise ValueError("Hubei chart malformed grouped number")
        return value.replace(",", "")
    return value


def base_year(native, day):
    if all(native[k] is None for k in ("FIRST_COMHOUSE_BASE", "SECOND_HOUSE_BASE")):
        return None
    if not "2011-01-01" <= day <= "2026-12-31":
        raise ValueError("new housing fixed-base period needs methodology review")
    return ((int(day[:4])-1)//5)*5


def cities(params):
    if set(params) != {"city_first", "city_second"}:
        raise ValueError("housing index needs explicit city selection")
    values = sorted(set(params.values()))
    if values == ["全部"]:
        return None
    if any(not re.fullmatch(r"[\u4e00-\u9fff]{2,10}", x) or x == "全部" for x in values):
        raise ValueError("invalid/mixed housing city scope")
    return values


def collect_housing(capture, params):
    selected = cities(params)
    query = {"reportName": "RPT_ECONOMY_HOUSE_PRICE", "columns": "REPORT_DATE,CITY,"+",".join(HOUSE_FIELDS)+",REPORT_DAY",
             "pageNumber": 1, "pageSize": 500, "sortColumns": "REPORT_DATE,CITY", "sortTypes": "1,1", "source": "WEB", "client": "WEB"}
    if selected:
        query["filter"] = '(CITY in ('+','.join('"'+v+'"' for v in selected)+'))'
    rows, declared = [], None
    for page in range(1, 41):
        query["pageNumber"] = page
        data = json.loads(capture.get("https://datacenter-web.eastmoney.com/api/data/v1/get?"+urlencode(query)))
        block = data.get("result")
        if data.get("success") is False or not isinstance(block, dict) or not isinstance(block.get("data"), list):
            raise ValueError("housing source rejected or schema changed")
        counts = int(block["pages"]), int(block["count"])
        if not 1 <= counts[0] <= 40 or counts[0] != ceil(counts[1]/500) or declared is not None and declared != counts:
            raise ValueError("housing count/page limit or concurrent source revision")
        declared = counts
        if not block["data"]:
            raise ValueError("housing page unexpectedly empty")
        for native in block["data"]:
            row = {label: native[key] for key, label in HOUSE_FIELDS.items()}
            row.update(日期=native["REPORT_DATE"], 城市=native["CITY"], source_native=native)
            day = reviewed_date(row["日期"])
            if day is None or day[:4] < "2011":
                raise ValueError("housing monthly date outside reviewed methodology")
            row["fixed_base_year"] = base_year(native, day)
            rows.append(row)
        if page == declared[0]:
            break
    identities = {(r["城市"], r["日期"]) for r in rows}
    actual_cities = sorted({r["城市"] for r in rows})
    if len(identities) != declared[1] or len(rows) != declared[1] or selected is not None and actual_cities != selected:
        raise ValueError("housing source count, unique keys or city filter mismatch")
    capture.dataset_metadata = {"source_report": "RPT_ECONOMY_HOUSE_PRICE", "source_count": len(rows), "pages_received": declared[0],
        "requested_cities": selected or "all cities exposed by source", "source_cities": actual_cities,
        "complete_requested_window": True, "complete_returned_window": True,
        "complete_source_city_universe": selected is None, "complete_history": False,
        "units": "index points; previous month/same month last year=100; fixed base resets every five years",
        "scope": "all source-visible months and requested cities; native fixed-base nulls retained",
        "methodology_sources": HOUSE_METHODOLOGY,
        "source_publication_date": None, "date_role": "statistical month represented as first day; not a publication date"}
    return rows


def parse_hubei(body):
    soup = BeautifulSoup(body.decode("utf-8"), "lxml")
    area = soup.select_one("div.threeLeft")
    if area is None:
        raise ValueError("Hubei source price chart absent")
    scripts = "\n".join(s.get_text() for s in area.select("script"))
    matches = list(re.finditer(r"(?<![\w])cjj\s*=\s*'(\[.*?\])'\s*;", scripts, re.S))
    if len(matches) != 1 or "name: '湖北'" not in scripts or "成交量：" not in scripts or "吨" not in scripts or "涨跌：" not in scripts:
        raise ValueError("Hubei chart identity/unit schema changed")
    source = json.loads(matches[0][1])
    axes = re.findall(r"(?<![\w])riqi\s*=\s*'(\[.*?\])'\s*;", scripts[:matches[0].start()], re.S)
    if not axes:
        raise ValueError("Hubei chart date axis absent")
    axis = json.loads(axes[-1])
    if not isinstance(source, list) or not 1 <= len(source) <= 10000:
        raise ValueError("Hubei chart empty or exceeds history bound")
    if not isinstance(axis, list) or len(axis) != len(source):
        raise ValueError("Hubei chart date/value axis count mismatch")
    rows = []
    for day, raw in zip(axis, source):
        if raw == {"value": ""}:
            rows.append({"日期": day, "成交价": None, "成交量": None, "最新": None, "涨跌": None,
                "source_native": raw, "source_chart_missing": True,
                "market": "湖北碳排放权交易中心", "product": "湖北碳排放配额"})
            continue
        if set(raw) != {"riqi", "cjj", "cjl", "zx", "zd", "value"} or raw["cjj"] != raw["value"]:
            raise ValueError("Hubei chart native fields or plotted price disagree")
        if raw["riqi"] != day:
            raise ValueError("Hubei chart date differs from axis")
        rows.append({"日期": raw["riqi"], "成交价": chart_number(raw["cjj"]), "成交量": chart_number(raw["cjl"]),
                     "最新": chart_number(raw["zx"]), "涨跌": chart_number(raw["zd"]), "source_native": raw,
                     "source_chart_missing": False,
                     "market": "湖北碳排放权交易中心", "product": "湖北碳排放配额"})
    return rows


def collect(capture, name, params):
    if name == HOUSE:
        return collect_housing(capture, params)
    if name != "energy_carbon_hb" or params:
        raise ValueError("Hubei price chart does not accept an inferred context")
    rows = parse_hubei(capture.get(HB_URL))
    capture.dataset_metadata = {"source": HB_URL, "source_count": len(rows), "pages_received": 1,
        "missing_chart_dates": [r["日期"] for r in rows if r["source_chart_missing"]],
        "complete_returned_window": True, "complete_history": False, "complete_universe": False,
        "scope": "all dated Hubei observations embedded in the current official homepage chart",
        "publication_time": None, "source_units": {"成交价": "CNY/tonne", "成交量": "tonne", "涨跌": "percent"},
        "uninterpreted_native_field": "zx/最新; zero is retained, never used as a market price",
        "limitation": "当前图表可见历史，不推定交易所全历史、其他产品或最近应发布交易日"}
    return rows


def validate(rows, job, as_of, numeric):
    if job["function"] == HOUSE:
        return validate_housing(rows, job, as_of, numeric)
    if job["function"] != "energy_carbon_hb":
        return None
    days, observed, missing = [], [], []
    for row in rows:
        day = reviewed_date(row.get("日期"))
        if not day or day > as_of or (row.get("market"), row.get("product")) != ("湖北碳排放权交易中心", "湖北碳排放配额"):
            raise ValueError("Hubei date/market/product mismatch")
        days.append(day)
        if row.get("source_chart_missing"):
            if row.get("source_native") != {"value": ""} or any(row.get(k) is not None for k in ("成交价", "成交量", "最新", "涨跌")):
                raise ValueError("Hubei missing chart cell must remain null")
            missing.append(day)
            continue
        price, quantity = numeric(row.get("成交价")), numeric(row.get("成交量"))
        if price is None or quantity is None or price < 0 or quantity < 0 or quantity > 0 and price <= 0:
            raise ValueError("Hubei price/quantity invalid; zero price requires no trade")
        native = row.get("source_native", {})
        if native.get("riqi") != row["日期"] or any(chart_number(native.get(k)) != row.get(label)
                for k, label in {"cjj": "成交价", "cjl": "成交量", "zx": "最新", "zd": "涨跌"}.items()):
            raise ValueError("Hubei projected value differs from native row")
        numeric(row.get("涨跌"))
        numeric(row.get("最新"))
        observed.append(day)
    if not observed or len(days) != len(set(days)):
        raise ValueError("Hubei chart empty or duplicate trading dates")
    return {"contract_reviewed": True, "business_key": ["market", "product", "日期"], "unique_keys": len(days),
        "units": {"成交价": "CNY/tonne", "成交量": "tonne", "涨跌": "percent", "最新": "native zx; interpretation unverified, not an accepted market price"},
        "source_rows": len(rows), "priced_observations": len(observed), "missing_chart_dates": missing,
        "frequency": "exchange_chart_publication_and_revisions", "latest_period": max(observed),
        "latest_announcement": None, "gaps": ["图表最近日无湖北行情，不能当作最新成交"] if max(days) not in observed else [],
        "limitations": ["成交价和成交吨数按交易所图表说明；源最新zx扩展字段未解释，不作行情价格",
            "零值原样保留；不以源码中的页面更新时间替代行情日期或推定不存在的交易日",
            "重读源图表保存新增及修订；未观察未来发布周期，当前图表不等于全历史"]}


def validate_housing(rows, job, as_of, numeric):
    selected = cities(job["parameters"])
    keys, last_by_city, field_last, gaps = [], {}, {}, []
    periods = []
    for row in rows:
        day, city = reviewed_date(row.get("日期")), row.get("城市")
        if not day or day > as_of or day[8:] != "01" or day < "2011-01-01" or not isinstance(city, str) or not city:
            raise ValueError("housing city/month invalid or future")
        if selected is not None and city not in selected:
            raise ValueError("housing response contains unrequested city")
        native = row.get("source_native", {})
        if row.get("fixed_base_year") != base_year(native, day):
            raise ValueError("housing fixed-base methodology period mismatch")
        if native.get("REPORT_DATE") != row["日期"] or native.get("CITY") != city:
            raise ValueError("housing native city/period mismatch")
        for field, label in HOUSE_FIELDS.items():
            if field not in native or row.get(label) != native[field]:
                raise ValueError("housing native field projection mismatch")
            value = numeric(row[label])
            if value is not None:
                if value <= 0:
                    raise ValueError("housing values are positive base-100 indexes, not percent changes")
                field_last[label] = max(field_last.get(label, day), day)
        last_by_city[city] = max(last_by_city.get(city, day), day)
        periods.append(day)
        keys.append((city, day))
    if not rows or len(keys) != len(set(keys)) or selected is not None and sorted(last_by_city) != selected:
        raise ValueError("housing missing city, empty or duplicate period")
    for row in rows:
        if reviewed_date(row["日期"]) == last_by_city[row["城市"]]:
            for field, label in HOUSE_FIELDS.items():
                if not field.endswith("_BASE") and numeric(row[label]) is None:
                    gaps.append("城市最近月同比/环比指数缺失："+row["城市"]+"/"+label)
    return {"contract_reviewed": True, "business_key": ["城市", "日期"], "unique_keys": len(keys), "source_rows": len(rows),
        "units": {label: "index points; base=100" for label in HOUSE_FIELDS.values()},
        "frequency": "monthly_statistical_release_and_revisions", "latest_period": max(periods), "latest_announcement": None,
        "source_cities": sorted(last_by_city), "latest_month_by_city": last_by_city, "field_latest_nonnull_periods": field_last,
        "gaps": sorted(set(gaps)), "limitations": ["同比/环比列为基准100的指数，不能把102直接解释为上涨102%",
            "定基按2010/2015/2020等五年基期分别保留；源不再提供的定基列保持空，不跨基期拼接",
            "统计月不是公告日；按实际接收时点保存源修订，未重建首次发布版本",
            "全部城市仅指来源当前暴露目录，是否等价官方70城及CMES全历史需另行对账"]}
