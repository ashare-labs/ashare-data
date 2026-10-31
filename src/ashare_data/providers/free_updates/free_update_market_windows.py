"""Current margin aggregates and bounded government-yield source windows.

The native units and date labels are retained. These windows are sufficient
for incremental storage; they do not claim to reconstruct the CMES archive.
"""
from datetime import datetime
from io import StringIO
import json
from urllib.parse import parse_qs, urlencode, urlsplit

MARGIN_URL = "https://cdn.jin10.com/data_center/reports/fs_1.json"
MARGIN_FIELDS = ["融资买入额", "融资余额", "融券卖出量", "融券余量", "融券余额", "融资融券余额"]
MARGIN_HEADERS = [{"name": name, "unit": "股" if name in {"融券卖出量", "融券余量"} else "元"}
                  for name in MARGIN_FIELDS]
CURVE_URL = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
CURVE_NAME = "中债国债收益率曲线"
TENORS = ["3月", "6月", "1年", "3年", "5年", "7年", "10年", "30年"]


def source_url(capture, base):
    """Match an old recorded cache-buster without weakening endpoint identity."""
    from ashare_data.providers.free_updates.free_update_coverage import CapturedResponses
    if not isinstance(capture, CapturedResponses):
        return base
    receipt = capture.result["receipts"][capture.index]
    url = urlsplit(receipt["url"])
    target = urlsplit(base)
    query = parse_qs(url.query)
    if (url.scheme, url.netloc, url.path, url.fragment) != (target.scheme, target.netloc, target.path, ""):
        raise ValueError("recorded margin endpoint changed")
    if query and (set(query) != {"_"} or len(query["_"]) != 1 or not query["_"][0].replace(".", "", 1).isdigit()):
        raise ValueError("unreviewed recorded margin query")
    return receipt["url"]


def curve_bounds(params):
    if set(params) != {"start_date", "end_date"}:
        raise ValueError("government curve requires explicit date bounds")
    start, end = [datetime.strptime(params[k], "%Y%m%d").date() for k in ("start_date", "end_date")]
    if not 0 <= (end-start).days <= 31:
        raise ValueError("government curve window must be at most31 days")
    return start.isoformat(), end.isoformat()


def collect(capture, name, params):
    import pandas as pd
    if name == "macro_china_market_margin_sh":
        if params:
            raise ValueError("Shanghai margin source takes no scope parameters")
        body = json.loads(capture.get(source_url(capture, MARGIN_URL)))
        values = body.get("values")
        if body.get("keys") != MARGIN_HEADERS or not isinstance(values, dict) or not values:
            raise ValueError("Shanghai margin columns or source units changed")
        if any(not isinstance(row, list) or len(row) != 6 for row in values.values()):
            raise ValueError("Shanghai margin row width changed")
        frame = pd.DataFrame.from_dict(values, orient="index", columns=MARGIN_FIELDS)
        frame.insert(0, "日期", pd.to_datetime(frame.index, errors="raise"))
        fields = MARGIN_FIELDS
        units = {h["name"]: "shares" if h["unit"] == "股" else "CNY" for h in MARGIN_HEADERS}
        coverage = {"source": MARGIN_URL, "source_headers": MARGIN_HEADERS,
                    "source_window": "all dates currently returned by Jin10 Shanghai exchange-wide margin JSON"}
    elif name == "china_government_yield":
        start, end = curve_bounds(params)
        query = {"startDate": start, "endDate": end, "gjqx": "0", "qxId": "ycqx", "locale": "cn_ZH"}
        body = capture.get(CURVE_URL+"?"+urlencode(query))
        tables = pd.read_html(StringIO(body.decode("utf-8").replace("&nbsp", "")), header=0)
        selected = [t for t in tables if {"曲线名称", "日期", *TENORS} <= set(t.columns)]
        if len(selected) != 1 or selected[0].columns.has_duplicates:
            raise ValueError("government curve table missing or ambiguous")
        frame = selected[0].loc[selected[0]["曲线名称"] == CURVE_NAME].copy()
        frame["日期"] = pd.to_datetime(frame["日期"], errors="raise")
        if frame.empty or any(not start <= d.date().isoformat() <= end for d in frame["日期"]):
            raise ValueError("government curve absent or returned dates outside request")
        fields = TENORS
        units = dict.fromkeys(TENORS, "percent_per_annum")
        coverage = {"source": CURVE_URL, "curve": CURVE_NAME, "start_date": start, "end_date": end,
                    "complete_requested_window": True,
                    "source_window": "all returned government-curve rows in the explicit date window; no other curves"}
    else:
        raise ValueError("unreviewed market window")
    for field in fields:
        frame[field] = pd.to_numeric(frame[field], errors="raise")
    frame = frame.sort_values("日期")
    if frame["日期"].duplicated().any():
        raise ValueError("duplicate source date")
    rows = json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
    capture.dataset_metadata = coverage | {"source_count": len(rows), "complete_returned_window": True,
        "complete_history": False, "pages_received": 1, "units": units,
        "publication_at": None, "historical_api_required": False}
    return rows
