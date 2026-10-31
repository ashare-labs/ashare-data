"""Bounded pages from the same reviewed Sina macro endpoints used by AKShare.

Offset and page coverage are explicit. A page never claims to be a full history.
JSONP is parsed as data by the pinned pure-Python parser, never evaluated as JS.
"""
import hashlib
from urllib.parse import urlencode

DATASETS={"macro_china_insurance":("fininfo","19"),
          "macro_china_passenger_load_factor":("industry","20"),
          "macro_china_society_traffic_volume":("industry","10"),
          "macro_china_retail_price_index":("price","12")}
PARSER_SHA256="c53d1f7149035569232eab97d2b4089029a98d2b9e16ea744de5ef33f7a29465"


def parse_page(payload, *, offset, requested):
    columns=payload["config"]["all"]
    names=[column[1] for column in columns]
    if len(set(names))!=len(names):
        raise ValueError("duplicate macro source column labels")
    units={column[1]:column[2] for column in columns if len(column)>2}
    sections=payload["data"]
    sections=sections if isinstance(sections,dict) else {"source":sections}
    output=[]
    for section, values in sections.items():
        if not isinstance(values,list):
            raise ValueError("macro source page is not a row list")
        for row in values:
            if len(row)!=len(names):
                raise ValueError("macro source row width differs from schema")
            output.append(dict(zip(names,row),source_section=section))
    count=int(payload["count"])
    page_rows=max((len(v) for v in sections.values()),default=0)
    # Sina may finish a statistical-period group beyond the requested boundary.
    # Keep that overlap; the next source offset still advances by the requested
    # size, and storage deduplicates identical observations across page offsets.
    if page_rows>requested+128 or count<0 or (not page_rows and offset<count):
        raise ValueError("invalid macro page coverage")
    metadata={"source_total":count,"offset":offset,"requested_page_size":requested,
              "section_row_counts":{k:len(v) for k,v in sections.items()},
              "next_offset":offset+requested if offset+requested<count else None,
              "boundary_overlap_possible":page_rows>requested,
              "complete_history":offset==0 and requested>=count,
              "units":units,"publication_time":"not provided; use local first_seen"}
    return output,metadata


def collect(capture, params):
    from ashare_data.providers.free_updates.akshare_loader import ReviewedAKShare
    ak=ReviewedAKShare()
    if hashlib.sha256((ak.root/"utils/demjson.py").read_bytes()).hexdigest()!=PARSER_SHA256:
        raise ValueError("installed macro data parser differs from reviewed source")
    from akshare.utils import demjson
    cate,event=DATASETS[params["dataset"]]
    offset,limit=int(params["offset"]),int(params["limit"])
    if not 0<=offset<=100000 or not 1<=limit<=1000:
        raise ValueError("macro page requires offset 0..100000 and limit 1..1000")
    url="https://quotes.sina.cn/mac/api/jsonp_v3.php/SINAREMOTECALLCALLBACK1601651495761/MacPage_Service.get_pagedata?"
    body=capture.get(url+urlencode({"cate":cate,"event":event,"from":offset,"num":limit,"condition":""}))
    try:
        text=body.decode("utf-8")
    except UnicodeDecodeError:
        text=body.decode("gb18030")
    payload=demjson.decode(text[text.index("{"):text.rindex("}")+1])
    rows,coverage=parse_page(payload,offset=offset,requested=limit)
    if params["dataset"] == "macro_china_passenger_load_factor":
        if coverage["units"] != {"客座率":"%","载运率":"%"}:
            raise ValueError("air-transport source units changed")
        rows=[dict(row,source_units=coverage["units"]) for row in rows]
    capture.dataset_metadata=coverage
    return rows
