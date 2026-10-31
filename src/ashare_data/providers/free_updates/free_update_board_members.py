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


def parse_page(body, name, params, page):
    """Decode one complete native page without claiming a complete board."""
    code = parameters(name, params)
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 1000:
        raise ValueError("invalid board page")
    payload = json.loads(body)
    data = payload.get("data") or {}
    count, values = data.get("total"), data.get("diff")
    if (payload.get("rc") != 0 or not isinstance(count, int) or isinstance(count, bool)
            or not 0 < count <= 100000 or not isinstance(values, list) or not values):
        raise ValueError("board member response missing status, total or members")
    if len(values) != min(100, count - (page - 1) * 100):
        raise ValueError("board member page is incomplete")
    rows, identities = [], set()
    for value in values:
        security, market, label = value.get("f12"), value.get("f13"), value.get("f14")
        if (not isinstance(security, str) or not re.fullmatch(r"\d{6}", security)
                or not isinstance(market, int) or isinstance(market, bool)
                or market not in {0, 1} or not isinstance(label, str) or not label.strip()):
            raise ValueError("board member native security identity missing")
        identity = (market, security)
        if identity in identities:
            raise ValueError("duplicate board member within page")
        identities.add(identity)
        rows.append({"板块代码": code, "板块类型": "concept" if "concept" in name else "industry",
                     "证券代码": security, "原生市场代码": market, "证券名称": label,
                     "原生证券身份": f"eastmoney:{market}.{security}"})
    return rows, count


def collect(capture, name, params):
    code = parameters(name, params)
    rows, identities, urls, total = [], set(), [], None
    page = 1
    while total is None or len(rows) < total:
        url = request_url(code, page)
        values, count = parse_page(capture.get_curl(url), name, params, page)
        urls.append(url)
        if total is not None and count != total:
            raise ValueError("board member total changed during pagination; retry a fresh snapshot")
        total = count
        for value in values:
            identity = value["原生证券身份"]
            if identity in identities:
                raise ValueError("duplicate board member across pages")
            identities.add(identity)
            rows.append(value)
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


def persist_received_page(root, name, code, page, body, receipt):
    """Retain a proven page independently when another page cannot be fetched.

    This performs no request and never replaces a whole-board checkpoint. Each
    page keeps its actual receipt time; independently received pages are not
    presented as a simultaneous full membership list or removal instructions.
    """
    from datetime import datetime, timezone
    import gzip
    import hashlib
    from pathlib import Path
    from zoneinfo import ZoneInfo
    from ashare_data.providers.free_updates.free_update_coverage import require
    from ashare_data.providers.free_updates.free_update_manual import write_snapshot
    from ashare_data.providers.free_updates import digest, encoded, implementation_identity
    from ashare_data.storage.free_updates import writer

    root = Path(root).resolve()
    require(not any((root / n).exists() for n in ("CURRENT", "CURRENT.json", "releases")),
            "production root forbidden")
    require(receipt.get("status") == 200 and receipt.get("protocol") == "http"
            and receipt.get("url") == request_url(code, page), "receipt status or page URL differs")
    require(len(body) == receipt.get("bytes") and hashlib.sha256(body).hexdigest() == receipt.get("sha256"),
            "received page hash differs")
    observed = datetime.fromisoformat(receipt["received_at"])
    require(observed.utcoffset() is not None and observed <= datetime.now(timezone.utc),
            "page receipt must have an actual nonfuture timezone-aware time")
    rows, total = parse_page(body, name, {"symbol": code}, page)
    for row in rows:
        row.update(采集时间=receipt["received_at"], 来源页码=page)
    day = observed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    semantic = validate(rows, {"function": name, "parameters": {"symbol": code}}, day, None)
    semantic["limitations"].append("page observations only; an absent member is not a confirmed removal")
    job = dict(function=name, parameters=dict(symbol=code, page=page, page_size=100,
               scope="received_page"), implementation_sha256=implementation_identity())
    job["job_id"] = digest(job)
    check = dict(function=name, job_id=job["job_id"], run_id=digest([job, receipt]),
                 observed_at=receipt["received_at"], snapshot_observation_date=day,
                 execution_kind="reparsed_received_public_page", raw_protocol_reparsed=True,
                 records=len(rows), receipts=[receipt], source_urls=[receipt["url"]], semantics=semantic,
                 coverage=dict(page=page, page_size=100, source_total=total, source_count=len(rows),
                     complete_received_page=True, complete_named_board=False, complete_history=False,
                     source_window=f"{code} page {page} only, received at {receipt['received_at']}; not a simultaneous whole-board snapshot"))
    with writer(root):
        raw = root / "raw" / (receipt["sha256"] + ".gz")
        raw.parent.mkdir(exist_ok=True)
        if raw.exists():
            require(gzip.decompress(raw.read_bytes()) == body, "stored page body differs")
        else:
            pending = raw.with_suffix(".gz.next")
            pending.write_bytes(gzip.compress(body, mtime=0))
            pending.replace(raw)
        evidence = raw.with_suffix(".receipt.json")
        # One body can be observed repeatedly; each immutable manifest retains
        # the exact receipt. This initial sidecar is only a convenience copy.
        if not evidence.exists():
            evidence.write_bytes(encoded(receipt))
        return write_snapshot(root, 75, job, rows, check)


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
