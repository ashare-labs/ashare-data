"""Actual pre-open diagnostics and separately observed opens for the three-stock pilot.

Public fields are preserved, never promoted into unproven ST/action/limit
coverage. An opening price packet does not itself authorize any transaction.
"""

import argparse
from datetime import datetime, time
from decimal import Decimal
import json
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from uuid import uuid4

from .daily_observation import atomic, now, sha, writer
from .model import TZ, canonical, require, timestamp
from .storage import immutable_write
from .transport import MAX_BYTES, _NoRedirect

UNIVERSE = ("600000.XSHG", "600036.XSHG", "601318.XSHG")
SCHEMA = "ad-calendar-phase-observation.v4"
CONDITIONAL_SCHEMA = "ad-calendar-phase-observation.v5"
ACTION_SCHEMAS = {SCHEMA, "ad-calendar-phase-observation.v3"}
SCOPED_SCHEMAS = ACTION_SCHEMAS | {"ad-calendar-phase-observation.v2"}


def specs(kind, day, schema=SCHEMA):
    if schema == CONDITIONAL_SCHEMA:
        # Both phases refresh identity, actions, limits and dated suspensions.
        # Fetch the live quote last, immediately before sealing the opening.
        values = specs("admission", day, SCHEMA)
        from .session_reference import REFERER, url

        for sid in UNIVERSE:
            code = sid[:6]
            del values["em_quote_" + code]
            values["sse_quote_" + code] = (url(code), REFERER)
        quote = values.pop("sina_quotes")
        return {**values, "sina_quotes": quote}
    values = {
        "sina_quotes": (
            "https://hq.sinajs.cn/list=sh600000,sh600036,sh601318",
            "https://finance.sina.com.cn/",
        )
    }
    if kind == "admission":
        d = day.replace("-", "")
        common = {
            "isPagination": "true",
            "pageHelp.pageNo": "1",
            "pageHelp.pageSize": "100",
            "pageHelp.beginPage": "1",
            "pageHelp.endPage": "1",
            "pageHelp.cacheSize": "1",
        }
        for name, p in {
            "sse_suspension": dict(sqlId="GW_PL_JYTS_TFPXX", startStopDate=d, endStopDate=d),
            "sse_action_calendar": dict(
                sqlId="PL_SCRL_SCRLB",
                tradeBeginDate=d,
                tradeEndDate=d,
                order="tradeBeginDate|desc,stockCode|desc",
                bizType="5",
            ),
        }.items():
            values[name] = (
                "https://query.sse.com.cn/commonSoaQuery.do?" + urlencode({**p, **common}),
                "https://www.sse.com.cn/disclosure/dealinstruc/suspension/",
            )
        for name, flag in (("sse_risk_warning", "S"), ("sse_delisting", "P")):
            values[name] = (
                "https://query.sse.com.cn/commonSoaQuery.do?"
                + urlencode(
                    dict(sqlId="PL_SSGSXX_FXJSBGPLB", domesticIndicator=flag, productType="0")
                ),
                "https://www.sse.com.cn/disclosure/listedinfo/announcement/",
            )
        params = dict(
            sortColumns="SUSPEND_START_DATE",
            sortTypes="-1",
            pageSize="500",
            pageNumber="1",
            reportName="RPT_CUSTOM_SUSPEND_DATA_INTERFACE",
            columns="ALL",
            source="WEB",
            client="WEB",
            filter=f"(MARKET=\"全部\")(DATETIME='{day}')",
        )
        base = "https://datacenter-web.eastmoney.com/api/data/v1/get?"
        values["em_suspension"] = (base + urlencode(params), "https://data.eastmoney.com/tfpxx/")
        for security in UNIVERSE:
            code = security[:6]
            if schema in SCOPED_SCHEMAS:
                values["sse_master_" + code] = (
                    "https://query.sse.com.cn/sseQuery/commonQuery.do?"
                    + urlencode(
                        dict(
                            sqlId="COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
                            type="inParams",
                            STOCK_CODE=code,
                            COMPANY_STATUS="2,4,5,7,8",
                            REG_PROVINCE="",
                            CSRC_CODE="",
                            STOCK_TYPE="1",
                            **common,
                        )
                    ),
                    "https://www.sse.com.cn/assortment/stock/list/share/",
                )
            quote_params = {
                "secid": "1." + code,
                "fltt": "2",
                "invt": "2",
                "fields": "f43,f46,f51,f52,f57,f58,f60,f86",
            }
            values["em_quote_" + code] = (
                "https://push2.eastmoney.com/api/qt/stock/get?" + urlencode(quote_params),
                f"https://quote.eastmoney.com/sh{code}.html",
            )
            p = dict(
                params,
                reportName="RPT_SHAREBONUS_DET",
                sortColumns="REPORT_DATE",
                filter=f'(SECURITY_CODE="{code}")',
            )
            values["em_dividend_" + code] = (
                base + urlencode(p),
                f"https://data.eastmoney.com/yjfp/detail/{code}.html",
            )
        if schema in ACTION_SCHEMAS:
            from .action_scope import specs as action_specs

            values.update(action_specs(day, replan=schema == SCHEMA))
    return values


