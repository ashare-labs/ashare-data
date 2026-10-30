"""Current native Eastmoney board members, from the official page's endpoint."""
import json
import re
from urllib.parse import urlencode

FUNCTIONS = {"stock_board_concept_cons_em", "stock_board_industry_cons_em"}


def parameters(name, params):
    if name not in FUNCTIONS or set(params) != {"symbol"} or not re.fullmatch(r"BK\d{4}", str(params["symbol"])):
        raise ValueError("board members require an explicit native BK code")
    return params["symbol"]


def request_url(code, page):
    return "https://push2.eastmoney.com/api/qt/clist/get?" + urlencode({
        "np": 1, "fltt": 1, "invt": 2, "fs": "b:" + code,
        "fields": "f12,f13,f14", "fid": "f12", "pn": page, "pz": 100, "po": 1,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b", "dect": 1,
    })


def collect(capture, name, params):
    code = parameters(name, params)
    rows, identities, urls, total = [], set(), [], None
    page = 1
    while total is None or len(rows) < total:
        url = request_url(code, page)
        payload = json.loads(capture.get_curl(url))
        urls.append(url)
        data = payload.get("data") or {}
        count, values = data.get("total"), data.get("diff")
        if (payload.get("rc") != 0 or not isinstance(count, int) or isinstance(count, bool)
                or not 0 < count <= 100000 or not isinstance(values, list) or not values):
            raise ValueError("board member response missing status, total or members")
        if total is not None and count != total:
            raise ValueError("board member total changed during pagination; retry a fresh snapshot")
        total = count
        if len(values) != min(100, total - len(rows)):
            raise ValueError("board member page is incomplete")
        for value in values:
            security, market, label = value.get("f12"), value.get("f13"), value.get("f14")
            if (not isinstance(security, str) or not re.fullmatch(r"\d{6}", security)
                    or not isinstance(market, int) or isinstance(market, bool)
                    or market not in {0, 1} or not isinstance(label, str) or not label.strip()):
                raise ValueError("board member native security identity missing")
            identity = (market, security)
            if identity in identities:
                raise ValueError("duplicate board member across pages")
            identities.add(identity)
            rows.append({"板块代码": code, "板块类型": "concept" if "concept" in name else "industry",
                         "证券代码": security, "原生市场代码": market, "证券名称": label,
                         "原生证券身份": f"eastmoney:{market}.{security}"})
        page += 1
    rows.sort(key=lambda r: r["原生证券身份"])
    capture.dataset_metadata = {
        "source": urls, "source_code": code, "source_total": total, "source_count": len(rows),
        "pages_received": len(urls), "complete_returned_window": True, "complete_history": False,
        "effective_date": None, "membership_time": "actual receipt time only",
        "scope": "all current members returned for the named Eastmoney board",
        "native_market_note": "f13 is the vendor routing code; 0 is not assumed to mean Shenzhen only",
        "transport": "system curl, existing system proxy, ordinary User-Agent, strict TLS; no retries or challenge",
    }
    return rows


def validate(rows, job, as_of, numeric):
    name, params = job["function"], job["parameters"]
    if name not in FUNCTIONS:
        return None
    code = parameters(name, params)
    kind = "concept" if "concept" in name else "industry"
    if not rows:
        raise ValueError("board member snapshot empty")
    for row in rows:
        if (row["板块代码"] != code or row["板块类型"] != kind
                or not re.fullmatch(r"\d{6}", row["证券代码"])
                or row["原生证券身份"] != f"eastmoney:{row['原生市场代码']}.{row['证券代码']}"
                or row["原生市场代码"] not in {0, 1} or not row["证券名称"]):
            raise ValueError("board member identity differs")
    if len({r["原生证券身份"] for r in rows}) != len(rows):
        raise ValueError("duplicate native board member")
    return {"contract_reviewed": True, "business_key": ["板块类型", "板块代码", "原生证券身份", "_snapshot_date"],
            "gaps": [], "unique_keys": len(rows), "source_rows": len(rows), "latest_period": None,
            "latest_announcement": None, "frequency": "current_snapshot", "units": {},
            "limitations": ["current membership received at actual capture time, not original entry date",
                            "native vendor market code preserved, no guessed exchange conversion"]}
