"""Bounded market facts and reviewed actions for the three-security pilot.

An empty daily calendar is never an action certificate. V3 requires the sealed
issuer review and its freshly rechecked sources; older schemas keep their
original blocked interpretation. No all-market or indefinite scope is implied.
"""

from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
import json

from ashare_data.core.model import TZ, number, require, timestamp

FIRST_DAY = "2026-10-13"
LAST_DAY = "2026-10-14"
RULE_VERSION = "SSE-2026-07-06:3.3.11,3.3.13,3.3.17,4.3.1-3"
RULE_URL = "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml"


def sse_rows(value):
    rows = value.get("result")
    require(
        not value.get("actionErrors")
        and not value.get("fieldErrors")
        and isinstance(rows, list)
        and value["pageHelp"]["total"] == len(rows),
        "SCOPE_INCOMPLETE_SSE_LIST",
        "不完整列表不能证明缺席",
    )
    return rows


def cash_inventory(value, code, day, *, coverage_from=None):
    """Actual dated cash rows remain events; proposals are not implementations."""
    result = value["result"]
    records = result["data"]
    require(
        value["success"] is True
        and value["code"] == 0
        and result["pages"] == 1
        and result["count"] == len(records),
        "SCOPE_CASH_PAGINATION",
        "现金事件响应不完整",
    )
    events, proposals, unresolved, excluded = [], [], [], []
    for r in records:
        require(r["SECURITY_CODE"] == code, "SCOPE_SECURITY", "不能接受被忽略的按证券筛选")
        if r["ASSIGN_PROGRESS"] != "实施分配":
            proposals.append(
                {k: r.get(k) for k in ("REPORT_DATE", "ASSIGN_PROGRESS", "PLAN_NOTICE_DATE")}
            )
            continue
        record, ex = r.get("EQUITY_RECORD_DATE"), r.get("EX_DIVIDEND_DATE")
        if coverage_from and record and ex and record[:10] < ex[:10] < coverage_from:
            # Explicitly outside this account's finite bootstrap; preserve the
            # exclusion instead of demanding historic payment dates/cleanups.
            excluded.append(dict(record_date=record[:10], ex_date=ex[:10]))
            continue
        if not record or not ex or not r.get("NOTICE_DATE"):
            unresolved.append("IMPLEMENTED_EVENT_DATES_MISSING")
            continue
        record, ex = record[:10], ex[:10]
        require(record < ex and r["NOTICE_DATE"][:10] <= record, "SCOPE_CASH_DATES", "实施日期不符")
        # Past share actions precede this zero-genesis account, but any current
        # share/price event remains unsupported regardless of entitlement.
        shares = any(
            Decimal(str(r.get(k) or 0)) != 0 for k in ("BONUS_IT_RATIO", "BONUS_RATIO", "IT_RATIO")
        )
        if ex >= FIRST_DAY and shares:
            unresolved.append("RELEVANT_SHARE_ACTION_UNSUPPORTED")
        if ex < FIRST_DAY and shares:
            continue
        cash = Decimal(str(r.get("PRETAX_BONUS_RMB") or 0)) / 10
        if cash > 0:
            events.append(
                dict(
                    type="cash_dividend",
                    record_date=record,
                    ex_date=ex,
                    cash_per_share=str(cash),
                    pay_date=None,
                    notice_date=r["NOTICE_DATE"][:10],
                    record_before_pilot_genesis=record < FIRST_DAY,
                )
            )
        if ex >= day:
            unresolved.append("CURRENT_OR_FUTURE_PRICE_ACTION_REVIEW_REQUIRED")
    result = dict(
        events=events,
        proposals=proposals,
        unresolved=sorted(set(unresolved)),
        entitlement_owner="AQ native account genesis and record checkpoints",
        cash_history_complete=True,
    )
    if coverage_from:
        result.update(coverage_from=coverage_from, excluded_earlier_events=excluded)
    return result


