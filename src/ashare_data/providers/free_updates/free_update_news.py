"""Reviewed public news feeds: native IDs and explicit Asia/Shanghai times.

Only the requested latest feed is read. No article links or embedded HTML are
executed; a latest-page sample never claims a complete day or full news archive.
"""
from datetime import datetime
import hashlib
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

NEWS={"stock_info_global_sina":"sina", "stock_info_global_ths":"ths",
      "stock_info_global_em":"eastmoney", "stock_info_global_cls":"cls"}
ZONE=ZoneInfo("Asia/Shanghai")


def publication(value, *, epoch=False):
    if epoch:
        if isinstance(value,bool) or not str(value).isdigit():
            raise ValueError("news epoch must be integer seconds")
        return datetime.fromtimestamp(int(value),ZONE).isoformat()
    parsed=datetime.strptime(value,"%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=ZONE).isoformat()


def parse(name, data):
    source=NEWS[name]
    if source == "sina":
        if data["result"]["status"]["code"] != 0:
            raise ValueError("Sina news source rejected")
        raw=data["result"]["data"]["feed"]["list"]
        fields=("id",None,"rich_text","create_time",None,False)
    elif source == "ths":
        if str(data["code"]) != "200":
            raise ValueError("THS news source rejected")
        raw=data["data"]["list"]
        fields=("id","title","digest","rtime","url",True)
    elif source == "eastmoney":
        if str(data["code"]) != "1":
            raise ValueError("Eastmoney news source rejected")
        raw=data["data"]["fastNewsList"]
        fields=("code","title","summary","showTime",None,False)
    else:
        if data["errno"] != 0:
            raise ValueError("CLS news source rejected")
        raw=data["data"]["roll_data"]
        fields=("id","title","content","ctime",None,True)
    if not isinstance(raw,list) or not raw:
        raise ValueError("empty news feed is not a successful sample")
    identity,title,content,clock,url,epoch=fields
    rows=[]
    for row in raw:
        at=publication(row[clock],epoch=epoch)
        item={"source":source,"source_id":str(row[identity]),"title":row[title] if title else "",
            "content":row[content],"publication_at":at,"日期":at[:10],
            "source_url":row[url] if url else None}
        if source == "eastmoney":
            if not item["source_id"].isdigit():
                raise ValueError("Eastmoney article code changed")
            item["source_url"]="https://finance.eastmoney.com/a/"+item["source_id"]+".html"
        rows.append(item)
    return rows


def collect(capture, name, params):
    if params:
        raise ValueError("reviewed news feed takes no arbitrary parameters")
    source=NEWS[name]
    if source == "sina":
        url="https://zhibo.sina.com.cn/api/zhibo/feed"
        query=dict(page="1",page_size="20",zhibo_id="152",tag_id="0",dire="f",dpc="1",pagesize="20",type="1")
    elif source == "ths":
        url="https://news.10jqka.com.cn/tapp/news/push/stock"
        query=dict(page="1",tag="",track="website")
    elif source == "eastmoney":
        url="https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        query=dict(client="web",biz="web_724",fastColumn="102",sortEnd="",pageSize="200",req_trace="1710315450384")
    else:
        url="https://www.cls.cn/v1/roll/get_roll_list"
        last_time=int(time.time())
        if hasattr(capture, "result"):
            # A captured request's cursor is part of its immutable scope. Do
            # not replace it with today's clock during offline replay.
            receipt=capture.result["receipts"][capture.index]
            last_time=int(parse_qs(urlsplit(receipt["url"]).query)["last_time"][0])
            if not capture.start.timestamp()-1 <= last_time <= capture.end.timestamp():
                raise ValueError("news request cursor outside its recorded live run")
        query=dict(app="CailianpressWeb",category="",last_time=last_time,os="web",refresh_type="1",rn="20",sv="8.4.6")
        # Same public, credential-free request checksum as the reviewed SDK.
        query["sign"]=hashlib.md5(hashlib.sha1(urlencode(query).encode()).hexdigest().encode()).hexdigest()
    data=json.loads(capture.get(url+"?"+urlencode(query)))
    rows=parse(name,data)
    dates=[r["publication_at"] for r in rows]
    capture.dataset_metadata={"source":source,"records_returned":len(rows),"source_count":len(rows),
        "complete_returned_window":True,"complete_history":False,
        "source_window":"one explicitly requested latest feed page; not complete daily coverage",
        "first_publication_at":min(dates),"last_publication_at":max(dates),
        "source_declared_total":data.get("data",{}).get("total"),
        "coverage":"latest source page only; not the whole day or source archive",
        "history_limit":"最新页可能短于一个采集周期；日常完整新闻采集还需分页和游标覆盖验证",
        "timezone":"Asia/Shanghai"}
    return rows


def validate(rows,job,as_of):
    if job["function"] not in NEWS:
        return None
    ids,dates=[],[]
    for row in rows:
        if row.get("source") != NEWS[job["function"]] or not row.get("source_id") or row["source_id"] == "None":
            raise ValueError("news source identity missing/mismatched")
        at=datetime.fromisoformat(row["publication_at"])
        if not at.tzinfo or at.utcoffset()!=ZONE.utcoffset(at) or at.date().isoformat()>as_of or row["日期"]!=at.date().isoformat():
            raise ValueError("news publication timezone/date invalid")
        if not all(isinstance(row.get(k),str) for k in ("title","content")) or not (row["title"].strip() or row["content"].strip()):
            raise ValueError("news title and content are empty/invalid")
        if row.get("source_url") and urlsplit(row["source_url"]).scheme not in {"http","https"}:
            raise ValueError("invalid article URL scheme")
        ids.append(row["source_id"])
        dates.append(at.isoformat())
    if len(set(ids))!=len(ids):
        raise ValueError("duplicate news IDs within a feed")
    return {"contract_reviewed":True,"business_key":["source","source_id"],"unique_keys":len(ids),
        "units":{"title":"text","content":"source text; never executed"},"frequency":"source_news_events",
        "latest_period":max(dates)[:10],"latest_announcement":max(dates),"gaps":[],
        "limitations":["仅验最新一页；持续全日覆盖需要游标/分页，不将一次快讯页当完整每日新闻",
            "发布时点按源时间保留；内容修订按实际接收时间留存，未还原首次发布内容"],"source_rows":len(rows)}
