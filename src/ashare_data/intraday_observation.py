"""Bounded actual intraday receipts, independent of the morning phase windows."""

import argparse
from datetime import time, timedelta
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from . import phase_observation as phase
from . import intraday_refresh as refresh
from .daily_observation import atomic, now, sha, writer
from .live import KLINE_URL, _bars
from .model import TZ, canonical, require, timestamp
from .storage import immutable_write

LEGACY_SCHEMA = "ad-intraday-observation.v1"
DIAGNOSTIC_SCHEMA = "ad-intraday-observation.v2"
SCHEMA = "ad-intraday-observation.v3"


def continuous(at):
    at = at.astimezone(TZ)
    return time(9, 30) <= at.time() < time(11, 30) or time(13) <= at.time() < time(14, 57)


def specs(day, kind):
    result = phase.specs("admission", day, phase.CONDITIONAL_SCHEMA)
    if kind == "execution":
        for sid in phase.UNIVERSE:
            result["minute_" + sid[:6]] = (
                KLINE_URL
                + "?"
                + urlencode(dict(symbol="sh" + sid[:6], scale=1, ma="no", datalen=10)),
                "https://finance.sina.com.cn/",
            )
        for sid in phase.UNIVERSE:
            result["minute_confirm_" + sid[:6]] = result["minute_" + sid[:6]]
    return result


def assess(packet, bodies):
    day, kind, sources = packet["day"], packet["kind"], packet["sources"]
    end, after = timestamp(packet["completed_at"]), timestamp(packet["not_before"])
    base = {k: v for k, v in sources.items() if not k.startswith("minute_")}
    a = phase.assess("admission", day, base, bodies, phase.CONDITIONAL_SCHEMA)
    blockers = list(a["blockers"])
    rows = a["rows"]
    # v1 reopens must still reproduce their sealed assessment byte for byte.
    # New captures preserve the upstream reason instead of adding a secondary
    # KeyError or converting missing limits to Decimal below.
    current = packet["schema"] in {DIAGNOSTIC_SCHEMA, SCHEMA}
    for row in rows:
        if current and row["blockers"]:
            continue
        try:
            if current:
                require(
                    isinstance(row["evidence"].get("sse_snapshot"), dict)
                    and isinstance(row["quote"], dict),
                    "INTRADAY_CURRENT_STATE",
                    "连续竞价双源当前状态不完整",
                )
            snap = row["evidence"]["sse_snapshot"]
            at = timestamp(snap["source_as_of"])
            quote = row["quote"]
            q_at = timestamp(quote["source_as_of"])
            require(
                row["suspended"] is False
                and snap["trade_phase"] == "T111"
                and after < at <= end
                and after < q_at <= end
                and continuous(at)
                and continuous(q_at)
                and (end - at).total_seconds() <= 90
                and (end - q_at).total_seconds() <= 90,
                "INTRADAY_CURRENT_STATE",
                "须取得启动/意图之后的连续竞价双源状态",
            )
            last = Decimal(snap["last"])
            require(
                Decimal(row["lower_limit"]) < last < Decimal(row["upper_limit"]),
                "INTRADAY_LIMIT_OR_PRICE",
                "涨跌停价或非法报价拒绝",
            )
        except Exception as exc:
            blockers.append(getattr(exc, "code", type(exc).__name__) + ":" + row["security"])
    if kind == "execution":
        eligible = {}
        for row in rows:
            code = row["security"][:6]
            # In v3 validate minute evidence independently of quote limits.
            # Otherwise one clock rejection suppresses a valid minute and
            # invents NO_COMMON_MINUTE, concealing the sole retryable cause.
            # Keep the sealed v2 interpretation unchanged.
            if packet["schema"] == DIAGNOSTIC_SCHEMA and row["blockers"]:
                continue
            try:
                source = sources["minute_" + code]
                if current:
                    require("sha256" in source, "INTRADAY_MINUTE_SOURCE_UNAVAILABLE", code)
                bars = _bars(bodies[source["sha256"]], 1)
                confirm_source = sources["minute_confirm_" + code]
                if current:
                    require("sha256" in confirm_source, "INTRADAY_MINUTE_SOURCE_UNAVAILABLE", code)
                confirmation = {r["day"]: r for r in _bars(bodies[confirm_source["sha256"]], 1)}
                started = timestamp(source["started_at"])
                complete = {}
                for bar in bars:
                    label = timestamp(bar["day"].replace(" ", "T") + "+08:00")
                    if (
                        label.date().isoformat() == day
                        and continuous(label)
                        and label - timedelta(minutes=1) > after
                        and label + timedelta(minutes=1) <= started
                        and (end - label).total_seconds() <= 180
                    ):
                        complete[label.isoformat()] = bar
                        require(
                            confirmation.get(bar["day"]) == bar,
                            "INTRADAY_MINUTE_REVISION",
                            "两次实际回执的已完成分钟不一致",
                        )
                require(complete, "INTRADAY_WAIT_COMPLETE_MINUTE", code)
                eligible[code] = complete
            except Exception as exc:
                blockers.append(getattr(exc, "code", type(exc).__name__) + ":" + row["security"])
        common = (
            set.intersection(*(set(v) for v in eligible.values())) if len(eligible) == 3 else set()
        )
        if not common:
            blockers.append("INTRADAY_NO_COMMON_POST_INTENT_MINUTE")
        else:
            label = max(common)
            for row in rows:
                code = row["security"][:6]
                bar = eligible[code][label]
                # Volume belongs to the selected common minute and does not
                # depend on quote limits. Retain both causes before deciding
                # whether a quote-only refresh can help. Other zero bars keep
                # their existing semantics; do not filter or fall back.
                if packet["schema"] == SCHEMA and int(bar["volume"]) <= 0:
                    blockers.append("INTRADAY_MINUTE_PRICE_OR_VOLUME:" + code)
                if current and row["blockers"]:
                    continue
                price = Decimal(bar["close"]) + Decimal("0.01")
                volume = int(bar["volume"])
                if (
                    not Decimal(row["lower_limit"]) < price < Decimal(row["upper_limit"])
                    or volume <= 0
                ):
                    blockers.append("INTRADAY_MINUTE_PRICE_OR_VOLUME:" + code)
                row["execution"] = dict(
                    minute_end=label,
                    raw_close=bar["close"],
                    match_price=str(price),
                    volume=volume,
                    source_sha256=sources["minute_" + code].get("sha256"),
                    confirmation_sha256=sources["minute_confirm_" + code].get("sha256"),
                    observed_at=sources["minute_" + code]["observed_at"],
                )
    result = dict(
        rows=rows,
        blockers=sorted(set(blockers)),
        ready=not blockers,
        execution_permission=False,
        source_publication_time_verified=False,
        model="post-intent-complete-minute-close-plus-one-tick.v1",
    )
    if current:
        from .intraday_diagnostics import source_diagnostics

        result["source_diagnostics"] = source_diagnostics(packet, bodies)
    return result