def fetch(url, referer):
    request = Request(
        url, headers={"User-Agent": "ashare-data/calendar-observation", "Referer": referer}
    )
    with build_opener(_NoRedirect()).open(request, timeout=12) as response:
        require(response.status == 200, "SOURCE_HTTP_ERROR", "需完整HTTP 200")
        body = response.read(MAX_BYTES + 1)
    require(len(body) <= MAX_BYTES, "SOURCE_RESPONSE_TOO_LARGE", "响应超过2MiB")
    return body


def quotes(raw):
    values = {}
    for code, body in re.findall(r'var hq_str_sh(\d{6})="([^"]*)";', raw.decode("gb18030")):
        parts = body.split(",")
        require(len(parts) >= 33, "PHASE_QUOTE_SCHEMA", "新浪报价字段不全")
        at = timestamp(parts[30] + "T" + parts[31] + "+08:00")
        values[code + ".XSHG"] = dict(
            name=parts[0],
            open=parts[1],
            previous_close=parts[2],
            source_as_of=at.isoformat(),
            state=parts[32],
        )
    require(set(values) == set(UNIVERSE), "PHASE_SECURITY_SET", "报价证券集不符")
    return values


def assess(kind, day, sources, bodies, schema=SCHEMA):
    rows, diagnostics = [], {}
    quote_rows = {}
    quote_source = sources["sina_quotes"]
    if "sha256" in quote_source:
        try:
            quote_rows = quotes(bodies[quote_source["sha256"]])
        except Exception as exc:
            diagnostics["sina_quotes"] = {"error": str(exc)}
    for name, source in sources.items():
        if name == "sina_quotes":
            continue
        if "sha256" not in source:
            diagnostics[name] = {"error": source["error"]}
            continue
        try:
            value = json.loads(bodies[source["sha256"]])
            if name.startswith("action_"):
                # Full, source-bound interpretation happens in action_scope.
                diagnostics[name] = dict(raw_sha256=source["sha256"])
                continue
            if name.startswith("em_quote_"):
                data = value.get("data") or {}
                require(
                    value.get("rc") == 0
                    and data.get("f57") == name[-6:]
                    and type(data.get("f86")) is int,
                    "PHASE_SOURCE_SCHEMA",
                    "报价标识或时间字段不符",
                )
                diagnostics[name] = dict(
                    fields=data,
                    f86_as_shanghai=datetime.fromtimestamp(data["f86"], TZ).isoformat(),
                    date_scope_verified=False,
                    execution_attestation=False,
                )
                continue
            if name.startswith("sse_quote_") and schema == CONDITIONAL_SCHEMA:
                from .session_reference import snapshot

                diagnostics[name] = dict(
                    fields=snapshot(value, name[-6:], day, source["observed_at"]),
                    execution_attestation=False,
                )
                continue
            if name.startswith("sse_"):
                records = value.get("result")
                require(
                    not value.get("actionErrors")
                    and not value.get("fieldErrors")
                    and isinstance(records, list)
                    and value["pageHelp"]["total"] == len(records),
                    "PHASE_SOURCE_PAGINATION",
                    "上交所列表查询失败或分页不完整",
                )
                key = (
                    "productCode"
                    if name == "sse_suspension"
                    else "INSTRUMENT_ID"
                    if name in {"sse_risk_warning", "sse_delisting"}
                    else "stockCode"
                )
                diagnostics[name] = dict(
                    count=len(records),
                    records=[r for r in records if r.get(key) in {s[:6] for s in UNIVERSE}],
                    absence_is_execution_attestation=False,
                    source_valid_through=None,
                )
                continue
            result = value.get("result") or {}
            require(
                value.get("success") is True and value.get("code") == 0,
                "PHASE_SOURCE_SCHEMA",
                "公开接口返回失败，不能按空列表解释",
            )
            records = result.get("data")
            require(
                isinstance(records, list)
                and result.get("pages") == 1
                and result.get("count") == len(records),
                "PHASE_SOURCE_PAGINATION",
                "响应不完整",
            )
            if name == "em_suspension":
                selected = [
                    r for r in records if r.get("SECURITY_CODE") in {s[:6] for s in UNIVERSE}
                ]
            else:
                selected = records[:3]
            diagnostics[name] = dict(
                count=len(records), records=selected, absence_is_execution_attestation=False
            )
        except Exception as exc:
            diagnostics[name] = {"error": str(exc)}
    for sid in UNIVERSE:
        quote = quote_rows.get(sid)
        fresh = bool(
            quote
            and quote["source_as_of"][:10] == day
            and timestamp(quote["source_as_of"]) <= timestamp(quote_source["observed_at"])
        )
        if kind == "opening" and schema != CONDITIONAL_SCHEMA:
            ready = bool(
                fresh
                and time(9, 30) <= timestamp(quote["source_as_of"]).time() <= time(9, 35)
                and quote["state"] == "00"
                and Decimal(quote["open"]) > 0
            )
            rows.append(
                dict(
                    security=sid,
                    trading_date=day,
                    ready=ready,
                    open=quote["open"] if quote else None,
                    source_as_of=quote["source_as_of"] if quote else None,
                    observed_at=quote_source.get("observed_at"),
                    raw_sha256=quote_source.get("sha256"),
                )
            )
        else:
            # Sina's own datas/hq.js interprets index 32: 02/03 are stop days.
            # Yesterday's code or a name without "ST" cannot certify today's state.
            suspended = (
                quote["state"] in {"02", "03"}
                if fresh and quote["state"] in {"00", "02", "03"}
                else None
            )
            rows.append(
                dict(
                    security=sid,
                    eligible=None,
                    suspended=suspended,
                    is_st=None,
                    lower_limit=None,
                    upper_limit=None,
                    actions_known_absent=False,
                    quote=quote,
                    quote_is_current_day=fresh,
                )
            )
    blockers = []
    if kind == "admission" or schema == CONDITIONAL_SCHEMA:
        blockers = [
            "CURRENT_ELIGIBILITY_UNVERIFIED",
            "CURRENT_ST_STATUS_UNVERIFIED",
            "CURRENT_PRICE_LIMITS_UNVERIFIED",
            "ALL_ACTION_TYPES_COVERAGE_UNVERIFIED",
        ]
        if any(r["suspended"] is not False for r in rows):
            blockers.append("CURRENT_SUSPENSION_UNKNOWN_OR_BLOCKED")
        if schema in SCOPED_SCHEMAS | {CONDITIONAL_SCHEMA}:
            from .calendar_scope import scoped_rows

            rows = scoped_rows(
                day,
                UNIVERSE,
                sources,
                bodies,
                rows,
                action_review=schema in ACTION_SCHEMAS | {CONDITIONAL_SCHEMA},
                replan=schema in {SCHEMA, CONDITIONAL_SCHEMA},
                temporal_phase=kind if schema == CONDITIONAL_SCHEMA else None,
            )
            blockers = sorted({reason for row in rows for reason in row["blockers"]})
    elif not all(r["ready"] for r in rows):
        blockers = ["OPENING_CURRENT_QUOTE_MISSING_OR_BLOCKED"]
    if schema == CONDITIONAL_SCHEMA:
        if kind == "opening":
            for row in rows:
                quote = row["quote"] or {}
                row.update(
                    open=quote.get("open"),
                    source_as_of=quote.get("source_as_of"),
                    observed_at=quote_source.get("observed_at"),
                    raw_sha256=quote_source.get("sha256"),
                    trading_date=day,
                )
        return dict(
            rows=rows,
            diagnostics=diagnostics,
            blockers=blockers,
            preparation_ready=kind == "admission" and not blockers,
            execution_ready=kind == "opening" and not blockers,
            execution_permission=False,
            opening_ready=kind == "opening" and not blockers,
            source_publication_time_verified=False,
            finality_verified=False,
            contract="conditional-intent-and-opening-recheck.v1",
        )
    return dict(
        rows=rows,
        diagnostics=diagnostics,
        blockers=blockers,
        execution_permission=(schema in ACTION_SCHEMAS and kind == "admission" and not blockers),
        opening_ready=kind == "opening" and not blockers,
        source_publication_time_verified=False,
        finality_verified=False,
    )


