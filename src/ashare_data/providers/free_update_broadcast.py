"""CCTV daily article transcripts with a count-checked source index.

No JavaScript or embedded links are executed. Video-only articles retain their
source identity and an explicit missing-text marker alongside available texts.
"""
from datetime import datetime
import re
from urllib.parse import urlsplit

from ashare_data.providers.free_updates import source_date


def article_url(url, day):
    parsed = urlsplit(url)
    prefix = "/"+day.replace("-", "/")+"/"
    if parsed.scheme != "https" or parsed.netloc != "tv.cctv.com" or not re.fullmatch(re.escape(prefix)+r"VIDE[A-Za-z0-9]+\.shtml", parsed.path) or parsed.query or parsed.fragment:
        raise ValueError("CCTV article identity/date/host differs from requested broadcast")


def index(body, day):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "lxml")
    items = soup.find_all("li")
    if not 2 <= len(items) <= 39:
        raise ValueError("broadcast index empty or exceeds one bounded run")
    first = items[0].find("a")
    if first is None or "《新闻联播》" not in first.get("title", "") or day.replace("-", "") not in first.get("title", ""):
        raise ValueError("CCTV broadcast date/title absent from index")
    article_url(first["href"], day)
    links = []
    for item in items[1:]:
        urls = {a["href"] for a in item.find_all("a", href=True)}
        if len(urls) != 1:
            raise ValueError("CCTV article index link ambiguity")
        url = urls.pop()
        article_url(url, day)
        links.append(url)
    if len(links) != len(set(links)):
        raise ValueError("CCTV broadcast repeated article URL")
    return links


def article(body, url, day):
    from bs4 import BeautifulSoup
    article_url(url, day)
    soup = BeautifulSoup(body, "lxml")
    title = soup.select_one("h3") or soup.select_one("div.tit")
    content = soup.select_one("div.cnt_bd") or soup.select_one("div.content_area")
    identity = soup.select_one('meta[name="contentid"]')
    if title is None or content is None or identity is None or identity.get("content") != urlsplit(url).path.rsplit("/", 1)[-1][:-6]:
        raise ValueError("CCTV transcript/identity missing")
    for unsafe in content.select("script,style"):
        unsafe.decompose()
    title_text, content_text = title.get_text(" ", strip=True), content.get_text(" ", strip=True)
    if not title_text:
        raise ValueError("empty CCTV title")
    row = {"date": day, "broadcast_date": day, "source_url": url,
            "source_id": identity["content"], "title": title_text.removeprefix("[视频]"),
            "content": content_text, "publication_at": None,
            "date_basis": "broadcast index day, not a verified article publication timestamp"}
    if not content_text:
        row["transcript_status"] = "source_text_absent"
    return row


def collect(capture, params):
    if set(params) != {"date"}:
        raise ValueError("CCTV requires an explicit broadcast date")
    day = datetime.strptime(params["date"], "%Y%m%d").date().isoformat()
    if day <= "2016-02-03":
        raise ValueError("only the reviewed modern broadcast index is supported")
    url = "https://tv.cctv.com/lm/xwlb/day/"+params["date"]+".shtml"
    links = index(capture.get(url), day)
    rows = [article(capture.get(link), link, day) for link in links]
    missing = [r["source_id"] for r in rows if not r["content"]]
    capture.dataset_metadata = {"source_index": url, "broadcast_date": day, "expected_articles": len(links),
        "received_articles": len(rows), "complete_requested_broadcast_index": True, "complete_history": False,
        "complete_returned_window": True, "source_count": len(rows),
        "excluded": "one full-broadcast video; individual linked articles retained",
        "available_transcripts": len(rows)-len(missing), "missing_transcript_ids": missing,
        "complete_transcript_text": not missing,
        "scope": "complete daily article index and available source text; video-only items explicitly marked"}
    return rows


def validate(rows, job, as_of):
    if job["function"] != "news_cctv":
        return None
    day = source_date(job["parameters"]["date"])
    if not day or day > as_of or not rows:
        raise ValueError("invalid broadcast date or empty transcripts")
    for row in rows:
        article_url(row["source_url"], day)
        if row["date"] != day or row["broadcast_date"] != day or row["source_id"] != urlsplit(row["source_url"]).path.rsplit("/", 1)[-1][:-6] or row["publication_at"] is not None:
            raise ValueError("broadcast identity/date/publication semantics mismatch")
        if not isinstance(row.get("title"), str) or not row["title"].strip() or not isinstance(row.get("content"), str):
            raise ValueError("broadcast title/content type invalid")
        if row["content"].strip():
            if row.get("transcript_status") not in {None, "available"}:
                raise ValueError("broadcast transcript status contradicts text")
        elif row.get("transcript_status") != "source_text_absent":
            raise ValueError("broadcast missing text must be explicitly marked")
    missing = [r["source_id"] for r in rows if not r["content"].strip()]
    if len(missing) == len(rows):
        raise ValueError("broadcast has no available transcript text")
    if len({r["source_id"] for r in rows}) != len(rows):
        raise ValueError("broadcast duplicate article identity")
    return {"contract_reviewed": True, "business_key": ["source_id"], "source_rows": len(rows),
            "unique_keys": len(rows), "units": {"title": "text", "content": "source transcript text"},
            "frequency": "daily_broadcast_after_source_index_publication", "latest_period": day,
            "latest_announcement": None, "gaps": [],
            "available_transcripts": len(rows)-len(missing), "missing_transcript_ids": missing,
            "limitations": ["播出日期与单篇发布时间分开，未推定分秒级公告时点", "目录完整；视频无正文时保留身份并显式标缺，不伪造文字稿"]}