def read(root, capture, *, replay_clock=None):
    root = Path(root)
    raw = (root / "packets" / (capture + ".json")).read_bytes()
    require(sha(raw) == capture, "INTRADAY_CORRUPT", "报文摘要不同")
    p = json.loads(raw)
    require(
        p["schema"] in {LEGACY_SCHEMA, DIAGNOSTIC_SCHEMA, SCHEMA}
        and (
            p["evidence_kind"] == "live_local_capture"
            and replay_clock is None
            or replay_clock is not None
            and p["evidence_kind"] == "sealed_response_replay"
            and p.get("replay", {}).get("market_result") is False
        )
        and p["kind"] in {"admission", "execution"},
        "INTRADAY_SCHEMA",
        "类型不符",
    )
    start, end, after = map(timestamp, (p["started_at"], p["completed_at"], p["not_before"]))
    require(
        after <= start <= end <= (replay_clock() if replay_clock else now())
        and (end - start).total_seconds() <= 240
        and start.astimezone(TZ).date().isoformat() == p["day"]
        and continuous(start)
        and continuous(end)
        and (start.astimezone(TZ).hour < 12) == (end.astimezone(TZ).hour < 12),
        "INTRADAY_CLOCK",
        "真实连续竞价阶段及240秒预算不符",
    )
    expected = specs(p["day"], p["kind"])
    require(set(expected) == set(p["sources"]), "INTRADAY_SOURCES", "来源集不同")
    bodies = {}
    previous = start
    receipts = (
        [(s["name"], s) for s in p["responses"]]
        if p["schema"] == SCHEMA
        else list((name, p["sources"][name]) for name in expected)
    )
    for name, s in receipts:
        require(
            name in expected
            and (s["url"], s["referer"]) == expected[name]
            and start <= timestamp(s["started_at"]) <= timestamp(s["observed_at"]) <= end,
            "INTRADAY_SOURCE_IDENTITY",
            name,
        )
        if p["schema"] in {DIAGNOSTIC_SCHEMA, SCHEMA}:
            require(previous <= timestamp(s["started_at"]), "INTRADAY_RECEIPT_ORDER", name)
            previous = timestamp(s["observed_at"])
        if "sha256" in s:
            raw = (root / "blobs" / s["sha256"]).read_bytes()
            require(sha(raw) == s["sha256"] and s["http_status"] == 200, "INTRADAY_CORRUPT", name)
            bodies[s["sha256"]] = raw
        else:
            require("error" in s, "INTRADAY_SOURCE_MISSING", name)
    require(p["assessment"] == assess(p, bodies), "INTRADAY_CORRUPT", "读端重算准入")
    if p["schema"] == SCHEMA:
        refresh.validate(p, bodies, assess, expected)
    return p


