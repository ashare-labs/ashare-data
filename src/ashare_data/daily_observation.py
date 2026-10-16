"""Current-calendar daily observations; never a trading-state attestation.

One explicit invocation, no scheduler, retries, fallback source or account writes.
Raw responses and receipt timestamps remain separate from their daily labels.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .live import CALENDAR_URL, Client, _calendar, _security
from .model import DataError, TZ, canonical, day as parse_day, require, timestamp
from .storage import immutable_write

SCHEMA = "ad-current-daily-observation.v1"
CLOSE_SCHEMA = "ad-current-daily-observation.v2"
WINDOW = time(17, 45)


def now():
    return datetime.now(timezone.utc)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid4().hex + ".pending")
    with tmp.open("xb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def writer(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("blobs", "packets", "attempts", "comparisons"):
        (root / name).mkdir(exist_ok=True)
    with (root / "observation.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DataError("OBSERVATION_BUSY", "另一显式观测尚未结束") from None
        yield


def _source(root, metadata):
    keys = ("url", "sha256", "request_started_at", "observed_at", "http_status")
    item = {key: metadata[key] for key in keys}
    raw = (root / "http-cache" / "objects" / item["sha256"]).read_bytes()
    require(sha(raw) == item["sha256"], "OBSERVATION_CORRUPT", "源原文摘要不同")
    immutable_write(root / "blobs" / item["sha256"], raw)
    return item


def _open_days(body, start, end):
    year, holidays = _calendar(body)
    require(start.year == end.year == year, "OBSERVATION_CALENDAR_YEAR", "日历年度不覆盖请求")
    return [
        (start + timedelta(days=i)).isoformat()
        for i in range((end - start).days + 1)
        if (start + timedelta(days=i)).weekday() < 5 and start + timedelta(days=i) not in holidays
    ]


def _revisions(packet, prior_packets):
    revisions = []
    for cap, prior in prior_packets:
        changes = []
        for security, item in packet["securities"].items():
            before = {r["day"]: r for r in prior["securities"][security]["rows"]}
            changes += [
                {"security": security, "day": r["day"], "before": before[r["day"]], "after": r}
                for r in item["rows"]
                if r["day"] in before and r != before[r["day"]]
            ]
        if changes:
            revisions.append({"capture_id": cap, "changes": changes})
    return revisions


def read(root, capture_id, *, _seen=None, _cache=None):
    """Verify raw responses again on every offline reopen; do not select latest."""
    root = Path(root)
    _seen = set() if _seen is None else _seen
    _cache = {} if _cache is None else _cache
    if capture_id in _cache:
        return _cache[capture_id]
    require(
        capture_id not in _seen and len(_seen) < 256,
        "OBSERVATION_CORRUPT",
        "修订比较依据循环或过长",
    )
    require(
        isinstance(capture_id, str)
        and len(capture_id) == 64
        and set(capture_id) <= set("0123456789abcdef"),
        "INVALID_ARGUMENT",
        "须固定完整观测摘要",
    )
    raw = (root / "packets" / (capture_id + ".json")).read_bytes()
    require(sha(raw) == capture_id, "OBSERVATION_CORRUPT", "观测包摘要不同")
    packet = json.loads(raw)
    _seen = _seen | {capture_id}
    attempt_id = packet["attempt_id"]
    require(
        len(attempt_id) == 32 and set(attempt_id) <= set("0123456789abcdef"),
        "OBSERVATION_CORRUPT",
        "尝试编号不符",
    )
    attempt = json.loads((root / "attempts" / (attempt_id + ".json")).read_bytes())
    require(
        attempt["attempt_id"] == attempt_id
        and (
            attempt["capture_id"] == capture_id
            or attempt["status"] == "started"
            and attempt["capture_id"] is None
        ),
        "OBSERVATION_CORRUPT",
        "包未被原采集尝试绑定",
    )
    require(
        packet["schema"] in {SCHEMA, CLOSE_SCHEMA} and packet["execution_permission"] is False,
        "OBSERVATION_CORRUPT",
        "观测不能变为执行许可",
    )
    require(
        packet["signal_ready"] == (not packet["revisions"])
        and packet["evidence_kind"] == "live_local_capture"
        and packet["source_publication_time_verified"] is False
        and packet["finality_verified"] is False,
        "OBSERVATION_CORRUPT",
        "质量或修订标记不一致",
    )
    end = timestamp(packet["completed_at"])
    start = timestamp(packet["started_at"])
    observed_day = date.fromisoformat(packet["signal_day"])
    require(
        start <= end <= now()
        and start.astimezone(TZ).date() == observed_day
        and end.astimezone(TZ).date() == observed_day
        and start.astimezone(TZ).time()
        >= (time(15) if packet["schema"] == CLOSE_SCHEMA else WINDOW),
        "OBSERVATION_CLOCK",
        "真实观测时钟不在声明交易日晚间",
    )
    sources = [packet["calendar"]["source"], *[x["source"] for x in packet["securities"].values()]]
    bodies = {}
    for source in sources:
        body = (root / "blobs" / source["sha256"]).read_bytes()
        require(
            sha(body) == source["sha256"] and source["http_status"] == 200,
            "OBSERVATION_CORRUPT",
            "源原文或HTTP状态不同",
        )
        require(
            start
            <= timestamp(source["request_started_at"])
            <= timestamp(source["observed_at"])
            <= end,
            "OBSERVATION_CLOCK",
            "源接收时刻不在实际请求范围",
        )
        bodies[source["sha256"]] = body
    calendar = packet["calendar"]
    require(calendar["source"]["url"] == CALENDAR_URL, "OBSERVATION_CORRUPT", "日历来源不同")
    days = _open_days(
        bodies[calendar["source"]["sha256"]],
        date.fromisoformat(calendar["start"]),
        date.fromisoformat(calendar["end"]),
    )
    require(
        days == calendar["open_days"] and packet["signal_day"] in days,
        "OBSERVATION_CALENDAR",
        "日历原文与投影不同",
    )
    require(
        calendar["next_open"] == next(d for d in days if d > packet["signal_day"]),
        "OBSERVATION_CALENDAR",
        "下一交易日与源日历不同",
    )
    for security, item in packet["securities"].items():
        require(
            security.endswith(".XSHG")
            and item["trading_state"] is None
            and item["corporate_action_coverage"] is None,
            "OBSERVATION_CORRUPT",
            "日线观测不授予状态/公司行动资格",
        )
        symbol = _security(security)
        require(
            item["source"]["url"].startswith(
                "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol="
                + symbol
                + "&scale=240&"
            ),
            "OBSERVATION_CORRUPT",
            "证券或频率来源不同",
        )
        if packet["schema"] == CLOSE_SCHEMA:
            from .live import _bars

            source_rows = _bars(bodies[item["source"]["sha256"]], 240)
        else:
            source_rows = json.loads(bodies[item["source"]["sha256"]])
        rows = [r for r in source_rows if r["day"] <= packet["signal_day"]][-120:]
        require(
            rows == item["rows"]
            and len(rows) == 120
            and [r["day"] for r in rows] == [d for d in days if d <= packet["signal_day"]][-120:],
            "OBSERVATION_HISTORY_GAP",
            "历史必须是完整120个源日历交易日，禁止补行",
        )
    require(
        ("comparison_captures" in packet) == ("comparison_captures" in attempt)
        and ("comparison_captures" in packet)
        == (root / "comparisons" / (attempt_id + ".json")).exists(),
        "OBSERVATION_CORRUPT",
        "不能删除新采集的修订比较依据",
    )
    if "comparison_captures" in packet:
        manifest_raw = (root / "comparisons" / (attempt_id + ".json")).read_bytes()
        manifest = json.loads(manifest_raw)
        require(
            sha(manifest_raw) == packet["comparison_sha256"]
            and manifest == {"attempt_id": attempt_id, "captures": packet["comparison_captures"]}
            and attempt["comparison_captures"] == packet["comparison_captures"],
            "OBSERVATION_CORRUPT",
            "修订比较基准与采集前封存清单不同",
        )
        basis = packet["comparison_captures"]
    else:
        # Legacy receipts are unchanged. Bind them to their original attempt and
        # reconstruct only earlier completed receipts, never future observations.
        basis = []
        for path in sorted((root / "attempts").glob("*.json")):
            prior_attempt = json.loads(path.read_bytes())
            cap = prior_attempt.get("capture_id")
            if cap and cap != capture_id and timestamp(prior_attempt["started_at"]) < start:
                prior_raw = json.loads((root / "packets" / (cap + ".json")).read_bytes())
                if timestamp(prior_raw["completed_at"]) <= start and set(
                    prior_raw["securities"]
                ) == set(packet["securities"]):
                    basis.append(cap)
        basis.sort()
    require(
        isinstance(basis, list) and basis == sorted(set(basis)),
        "OBSERVATION_CORRUPT",
        "比较依据须为唯一有序集合",
    )
    priors = [(cap, read(root, cap, _seen=_seen, _cache=_cache)) for cap in basis]
    require(
        all(
            set(p["securities"]) == set(packet["securities"])
            and timestamp(p["completed_at"]) <= start
            for _, p in priors
        ),
        "OBSERVATION_CORRUPT",
        "比较依据超出观测时点或证券集",
    )
    expected_revisions = _revisions(packet, priors)
    require(
        packet["revisions"] == expected_revisions
        and packet["signal_ready"] == (not expected_revisions),
        "OBSERVATION_CORRUPT",
        "修订派生语义与原始比较数据不同",
    )
    if packet["schema"] == CLOSE_SCHEMA:
        from .close_confirmation import verify

        require(
            attempt.get("close_confirmation") == packet.get("close_confirmation"),
            "OBSERVATION_CLOSE_PROOF",
            "收盘证据须绑定原采集尝试",
        )
        verify(root, packet)
    else:
        require(
            "close_confirmation" not in packet, "OBSERVATION_CLOSE_PROOF", "旧契约不能冒充收盘确认"
        )
    _cache[capture_id] = packet
    return packet


def capture(root, *, securities, signal_day, confirmed_close=False):
    """Collect today's data only. Failure/unfinished attempts remain inspectable."""
    root = Path(root)
    parsed = date.fromisoformat(parse_day(signal_day))
    require(
        isinstance(securities, (tuple, list))
        and 1 <= len(securities) <= 3
        and len(set(securities)) == len(securities),
        "INVALID_ARGUMENT",
        "须为1–3个不同证券",
    )
    for security in securities:
        _security(security)
    with writer(root):
        attempt_id = uuid4().hex
        attempt_path = root / "attempts" / (attempt_id + ".json")
        started = now()
        attempt = {
            "attempt_id": attempt_id,
            "signal_day": signal_day,
            "securities": sorted(securities),
            "started_at": started.isoformat(),
            "status": "started",
            "capture_id": None,
        }
        atomic(attempt_path, attempt)
        try:
            require(
                parsed == started.astimezone(TZ).date(),
                "OBSERVATION_NOT_TODAY",
                "只记录实际当天；历史补采或未来日期不能冒充实时观测",
            )
            require(
                started.astimezone(TZ).time() >= (time(15) if confirmed_close else WINDOW),
                "OBSERVATION_WAIT_PUBLICATION",
                "须在15:00收盘后确认实际数据就绪"
                if confirmed_close
                else "旧入口17:45后观察；这不是源最终发布保证",
            )
            priors = [
                (p.stem, read(root, p.stem)) for p in sorted((root / "packets").glob("*.json"))
            ]
            priors = [(cap, p) for cap, p in priors if set(p["securities"]) == set(securities)]
            basis = [cap for cap, _ in priors]
            manifest = canonical({"attempt_id": attempt_id, "captures": basis})
            immutable_write(root / "comparisons" / (attempt_id + ".json"), manifest)
            attempt["comparison_captures"] = basis
            atomic(attempt_path, attempt)
            client = Client(cache=root / "http-cache", cache_mode="refresh", timeout=12)
            values = {}
            for security in sorted(securities):
                if confirmed_close:
                    from .close_confirmation import daily_candidate

                    rows, provenance = daily_candidate(client, security, signal_day)
                    finality = "CLOSED_PROVISIONAL"
                else:
                    frame = client.get_price(
                        security, frequency="daily", end_date=signal_day, count=120
                    )
                    provenance = frame.attrs["provenance"][0]
                    rows = provenance["source_rows"]
                    finality = frame.attrs["finality"]
                require(
                    len(rows) == 120 and rows[-1]["day"] == signal_day,
                    "OBSERVATION_WAIT_LATEST",
                    "当日收盘数据缺失/晚到，不能沿用昨日水位",
                )
                values[security] = {
                    "rows": rows,
                    "source": _source(root, provenance),
                    "source_available_at": None,
                    "finality": finality,
                    "trading_state": None,
                    "corporate_action_coverage": None,
                }
            first = min(v["rows"][0]["day"] for v in values.values())
            last = (parsed + timedelta(days=7)).isoformat()
            calendar_info = client.calendar()
            calendar_meta = calendar_info["provenance"]
            require(
                date.fromisoformat(first).year
                == date.fromisoformat(last).year
                == calendar_info["year"],
                "OBSERVATION_CALENDAR_YEAR",
                "日历年度不覆盖请求",
            )
            days = [d.isoformat() for d in calendar_info["days"] if first <= d.isoformat() <= last]
            require(
                signal_day in days and any(d > signal_day for d in days),
                "OBSERVATION_CALENDAR",
                "当日及下一开市日须有源日历证据",
            )
            packet = {
                "schema": CLOSE_SCHEMA if confirmed_close else SCHEMA,
                "attempt_id": attempt_id,
                "comparison_captures": basis,
                "comparison_sha256": sha(manifest),
                "signal_day": signal_day,
                "started_at": started.isoformat(),
                "completed_at": now().isoformat(),
                "evidence_kind": "live_local_capture",
                "producer": {"module_sha256": sha(Path(__file__).read_bytes())},
                "calendar": {
                    "start": first,
                    "end": last,
                    "open_days": days,
                    "next_open": next(d for d in days if d > signal_day),
                    "exchange": "XSHG",
                    "source": _source(root, calendar_meta),
                },
                "securities": values,
                "execution_permission": False,
                "source_publication_time_verified": False,
                "finality_verified": False,
            }
            # Only this scope is supported by the SSE calendar observation.
            require(
                all(s.endswith(".XSHG") for s in securities),
                "OBSERVATION_EXCHANGE_SCOPE",
                "不能将SSE日历自动授予深市资格",
            )
            require(
                all(
                    [r["day"] for r in v["rows"]] == [d for d in days if d <= signal_day][-120:]
                    for v in values.values()
                ),
                "OBSERVATION_HISTORY_GAP",
                "缺交易日或出现日历外数据",
            )
            previous = _revisions(packet, priors)
            packet["revisions"] = previous
            packet["signal_ready"] = not previous
            if confirmed_close:
                from .close_confirmation import collect, verify

                proof = attempt["close_confirmation"] = {}
                collect(root, client, values, signal_day, proof)
                packet["close_confirmation"] = proof
                packet["completed_at"] = now().isoformat()
                atomic(attempt_path, attempt)
                verify(root, packet)
            data = canonical(packet)
            cap = sha(data)
            immutable_write(root / "packets" / (cap + ".json"), data)
            read(root, cap)
            attempt.update(status="revision_blocked" if previous else "observed", capture_id=cap)
        except Exception as exc:
            attempt.update(
                status="blocked",
                error={
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": str(exc),
                    "details": getattr(exc, "details", None),
                },
            )
        attempt["finished_at"] = now().isoformat()
        atomic(attempt_path, attempt)
        return attempt