def read(root, capture_id):
    root = Path(root)
    require(
        isinstance(capture_id, str)
        and len(capture_id) == 64
        and set(capture_id) <= set("0123456789abcdef"),
        "INVALID_ARGUMENT",
        "须指定完整摘要",
    )
    raw = (root / "packets" / (capture_id + ".json")).read_bytes()
    require(sha(raw) == capture_id, "PHASE_CORRUPT", "包摘要不符")
    packet = json.loads(raw)
    require(
        packet["schema"]
        in SCOPED_SCHEMAS | {"ad-calendar-phase-observation.v1", CONDITIONAL_SCHEMA}
        and packet["kind"] in {"admission", "opening"}
        and packet["evidence_kind"] == "live_local_capture",
        "PHASE_CORRUPT",
        "观测类型不符",
    )
    start, end = timestamp(packet["started_at"]), timestamp(packet["completed_at"])
    earliest, latest = (
        (time(8, 30), time(9)) if packet["kind"] == "admission" else (time(9, 30), time(9, 35))
    )
    require(
        start <= end <= now()
        and start.astimezone(TZ).date().isoformat() == packet["day"]
        and end.astimezone(TZ).date().isoformat() == packet["day"]
        and earliest <= start.astimezone(TZ).time() <= end.astimezone(TZ).time() <= latest,
        "PHASE_CLOCK",
        "必须在当天对应阶段实际观测，禁止回填",
    )
    expected = specs(packet["kind"], packet["day"], packet["schema"])
    require(set(expected) == set(packet["sources"]), "PHASE_CORRUPT", "来源集不同")
    bodies = {}
    for name, source in packet["sources"].items():
        require(
            source["url"] == expected[name][0]
            and source["referer"] == expected[name][1]
            and start <= timestamp(source["started_at"]) <= timestamp(source["observed_at"]) <= end,
            "PHASE_CORRUPT",
            "来源或接收时间不符",
        )
        if "sha256" in source:
            body = (root / "blobs" / source["sha256"]).read_bytes()
            require(
                sha(body) == source["sha256"] and source["http_status"] == 200,
                "PHASE_CORRUPT",
                "原文不符",
            )
            bodies[source["sha256"]] = body
        else:
            require("error" in source, "PHASE_CORRUPT", "缺失败原因")
    require(
        packet["assessment"]
        == assess(packet["kind"], packet["day"], packet["sources"], bodies, packet["schema"]),
        "PHASE_CORRUPT",
        "须重新从原文解释，不能手填执行布尔值",
    )
    return packet