def transcript_summary(rows, coverage):
    """Build acceptance counts from actual text, rejecting stale cached labels."""
    day = coverage["broadcast_date"]
    checked = validate(rows, {"function": "news_cctv", "parameters": {"date": day.replace("-", "")}}, day)
    expected = {"expected_articles": len(rows), "received_articles": len(rows),
                "available_transcripts": checked["available_transcripts"],
                "missing_transcript_ids": checked["missing_transcript_ids"],
                "complete_transcript_text": not checked["missing_transcript_ids"]}
    if any(coverage.get(k) != v for k, v in expected.items()):
        raise ValueError("broadcast coverage counts contradict retained article text")
    return {"broadcast_date": day, **expected,
            "description": f"{day}：{len(rows)}条目录、{checked['available_transcripts']}篇完整文字稿；"
                           f"缺正文{len(checked['missing_transcript_ids'])}篇"}


def receipt_window_start(root, result, run):
    """Admit individually proven cached receipts, without backdating a retry."""
    import hashlib
    import json
    from ashare_data.providers.free_update_coverage import require
    from ashare_data.storage.free_updates import connect
    start, end = datetime.fromisoformat(run["started_at"]), datetime.fromisoformat(run["finished_at"])
    reuse = result.get("receipt_reuse")
    if not reuse:
        return start
    source_id = reuse["source_run_id"]
    require(isinstance(source_id, str) and re.fullmatch(r"[a-zA-Z0-9-]{1,80}", source_id), "invalid cached source run")
    path = root / "runs" / (source_id+".json")
    body = path.read_bytes()
    require(hashlib.sha256(body).hexdigest() == reuse["source_result_sha256"], "cached source result changed")
    prior = json.loads(body)
    with connect(root, readonly=True) as db:
        source = db.execute("SELECT * FROM runs WHERE run_id=?", (source_id,)).fetchone()
    require(source and source["status"] == "received", "cached source run is not accepted")
    job = json.loads(source["job_json"])
    require(job["function"] == "news_cctv" and len(prior["receipts"]) == len(result["receipts"]),
            "cached broadcast receipt count or function differs")
    indices = reuse["indices"]
    require(isinstance(indices, list) and len(set(indices)) == len(indices)
            and all(isinstance(i, int) and 0 <= i < len(prior["receipts"]) for i in indices), "invalid cached receipt indices")
    lower, upper = datetime.fromisoformat(source["started_at"]), datetime.fromisoformat(source["finished_at"])
    # Do not build an unbounded chain of partly cached attempts.
    if prior.get("receipt_reuse"):
        raise ValueError("use the original complete capture as the retry base")
    for i, receipt in enumerate(result["receipts"]):
        received = datetime.fromisoformat(receipt["received_at"])
        if i in indices:
            require(receipt == prior["receipts"][i] and lower <= received <= upper,
                    "cached receipt differs from its proven source run")
        else:
            require(start <= received <= end, "new retry receipt outside actual execution")
    return min([start, *(datetime.fromisoformat(result["receipts"][i]["received_at"]) for i in indices)])


def refresh_missing(capture, root, result, params, start, end, *, max_articles=5):
    """Reuse the exact index and available texts; fetch only absent transcripts."""
    from ashare_data.providers.free_update_coverage import CapturedResponses, require
    replay = CapturedResponses(root, result, start, end)
    previous = collect(replay, params)
    require(replay.index == len(result["receipts"]), "unused prior broadcast receipts")
    missing = [row for row in previous if not row["content"]]
    require(1 <= len(missing) <= max_articles <= 5, "no missing articles or retry exceeds bounded scope")
    replacements = {}
    for row in missing:
        body = capture.get(row["source_url"])
        article(body, row["source_url"], row["date"])
        replacements[row["source_url"]] = capture.receipts[-1]
    receipts = [replacements.get(r["url"], r) for r in result["receipts"]]
    lower = min([start, *(datetime.fromisoformat(r["received_at"]) for r in capture.receipts)])
    upper = max([end, *(datetime.fromisoformat(r["received_at"]) for r in capture.receipts)])
    combined = CapturedResponses(root, {"receipts": receipts}, lower, upper)
    rows = collect(combined, params)
    require(len(rows) == len(previous) and [r["source_id"] for r in rows] == [r["source_id"] for r in previous],
            "retry changed the original broadcast directory")
    return rows, combined.dataset_metadata, receipts, [i for i, r in enumerate(result["receipts"]) if r["url"] not in replacements]
