"""CNInfo announcements with native IDs, epoch dates and bounded pagination."""
from datetime import datetime, timezone
import json
import math
import re
from zoneinfo import ZoneInfo

from ashare_data.providers.free_updates import digest, source_date

DIRECTORY = "https://www.cninfo.com.cn/new/data/szse_stock.json"
QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"


def announcement_time(value):
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("announcementTime must retain native integer epoch milliseconds")
    stamp = datetime.fromtimestamp(value/1000, timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    if not 1990 <= stamp.year <= 2200:
        raise ValueError("announcement epoch scale or year invalid")
    return stamp


def collect(capture, params):
    if set(params) != {"symbol", "market", "start_date", "end_date"} or params["market"] != "沪深京" or not re.fullmatch(r"(?:60|68|00|30)\d{4}", params["symbol"]):
        raise ValueError("announcement menu needs one explicit SH/SZ A-share security and date window")
    start, end = source_date(params["start_date"]), source_date(params["end_date"])
    if not start or not end or start > end or (datetime.fromisoformat(end)-datetime.fromisoformat(start)).days > 366:
        raise ValueError("announcement window invalid or exceeds 366 days; split scope")
    directory = json.loads(capture.get(DIRECTORY))["stockList"]
    matches = [r for r in directory if r.get("code") == params["symbol"] and r.get("category") == "A股"]
    if len(matches) != 1 or not matches[0].get("orgId"):
        raise ValueError("official announcement directory identity missing or ambiguous")
    identity = matches[0]
    query = dict(pageNum="1", pageSize="30", column="szse", tabName="fulltext", plate="",
        stock=params["symbol"]+","+identity["orgId"], searchkey="", secid="", category="", trade="",
        seDate=start+"~"+end, sortName="", sortType="", isHLtitle="true")
    rows, hashes, declared, page_count = [], set(), None, None
    for page in range(1, 40):
        query["pageNum"] = str(page)
        response = json.loads(capture.post(QUERY, query))
        total = int(response["totalAnnouncement"])
        declared = total if declared is None else declared
        page_count = max(1, math.ceil(total/30))
        if total != declared or total < 0 or page_count > 39:
            raise ValueError("announcement native count changed or exceeds budget; split scope")
        records = response.get("announcements") or []
        if not isinstance(records, list) or bool(response["hasMore"]) != (page < page_count) or len(records) != min(30, max(0, total-(page-1)*30)):
            raise ValueError("announcement page length/hasMore/count disagree")
        # totalpages has been observed as zero for a nonempty single page.
        # totalAnnouncement + hasMore + received counts are reconciled instead.
        if digest(records) in hashes:
            raise ValueError("announcement repeated page")
        hashes.add(digest(records))
        for row in records:
            if row.get("secCode") != params["symbol"] or row.get("orgId") != identity["orgId"]:
                raise ValueError("announcement identity differs from official stock directory")
            stamp = announcement_time(row["announcementTime"])
            day = stamp.date().isoformat()
            if not start <= day <= end:
                raise ValueError("announcement date outside requested source window")
            rows.append(row | {"announcement_date": day, "公告日期": day,
                "source_timestamp_asia_shanghai": stamp.isoformat(),
                "source_time_precision": "source_calendar_date" if stamp.time().isoformat() == "00:00:00" else "source_timestamp_not_proven_first_online",
                "first_publication_at": None})
        if page == page_count:
            break
    if len(rows) != declared or len({r["announcementId"] for r in rows}) != len(rows):
        raise ValueError("announcement complete-window count/ID uniqueness mismatch")
    capture.dataset_metadata = {"source": "CNInfo official announcement query", "source_count": declared,
        "pages_received": page_count, "requested_security": params["symbol"], "official_directory_identity": identity,
        "start_date": start, "end_date": end, "complete_requested_security_window": True,
        "complete_returned_window": True, "complete_history": False, "complete_universe": False,
        "scope": "all announcements/pages for one explicit SH/SZ security and the requested date window",
        "date_basis": "native announcementTime epoch milliseconds converted to Asia/Shanghai; midnight means date precision",
        "publication_limitation": "source date is not evidence of exact first-online time; raw receive time retained"}
    return rows


def validate(rows, job, as_of):
    if job["function"] != "stock_zh_a_disclosure_report_cninfo":
        return None
    keys, dates, precise = [], [], 0
    for row in rows:
        stamp = announcement_time(row["announcementTime"])
        day = stamp.date().isoformat()
        if not source_date(job["parameters"]["start_date"]) <= day <= min(as_of, source_date(job["parameters"]["end_date"])):
            raise ValueError("announcement date invalid/future or outside window")
        if row.get("secCode") != job["parameters"]["symbol"] or not row.get("orgId") or not row.get("secName"):
            raise ValueError("announcement security/org identity missing")
        if not str(row.get("announcementId", "")).isdigit() or not row.get("announcementTitle"):
            raise ValueError("announcement native ID/title absent")
        if row.get("announcement_date") != day or row.get("公告日期") != day or row.get("source_timestamp_asia_shanghai") != stamp.isoformat() or row.get("first_publication_at") is not None:
            raise ValueError("announcement date/visibility mapping incorrect")
        expected = "source_calendar_date" if stamp.time().isoformat() == "00:00:00" else "source_timestamp_not_proven_first_online"
        if row.get("source_time_precision") != expected:
            raise ValueError("announcement time precision overclaimed")
        precise += expected != "source_calendar_date"
        path = row.get("adjunctUrl", "")
        if not isinstance(path, str) or not re.fullmatch(r"finalpage/\d{4}-\d{2}-\d{2}/[A-Za-z0-9._-]+", path):
            raise ValueError("announcement attachment reference outside reviewed static path")
        keys.append((row["orgId"], row["secCode"], row["announcementId"]))
        dates.append(day)
    if not rows or len(keys) != len(set(keys)):
        raise ValueError("announcement duplicate or empty IDs")
    return {"contract_reviewed": True, "business_key": ["orgId", "secCode", "announcementId"], "unique_keys": len(keys),
        "source_rows": len(rows), "units": {"announcementTime": "Unix milliseconds", "adjunctSize": "source native; not used as verified byte length"},
        "frequency": "event_release_and_corrections", "latest_period": max(dates), "latest_announcement": max(dates),
        "non_midnight_source_timestamps": precise, "gaps": [],
        "limitations": ["保留公告ID、机构ID、原始毫秒时间与Asia/Shanghai公告日；午夜不冒充精确上网时间",
            "只验请求证券和日期窗口；PDF仅保留原始引用，不宣称已下载全文或完成全市场历史覆盖"]}