def scoped_rows(
    day,
    universe,
    sources,
    bodies,
    diagnostic_rows,
    *,
    action_review=False,
    replan=False,
    temporal_phase=None,
):
    from ashare_data.observations.action_baseline import date_scope

    first_day, last_day = date_scope(replan)

    def value(name):
        source = sources[name]
        require("sha256" in source, "SCOPE_SOURCE_MISSING", name)
        return json.loads(bodies[source["sha256"]])

    output = []
    for original in diagnostic_rows:
        row = dict(original)
        sid, code = row["security"], row["security"][:6]
        reasons, evidence = [], {}
        row.update(rule_version=None, reference_close=None)
        if not first_day <= day <= last_day:
            reasons.append("PILOT_DATE_SCOPE_EXCEEDED")
        try:
            records = sse_rows(value("sse_master_" + code))
            require(
                len(records) == 1 and records[0]["A_STOCK_CODE"] == code,
                "SCOPE_MASTER_FILTER",
                "证券身份必须唯一精确匹配",
            )
            master = records[0]
            row["eligible"] = bool(
                master["STOCK_TYPE"] == "1"
                and master["LIST_BOARD"] == "1"
                and master["DELIST_DATE"] == "-"
                and master["LIST_DATE"] < "20260101"
            )
            evidence["master"] = master
        except Exception as exc:
            reasons.append("CURRENT_IDENTITY_UNVERIFIED:" + str(exc))
        try:
            risk = sse_rows(value("sse_risk_warning"))
            delisting = sse_rows(value("sse_delisting"))
            row["is_st"] = any(r["INSTRUMENT_ID"] == code for r in risk)
            if any(r["INSTRUMENT_ID"] == code for r in delisting):
                row["eligible"] = False
            evidence["risk_list_semantics"] = (
                "current official risk-warning board, SSE rule 4.4.2; received in this day's pre-open window"
                if temporal_phase != "opening"
                else "current official risk-warning board, SSE rule 4.4.2; rechecked in this day's opening window"
            )
        except Exception as exc:
            reasons.append("CURRENT_RISK_LIST_UNVERIFIED:" + str(exc))
        try:
            suspension = sse_rows(value("sse_suspension"))
            official_suspension = any(r["productCode"] == code for r in suspension)
            # Positive suspension always blocks; stale provider state never
            # becomes a positive assertion of current-day normal trading.
            if official_suspension:
                row["suspended"] = True
            if temporal_phase is not None:
                other = value("em_suspension")
                result = other["result"]
                require(
                    other["success"] is True
                    and other["code"] == 0
                    and result["pages"] == 1
                    and isinstance(result["data"], list)
                    and result["count"] == len(result["data"]),
                    "SCOPE_SUSPENSION_INCOMPLETE",
                    "停复牌快照不完整",
                )
                scheduled = official_suspension or any(
                    r["SECURITY_CODE"] == code for r in result["data"]
                )
                row["scheduled_suspension"] = scheduled
                require(
                    not scheduled and row["suspended"] is not True,
                    "SCOPE_SUSPENSION_BLOCKED",
                    "已有停牌记录，不能准备或成交",
                )
                evidence["suspension_semantics"] = (
                    "No selected security in two complete dated suspension snapshots; "
                    "absence is not a live normal-trading attestation."
                )
            if temporal_phase != "admission":
                require(
                    row["quote_is_current_day"] and row["suspended"] is False,
                    "SCOPE_CURRENT_QUOTE",
                    "当前日正常报价未实际可见",
                )
            if temporal_phase == "opening":
                quote = row["quote"]
                at = timestamp(quote["source_as_of"])
                receipt = timestamp(sources["sina_quotes"]["observed_at"])
                require(
                    time(9, 30) <= at.time() <= time(9, 35)
                    and at <= receipt
                    and (receipt - at).total_seconds() <= 300
                    and number(quote["open"]) > 0,
                    "SCOPE_OPENING_NOT_OBSERVED",
                    "成交前须实际观察当日开盘及正常状态",
                )
        except Exception as exc:
            reasons.append("CURRENT_SUSPENSION_UNVERIFIED:" + str(exc))
        try:
            if temporal_phase is None:
                q = value("em_quote_" + code)
                data = q["data"]
                at = datetime.fromtimestamp(data["f86"], TZ)
                receipt = timestamp(sources["em_quote_" + code]["observed_at"])
                require(
                    q["rc"] == 0
                    and data["f57"] == code
                    and at.date().isoformat() == day
                    and at <= receipt
                    and (receipt - at).total_seconds() <= 300,
                    "SCOPE_LIMIT_DATE",
                    "正式限价字段尚无当前日有效报价回执",
                )
                ref, upper, lower = (Decimal(str(data[k])) for k in ("f60", "f51", "f52"))
            else:
                from ashare_data.providers.sse_snapshot import snapshot

                source = sources["sse_quote_" + code]
                data = snapshot(value("sse_quote_" + code), code, day, source["observed_at"])
                at, receipt = timestamp(data["source_as_of"]), timestamp(source["observed_at"])
                ref, upper, lower = (
                    Decimal(data[k]) for k in ("reference_close", "upper_limit", "lower_limit")
                )
                require(
                    data["trade_phase"][0] not in "PMN",
                    "SCOPE_SSE_HALT",
                    "官方快照停牌/熔断阻止准备和执行",
                )
                if temporal_phase == "opening":
                    require(
                        data["trade_phase"] == "T111"
                        and time(9, 30) <= at.time() <= time(9, 35)
                        and (receipt - at).total_seconds() <= 300,
                        "SCOPE_SSE_OPENING",
                        "成交前须取得官方当日连续竞价状态",
                    )
                    require(
                        Decimal(data["open"]) == number(row["quote"]["open"]),
                        "SCOPE_OPENING_PRICE_CONFLICT",
                        "官方与新浪当日开盘价冲突",
                    )
                evidence["sse_snapshot"] = data
            require(
                row["eligible"] is True
                and row["is_st"] is False
                and ref > 0
                and upper == (ref * Decimal("1.1")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                and lower == (ref * Decimal(".9")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP),
                "SCOPE_LIMIT_RULE",
                "普通主板限价与现行规则或身份不同",
            )
            row.update(
                lower_limit=str(lower),
                upper_limit=str(upper),
                reference_close=str(ref),
                rule_version=RULE_VERSION,
            )
            evidence["limit_rule_url"] = RULE_URL
            evidence["limits_as_of"] = at.isoformat()
            if temporal_phase is not None:
                evidence["limits_semantics"] = dict(
                    effective_trade_day=day,
                    source="SSE public stock snapshot",
                    snapshot_as_of=at.isoformat(),
                    observed_at=receipt.isoformat(),
                    rule="same-day-session-parameters.v1",
                    publication_time_verified=False,
                    expiry="recheck at opening; reject changed parameters",
                )
        except Exception as exc:
            reasons.append("CURRENT_LIMITS_UNVERIFIED:" + str(exc))
        try:
            inventory = cash_inventory(
                value("em_dividend_" + code),
                code,
                day,
                coverage_from="2026-01-01" if action_review else None,
            )
            row["cash_inventory"] = inventory
            reasons.extend(inventory["unresolved"])
        except Exception as exc:
            reasons.append("SCOPED_CASH_EVENTS_UNVERIFIED:" + str(exc))
        # Action review is deliberately not inferred from absence in the hidden
        # SSE daily calendar, stock name, or old dividend rows. This is an exact
        # remaining bootstrap dependency, not a demand for unlimited history.
        row["action_scope"] = dict(
            from_day=first_day,
            through_day=last_day,
            security=sid,
            relevant_types=[
                "cash_entitlement",
                "price_adjustment",
                "share_transformation",
                "code_change",
            ],
            price_share_code_review=None,
            actions_known_absent=False,
            missing="Issuer implementation texts / explicit effective dates covering this pilot interval; establish current pending-action inventory once, then verify new notices per decision.",
        )
        if action_review:
            try:
                from ashare_data.observations.action_baseline import assess_security

                scope, inventory = assess_security(
                    code, day, sources, bodies, row["cash_inventory"], replan=replan
                )
                row["action_scope"] = scope
                row["cash_inventory"] = inventory
            except Exception as exc:
                reasons.append("SCOPED_PRICE_SHARE_CODE_REVIEW_MISSING:" + sid + ":" + str(exc))
        else:
            # v2 evidence remains unchanged and cannot acquire v3 coverage.
            reasons.append("SCOPED_PRICE_SHARE_CODE_REVIEW_MISSING:" + sid)
        if (
            row["eligible"] is not True
            or row["is_st"] is not False
            or (
                row["suspended"] is not False
                if temporal_phase != "admission"
                else row["suspended"] is True
            )
        ):
            reasons.append("SECURITY_NOT_ADMITTED:" + sid)
        row["evidence"] = evidence
        row["blockers"] = reasons
        output.append(row)
    return output
