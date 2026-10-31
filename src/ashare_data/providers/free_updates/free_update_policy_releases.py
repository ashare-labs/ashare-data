"""Official current policy/statistical releases, with explicit date roles."""
from datetime import datetime, timedelta
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

PBC_DIRECTORY = "https://www.pbc.gov.cn/diaochatongjisi/116219/116319/{year}ntjsj/shrzgm/index.html"
NDRC_DIRECTORY = "https://www.ndrc.gov.cn/xwdt/xwfb/"
SH_DIRECTORY = "https://fgw.sh.gov.cn/fgw_jggl/index.html"
FUNCTIONS = {"pbc_social_financing_flow", "ndrc_oil_adjustment", "shanghai_oil_prices"}
FLOW_FIELDS = ("社会融资规模增量", "人民币贷款", "外币贷款（折合人民币）", "委托贷款", "信托贷款",
               "未贴现银行承兑汇票", "企业债券", "政府债券", "非金融企业境内股票融资",
               "存款类金融机构资产支持证券", "贷款核销")


def text(node):
    return re.sub(r"\s+", "", node.get_text())


def number(value):
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        raise ValueError("official numeric field is absent or ambiguous")
    return value


def link(url, href, host, prefix):
    target = urljoin(url, href)
    parsed = urlsplit(target)
    # This official directory still emits HTTP links. Read the verified HTTPS
    # equivalent on the very same host; never downgrade TLS or change hosts.
    if host == "fgw.sh.gov.cn" and parsed.scheme == "http" and parsed.netloc == host:
        parsed = parsed._replace(scheme="https")
        target = parsed.geturl()
    if parsed.scheme != "https" or parsed.netloc != host or not parsed.path.startswith(prefix):
        raise ValueError("official release link left its reviewed directory")
    return target


def parse_flow(body, year, url):
    soup = BeautifulSoup(body, "lxml")
    if "社会融资规模增量统计表" not in text(soup) or "单位：亿元人民币" not in text(soup):
        raise ValueError("PBOC flow identity or unit changed")
    rows, unpublished = [], []
    header_found = False
    for tr in soup.select("table tr"):
        values = [text(c) for c in tr.select("td,th")]
        if tuple(values[:11]) == FLOW_FIELDS:
            header_found = True
        if not values or not re.fullmatch(r"\d{4}\.\d{2}", values[0]):
            continue
        if not header_found or len(values) < 12 or any(values[12:]):
            raise ValueError("PBOC flow column layout changed")
        period = datetime.strptime(values[0], "%Y.%m").strftime("%Y-%m")
        if period[:4] != str(year):
            raise ValueError("PBOC annual flow table contains another year")
        native = values[1:12]
        if not any(native):
            unpublished.append(period)
            continue
        # A blank future-month row is a publication placeholder, not zero.
        rows.append({"统计月份": period, **dict(zip(FLOW_FIELDS, map(number, native))),
                     "单位": "亿元人民币", "source_url": url,
                     "period_kind": "monthly_flow", "publication_at": None})
    if not rows or len({r["统计月份"] for r in rows}) != len(rows):
        raise ValueError("PBOC flow table empty or has duplicate months")
    return rows, unpublished


def effective(text_value, announcement):
    found = re.findall(r"(?:自|从)(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日(\d{1,2})时起", text_value)
    if len(set(found)) != 1:
        raise ValueError("oil policy effective time missing or ambiguous")
    year_text, month_text, day_text, hour_text = found[0]
    year = int(year_text or announcement[:4])
    month, day, hour = map(int, (month_text, day_text, hour_text))
    if not year_text and (month, day) != (int(announcement[5:7]), int(announcement[8:10])):
        raise ValueError("yearless effective date differs from the dated announcement")
    if not 0 <= hour <= 24:
        raise ValueError("oil policy effective hour invalid")
    return (datetime(year, month, day, tzinfo=ZoneInfo("Asia/Shanghai"))
            + timedelta(hours=hour)).isoformat()


