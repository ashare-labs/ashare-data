"""Explicit close readiness for the bounded SSE three-stock observation.

Two fresh daily reads must agree. An independently fetched SSE snapshot must
report the same day's end-of-trading phase and agree on open/close/prior close.
This is observable readiness, not a guaranteed provider publication time/finality.
"""

from datetime import time
from decimal import Decimal
import json
from urllib.parse import urlencode

from ashare_data.core.model import TZ, require, timestamp
from ashare_data.providers.sse_snapshot import REFERER, snapshot, url
from ashare_data.storage.immutable import immutable_write

POLICY = "sse-ended-and-repeat-daily-agrees.v1"


def daily_candidate(client, security, day):
    # Deliberately do not use Client.get_price's legacy software 15:05 cutoff.
    # A candidate cannot be published until verify() proves this opt-in policy.
    from ashare_data.market.client import KLINE_URL, _bars, _security

    address = (
        KLINE_URL
        + "?"
        + urlencode(dict(symbol=_security(security), scale=240, ma="no", datalen=1023))
    )
    rows, metadata = client.transport.read(address, lambda raw: _bars(raw, 240))
    return [r for r in rows if r["day"] <= day][-120:], metadata


def collect(root, client, values, day, result):
    from ashare_data.observations.daily import _source, now, sha
    from ashare_data.observations.phases import fetch

    result.update(policy=POLICY, securities={})
    for sid in sorted(values):
        sources = result["securities"][sid] = {}
        address = url(sid[:6])
        start = now()
        body = fetch(address, REFERER)
        source = dict(
            url=address,
            request_started_at=start.isoformat(),
            observed_at=now().isoformat(),
            http_status=200,
            sha256=sha(body),
        )
        immutable_write(root / "blobs" / source["sha256"], body)
        sources["sse"] = source
        _, metadata = daily_candidate(client, sid, day)
        sources["daily_repeat"] = _source(root, metadata)


def verify(root, packet):
    from ashare_data.observations.daily import sha
    from ashare_data.market.client import _bars

    proof = packet["close_confirmation"]
    require(
        set(proof) == {"policy", "securities"}
        and proof["policy"] == POLICY
        and set(proof["securities"]) == set(packet["securities"]),
        "OBSERVATION_CLOSE_PROOF",
        "收盘就绪证据不完整",
    )
    for sid, item in packet["securities"].items():
        sources = proof["securities"][sid]
        require(
            set(sources) == {"sse", "daily_repeat"},
            "OBSERVATION_CLOSE_PROOF",
            "须有官方收盘和日线复读",
        )
        bodies = {}
        for role, source in sources.items():
            raw = (root / "blobs" / source["sha256"]).read_bytes()
            require(
                sha(raw) == source["sha256"] and source["http_status"] == 200,
                "OBSERVATION_CORRUPT",
                "收盘原文摘要或HTTP状态不同",
            )
            require(
                timestamp(packet["started_at"])
                <= timestamp(source["request_started_at"])
                <= timestamp(source["observed_at"])
                <= timestamp(packet["completed_at"]),
                "OBSERVATION_CLOCK",
                "收盘源收讫不在采集范围",
            )
            bodies[role] = _bars(raw, 240) if role == "daily_repeat" else json.loads(raw)
        require(
            sources["sse"]["url"] == url(sid[:6])
            and sources["daily_repeat"]["url"] == item["source"]["url"]
            and timestamp(item["source"]["observed_at"])
            <= timestamp(sources["sse"]["request_started_at"])
            and timestamp(sources["sse"]["observed_at"])
            <= timestamp(sources["daily_repeat"]["request_started_at"]),
            "OBSERVATION_CLOSE_PROOF",
            "来源或两次日线读取顺序不符",
        )
        official = snapshot(
            bodies["sse"], sid[:6], packet["signal_day"], sources["sse"]["observed_at"]
        )
        require(
            official["trade_phase"][0] == "E"
            and timestamp(official["source_as_of"]).astimezone(TZ).time() >= time(15),
            "OBSERVATION_WAIT_CLOSE",
            "官方同日行情尚未到交易结束阶段",
        )
        rows = [r for r in bodies["daily_repeat"] if r["day"] <= packet["signal_day"]][-120:]
        require(
            rows == item["rows"],
            "OBSERVATION_WAIT_STABLE",
            "两次新鲜日线读取不同，须等待或调查修订",
        )
        last, prior = item["rows"][-1], item["rows"][-2]
        require(
            Decimal(official["open"]) == Decimal(last["open"])
            and Decimal(official["last"]) == Decimal(last["close"])
            and Decimal(official["reference_close"]) == Decimal(prior["close"]),
            "OBSERVATION_CLOSE_CONFLICT",
            "官方行情与日线的开收盘或前收不一致",
        )