def capture(root, kind, day, *, schema=SCHEMA):
    root = Path(root)
    with writer(root):
        started = now()
        attempt_id = uuid4().hex
        path = root / "attempts" / (attempt_id + ".json")
        attempt = dict(
            attempt_id=attempt_id,
            day=day,
            kind=kind,
            started_at=started.isoformat(),
            status="started",
            capture_id=None,
        )
        atomic(path, attempt)
        try:
            require(kind in {"admission", "opening"}, "PHASE_KIND", "阶段不符")
            require(
                schema in {SCHEMA, CONDITIONAL_SCHEMA},
                "PHASE_SCHEMA",
                "须显式选择受支持的新采集契约",
            )
            earliest, latest = (
                (time(8, 30), time(9)) if kind == "admission" else (time(9, 30), time(9, 35))
            )
            require(
                started.astimezone(TZ).date().isoformat() == day
                and earliest <= started.astimezone(TZ).time() <= latest,
                "PHASE_OUTSIDE_REAL_WINDOW",
                "不在当天实际采集窗口",
            )
            sources, bodies = {}, {}
            for name, (url, referer) in specs(kind, day, schema).items():
                item = dict(url=url, referer=referer, started_at=now().isoformat())
                try:
                    body = fetch(url, referer)
                    item.update(sha256=sha(body), http_status=200)
                    bodies[item["sha256"]] = body
                    immutable_write(root / "blobs" / item["sha256"], body)
                except Exception as exc:
                    item["error"] = dict(
                        type=type(exc).__name__,
                        message=str(exc),
                        http_status=getattr(exc, "code", None),
                    )
                item["observed_at"] = now().isoformat()
                sources[name] = item
            packet = dict(
                schema=schema,
                kind=kind,
                day=day,
                attempt_id=attempt_id,
                evidence_kind="live_local_capture",
                started_at=started.isoformat(),
                completed_at=now().isoformat(),
                sources=sources,
                assessment=assess(kind, day, sources, bodies, schema),
            )
            raw = canonical(packet)
            capture_id = sha(raw)
            immutable_write(root / "packets" / (capture_id + ".json"), raw)
            read(root, capture_id)
            attempt.update(
                status="observed" if not packet["assessment"]["blockers"] else "blocked",
                capture_id=capture_id,
                blockers=packet["assessment"]["blockers"],
            )
        except Exception as exc:
            attempt.update(
                status="blocked",
                error=dict(code=getattr(exc, "code", type(exc).__name__), message=str(exc)),
            )
        attempt["finished_at"] = now().isoformat()
        atomic(path, attempt)
        return attempt