def parse_ndrc(body, url):
    soup = BeautifulSoup(body, "lxml")
    title = soup.find("h2") or soup.find("h1")
    title_value = text(title) if title else text(soup.title) if soup.title else ""
    if not re.search(r"国内成品油价格|成品油价格.*调整|成品油价格.*调控", title_value):
        raise ValueError("NDRC oil article identity missing")
    main = soup.select_one(".TRS_Editor")
    content = text(main) if main else text(soup)
    date = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", title_value)
    if not date:
        raise ValueError("NDRC oil announcement date missing")
    publication = datetime(*map(int, date.groups())).date().isoformat()
    # Prefer the actual regulated change, never the earlier theoretical one.
    actual = re.findall(r"(?:调控后)?实际(上调|下调)(\d+)元[、，](\d+)元", content)
    if not actual:
        actual = re.findall(r"价格每吨分别(上调|下调)(\d+)元[、，](\d+)元", content)
    unchanged = "本次汽、柴油价格不作调整" in content
    if unchanged:
        if actual:
            raise ValueError("conflicting unchanged and adjusted oil policy")
        gas = diesel = "0"
        stamp = None
    elif len(actual) == 1:
        direction, gas, diesel = actual[0]
        sign = -1 if direction == "下调" else 1
        gas, diesel = str(sign*int(gas)), str(sign*int(diesel))
        stamp = effective(content, publication)
    else:
        raise ValueError("NDRC actual adjustment cannot be uniquely identified")
    return [{"公告日期": publication, "生效时间": stamp,
             "汽油调整": gas, "柴油调整": diesel, "单位": "CNY/tonne",
             "policy_status": "unchanged" if unchanged else "adjusted",
             "source_url": url, "source_title": title_value}]


def parse_shanghai(body, url):
    soup = BeautifulSoup(body, "lxml")
    meta = soup.select_one('meta[name="ArticleTitle"]')
    title = meta["content"] if meta else ""
    if not title.startswith("上海市发展和改革委员会关于车用汽、柴油价格的通知"):
        raise ValueError("Shanghai oil policy identity changed")
    date = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", title)
    if not date:
        raise ValueError("Shanghai oil announcement date absent")
    publication = datetime(*map(int, date.groups())).date().isoformat()
    stamp = effective(text(soup), publication)
    candidates = [t for t in soup.select("table") if "最高零售价" in text(t)]
    if len(candidates) != 1:
        raise ValueError("Shanghai retail price table ambiguous")
    rows, grade, pending = [], None, False
    for tr in candidates[0].select("tr"):
        cells = tr.select("td,th")
        values = [text(c) for c in cells]
        if values == ["标号", "单位", "最高零售价"]:
            continue
        if len(cells) == 3 and re.fullmatch(r"-?\d+号[汽柴]油", values[0]):
            if pending or cells[0].get("rowspan") != "2":
                raise ValueError("Shanghai fuel grade row span changed")
            grade, pending = values[0], True
            unit, value = values[1:]
        elif len(cells) == 2 and pending:
            unit, value = values
            pending = False
        else:
            raise ValueError("Shanghai oil table row cannot be bound to grade")
        if unit not in {"元/升", "元/吨"}:
            raise ValueError("Shanghai fuel price unit changed")
        rows.append({"地区": "上海市", "油号": grade, "单位": unit,
                     "最高零售价": number(value), "公告日期": publication,
                     "生效时间": stamp, "price_kind": "maximum_retail_ceiling",
                     "source_url": url})
    if pending or not rows or len({(r["油号"], r["单位"]) for r in rows}) != len(rows):
        raise ValueError("Shanghai oil table incomplete or duplicated")
    return rows


def collect(capture, name, params):
    if name == "pbc_social_financing_flow":
        if set(params) != {"year"} or not re.fullmatch(r"20\d{2}", str(params["year"])):
            raise ValueError("PBOC flow needs an explicit calendar year")
        url = PBC_DIRECTORY.format(year=params["year"])
        soup = BeautifulSoup(capture.get(url), "lxml")
        cells = [a for a in soup.select("a[href]") if text(a) == "htm"]
        if not cells or "社会融资规模增量统计表" not in text(cells[0].parent.parent):
            raise ValueError("PBOC flow table download binding changed")
        article = link(url, cells[0]["href"], "www.pbc.gov.cn", "/diaochatongjisi/attachDir/")
        rows, unpublished = parse_flow(capture.get(article), params["year"], article)
        extra = {"unpublished_month_placeholders": unpublished,
                 "publication_time": "not supplied by this table; receipt time retained"}
    else:
        if params or name not in FUNCTIONS:
            raise ValueError("official oil release has no implicit optional context")
        ndrc = name == "ndrc_oil_adjustment"
        url = NDRC_DIRECTORY if ndrc else SH_DIRECTORY
        soup = BeautifulSoup(capture.get(url), "lxml")
        choices = {}
        for a in soup.select("a[href]"):
            title = text(a)
            match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", title)
            relevant = ("成品油价格" in title) if ndrc else title.startswith("上海市发展和改革委员会关于车用汽、柴油价格的通知")
            if relevant and match:
                day = datetime(*map(int, match.groups())).date().isoformat()
                choices[day] = link(url, a["href"], "www.ndrc.gov.cn" if ndrc else "fgw.sh.gov.cn",
                                    "/xwdt/xwfb/" if ndrc else "/fgw_jggl/")
        if not choices:
            raise ValueError("latest official oil release absent from current directory")
        article = choices[max(choices)]
        rows = (parse_ndrc if ndrc else parse_shanghai)(capture.get(article), article)
        extra = {"publication_date": rows[0]["公告日期"], "effective_at": rows[0]["生效时间"],
                 "scope": "latest national adjustment event" if ndrc else "Shanghai current maximum retail prices; other provinces not claimed"}
    capture.dataset_metadata = {"source": url, "release_url": article, "source_count": len(rows),
        "complete_returned_window": True, "complete_history": False, "pages_received": 2,
        "source_window": "current official release; retain every newly observed revision", **extra}
    return rows


