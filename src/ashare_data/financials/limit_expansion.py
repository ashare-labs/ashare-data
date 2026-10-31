"""Anonymous historical reference fields, using the existing resumable engine.

No SDK/account, Store/database writes, copied daily bars or inferred limits.
The provider may generate its reported limits; upstream provenance is unknown.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler

from ashare_data.financials import expansion as e
from ashare_data.providers.zzshare import _json, _rows, _value

KIND = "zzshare_daily_reference"
ENDPOINT = "https://api.zizizaizai.com/v3/market/kline/day/"
MAX_DAYS = 1000
MAX_BODY = 2 * 1024 * 1024
FIELDS = ("high_limit", "low_limit", "is_paused", "is_st", "factor")
NORMALIZER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
PROJECTION_SHA256 = hashlib.sha256(
    (Path(__file__).parents[1] / "providers/zzshare.py").read_bytes()
).hexdigest()


def scope(symbol, start, end):
    e.security(symbol)
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if not 0 <= (b - a).days < MAX_DAYS:
        raise ValueError("Reference request must cover 1–1000 natural days")
    return {
        "security": e.security(symbol),
        "source_security": symbol[2:] + (".SH" if symbol.startswith("sh") else ".SZ"),
        "start_date": start,
        "end_date": end,
        "mode": 0,
        "limit": MAX_DAYS,
    }


def url_for(symbol, start, end):
    s = scope(symbol, start, end)
    return (
        ENDPOINT
        + s["source_security"]
        + "?"
        + urlencode(
            {
                "candle_mode": 0,
                "get_type": "range",
                "start_date": start.replace("-", ""),
                "end_date": end.replace("-", ""),
                "limit": MAX_DAYS,
            }
        )
    )


def validate_url(url):
    p = urlparse(url)
    if not url.startswith(ENDPOINT) or p.fragment or p.username or p.password:
        raise ValueError("Only explicit anonymous zzshare daily endpoint is allowed")
    code = p.path.rsplit("/", 1)[-1]
    if not re.fullmatch(r"\d{6}\.(SH|SZ)", code):
        raise ValueError("Invalid source code")
    q = parse_qs(p.query)
    if set(q) != {"candle_mode", "get_type", "start_date", "end_date", "limit"} or any(
        len(v) != 1 for v in q.values()
    ):
        raise ValueError("Unexpected or duplicated request parameter")
    symbol = ("sh" if code.endswith("SH") else "sz") + code[:6]
    start, end = (e.day(q[k][0]) for k in ("start_date", "end_date"))
    if url != url_for(symbol, start, end):
        raise ValueError("Request is not a canonical unadjusted bounded range")
    return scope(symbol, start, end)


def prepare(root, baseline_root, start="2010-01-01", end="2026-10-19"):
    root = Path(root).resolve()
    baseline_root, base, _ = e.load_batch(baseline_root)
    if not (base["target_start"] <= start <= end <= base["target_end"]):
        raise ValueError("Reference window must fit the fixed baseline")
    if root == baseline_root:
        raise ValueError("Reference batch requires an independent directory")
    if (root / "manifest.json").exists():
        _, m, tasks = e.load_batch(root)
        if (m.get("target_start"), m.get("target_end"), m.get("baseline_batch")) != (
            start,
            end,
            str(baseline_root),
        ) or m["source_families"] != [KIND]:
            raise ValueError("Existing reference batch identity changed")
        return m
    baseline_raw = (baseline_root / "universe.json").read_bytes()
    universe = json.loads(baseline_raw)
    tasks, codes = [], set()
    for instrument in universe["instruments"]:
        a = max(date.fromisoformat(start), date.fromisoformat(instrument["ipo"]))
        b = date.fromisoformat(end)
        if instrument["out"]:
            b = min(b, date.fromisoformat(instrument["out"]) - timedelta(days=1))
        while a <= b:
            last = min(b, a + timedelta(days=MAX_DAYS - 1))
            symbol = instrument["symbol"]
            tasks.append(
                {
                    "id": KIND + "-" + symbol + "-" + a.isoformat(),
                    "kind": KIND,
                    "symbol": symbol,
                    "ipo": instrument["ipo"],
                    "out": instrument["out"],
                    "board": instrument["board"],
                    "start": a.isoformat(),
                    "end": last.isoformat(),
                    "url": url_for(symbol, a.isoformat(), last.isoformat()),
                }
            )
            codes.add(symbol)
            a = last + timedelta(days=1)
    # Validate both exchanges, exited securities, renamed securities and recent IPOs first.
    pilots = ("sh600000", "sz000001", "sh600656", "sh600849", "sz000022", "sh601975", "sz001367")
    rank = {s: i for i, s in enumerate(pilots)}
    tasks.sort(key=lambda t: (rank.get(t["symbol"], 100), t["symbol"], t["start"]))
    task_raw = e.encoded(tasks)
    root.mkdir(parents=True, exist_ok=True)
    e.immutable(root / "universe.json", baseline_raw)
    e.immutable(root / "tasks.json", task_raw)
    m = {
        "schema": e.SCHEMA,
        "created_at": e.now(),
        "baseline_batch": str(baseline_root),
        "target_start": start,
        "target_end": end,
        "baseline_codes": len(universe["instruments"]),
        "target_codes": len(codes),
        "planned_tasks": len(tasks),
        "source_families": [KIND],
        "universe_sha256": e.digest(baseline_raw),
        "tasks_sha256": e.digest(task_raw),
        "window_rule": "at_most_1000_natural_days; max_1000_rows; half_open_code_lifetime",
        "source_claimed_earliest": "2005-01-01",
        "actual_earliest": "per_task_coverage_only",
        "source_policy": "unauthenticated_local_research_no_redistribution",
        "request_interval_seconds": 2.2,
        "source_numeric_daily_quota": None,
        "sdk_license_is_not_data_redistribution_license": True,
        "primary_status_source": "existing_daily_bars.tradestatus_and_is_st_reused_in_place",
        "normalized_fields": list(FIELDS),
        "normalized_ohlcv_copied": False,
        "official_limit_certified": False,
        "historical_pit_certified": False,
        "unified_database_imported": False,
        "unified_database_engine": "pending_user_review",
    }
    e.immutable(root / "manifest.json", e.encoded(m))
    return m


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url):
    validate_url(url)
    req = Request(
        url,
        headers={
            "sdk-key": "anonymous",
            "Accept": "application/json",
            "User-Agent": "ashare-local-research/1.0",
        },
    )
    try:
        with build_opener(NoRedirect()).open(req, timeout=30) as res:
            raw = res.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise ValueError("Reference response exceeds 2 MiB")
            lengths = res.headers.get_all("Content-Length") or []
            if lengths and (
                len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) != len(raw)
            ):
                raise ValueError("HTTP body length mismatch")
            # APIs sometimes send access/rate failures in an HTTP 200 envelope.
            payload = _json(raw)
            business = payload.get("code") if isinstance(payload, dict) else None
            if str(business) in {"401", "402", "403", "429"}:
                return int(business), raw, dict(res.headers) | {"_transport_status": res.status}
            return res.status, raw, dict(res.headers)
    except HTTPError as error:
        return error.code, error.read(4096), dict(error.headers)


def parse(raw, task):
    s = validate_url(task["url"])
    if s != scope(task["symbol"], task["start"], task["end"]):
        raise ValueError("Task/request identity mismatch")
    payload = _json(raw)
    if (
        payload == {"code": 200, "data": {"count": 0, "list": []}}
        and type(payload["data"]["count"]) is int
    ):
        # This observed empty form omits the response identity. It proves only
        # that this request returned no rows, never historical nonexistence.
        return [], {
            "source_status": "empty_unknown",
            "request_start": task["start"],
            "request_end": task["end"],
            "source_rows": 0,
            "response_identity": "omitted_by_source_request_bound_only",
            "full_history_proven": False,
            "missing_dates_are_not_suspensions": True,
            "absence_proved": False,
        }
    # Reuse original adapter's exact JSON number, row identity, count/order validation.
    source = _rows(
        {
            "state": "response_saved",
            "http_status": 200,
            "body_truncated": False,
            "response_headers": {},
        },
        s,
        raw,
    )
    rows = []
    counts = {f: {} for f in FIELDS}
    for i, row in enumerate(source):
        fields = {}
        for field in FIELDS:
            val = _value(row, field, flag=field in {"is_st", "is_paused"})
            fields[field] = {
                "value": format(val.value, "f") if isinstance(val.value, Decimal) else val.value,
                "raw_token": val.raw_token,
                "state": val.state,
                "unit": "CNY/share" if field.endswith("limit") else "dimensionless",
            }
            counts[field][val.state] = counts[field].get(val.state, 0) + 1
        hi, lo = fields["high_limit"]["value"], fields["low_limit"]["value"]
        flags = (
            ["inverted_source_limits"]
            if hi is not None and lo is not None and Decimal(hi) < Decimal(lo)
            else []
        )
        rows.append(
            {
                "schema": "ad.daily-reference-observation.v1",
                "security": s["security"],
                "trade_date": e.day(row["trade_date"]),
                "fields": fields,
                "quality_flags": flags,
                "source": "zzshare",
                "source_locator": f"data/list/{i}",
                "source_revision_id": e.digest(e.encoded(fields)),
                "classification": "source_claimed",
                "upstream_provider": "unknown",
                "source_generation_method": "unknown",
                "price_basis": "source_claimed_mode0",
                "official_limit_certified": False,
                "historical_pit_certified": False,
                "historical_available_at": None,
                "absence_proved": False,
                "status_role": "secondary_observation_existing_daily_remains_primary",
            }
        )
    dates = [r["trade_date"] for r in rows]
    return rows, {
        "source_status": "parsed_source_reference" if rows else "empty_unknown",
        "request_start": task["start"],
        "request_end": task["end"],
        "source_earliest_date": min(dates) if dates else None,
        "source_latest_date": max(dates) if dates else None,
        "source_rows": len(rows),
        "field_states": counts,
        "response_count_matches": True,
        "pagination_truncation_precluded_by_natural_day_bound": True,
        "full_history_proven": False,
        "missing_dates_are_not_suspensions": True,
        "absence_proved": False,
    }


def normalize_saved(root, manifest, task, receipt, raw=None):
    if raw is None:
        e.verify_receipt(root, receipt)
        raw = gzip.decompress((root / receipt["raw"]["path"]).read_bytes())
    if e.digest(raw) != receipt["attempts"][-1]["response_sha256"]:
        raise ValueError("Uncompressed response checksum mismatch")
    try:
        rows, coverage = parse(raw, task)
        for row in rows:
            row.update(observed_at=receipt["observed_at"], response_sha256=e.digest(raw))
        receipt["normalized"] = e.artifact(
            root,
            "normalized/" + task["id"] + ".jsonl.gz",
            gzip.compress(b"".join(e.encoded(r) for r in rows), mtime=0),
        )
        receipt.update(
            state="complete" if rows else "empty_unknown", row_count=len(rows), coverage=coverage
        )
        receipt.pop("parse_error", None)
    except Exception as error:
        receipt.update(state="parse_failed", parse_error=f"{type(error).__name__}: {error}"[:500])
    receipt.update(
        updated_at=e.now(),
        normalizer_sha256=NORMALIZER_SHA256,
        projection_sha256=PROJECTION_SHA256,
        request_headers={
            "sdk-key": "anonymous",
            "Accept": "application/json",
            "User-Agent": "ashare-local-research/1.0",
        },
        credentials_loaded=False,
        cookies_sent=False,
        redirects=False,
    )
    return receipt


def run(root, max_jobs=0, interval=2.2):
    _, manifest, tasks = e.load_batch(root)
    if manifest["source_families"] != [KIND] or any(t["kind"] != KIND for t in tasks):
        raise ValueError("Not a zzshare reference batch")
    return e.run(root, max_jobs, interval, fetcher=fetch, normalizer=normalize_saved, adaptive=True)


class ReferenceReader:
    """One captured reference fact, with explicit ordinary research use only."""

    def __init__(self, root):
        self.root, manifest, self.tasks = e.load_batch(root)
        if manifest["source_families"] != [KIND]:
            raise ValueError("Not a reference batch")

    def read(self, symbol, trade_date, field, *, purpose="source_research", as_of=None):
        from ashare_data.financials.official_daily import SourceUseDenied, timestamp, SHANGHAI

        if (
            purpose not in {"source_research", "source_audit", "historical_pit"}
            or field not in FIELDS
        ):
            raise ValueError("Choose an explicit reference field and supported purpose")
        if purpose == "historical_pit":
            raise SourceUseDenied(["supplier_reference_has_no_verified_historical_release"])
        e.security(symbol)
        e.day(trade_date)
        matches = [
            t for t in self.tasks if t["symbol"] == symbol and t["start"] <= trade_date <= t["end"]
        ]
        if len(matches) != 1:
            raise SourceUseDenied(["date_outside_collected_code_lifetime"])
        task = matches[0]
        path = self.root / "receipts" / (task["id"] + ".json")
        if not path.exists():
            raise SourceUseDenied(["reference_window_not_yet_captured"])
        receipt = json.loads(path.read_text())
        if receipt["state"] != "complete":
            raise SourceUseDenied(["reference_source_not_available:" + receipt["state"]])
        e.verify_receipt(self.root, receipt)
        with gzip.open(self.root / receipt["normalized"]["path"], "rt") as stream:
            rows = [json.loads(line) for line in stream]
        selected = [r for r in rows if r["trade_date"] == trade_date]
        if len(selected) != 1:
            raise SourceUseDenied(["reference_date_missing_or_ambiguous_no_inference"])
        row = selected[0]
        if (
            row["security"] != e.security(symbol)
            or row["observed_at"] != receipt["observed_at"]
            or row["response_sha256"] != receipt["attempts"][-1]["response_sha256"]
        ):
            raise ValueError("Reference provenance differs from verified capture")
        if purpose == "source_audit":
            return {"source_record": row, "calculation_value_exposed": False}
        if as_of is None:
            raise ValueError("Research read requires explicit as_of")
        if timestamp(as_of) < timestamp(row["observed_at"]):
            raise SourceUseDenied(["reference_snapshot_not_yet_captured"])
        if timestamp(as_of).astimezone(SHANGHAI).date().isoformat() < trade_date:
            raise SourceUseDenied(["reference_date_after_requested_day"])
        value = row["fields"][field]
        if value["state"] != "source_claimed" or (field.endswith("limit") and row["quality_flags"]):
            raise SourceUseDenied(["reference_field_missing_invalid_or_conflicted"])
        return {
            "security": row["security"],
            "trade_date": trade_date,
            "field": field,
            "value": value["value"],
            "unit": value["unit"],
            "raw_token": value["raw_token"],
            "source_locator": row["source_locator"],
            "response_sha256": row["response_sha256"],
            "observed_at": row["observed_at"],
            "classification": "source_claimed",
            "official_limit_certified": False,
            "historical_pit_certified": False,
            "status_role": row["status_role"],
            "execution_permission": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "audit", "replay", "read"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--end", default="2026-10-19")
    parser.add_argument("--max-jobs", type=int, default=0)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--all-captured", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--date")
    parser.add_argument("--field", choices=FIELDS)
    parser.add_argument("--as-of")
    parser.add_argument("--source-audit", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        if not args.baseline_root:
            parser.error("prepare requires --baseline-root")
        result = prepare(args.root, args.baseline_root, end=args.end)
    elif args.command == "run":
        if args.max_jobs < 0:
            parser.error("max-jobs must be nonnegative")
        result = run(args.root, args.max_jobs)
    elif args.command == "replay":
        _, manifest, _ = e.load_batch(args.root)
        if manifest["source_families"] != [KIND]:
            raise ValueError("Not a reference batch")
        result = e.replay(args.root, args.all_captured, normalizer=normalize_saved)
    elif args.command == "read":
        if not (args.symbol and args.date and args.field):
            parser.error("read requires symbol/date/field")
        result = ReferenceReader(args.root).read(
            args.symbol,
            args.date,
            args.field,
            purpose="source_audit" if args.source_audit else "source_research",
            as_of=args.as_of,
        )
    else:
        result = e.audit(args.root, verify=args.verify)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
