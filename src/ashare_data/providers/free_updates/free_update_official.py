"""Reviewed official releases with explicit periods, vintages and local dates."""
from calendar import monthrange
from datetime import datetime, timezone
import json
import re
from urllib.parse import urljoin, urlsplit
from urllib.request import Request
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

NBS_RELEASES = "https://www.stats.gov.cn/sj/zxfb/"
SGE_ENDPOINTS = {"gold": "DayilyJzj", "silver": "DayilyShsilverJzj"}
GDP_TITLE = r"(\d{4})年([一二三四])季度(?:和全年)?国内生产总值初步核算结果"


def parse_gdp(body, url):
    soup = BeautifulSoup(body, "lxml")
    title = soup.select_one('meta[name="ArticleTitle"]')
    published = soup.select_one('meta[name="PubDate"]')
    matched = re.fullmatch(GDP_TITLE, title["content"] if title else "")
    if not matched or published is None:
        raise ValueError("NBS GDP release identity or publication time missing")
    release = datetime.strptime(published["content"], "%Y/%m/%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    year, quarter = int(matched[1]), "一二三四".index(matched[2])+1
    candidates = []
    for table in soup.select("table"):
        text = table.get_text("", strip=True)
        if "同比增长速度为与上年同期对比" not in text:
            continue
        if "单位：%" not in text:
            raise ValueError("NBS GDP growth unit changed")
        cells = [[c.get_text("", strip=True) for c in tr.select("td,th")] for tr in table.select("tr")]
        if ["年份", "1季度", "2季度", "3季度", "4季度"] not in cells:
            raise ValueError("NBS GDP quarterly header changed")
        rows = []
        for values in cells:
            if len(values) != 5 or not re.fullmatch(r"\d{4}", values[0]):
                continue
            for q, value in enumerate(values[1:], 1):
                if not value:
                    continue
                if not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                    raise ValueError("NBS GDP value not numeric")
                y, month = int(values[0]), q*3
                if (y, q) > (year, quarter):
                    raise ValueError("NBS GDP has future quarter actuals")
                rows.append({"日期": f"{y}-{month:02d}-{monthrange(y, month)[1]}",
                             "quarter": f"{y}Q{q}", "value": float(value), "unit": "percent",
                             "definition": "real_GDP_single_quarter_year_over_year",
                             "publication_at": release.isoformat(), "source_url": url,
                             "forecast": None})
        candidates.append(rows)
    # Desktop/mobile HTML repeats the same table. Conflicting copies fail.
    if not candidates or not candidates[0] or any(r != candidates[0] for r in candidates[1:]):
        raise ValueError("NBS GDP quarterly table absent or conflicting")
    if candidates[0][-1]["quarter"] != f"{year}Q{quarter}":
        raise ValueError("NBS GDP release quarter missing")
    return candidates[0]


def collect_gdp(capture):
    # NBS's published createPageHTML uses index_N.html; never execute its JS.
    for page in range(4):
        url = NBS_RELEASES if page == 0 else NBS_RELEASES+f"index_{page}.html"
        body = capture.get(url)
        soup = BeautifulSoup(body, "lxml")
        links = {}
        for a in soup.select("a[href]"):
            title = a.get_text("", strip=True)
            match = re.fullmatch(GDP_TITLE, title)
            if match:
                target = urljoin(url, a["href"])
                parsed = urlsplit(target)
                if parsed.scheme != "https" or parsed.hostname != "www.stats.gov.cn" or not parsed.path.startswith(("/sj/zxfb/", "/sj/zxfbhjd/")):
                    raise ValueError("NBS GDP article link outside reviewed directory")
                links[(int(match[1]), "一二三四".index(match[2]))] = target
        if links:
            article = links[max(links)]
            rows = parse_gdp(capture.get(article), article)
            capture.dataset_metadata = {"source": "国家统计局季度 GDP 初步核算", "release_url": article,
                "publication_at": rows[-1]["publication_at"], "latest_quarter": rows[-1]["quarter"],
                "complete_history": False, "complete_returned_window": True,
                "source_count": len(rows), "discovery_pages": page+1,
                "source_window": "all quarters in this official release's quarterly growth table; not the full GDP archive",
                "definition": "单季度不变价同比；不是年内累计同比或季调环比",
                "vintage": "本次发布的修订序列；不推定旧值的首次公告可见时点"}
            return rows
        pagination = re.search(r'createPageHTML\((\d+),\s*\d+,\s*"index",\s*"html"\)', body.decode("utf-8"))
        if not pagination or page+1 >= int(pagination[1]):
            break
    raise ValueError("latest GDP release not found within four published directory pages")


def parse_sge_benchmark(data, kind):
    if kind not in SGE_ENDPOINTS or set(data) != {"zp", "wp"}:
        raise ValueError("SGE benchmark series identity changed")
    by_day = {}
    for key, column in [("zp", "早盘价"), ("wp", "午盘价")]:
        for entry in data[key]:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError("SGE benchmark row width changed")
            ms, value = entry
            if isinstance(ms, bool) or not isinstance(ms, int):
                raise ValueError("SGE benchmark timestamp must be epoch milliseconds")
            timestamp = datetime.fromtimestamp(ms/1000, timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
            if timestamp.time().isoformat() != "00:00:00":
                raise ValueError("SGE benchmark date timestamp is not Shanghai midnight")
            day = timestamp.date().isoformat()
            row = by_day.setdefault(day, {"交易时间": day, "品种": "SHAU" if kind == "gold" else "SHAG",
                "早盘价": None, "午盘价": None, "source_epoch_ms": ms})
            seen = column+"_source_present"
            if row.get(seen):
                raise ValueError("duplicate SGE session date")
            row[seen] = True
            row[column] = value
    if not by_day:
        raise ValueError("empty SGE benchmark response")
    return [by_day[d] for d in sorted(by_day)]


def collect_sge(capture, kind):
    url = "https://www.sge.com.cn/graph/"+SGE_ENDPOINTS[kind]
    request = Request(url, data=b"", headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": "https://www.sge.com.cn/"})
    with capture.open(request) as response:
        rows = parse_sge_benchmark(json.loads(response.read()), kind)
    capture.dataset_metadata = {"source": "Shanghai Gold Exchange", "date_timezone": "Asia/Shanghai",
        "price_unit": "CNY/gram" if kind == "gold" else "CNY/kilogram", "session_alignment": "source date, outer join",
        "publication_time": "not supplied; local received time retained", "full_market": False}
    return rows
