"""Offline assembly of the frozen three-stock minute request and public evidence.

This bounded recipe is reproducible from the delivered acquisition directory;
it uses existing public price/status captures, never credentials or live calls.
"""

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil

from ashare_data import seal_minute_evidence
from ashare_data.historical_market import DATA_SCHEMA, SCHEMA, ordinary_limits
from ashare_data.live import _bars, _security
from ashare_data.model import canonical, require, timestamp

REQUEST_SHA = "be200b710f64d5c81956a3c1c230ea89ab346a00018ce09e619197f254bbcc99"
AMENDMENT_SHA = "a7dec58ce8a7f1930a24f9a73a2161da288ad98c80117e60441aa08ba25945c1"


def read(path):
    return json.loads(path.read_bytes())


def fact(value, refs, reason, *, raw=None, status="source_observed"):
    return {
        "value": value,
        "status": status if value is not None else "unavailable",
        "source_refs": list(dict.fromkeys(refs)),
        "raw_value": raw,
        "reason": reason,
        "historical_available_at": None,
    }


def build(evidence_root, request_root, staging, destination, *, request_amendment=None):
    evidence, request_root = Path(evidence_root).resolve(), Path(request_root).resolve()
    stage = Path(staging).resolve()
    require(not stage.exists(), "DESTINATION_EXISTS", "Do not replace an earlier assembly")
    raw_request = (request_root / "request.json").read_bytes()
    require(
        hashlib.sha256(raw_request).hexdigest() == REQUEST_SHA,
        "MINUTE_REQUEST_IDENTITY",
        "This recipe is only for the frozen request",
    )
    request = json.loads(raw_request)
    symbols, dates = request["scope"]["securities"], request["scope"]["days"]
    stage.mkdir(parents=True)
    excluded_attempts = []
    # Preserve source captures and their relative locators. Failed HTTP attempts
    # remain in the separate acquisition audit, not the successful-source binding.
    for prefix, root in [("request", request_root)] + [
        (n, evidence / n) for n in ("daily", "state", "state-agent", "intervals", "events")
    ]:
        bad = set()
        for receipt in root.rglob("*.receipt.json"):
            if read(receipt).get("status") != 200:
                bad.update({receipt, receipt.with_name(receipt.name.removesuffix(".receipt.json"))})
                excluded_attempts.append(str(Path(prefix) / receipt.relative_to(root)))
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path in bad or path.is_symlink():
                continue
            if path.suffix in {".gz", ".pyc", ".lock", ".wal"} or "__pycache__" in path.parts:
                continue
            dest = stage / prefix / path.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    amendment = None
    if request_amendment is not None:
        raw_amendment = Path(request_amendment).read_bytes()
        require(
            hashlib.sha256(raw_amendment).hexdigest() == AMENDMENT_SHA,
            "MINUTE_REQUEST_IDENTITY",
            "Unexpected consumer amendment",
        )
        amendment = json.loads(raw_amendment)
        require(
            amendment["base_request"]["sha256"] == REQUEST_SHA
            and amendment["request_id"] == request["request_id"],
            "MINUTE_REQUEST_IDENTITY",
            "Amendment changes request identity",
        )
        (stage / "request/request-amendment.json").write_bytes(raw_amendment)
    (stage / "excluded-attempts.json").write_bytes(
        canonical(
            {
                "paths": excluded_attempts,
                "retained_in_acquisition_audit": True,
                "reason": "Non-200 captures are diagnostics, not admitted successful sources",
            }
        )
    )
    statuses = read(stage / "state-agent/summary.json")
    interval = read(stage / "intervals/interval_report.json")
    action_coverage = read(stage / "events/coverage.json")
    action_facts = read(stage / "events/facts.json")
    sse_rule = read(stage / "intervals/sse-rule-report.json")
    szse_rule = read(stage / "intervals/calendar_rule_report.json")
    sessions_supported = (
        sse_rule["status"] == "current_effective_rule_text_and_deferred_attachment_verified"
        and sse_rule["deferred_implementation"]["referenced_clause_intersection"] == []
        and sse_rule["publication"]["effective_date"] <= dates[0]
        and szse_rule["price_limit_rule"]["effective_date"] <= dates[0]
        and all(
            r["sessions"]["status"] == "rule_supported"
            and r["sessions"]["continuous_windows"] == request["scope"]["requested_sessions_local"]
            for r in (sse_rule, szse_rule)
        )
    )
    require(sessions_supported, "MINUTE_SESSION_RULE", "Official session rule evidence differs")
    prices = []
    daily_rows = {}
    for frequency, cache in [
        ("1m", "request/evidence/bounded-public-probe/cache"),
        ("daily", "daily/cache"),
    ]:
        for receipt_path in sorted((stage / cache / "observations").glob("*.json")):
            receipt = read(receipt_path)
            if "CN_MarketDataService.getKLineData" not in receipt["url"]:
                continue
            from urllib.parse import parse_qs, urlsplit

            source_symbol = parse_qs(urlsplit(receipt["url"]).query)["symbol"][0]
            symbol = next(s for s in symbols if _security(s) == source_symbol)
            body = str(Path(cache) / "objects" / receipt["sha256"])
            record = {
                "security": symbol,
                "frequency": frequency,
                "body": body,
                "receipt": str(receipt_path.relative_to(stage)),
            }
            prices.append(record)
            if frequency == "daily":
                daily_rows[symbol] = {r["day"]: r for r in _bars((stage / body).read_bytes(), 240)}
    source_refs = [
        "state-agent/summary.json",
        "state-agent/verification.json",
        "state-agent/raw/baostock-StockBasicInfoAPI.md",
        "state-agent/raw/baostock-stockKData.md",
    ]
    instruments = [
        {
            "security": symbol,
            "board": "main",
            "source_refs": source_refs,
            "initial_listing_date": fact(
                None, source_refs, "Dated membership does not supply initial listing date"
            ),
            "delisting_date": fact(
                None, source_refs, "No null current outDate is used as historical evidence"
            ),
        }
        for symbol in symbols
    ]
    states = []
    for symbol in symbols:
        for label in dates:
            original = next(
                r for r in statuses["states"] if (r["security"], r["date"]) == (symbol, label)
            )
            reference = next(
                r
                for r in statuses["preclose_diagnostics"]
                if (r["security"], r["date"]) == (symbol, label)
            )
            row = {
                "security": symbol,
                "date": label,
                "board": "main",
                "historical_available_at": None,
                "replay_effective_at": None,
                "intraday_halts_verified": False,
            }
            for field in ("listed", "suspended", "is_st"):
                item = original[field]
                refs = source_refs + ["state-agent/" + r["path"] for r in item["source_refs"]]
                refs += [
                    "state-agent/" + r["raw_response_path"]
                    for r in item["source_refs"]
                    if "raw_response_path" in r
                ]
                row[field] = fact(item["value"], refs, item["basis"], raw=item["raw_value"])
            row["listed"]["scope"] = "membership_in_dated_provider_securities_roster"
            row["listed"]["exchange_legal_listing_certified"] = False
            row["previous_session"] = previous = reference["previous_observed_day"]
            daily = next(p for p in prices if p["security"] == symbol and p["frequency"] == "daily")
            raw_close = daily_rows[symbol][previous]["close"]
            row["previous_close"] = fact(
                raw_close,
                [daily["body"], daily["receipt"]],
                "actual predecessor raw daily close; source text unchanged",
                raw=raw_close,
            )
            bao_refs = source_refs + ["state-agent/" + reference["raw_response_path"]]
            row["source_preclose"] = fact(
                reference["source_preclose"],
                bao_refs,
                "BaoStock adjustflag=3 source preclose; compressed outer integrity unverified",
                raw=reference["source_preclose"],
            )
            require(
                Decimal(raw_close) == Decimal(reference["source_preclose"]),
                "MINUTE_REFERENCE_MISMATCH",
                "Independent source preclose disagrees with daily close",
            )
            row["reference_price"] = fact(
                None,
                bao_refs + ["events/coverage.json"],
                "Exchange/ex-right reference basis and full action absence not established",
            )
            for field in ("upper_limit", "lower_limit"):
                row[field] = fact(
                    None,
                    ["events/coverage.json"],
                    "Unproven exchange reference prevents admitted model bands",
                )
            row["rule"] = {"kind": "unknown", "depends_on_st": True, "source_refs": source_refs}
            row["source_claimed_limits"] = {
                "raw_fields": {},
                "source_refs": bao_refs,
                "execution_usable": False,
                "validation": "source_missing",
            }
            upper, lower = ordinary_limits(
                str(Decimal(raw_close).quantize(Decimal("0.01"))), "0.10"
            )
            rule_refs = ["intervals/calendar_rule_report.json"] if symbol.endswith("XSHE") else []
            if symbol.endswith("XSHG") and (stage / "intervals/sse-rule-report.json").exists():
                rule_refs = ["intervals/sse-rule-report.json"]
            row["limit_model_candidate"] = {
                "upper_limit": upper,
                "lower_limit": lower,
                "ratio": "0.10",
                "tick": "0.01",
                "rounding": "ROUND_HALF_UP_minimum_one_tick",
                "assumed_reference": raw_close,
                "source_refs": [daily["body"], daily["receipt"]] + rule_refs,
                "rule_source_available": bool(rule_refs),
                "execution_usable": False,
                "dependencies_unproved": [
                    "source_preclose_is_exchange_limit_reference",
                    "full_relevant_action_absence",
                    "unverified_ST_source_claim_acceptance",
                ],
            }
            row["source_quality"] = {
                "listed": "dated_roster_uncompressed_frame_crc_identity_verified",
                "suspended": "dated_roster_tradeStatus_plus_public_daily_status",
                "is_st": "source_claim_unverified_compressed_outer_integrity_unverified",
                "source_preclose": "diagnostic_only_compressed_outer_integrity_unverified",
                "source_authenticity_verified": False,
                "execution_permission": False,
                "source_refs": source_refs + ["state-agent/public-status-v2/result.json"],
            }
            states.append(row)
    actions = []
    for a in action_facts["linked_prior_boundary_events"]:
        refs = ["events/facts.json"] + [
            "events/" + r[k] for r in a["source_refs"] for k in ("raw", "receipt")
        ]
        actions.append(
            {
                "event_id": a["event_id"],
                "security": a["security"],
                "action_type": "distribution",
                "currency": "CNY",
                "unit": "per_old_eligible_share",
                "record_date": a["record_date"],
                "ex_date": a["ex_date"],
                "publication_date": a["publication_date"],
                "payment_date": a["cash_payment_date"],
                "share_credit_date": a["share_credit_date"],
                "new_share_trading_date": a["new_share_trading_date"],
                "cash_per_share": a["cash_per_old_eligible_share_gross"],
                "bonus_per_share": a["bonus_per_old_share"],
                "transfer_per_share": a["transfer_per_old_share"],
                "ex_price_cash_deduction": None,
                "historical_available_at": None,
                "cash_available_at": None,
                "share_credit_at": None,
                "payment_date_scope": a["payment_scope"],
                "share_credit_scope": a["share_credit_date_status"],
                "new_share_trading_scope": a["new_share_trading_date_status"],
                "source_refs": refs,
            }
        )
    opening = [
        {
            "security": x["security"],
            "date": x["date"],
            "timepoint_0930_volume": x["same_source_intraday"]["volume_0930"],
            "timepoint_0931_volume": x["same_source_intraday"]["volume_0931"],
            "source_refs": [
                "intervals/interval_report.json",
                "intervals/sina-history-decoded.json",
                "intervals/" + x["same_source_intraday"]["source"]["path"],
            ],
            "classification": "contradicted_continuous_only_opening_bar",
        }
        for x in interval["first_bar_comparisons"]
    ]
    calendar_cache = stage / "request/evidence/bounded-public-probe/cache"
    sse_receipt = next(
        p
        for p in (calendar_cache / "observations").glob("*.json")
        if read(p)["url"].endswith("/closed/")
    )
    sse_raw = "request/evidence/bounded-public-probe/cache/objects/" + read(sse_receipt)["sha256"]
    calendars = [
        {
            "market": "XSHG",
            "body": sse_raw,
            "receipt": str(sse_receipt.relative_to(stage)),
            "source_refs": [
                sse_raw,
                str(sse_receipt.relative_to(stage)),
                "intervals/sse-rule-report.json",
            ],
        },
        {
            "market": "XSHE",
            "body": "intervals/raw/szse-month-october.json",
            "receipt": "intervals/raw/szse-month-october.json.receipt.json",
            "source_refs": [
                "intervals/raw/szse-month-october.json",
                "intervals/calendar_rule_report.json",
            ],
        },
    ]
    checks = [
        (
            "obtained",
            "AD",
            "Frozen request identity and raw source provenance preserved",
            ["request/request.json"],
        ),
        (
            "obtained",
            "AD",
            "Official SSE annual and SZSE monthly calendars cover predecessor/successor and weekend",
            [sse_raw, "intervals/calendar_rule_report.json", "intervals/sse-rule-report.json"],
        ),
        (
            "obtained",
            "AD",
            "Requested session grid is separate from source interval evidence; see exchange-specific rules",
            ["intervals/calendar_rule_report.json", "intervals/sse-rule-report.json"],
        ),
        (
            "blocked",
            "AD/source",
            "All six 09:31 bars equal the separate 09:30 plus 09:31 source buckets; remaining boundaries unverified",
            ["intervals/interval_report.json", "intervals/amount-consistency-supplement.json"],
        ),
        (
            "requires_explicit_model",
            "AD/parent",
            "Raw decimal OHLCV/amount preserved; share unit supported, absolute amount currency unit not independently documented",
            ["intervals/interval_report.json", "intervals/amount-consistency-supplement.json"],
        ),
        (
            "requires_explicit_model",
            "AQ",
            "No historical first-publication/revision watermark; null is retained",
            ["request/request.json"],
        ),
        (
            "requires_explicit_model",
            "AD/AQ",
            "All six dated membership/tradeStatus/ST observations obtained; ST source claim keeps unverified outer frame integrity",
            ["state-agent/summary.json", "state-agent/verification.json"],
        ),
        (
            "blocked",
            "AD/source",
            "Six previous raw closes agree with source preclose; exchange/ex-right basis remains unknown; 10% bands are non-admitted candidates",
            ["state-agent/summary.json", "events/coverage.json"],
        ),
        (
            "requires_explicit_model" if amendment else "consumer_pending",
            "AQ/parent",
            "AQ r2 supplies an opt-in state replay encoding, default off and pending independent review; null source clocks and missing facts remain blocked"
            if amendment
            else "Fixed consumer requires separately reviewed explicit state replay",
            ["request/request-amendment.json"] if amendment else ["request/request.json"],
        ),
        (
            "requires_explicit_model",
            "parent/AQ",
            "Daily source status and bounded SSE halt-list absence do not certify no intraday halt",
            ["state/sse-halts-20261009.json", "state/sse-halts-20261012.json"],
        ),
        (
            "blocked",
            "AD/source",
            "287 rows/17 pages have no nonnull record/ex/pay milestone in range; missing credit/tradability and other action domains prevent full absence",
            ["events/coverage.json", "events/facts.json"],
        ),
        (
            "consumer_pending",
            "AQ",
            "Actual real-script execution must wait for admissible evidence and model decisions",
            ["request/request.json"],
        ),
    ]
    extension = {
        "schema": "ad.minute-source-evidence.v1",
        "request_id": request["request_id"],
        "securities": symbols,
        "dates": dates,
        "sessions": request["scope"]["requested_sessions_local"],
        "calendar_range": ["2026-10-08", "2026-10-13"],
        "calendars": calendars,
        "price_sources": prices,
        "opening_comparisons": opening,
        "historical_pit": False,
        "execution_permission": False,
        "actions": {
            "start": "2026-10-08",
            "end": "2026-10-12",
            "absence_proved": False,
            "complete": False,
            "source_refs": ["events/coverage.json", "events/facts.json"],
            "unknown_domains": action_coverage["absence_boundary"],
        },
        "action_queries": [
            {
                "report": q["report"],
                "security": q["security"],
                "pages": [
                    {"body": "events/" + p["raw"], "receipt": "events/" + p["receipt"]}
                    for p in q["page_sources"]
                ],
            }
            for q in action_coverage["adopted_reports"]
        ],
        "assessments": [
            {"gate_id": f"G{i:02d}", "status": s, "owner": o, "reason": reason, "source_refs": refs}
            for i, (s, o, reason, refs) in enumerate(checks)
        ],
    }
    data = {
        "schema": DATA_SCHEMA,
        "scope": {
            "request_id": request["request_id"],
            "securities": symbols,
            "days": dates,
            "source_scope_expanded": False,
        },
        "states": states,
        "instruments": instruments,
        "universes": [],
        "actions": actions,
        "minute_evidence": extension,
    }
    if amendment:
        extension["consumer_contract"] = {
            "amendment_ref": "request/request-amendment.json",
            "aq_candidate_sha": amendment["aq_candidate_sha"],
            "state_replay_default": False,
            "source_clocks_modelled": False,
            "historical_pit_verified": False,
            "evidence_admitted": False,
        }
    (stage / "data.json").write_bytes(canonical(data))
    # Fix acquisition completion to the maximum actual capture, never build time.
    clocks = []
    for path in stage.rglob("*.json"):
        try:
            item = read(path)
            if isinstance(item, dict):
                for key in ("captured_end", "observed_at", "finished_at"):
                    if item.get(key):
                        clocks.append(timestamp(item[key]))
        except (ValueError, TypeError):
            pass
    files = [
        {
            "path": str(p.relative_to(stage)),
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(stage.rglob("*"))
        if p.is_file()
    ]
    inventory = {
        "schema": SCHEMA,
        "data": "data.json",
        "files": files,
        "received_at": max(clocks).isoformat(),
        "historical_pit": False,
        "execution_permission": False,
        "source_request_sha256": REQUEST_SHA,
    }
    (stage / "inventory.json").write_bytes(canonical(inventory))
    return seal_minute_evidence(stage, destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--request-root", required=True)
    parser.add_argument("--staging", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--request-amendment")
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.evidence_root,
                args.request_root,
                args.staging,
                args.destination,
                request_amendment=args.request_amendment,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