def validate(rows, job, as_of, numeric):
    name = job["function"]
    if name not in FUNCTIONS:
        return None
    flow = name == "pbc_social_financing_flow"
    key = ["统计月份"] if flow else ["公告日期"] if name == "ndrc_oil_adjustment" else ["地区", "油号", "单位", "公告日期"]
    periods, keys = [], []
    for row in rows:
        period = str(row.get("统计月份", ""))+"-01" if flow else row.get("公告日期")
        if not period or datetime.fromisoformat(period).date().isoformat() != period or period > as_of:
            raise ValueError("official release has invalid/future observed period")
        periods.append(period)
        keys.append(tuple(row.get(k) for k in key))
        fields = FLOW_FIELDS if flow else ("汽油调整", "柴油调整") if name == "ndrc_oil_adjustment" else ("最高零售价",)
        if any(numeric(row.get(k)) is None for k in fields):
            raise ValueError("official release required numerical value missing")
        if flow and row.get("单位") != "亿元人民币":
            raise ValueError("PBOC flow amount unit mismatch")
        if not flow and row.get("生效时间") is not None:
            stamp = datetime.fromisoformat(row["生效时间"])
            if stamp.tzinfo is None or str(stamp.utcoffset()) != "8:00:00" or stamp.date().isoformat() < period:
                raise ValueError("oil policy effective time invalid")
        if name == "ndrc_oil_adjustment" and row.get("单位") != "CNY/tonne":
            raise ValueError("oil adjustment unit mismatch")
        if name == "ndrc_oil_adjustment":
            if row.get("policy_status") not in {"unchanged", "adjusted"}:
                raise ValueError("oil policy status missing")
            if row["policy_status"] == "unchanged" and (numeric(row["汽油调整"]) != 0
                    or numeric(row["柴油调整"]) != 0 or row.get("生效时间") is not None):
                raise ValueError("unchanged oil policy must not invent a change or effective time")
            if row["policy_status"] == "adjusted" and row.get("生效时间") is None:
                raise ValueError("adjusted oil policy needs its explicit effective time")
        if name == "shanghai_oil_prices" and (row.get("地区") != "上海市" or row.get("单位") not in {"元/吨", "元/升"}
                or numeric(row["最高零售价"]) <= 0 or row.get("price_kind") != "maximum_retail_ceiling"):
            raise ValueError("regional retail ceiling identity/unit mismatch")
    if not rows or len(set(keys)) != len(keys) or any(None in k for k in keys):
        raise ValueError("official release empty/duplicate identity")
    return {"contract_reviewed": True, "business_key": key, "source_rows": len(rows), "unique_keys": len(keys),
        "units": dict.fromkeys(FLOW_FIELDS, "100000000 CNY") if flow else
            {"汽油调整": "CNY/tonne", "柴油调整": "CNY/tonne"} if name == "ndrc_oil_adjustment" else {"最高零售价": "row explicit unit: CNY/tonne or CNY/litre"},
        "frequency": "monthly_statistical_release" if flow else "official_oil_adjustment_events",
        "latest_period": max(periods), "latest_announcement": None if flow else max(periods), "gaps": [],
        "limitations": ["当期公开表按实际取得时点保留修订；统计月不冒充公告时间" if flow else
            "公告日期与生效时刻分开；24时换至次日00时；最高零售价不是各加油站实际成交价",
            "仅表内明确范围；未知或未发布值不补零，不推导价格单位转换"]}