def recover(root):
    root = Path(root)
    with writer(root):
        recovered = []
        for path in sorted((root / "attempts").glob("*.json")):
            attempt = json.loads(path.read_bytes())
            if attempt["status"] != "started":
                continue
            matches = [
                p
                for p in (root / "packets").glob("*.json")
                if json.loads(p.read_bytes())["attempt_id"] == attempt["attempt_id"]
            ]
            require(len(matches) <= 1, "PHASE_CORRUPT", "同一尝试出现多个包")
            if matches:
                packet = read(root, matches[0].stem)
                attempt.update(
                    capture_id=matches[0].stem,
                    blockers=packet["assessment"]["blockers"],
                    status="blocked" if packet["assessment"]["blockers"] else "observed",
                )
            else:
                attempt.update(
                    status="interrupted",
                    error={
                        "code": "PHASE_INTERRUPTED",
                        "message": "未完成观测，须在窗口内显式重采",
                    },
                )
            attempt["recovered_at"] = now().isoformat()
            atomic(path, attempt)
            recovered.append(attempt)
        return recovered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("admission", "opening"):
        command = sub.add_parser(name)
        command.add_argument("--day", required=True)
        command.add_argument(
            "--conditional",
            action="store_true",
            help="Explicit v5 conditional preparation and opening recheck",
        )
    sub.add_parser("read").add_argument("--capture", required=True)
    sub.add_parser("recover")
    args = parser.parse_args()
    result = (
        read(args.root, args.capture)
        if args.command == "read"
        else recover(args.root)
        if args.command == "recover"
        else capture(
            args.root,
            args.command,
            args.day,
            schema=CONDITIONAL_SCHEMA if args.conditional else SCHEMA,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command not in {"read", "recover"}:
        raise SystemExit(0 if result["status"] == "observed" else 2)


if __name__ == "__main__":
    main()
