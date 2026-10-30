"""The separately authorized 19 menu items, derived from verified source snapshots.

No network requests or first-batch acceptance changes occur here. Current
membership is joined only to its stated current date or later, with reception
times preserved; it is never backfilled into older constituent histories.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

from ashare_data.providers.free_update_coverage import require
from ashare_data.providers.free_update_manual import read_snapshot, write_snapshot, query_snapshot
from ashare_data.providers.free_updates import digest, encoded, implementation_identity
from ashare_data.storage.free_updates import writer

ROWS = frozenset(
    {30, 50, 51, 52, 57, 64, 82, 112, 125, 126, 127, 128, 129, 130, 131, 132, 201, 207, 214}
)
INDICES = {
    125: "000300",
    126: "000016",
    127: "399006",
    128: "000688",
    129: "000905",
    130: "000906",
    131: "000852",
    132: "000010",
}
FUNCTIONS = {
    "bond_zh_cov_info",
    "tdx_f10_valuation",
    "sina_raw_daily",
    "tdx_f10_company",
    "tdx_members",
    "index_stock_cons_csindex",
    "treasury_rates",
    "sina_corporate_actions",
    "sina_current_quotes",
    "cni_current_members",
    "tdx_f10_hot_topics",
}


def catalog():
    value = json.loads(Path(__file__).with_name("supplemental-menu.json").read_text())
    require(
        len(value["items"]) == 19 and {i["excel_row"] for i in value["items"]} == ROWS,
        "supplemental menu scope changed",
    )
    return value


def moving_averages(records):
    groups = {}
    for row in records:
        if row.get("asset_type") != "stock":
            continue
        require(row.get("price_basis") == "unadjusted", "mixed or unspecified price basis")
        datetime.strptime(row["date"], "%Y-%m-%d")
        groups.setdefault(row["security"], []).append(row)
    output = []
    for security, values in sorted(groups.items()):
        values = sorted(values, key=lambda r: r["date"])
        require(len({v["date"] for v in values}) == len(values), "duplicate daily price")
        closes = [Decimal(v["close"]) for v in values]
        require(all(v.is_finite() and v > 0 for v in closes), "invalid daily close")
        for pos, row in enumerate(values):
            item = dict(row, available_source_sessions=pos + 1)
            for window in (5, 10, 20):
                item[f"sma_{window}"] = (
                    str(sum(closes[pos - window + 1 : pos + 1]) / window)
                    if pos + 1 >= window
                    else None
                )
            item["return_previous_observed_session"] = (
                str(closes[pos] / closes[pos - 1] - 1) if pos else None
            )
            output.append(item)
    return output


def inputs(index):
    selected = {}
    for entry in index["manual_snapshot_queries"]:
        if entry["function"] not in FUNCTIONS:
            continue
        manifest, records = read_snapshot(entry["snapshot"])
        require(
            manifest["function"] == entry["function"], "input function differs from signed snapshot"
        )
        require(
            manifest["semantics"].get("contract_reviewed")
            and not manifest["semantics"].get("gaps"),
            "input source semantics not verified",
        )
        # Collection windows are request parameters, not separate instruments.
        # Keep the latest immutable snapshot of each stable source scope.
        key = (manifest["function"], manifest["scope_id"])
        if key not in selected or manifest["observed_at"] > selected[key][0]["observed_at"]:
            selected[key] = (manifest, records)
    return list(selected.values())


def project(row, sources):
    used = []

    def members():
        # A later received current list supersedes an older list, even when
        # both still exist in the immutable input ledger.
        latest = {}
        for manifest, records in sources:
            if manifest["function"] not in {"index_stock_cons_csindex", "cni_current_members"}:
                continue
            code = records[0]["指数代码"]
            if row in INDICES and code != INDICES[row]:
                continue
            if code not in latest or manifest["observed_at"] > latest[code][0]["observed_at"]:
                latest[code] = manifest, records
        used.extend(m for m, _ in latest.values())
        return [r for _, records in latest.values() for r in records]

    def take(function):
        pairs = [s for s in sources if s[0]["function"] == function]
        if function == "treasury_rates":
            # Other reviewed Treasury tables use maturity columns, not the
            # long-term-average RATE_TYPE schema. Select the actual requested
            # series before revising overlapping observations by business key.
            pairs = [(m, [r for r in rs if r.get("RATE_TYPE") in {"Over_10_Years", "Real_Rate"}])
                     for m, rs in pairs]
            pairs = [(m, rs) for m, rs in pairs if rs]
        current_fields = {"bond_zh_cov_info": "SECUCODE", "tdx_f10_company": "security",
                          "tdx_members": "block_code", "tdx_f10_hot_topics": "security"}
        if function in current_fields:
            field = current_fields[function]
            groups = {}
            for manifest, records in pairs:
                for entity in {r[field] for r in records}:
                    if entity not in groups or manifest["observed_at"] > groups[entity][0]["observed_at"]:
                        groups[entity] = manifest, [r for r in records if r[field] == entity]
            pairs = list(groups.values())
        used.extend(m for m, _ in pairs)
        if function == "sina_raw_daily":
            # Multiple packaging rows may contain the same daily bar. Prefer
            # the later actual observation for revisions, preserving every
            # immutable input in derived_from. Never count a duplicate twice
            # in a rolling formula or silently mix price bases.
            latest = {}
            for manifest, rows in sorted(pairs, key=lambda s: s[0]["observed_at"]):
                for record in rows:
                    key = (record["security"], record["date"])
                    stamp = manifest["observed_at"]
                    if key in latest:
                        prior_stamp, prior = latest[key]
                        require(
                            prior.get("price_basis") == record.get("price_basis"),
                            "overlapping daily windows use different price bases",
                        )
                        require(
                            stamp != prior_stamp or record == prior,
                            "same-time daily observations conflict",
                        )
                    latest[key] = (stamp, record)
            return [record for _, record in latest.values()]
        keys = {"tdx_f10_valuation": ["security", "date"],
                "treasury_rates": ["date", "RATE_TYPE"],
                "sina_corporate_actions": ["security", "native_business_identity"],
                "sina_current_quotes": ["security", "quote_time"]}.get(function)
        if keys:
            latest = {}
            for manifest, records in sorted(pairs, key=lambda p: p[0]["observed_at"]):
                for record in records:
                    key = digest([record[k] for k in keys])
                    stamp = manifest["observed_at"]
                    require(key not in latest or latest[key][0] != stamp or latest[key][1] == record,
                            "same-time source observations conflict")
                    latest[key] = stamp, record
            return [record for _, record in latest.values()]
        return [r for _, rs in pairs for r in rs]

    scope = {}
    limitations = []
    units = {}
    dates = []
    if row == 30:
        native = take("bond_zh_cov_info")
        fields = [
            "SECUCODE",
            "SECURITY_NAME_ABBR",
            "COUPON_IR",
            "INTEREST_RATE_EXPLAIN",
            "IB_START_DATE",
            "IB_END_DATE",
            "CASHFLOW_DATE",
        ]
        records = [{k: r.get(k) for k in fields} for r in native]
        require(
            all(r.get("SECUCODE") and r.get("COUPON_IR") is not None for r in records),
            "coupon identity/value missing",
        )
        units = {"COUPON_IR": "percent_per_annum"}
        keys = ["SECUCODE"]
        limitations = [
            "current stated coupon and original year-by-year text; no invented payment confirmation or announcement time"
        ]
    elif row in {50, 64}:
        records = [
            {k: r.get(k) for k in ["security", "name", "date", "pe_ttm", "pb_mrq"]}
            for r in take("tdx_f10_valuation")
        ]
        keys = ["security", "date"]
        units = {"pe_ttm": "ratio", "pb_mrq": "ratio"}
        limitations = ["PE TTM and PB MRQ subset; other CMES financial factors are not supplied"]
    elif row in {51, 52}:
        records = moving_averages(take("sina_raw_daily"))
        keys = ["security", "date"]
        units = {
            "OHLC": "CNY/share",
            "sma_5": "CNY/share",
            "sma_10": "CNY/share",
            "sma_20": "CNY/share",
            "return_previous_observed_session": "fraction",
            "volume": "native unconverted",
        }
        scope["formulas"] = {
            "SMA(n)": "sum of current and preceding n-1 available trading-session closes / n",
            "return_previous_observed_session": "close[t]/close[t-1]-1",
            "price_basis": "unadjusted",
            "warmup": "null until n source sessions are present; no zero padding",
            "window": "only the input source daily window; no interpolation for absent dates",
        }
        limitations = [
            "SMA5/10/20 and adjacent-observed-session return only; not a claim of the full CMES indicator set"
        ]
    elif row == 57:
        profiles = take("tdx_f10_company")
        vals = take("tdx_f10_valuation")
        latest = {}
        for v in vals:
            if v["security"] not in latest or v["date"] > latest[v["security"]]["date"]:
                latest[v["security"]] = v
        records = []
        for r in profiles:
            v = latest.get(r["security"], {})
            records.append(
                {k: r.get(k) for k in ["security", "name", "listing_date", "market", "share_class"]}
                | {
                    "valuation_date": v.get("date"),
                    "pe_ttm": v.get("pe_ttm"),
                    "pb_mrq": v.get("pb_mrq"),
                    "first_publication_at": None,
                }
            )
        keys = ["security"]
        units = {"pe_ttm": "ratio", "pb_mrq": "ratio"}
        limitations = [
            "company identity/listing and current PE/PB subset; detailed statements remain in their existing verified separate entry"
        ]
    elif row == 82:
        records = [
            dict(r, taxonomy="TDX", effective_membership_date=None) for r in take("tdx_members")
        ]
        keys = ["taxonomy", "block_code", "security"]
        units = {"code": "identifier"}
        limitations = [
            "TDX named current theme; not asserted equivalent to a differently named CMES/THS/Eastmoney taxonomy"
        ]
    elif row == 112:
        records = members()
        keys = ["指数代码", "日期", "交易所", "成分券代码"]
        units = {"codes": "identifiers"}
        limitations = [
            "only explicit input indices; current file date and actual receipt time retained, no historic membership backfill"
        ]
    elif row in INDICES:
        current_members = [r for r in members() if r["指数代码"] == INDICES[row]]
        bars = take("sina_raw_daily")
        quotes = take("sina_current_quotes")
        records = []
        universe = set()
        for member in current_members:
            exchange = {"上海证券交易所": "sh", "深圳证券交易所": "sz"}.get(member["交易所"])
            require(exchange, "unmapped member exchange")
            security = exchange + member["成分券代码"]
            universe.add(security)
            eligible = [
                b
                for b in bars + quotes
                if b.get("asset_type") == "stock"
                and b["security"] == security
                and b["date"] >= member["日期"][:10]
            ]
            if eligible:
                b = max(eligible, key=lambda r: (r["date"], r.get("quote_time", "")))
                records.append(
                    dict(b, index_code=INDICES[row], membership_source_date=member["日期"][:10])
                )
        keys = ["index_code", "security", "date"]
        units = {"OHLC": "CNY/share", "volume": "see each record's volume_unit; legacy daily native",
                 "amount": "CNY for current quote source"}
        newest = max((r["date"] for r in records), default=None)
        scope.update(
            index_code=INDICES[row],
            member_count=len(universe),
            quoted_members=len(records),
            missing_member_quotes=sorted(universe - {r["security"] for r in records}),
            complete_current_member_quotes=bool(universe) and len(records) == len(universe),
            latest_quote_date=newest,
            older_quote_dates={r["security"]: r["date"] for r in records if r["date"] != newest},
            zero_trade_field_members=[r["security"] for r in records if r.get("zero_trade_fields")],
        )
        limitations = [
            "only members with available matching-date quotes; missing securities explicitly enumerated",
            "membership receipt time is retained; this current join is not historical point-in-time membership",
        ]
    elif row == 201:
        records = [
            {"date": r["date"], "series": r["RATE_TYPE"], "value": r["RATE"]}
            for r in take("treasury_rates")
            if r.get("RATE_TYPE") in {"Over_10_Years", "Real_Rate"}
        ]
        keys = ["series", "date"]
        units = {"value": "percent_per_annum"}
        scope["series"] = {
            "Over_10_Years": "US Treasury nominal over-10-year average",
            "Real_Rate": "US Treasury real long-term average",
        }
        limitations = [
            "two original series kept separate; not replaced with the 20-year point yield"
        ]
    elif row == 207:
        records = take("sina_corporate_actions")
        keys = ["security", "native_business_identity"]
        units = {
            "cash_per_share": "CNY per old eligible share before tax",
            "bonus/share_rights_ratios": "shares per old eligible share",
        }
        limitations = [
            "source-returned dividend/bonus/rights events only; other financing categories are not provided"
        ]
    elif row == 214:
        records = take("tdx_f10_hot_topics")
        keys = ["security", "topic_id"]
        units = {"topic_id": "identifier"}
        limitations = ["TDX F10 current issuer topic names and descriptions; explicit issuer scope",
                       "native dates/flags/scores retained; not interpreted as disclosure dates or market-wide heat ranking"]
    else:
        return None
    if not records:
        return None
    # Identical wrapper inputs can share a native job; divergent records must
    # never be silently collapsed under the same business key.
    unique = {}
    for r in records:
        key = digest([r[k] for k in keys])
        require(
            key not in unique or unique[key] == r, "conflicting source records for one business key"
        )
        unique[key] = r
        for k in ("date", "日期", "valuation_date", "announcement_date"):
            if r.get(k):
                dates.append(r[k][:10])
    records = sorted(unique.values(), key=lambda r: encoded([r[k] for k in keys]))
    used = list({m["snapshot_id"]: m for m in used}.values())
    scope.update(
        securities=sorted(
            {
                r.get("security", r.get("SECUCODE"))
                for r in records
                if r.get("security", r.get("SECUCODE"))
            }
        ),
        source_dates={"from": min(dates), "through": max(dates)} if dates else None,
    )
    return records, keys, units, limitations, scope, used, max(dates) if dates else None


def refresh(source_index, root):
    root = Path(root).resolve()
    require(
        not any((root / x).exists() for x in ["CURRENT", "CURRENT.json", "releases"]),
        "formal publication root forbidden",
    )
    index = json.loads(Path(source_index).read_text())
    sources = inputs(index)
    out = []
    root.mkdir(parents=True, exist_ok=True)
    with writer(root):
        for item in catalog()["items"]:
            row = item["excel_row"]
            value = project(row, sources)
            if value is None:
                out.append(
                    dict(
                        item,
                        status="待接入",
                        reason="existing verified source inputs not yet present for this item",
                        daily_enabled=False,
                    )
                )
                continue
            records, keys, units, limitations, scope, used, latest = value
            job = {
                "function": "supplemental_snapshot_projection",
                "parameters": {"row": row, "schema_version": 1},
                "implementation_sha256": implementation_identity(),
            }
            job["job_id"] = digest(job)
            check = {
                "records": len(records),
                "function": job["function"],
                "job_id": job["job_id"],
                "run_id": digest([job, sorted(m["snapshot_id"] for m in used)]),
                "observed_at": max(m["observed_at"] for m in used),
                "execution_kind": "derived_from_verified_source_snapshots",
                "raw_protocol_reparsed": False,
                "derived_from": [
                    {
                        "snapshot_id": m["snapshot_id"],
                        "function": m["function"],
                        "observed_at": m["observed_at"],
                        "latest_period": m["semantics"].get("latest_period"),
                        "source_urls": m.get("source_urls", []),
                    }
                    for m in used
                ],
                "source_urls": sorted({u for m in used for u in m.get("source_urls", [])}),
                "semantics": {
                    "contract_reviewed": True,
                    "business_key": keys,
                    "units": units,
                    "gaps": [],
                    "latest_period": latest,
                    "limitations": limitations,
                    "frequency": "follow_existing_source_updates",
                },
                "coverage": dict(
                    scope,
                    source_count=len(records),
                    complete_returned_window=True,
                    complete_history=False,
                    source_window="explicit verified source scopes; missing fields and securities are listed",
                ),
                "network_requests": 0,
                "source_index": str(Path(source_index).resolve()),
            }
            saved = write_snapshot(root, row, job, records, check)
            reread = []
            offset = 0
            while True:
                page = query_snapshot(saved["snapshot"], offset=offset, limit=1000)
                reread.extend(page["records"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            require(reread == records, "supplemental snapshot readback differs")
            out.append(
                dict(
                    item,
                    status="可运行（已列范围）",
                    **{k: v for k, v in saved.items() if k not in {"excel_row", "status"}},
                    latest_period=latest,
                    scope=scope,
                    limitations=limitations,
                    daily_enabled=False,
                    query_readback_verified=True,
                )
            )
    result = {
        "schema": "ad.supplemental-19-result.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope_total": 19,
        "usable_count": sum(i["status"] == "可运行（已列范围）" for i in out),
        "network_requests": 0,
        "first_batch_unchanged": True,
        "daily_enabled": 0,
        "items": out,
    }
    pending = root / "latest.next.json"
    pending.write_bytes(encoded(result))
    pending.replace(root / "latest.json")
    return result
