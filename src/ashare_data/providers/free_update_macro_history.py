"""Count-reconciled public macro histories and explicit source date windows."""
from datetime import datetime
import json
from urllib.parse import urlencode

from ashare_data.providers.free_updates import digest, source_date


def window(parameters):
    if set(parameters)-{"start_date","end_date","page_size"}:
        raise ValueError("macro history accepts only explicit date bounds and page size")
    output={}
    for key in ("start_date","end_date"):
        if key in parameters:
            value=parameters[key]
            day=source_date(value)
            if not day or value not in {day,day.replace("-","")}:
                raise ValueError("macro window requires an exact ISO date or YYYYMMDD")
            datetime.strptime(day,"%Y-%m-%d")
            output[key]=day
    if output.get("start_date","0000") > output.get("end_date","9999"):
        raise ValueError("macro history window is reversed")
    if "page_size" in parameters:
        size=parameters["page_size"]
        if isinstance(size,bool) or not isinstance(size,int) or not 1 <= size <= 1000:
            raise ValueError("macro page size must be an integer in 1..1000")
        output["page_size"]=size
    return output


def collect(capture, spec, parameters):
    limits=window(parameters)
    query=dict(spec["parameters"])
    date_field="TRADE_DATE" if query["reportName"] == "RPTA_WEB_RATE" else "REPORT_DATE"
    if "page_size" in limits:
        query["pageSize"]=str(limits["page_size"])
    predicate=query.get("filter","")
    for key,op in [("start_date",">="),("end_date","<=")]:
        if key in limits:
            predicate+=f"({date_field}{op}'{limits[key]}')"
    if predicate:
        query["filter"]=predicate
    output, page_hashes = [], set()
    pages, count = None, None
    for page in range(1,41):
        query["pageNumber"]=str(page)
        for alias in ("pageNo","pageNum","p"):
            if alias in query:
                query[alias]=str(page)
        response=json.loads(capture.get(spec["url"]+"?"+urlencode(query)))
        block=response.get("result")
        if response.get("success") is False or not isinstance(block,dict) or not isinstance(block.get("data"),list):
            raise ValueError("macro report rejected or rows absent")
        declared=(int(block["pages"]),int(block["count"]))
        if pages is None:
            pages,count=declared
            if not 1 <= pages <= 40 or not 1 <= count <= 100000:
                raise ValueError("macro report empty or exceeds bounded coverage; split the date range")
        if declared != (pages,count):
            raise ValueError("macro count/pages changed during collection; no complete-history claim")
        records=block["data"]
        if not records or digest(records) in page_hashes:
            raise ValueError("macro history repeated/empty page before completion")
        page_hashes.add(digest(records))
        for row in records:
            day=source_date(row.get(date_field))
            if not day or not limits.get("start_date","0000") <= day <= limits.get("end_date","9999"):
                raise ValueError("macro source ignored the requested date window")
        output.extend(records)
        if page == pages:
            break
    if len(output) != count:
        raise ValueError("macro reported count and pagination mismatch")
    # The industry report joins some indicators to more than one board (LPI
    # currently appears twice). Reconcile the raw count first. Only identical
    # projected rows may collapse; conflicting values for one period must fail.
    raw_count=len(output)
    if query["reportName"] == "RPT_INDUSTRY_INDEX":
        output=list({digest(row):row for row in output}.values())
    keys = {(source_date(r[date_field]),source_date(r.get('TRADE_DATE'))) for r in output} if query['reportName']=='RPT_ECONOMY_DEPOSIT_RESERVE' else {source_date(r[date_field]) for r in output}
    if len(keys) != len(output):
        raise ValueError("macro statistical-period uniqueness mismatch")
    days=[source_date(r[date_field]) for r in output]
    capture.dataset_metadata={"report":query["reportName"],"date_field":date_field,"source_filter":query.get("filter"),
        "source_count":count,"unique_observations":len(output),
        "identical_source_duplicates":raw_count-len(output),
        "duplicate_policy":"exact projected rows only, after raw count reconciliation; conflicting periods rejected",
        "pages_received":pages,"page_size":int(query["pageSize"]),
        "complete_requested_window":True,"complete_history":not any(k in limits for k in ("start_date","end_date")),
        "first_source_period":min(days),"last_source_period":max(days),"requested_window":limits,
        "scope":"all source-available periods and requested report columns",
        "limitation":"counts reconcile the public source; no promise of pre-source history or first-publication vintages"}
    return output