def capture(root, kind, day, not_before, *, replay=None, quote_refresh_rounds=refresh.MAX_ROUNDS):
    """The same collector handles live transport or an explicit sealed replay.

    Replay packets have a different evidence kind; normal readers reject them.
    No clock/environment override is exposed on the live CLI.
    """
    root = Path(root)
    require(
        type(quote_refresh_rounds) is int and 0 <= quote_refresh_rounds <= refresh.MAX_ROUNDS,
        "INTRADAY_REFRESH_POLICY",
        "重取报价最多两轮",
    )
    clock = replay.now if replay is not None else now
    fetch = replay.fetch if replay is not None else phase.fetch
    with writer(root):
        started = clock()
        attempt_id = replay.identity[:32] if replay is not None else uuid4().hex
        path = root / "attempts" / (attempt_id + ".json")
        attempt = dict(
            attempt_id=attempt_id,
            day=day,
            kind=kind,
            started_at=started.isoformat(),
            status="started",
            capture_id=None,
            sources={},
            responses=[],
            refresh_policy=dict(name=refresh.POLICY, max_rounds=quote_refresh_rounds),
        )
        if replay is not None:
            attempt["replay"] = replay.provenance
        atomic(path, attempt)
        try:
            after = timestamp(not_before)
            require(
                after <= started
                and after.astimezone(TZ).date().isoformat() == day
                and started.astimezone(TZ).date().isoformat() == day
                and continuous(started),
                "INTRADAY_WINDOW",
                "仅实际当前连续竞价窗口；禁止历史回填或午间成交",
            )
            sources = {}
            bodies = {}
            expected = specs(day, kind)

            def collect(name):
                url, referer = expected[name]
                require((clock() - started).total_seconds() < 225, "INTRADAY_BUDGET", "总预算耗尽")
                item = dict(url=url, referer=referer, started_at=clock().isoformat())
                attempt["pending_source"] = {"name": name, **item}
                atomic(path, attempt)
                try:
                    raw = fetch(url, referer)
                    h = sha(raw)
                    immutable_write(root / "blobs" / h, raw)
                    bodies[h] = raw
                    item.update(sha256=h, http_status=200)
                except Exception as exc:
                    item["error"] = dict(
                        type=type(exc).__name__,
                        message=str(exc),
                        http_status=getattr(exc, "code", None),
                    )
                item["observed_at"] = clock().isoformat()
                sources[name] = item
                attempt["responses"].append(
                    dict(item, name=name, sequence=len(attempt["responses"]))
                )
                attempt.update(sources=sources, pending_source=None)
                atomic(path, attempt)

            for name in expected:
                collect(name)
            rounds = 0
            while True:
                interim = dict(
                    schema=SCHEMA,
                    kind=kind,
                    day=day,
                    not_before=not_before,
                    completed_at=attempt["responses"][-1]["observed_at"],
                    sources=sources,
                )
                status = refresh.state(assess(interim, bodies), rounds, quote_refresh_rounds)
                attempt["refresh"] = status
                atomic(path, attempt)
                if status["status"] != "refresh_quotes":
                    break
                # Re-fetch both quote providers as a group. The old bodies and
                # receipt times remain in the journal; no cached receipt is aged
                # into validity and the existing 225/240 second budgets apply.
                for name in refresh.QUOTE_SOURCES:
                    collect(name)
                rounds += 1
            p = dict(
                schema=SCHEMA,
                kind=kind,
                day=day,
                not_before=not_before,
                attempt_id=attempt_id,
                evidence_kind="live_local_capture" if replay is None else "sealed_response_replay",
                started_at=started.isoformat(),
                completed_at=clock().isoformat(),
                sources=sources,
                responses=attempt["responses"],
                refresh_policy=attempt["refresh_policy"],
            )
            if replay is not None:
                p["replay"] = replay.provenance
            p["assessment"] = assess(p, bodies)
            p["refresh"] = refresh.state(p["assessment"], rounds, quote_refresh_rounds)
            raw = canonical(p)
            cap = sha(raw)
            immutable_write(root / "packets" / (cap + ".json"), raw)
            read(root, cap, replay_clock=clock if replay is not None else None)
            attempt.update(
                capture_id=cap,
                status="observed" if p["assessment"]["ready"] else "blocked",
                blockers=p["assessment"]["blockers"],
                refresh=p["refresh"],
            )
        except Exception as exc:
            attempt.update(
                status="blocked",
                error=dict(code=getattr(exc, "code", type(exc).__name__), message=str(exc)),
            )
            if getattr(exc, "code", None) == "INTRADAY_BUDGET":
                attempt["refresh"] = dict(
                    rounds=attempt.get("refresh", {}).get("rounds", 0),
                    retryable=False,
                    status="budget_exhausted",
                )
        attempt["finished_at"] = clock().isoformat()
        atomic(path, attempt)
        return attempt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--kind", choices=["admission", "execution"], required=True)
    c.add_argument("--day", required=True)
    c.add_argument("--not-before", required=True)
    c.add_argument("--quote-refresh-rounds", type=int, choices=range(3), default=refresh.MAX_ROUNDS)
    sub.add_parser("read").add_argument("--capture", required=True)
    a = parser.parse_args()
    v = (
        read(a.root, a.capture)
        if a.command == "read"
        else capture(
            a.root, a.kind, a.day, a.not_before, quote_refresh_rounds=a.quote_refresh_rounds
        )
    )
    print(json.dumps(v, ensure_ascii=False, indent=2))
    if a.command == "capture":
        raise SystemExit(0 if v["status"] == "observed" else 2)


if __name__ == "__main__":
    main()
