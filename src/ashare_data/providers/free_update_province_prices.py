"""Soozhu's actual province ranking, separate from its unordered map response."""

import json
import re

URL = "https://www.soozhu.com/price/data/center/"
FUNCTION = "soozhu_province_rank"
PROVINCES = set("北京市 天津市 上海市 重庆市 河北省 山西省 辽宁省 吉林省 黑龙江省 江苏省 浙江省 安徽省 福建省 江西省 山东省 河南省 湖北省 湖南省 广东省 海南省 四川省 贵州省 云南省 陕西省 甘肃省 青海省 内蒙古 广西 西藏 宁夏 新疆".split())


def page_contract(page):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "lxml")
    scripts = "\n".join(s.get_text() for s in soup.find_all("script"))
    rank = scripts[scripts.find("function provincepricerank()"):scripts.find("function showdayinds()")]
    map_start = scripts.find("function getmapdata()")
    map_text = scripts[map_start:scripts.find("function drawline1(", map_start)]
    if ("各省均价实时排行榜" not in soup.get_text() or 'act: "provincerank"' not in rank
            or "data.regionlist" not in rank or '$("#reptable")' not in rank
            or "全国瘦肉型猪出栏均价" not in scripts or "元/公斤" not in map_text):
        raise ValueError("Soozhu province ranking identity/unit display changed")
    token = soup.find("input", attrs={"name": "csrfmiddlewaretoken"})
    if token is None or not token.get("value"):
        raise ValueError("public form token missing; no access workaround")
    return token["value"]


def project(native, position):
    if set(native) != {"name", "value", "diff"} or native["name"] not in PROVINCES:
        raise ValueError("province rank native fields or region identity changed")
    if not isinstance(native["diff"], str) or not re.fullmatch(r"[+-]?\d+\.\d{2}", native["diff"]):
        raise ValueError("province change display format changed")
    return {"省份": native["name"], "价格": native["value"], "源展示序号": position,
            "source_change_display": native["diff"], "source_change_unit": None,
            "source_product": "瘦肉型猪出栏均价", "source_unit": "元/公斤",
            "source_publication_date": None, "source_native": native}


def parse(capture, page, response):
    page_contract(page)
    body = json.loads(response)
    if set(body) != {"regionlist", "res"} or str(body["res"]) != "1":
        raise ValueError("province ranking response rejected or schema changed")
    native = body["regionlist"]
    if not isinstance(native, list) or not 1 <= len(native) <= len(PROVINCES):
        raise ValueError("province ranking empty or exceeds known province scope")
    rows = [project(r, i+1) for i, r in enumerate(native)]
    capture.dataset_metadata = {
        "source_count": len(rows), "complete_returned_window": True, "complete_history": False,
        "complete_national_coverage": {r["省份"] for r in rows} == PROVINCES,
        "missing_provinces": sorted(PROVINCES - {r["省份"] for r in rows}),
        "source_product": "瘦肉型猪出栏均价", "source_unit": "元/公斤", "publication_at": None,
        "scope": "source's entire current province ranking only; missing provinces are not filled; no historical rankings",
        "time_policy": "page update clock is browser new Date(), not a publication timestamp; use actual receipt time only",
        "change_policy": "diff retained as native display text; its unit/period is not asserted or renamed percent",
    }
    return rows


def collect(capture, params):
    if params:
        raise ValueError("province ranking is an undated current snapshot")
    import requests
    capture.install()
    # Ordinary public-page session only. Cookies/token stay in this short-lived
    # worker; no account credentials, persistent cookie jar or browser imitation.
    with requests.Session() as session:
        page = session.get(URL).content
        token = page_contract(page)
        response = session.post(URL, data={"act": "provincerank", "csrfmiddlewaretoken": token},
                                headers={"Referer": URL}).content
    return parse(capture, page, response)


def replay(captured):
    page = captured.get(URL)
    token = page_contract(page)
    response = captured.post(URL, {"act": "provincerank", "csrfmiddlewaretoken": token})
    return parse(captured, page, response)


def validate(rows, job, as_of, numeric):
    if job["function"] != FUNCTION:
        return None
    if job["parameters"] or not rows:
        raise ValueError("province snapshot params/empty result invalid")
    names, prices = [], []
    for i, row in enumerate(rows):
        if row != project(row["source_native"], i+1):
            raise ValueError("province snapshot projection changed or publication date inferred")
        price = numeric(row["价格"])
        if price is None or price <= 0:
            raise ValueError("province price missing/nonpositive")
        names.append(row["省份"])
        prices.append(price)
    if len(set(names)) != len(names) or prices != sorted(prices, reverse=True):
        raise ValueError("province ranking duplicate or source order is not descending price")
    return {"contract_reviewed": True, "business_key": ["省份", "_snapshot_date"],
            "unique_keys": len(rows), "source_rows": len(rows), "gaps": [],
            "units": {"价格": "CNY/kg", "source_change_display": "uninterpreted native text"},
            "frequency": "current published snapshot; revisions retained by actual receipt time",
            "latest_period": None, "latest_announcement": None,
            "limitations": ["仅来源当前有报价省份；不声称31省齐全或历史排名", "源未提供有效发布日期；页面时钟不能冒充发布时点",
                            "同价次序沿用源展示位置，不推定并列排名；变化文本未当作百分比"]}
