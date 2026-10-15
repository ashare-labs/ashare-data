"""SSE public page's exact stock snapshot fields; no provider fallback."""

from datetime import datetime
import re

from .model import TZ, number, require, timestamp

FIELDS = "code,name,prev_close,open,last,up_limit,down_limit,tradephase"
REFERER = "https://www.sse.com.cn/market/price/trends/"


def url(code):
    return "https://yunhq.sse.com.cn:32042/v1/sh1/list/self/" + code + "?select=" + FIELDS


def snapshot(value, code, day, received_at):
    """Field order and phase letters come from SSE search_HQ_2021.js.

    Snapshot time is not a publication SLA. Static D reference/limits are
    separately checked against D-1 close, eligibility, actions and price rules.
    """
    require(
        value.get("total") == 1
        and value.get("begin") == 0
        and value.get("end") == 1
        and isinstance(value.get("list"), list)
        and len(value["list"]) == 1,
        "SSE_SNAPSHOT_INCOMPLETE",
        "官方快照必须精确返回一只证券",
    )
    row = value["list"][0]
    require(
        isinstance(row, list) and len(row) == 8 and row[0] == code,
        "SSE_SNAPSHOT_IDENTITY",
        "官方快照字段或代码不符",
    )
    require(
        type(value.get("date")) is int
        and len(str(value["date"])) == 8
        and type(value.get("time")) is int
        and 0 <= value["time"] <= 235959,
        "SSE_SNAPSHOT_CLOCK",
        "官方快照须含整数交易日和时间",
    )
    at = datetime.strptime(
        str(value["date"]) + str(value["time"]).zfill(6), "%Y%m%d%H%M%S"
    ).replace(tzinfo=TZ)
    receipt = timestamp(received_at)
    require(
        at.date().isoformat() == day and at <= receipt,
        "SSE_SNAPSHOT_CLOCK",
        "官方快照不是当日或晚于收讫",
    )
    require(
        isinstance(row[7], str) and re.fullmatch(r"[SCDTBEPMNU][01]{3}", row[7].strip()),
        "SSE_SNAPSHOT_PHASE",
        "未知官方交易阶段",
    )
    return dict(
        code=code,
        name=row[1],
        reference_close=str(number(row[2])),
        open=str(number(row[3])),
        last=str(number(row[4])),
        upper_limit=str(number(row[5])),
        lower_limit=str(number(row[6])),
        trade_phase=row[7].strip(),
        source_as_of=at.isoformat(),
        observed_at=receipt.isoformat(),
    )
