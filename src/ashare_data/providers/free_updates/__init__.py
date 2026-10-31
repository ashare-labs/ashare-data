"""Exact CMES menu identity and explicit, bounded public-source update plans."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def implementation_identity():
    root = Path(__file__).resolve().parents[2]
    paths = ["providers/free_updates/__init__.py", "providers/free_updates/free_update_worker.py",
             "providers/free_updates/free_update_public_transport.py", "providers/free_updates/free_update_board_members.py", "providers/free_updates/free_update_ths_bars.py", "providers/free_updates/free_update_board_bars.py", "providers/free_updates/supplemental_updates.py",
             "providers/free_updates/supplemental_sources.py", "providers/free_updates/supplemental_batch.py",
             "providers/free_updates/supplemental-menu.json",
             "providers/free_updates/free_update_budget.py", "financials/expansion.py",
             "providers/free_updates/free_update_current_members.py",
             "providers/free_updates/free_update_derived.py", "providers/free_updates/free_update_billboard_queue.py",
             "providers/free_updates/free_update_market_windows.py",
             "providers/free_updates/free_update_billboard.py", "providers/free_updates/free_update_fx.py",
             "providers/free_updates/akshare_loader.py", "providers/tdx/tdx_blocks.py", "storage/free_updates.py",
             "providers/free_updates/free_update_semantics.py",
             "providers/free_updates/free_update_schedule.py",
             "providers/free_updates/free_update_manual.py",
             "providers/free_updates/free_update_batch.py",
             "providers/free_updates/free_update_tdx.py", "providers/free_updates/free_update_tdx_worker.py", "providers/tdx/tdx_collection.py",
             "providers/free_updates/free_update_quotes.py",
             "providers/free_updates/free_update_industry_directory.py",
             "providers/free_updates/free_update_sina_minutes.py", "market/client.py",
             "providers/free_updates/free_update_macro_pages.py",
             "providers/free_updates/free_update_public_pages.py", "providers/free_updates/free_update_macro_contracts.py",
             "providers/free_updates/free_update_official.py",
             "providers/free_updates/free_update_security_events.py", "providers/free_updates/free_update_spot_contracts.py",
             "providers/free_updates/free_update_queue.py",
             "providers/free_updates/free_update_macro_history.py",
             "providers/free_updates/free_update_industry_contracts.py", "providers/free_updates/free_update_coverage.py",
             "providers/free_updates/free_update_news.py", "providers/free_updates/free_update_handoff.py",
             "providers/free_updates/free_update_shareholders.py",
             "providers/free_updates/free_update_period_tables.py", "providers/free_updates/sina-macro-contracts.json",
             "providers/free_updates/free_update_broadcast.py",
             "providers/free_updates/free_update_valuations.py",
             "providers/free_updates/free_update_dates.py",
             "providers/free_updates/free_update_research.py", "providers/free_updates/free_update_disclosures.py",
             "providers/free_updates/free_update_rates.py", "providers/free_updates/free_update_oil.py",
             "providers/free_updates/free_update_policy_releases.py",
             "providers/free_updates/free_update_acceptance.py", "providers/free_updates/free_update_reference.py",
             "providers/free_updates/free_update_futures.py",
             "providers/free_updates/free_update_futures_universe.py",
             "providers/free_updates/free_update_regional_prices.py",
             "providers/free_updates/free_update_limit_pools.py",
             "providers/free_updates/free_update_business_segments.py",
             "providers/free_updates/free_update_market_reports.py", "providers/free_updates/free_update_futures_minutes.py",
             "providers/free_updates/free_update_province_prices.py",
             "providers/free_updates/free_update_financial_indicators.py", "providers/free_updates/financial-indicators-contract.json", "providers/free_updates/futures-product-contracts.json",
             "providers/free_updates/free_update_equities.py", "providers/daily_sources.py", "providers/sina-decode.js",
             "providers/free_updates/free_update_legacy_reference.py", "corporate_actions/sources.py",
             "providers/free_updates/cmes-free-menu.json", "providers/free_updates/public-macro-requests.json",
             "providers/free_updates/free-source-routing.json",
             "providers/free_updates/akshare-reviewed-modules.json"]
    return digest({p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths})


def catalog():
    result = json.loads(Path(__file__).with_name("cmes-free-menu.json").read_text())
    items = result["items"]
    if len(items) != 198 or len({i["excel_row"] for i in items}) != 198:
        raise ValueError("CMES menu identity mismatch")
    if any(i["candidate_status"] != "可尝试" for i in items):
        raise ValueError("out-of-phase menu item")
    if any(not isinstance(i.get("exclude_superseded_functions", []), list)
           or not all(isinstance(f, str) for f in i.get("exclude_superseded_functions", []))
           for i in items):
        raise ValueError("superseded functions must be an explicit function-name list")
    return result


def source_families(function):
    routing=json.loads(Path(__file__).with_name("free-source-routing.json").read_text())
    return routing["functions"].get(function,["unreviewed"])


def plan(*, rows=None, context=None, as_of=None, lookback_days=14, source_window=None):
    """Keep all selected menu rows, including unimplemented or unbound requests.

    Context is explicit: callers supply securities/boards/contracts. No default
    symbol is silently treated as whole-market coverage. Identical calls share a job.
    """
    if not 1 <= lookback_days <= 366:
        raise ValueError("lookback_days must be in 1..366")
    day = date.fromisoformat(as_of) if as_of else datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = day - timedelta(days=lookback_days)
    contexts = context if isinstance(context, list) else [context or {}]
    if not contexts or not all(isinstance(c, dict) for c in contexts):
        raise ValueError("context must be a nonempty object or list of objects")
    requested = set(rows) if rows is not None else None
    items = catalog()["items"]
    if requested is not None and requested - {i["excel_row"] for i in items}:
        raise ValueError("selection contains a row outside the frozen 198 items")
    jobs, selections = {}, []
    identity = implementation_identity()
    specs=json.loads(Path(__file__).with_name("public-macro-requests.json").read_text())["specs"]
    if source_window is not None:
        from ashare_data.providers.free_updates.free_update_macro_history import window
        source_window=window(source_window)
    for item in items:
        if requested is not None and item["excel_row"] not in requested:
            continue
        refs, missing = [], set()
        active_calls = [] if item.get("source_lifecycle",{}).get("status") == "discontinued" else item["calls"]
        for entry in active_calls:
            for ctx in contexts:
                values = dict({"macro_offset":0, "source_page":1,
                               "broadcast_date":day.strftime("%Y%m%d")}, **ctx)
                if "stock_prefixed" not in values and re.fullmatch(r"(?:60|68|00|30)\d{4}", str(values.get("stock", ""))):
                    values["stock_prefixed"] = ("sh" if str(values["stock"]).startswith(("60", "68")) else "sz") + values["stock"]
                values.update(date=day.strftime("%Y%m%d"),
                              start_date=start.strftime("%Y%m%d"), year=str(day.year),
                              month_start=start.replace(day=1).strftime("%Y%m%d"),
                              week_start=(start-timedelta(days=start.weekday())).strftime("%Y%m%d"),
                              start_time=f"{start.isoformat()} 00:00:00",
                              end_time=f"{day.isoformat()} 23:59:59")
                params = {}
                for key, value in entry["parameters"].items():
                    if isinstance(value, str) and value.startswith("$"):
                        name = value[1:]
                        if name not in values or values[name] in (None, ""):
                            missing.add(name)
                        else:
                            params[key] = values[name]
                    else:
                        params[key] = value
                needed = {v[1:] for v in entry["parameters"].values()
                          if isinstance(v, str) and v.startswith("$")}
                if needed - values.keys() or any(values.get(k) in (None, "") for k in needed):
                    continue
                if source_window is not None:
                    if specs.get(entry["function"],{}).get("kind") != "eastmoney_datacenter":
                        raise ValueError("source windows are supported only for reviewed direct macro reports")
                    params.update(source_window)
                job = {"function": entry["function"], "parameters": params,
                       "mode": item["mode"], "timezone": item["timezone"],
                       "source_families":source_families(entry["function"]),
                       "implementation_sha256": identity}
                key = digest(job)
                jobs.setdefault(key, dict(job, job_id=key, menu_rows=[]))
                if item["excel_row"] not in jobs[key]["menu_rows"]:
                    jobs[key]["menu_rows"].append(item["excel_row"])
                if key not in refs:
                    refs.append(key)
        selections.append(dict(item, job_ids=refs, missing_context=sorted(missing)))
    return {"schema": "ad.cmes-free-plan.v1", "as_of": day.isoformat(),
            "scope": "explicit_context_sample", "workbook_sha256": catalog()["workbook_sha256"],
            "items": selections, "jobs": list(jobs.values())}


DATE_FIELDS = ("日期", "时间", "交易日期", "报告日期", "报告期", "报告日", "公告日期",
               "数据日期", "月份", "统计时间", "年份", "date", "datetime", "day", "trade_date",
               "REPORT_DATE", "TRADE_DATE", "公告时间", "发布时间", "发布时点", "净值日期", "统计日期",
               "交易时间", "上榜日", "统计年度", "截止日期", "调整日期", "统计月份")


def source_date(value):
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4})[-/.年](\d{1,2})(?:[-/.月](\d{1,2}))?", value)
    if match:
        year, month, day = match.groups()
        try:
            return date(int(year), int(month), int(day or 1)).isoformat()
        except ValueError:
            return None
    if re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    if re.fullmatch(r"\d{6}", value):
        try:
            return datetime.strptime(value, "%Y%m").date().isoformat()
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}", value) and 1900 <= int(value) <= 2200:
        return value+"-01-01"
    return None


def assess_records(records, *, mode, as_of):
    if not records:
        raise ValueError("empty response is not successful collection")
    if len(records) > 100_000:
        raise ValueError("response exceeds 100000 records; split scope")
    if not all(isinstance(r, dict) and r for r in records):
        raise ValueError("expected nonempty record objects")
    # Jin10 includes future release/forecast rows with a null actual value.
    # Those dates cannot make the observed series appear newer than its data.
    observed_rows = [r for r in records if "今值" not in r or r["今值"] not in (None, "", "--", "-")]
    dates = [d for row in observed_rows for k, v in row.items()
             if k in DATE_FIELDS and (d := source_date(v))]
    source_max = max(dates, default=None)
    end = date.fromisoformat(as_of)
    # Calendar carries future dates deliberately. Ordinary datasets do not earn
    # a freshness pass through future reporting dates or lack of a date column.
    actual_dates = [d for d in dates if d <= as_of]
    latest = max(actual_dates, default=None)
    age = (end - date.fromisoformat(latest)).days if latest else None
    if mode in {"bars", "futures_bars"}:
        aliases = [("开盘", "开盘价", "open"), ("最高", "最高价", "high"),
                   ("最低", "最低价", "low"), ("收盘", "收盘价", "close")]
        for row in records:
            numbers = []
            for names in aliases:
                val = next((row[k] for k in names if k in row), None)
                if val is None:
                    raise ValueError("OHLC field missing or null")
                numbers.append(float(val))
            opening, high, low, close = numbers
            if not low <= min(opening, close) <= max(opening, close) <= high:
                raise ValueError("invalid OHLC ordering")
            if mode == "bars" and low <= 0:
                raise ValueError("nonpositive equity/fund/index OHLC")
    threshold = 14 if mode in {"bars", "futures_bars"} else 185
    period_precision = "source_date_or_month"
    annual_values = [str(r[k]) for r in observed_rows for k in ("统计年度", "年份")
                     if k in r and re.fullmatch(r"\d{4}", str(r[k]))]
    if annual_values and latest and latest == max(annual_values)+"-01-01":
        period_precision = "year"
        # The year is a statistical period, not January's publication date.
        period_end = min(end, date(int(max(annual_values)),12,31))
        age = (end-period_end).days
        threshold = 550
    if mode == "policy_event":
        freshness = "release_verification_pending"
    elif mode in {"snapshot", "event", "financial", "calendar"}:
        freshness = "observed_snapshot" if mode == "snapshot" else "publication_not_inferred"
    elif latest is None:
        freshness = "unverified_date"
    elif age > threshold:
        freshness = "stale"
    else:
        freshness = "recent"
    return {"source_max_date": source_max, "latest_nonfuture_date": latest,
            "age_days": age, "freshness": freshness,
            "period_precision":period_precision,"freshness_threshold_days":threshold,
            "columns": sorted({k for row in records for k in row}),
            "publication_freshness_verified": False,
            "age_screen_only": True,
            "freshness_limitation": "broad age screen is not latest-publication verification; compare original release frequency and latest nonnull fields separately",
            "units": "source-native; no CMES unit conversion",
            "historical_visibility": "not inferred from report or trade date"}