def recover(root):
    """Explicit offline recovery after a killed collector; no network replay."""
    root = Path(root)
    with writer(root):
        packets = {p.stem: read(root, p.stem) for p in (root / "packets").glob("*.json")}
        recovered = []
        for path in sorted((root / "attempts").glob("*.json")):
            attempt = json.loads(path.read_bytes())
            if attempt["status"] != "started":
                continue
            matching = [
                (cap, p) for cap, p in packets.items() if p["attempt_id"] == attempt["attempt_id"]
            ]
            require(len(matching) <= 1, "OBSERVATION_CORRUPT", "中断尝试有多个不一致结果")
            if matching:
                cap, packet = matching[0]
                attempt.update(
                    status="revision_blocked" if packet["revisions"] else "observed",
                    capture_id=cap,
                    finished_at=packet["completed_at"],
                    recovered_at=now().isoformat(),
                )
            else:
                attempt.update(
                    status="interrupted",
                    finished_at=now().isoformat(),
                    error={
                        "code": "OBSERVATION_INTERRUPTED",
                        "message": "采集未完整发布，须显式重新采集",
                    },
                )
            atomic(path, attempt)
            recovered.append(attempt)
        return recovered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("capture")
    fetch.add_argument("--day", required=True)
    fetch.add_argument("--securities", nargs="+", required=True)
    fetch.add_argument(
        "--confirmed-close",
        action="store_true",
        help="After 15:00, require SSE ended phase and identical fresh daily reads",
    )
    view = sub.add_parser("read")
    view.add_argument("--capture", required=True)
    sub.add_parser("recover")
    args = parser.parse_args()
    if args.command == "capture":
        result = capture(
            args.root,
            signal_day=args.day,
            securities=args.securities,
            confirmed_close=args.confirmed_close,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["status"] == "observed" else 2)
    result = recover(args.root) if args.command == "recover" else read(args.root, args.capture)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
