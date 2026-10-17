"""Build a new pinned package from already captured public sources; no network.

The scope/curation files are owner inputs. Archive SQL identities, pagination,
raw hashes and source row keys are checked before any normalized rows are used.
"""

import argparse
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

from ashare_data.reference.historical_market import (
    DATA_SCHEMA,
    SCHEMA,
    HistoricalMarketView,
    ordinary_limits,
    _state_dependency_conflicts,
)
from ashare_data.core.model import canonical, day, require, timestamp


def convert(symbol):
    require(
        isinstance(symbol, str) and re.fullmatch(r"(SH|SZ|BJ)\d{6}", symbol),
        "HISTORICAL_SOURCE",
        "Unknown archive security",
    )
    return symbol[2:] + {"SH": ".XSHG", "SZ": ".XSHE", "BJ": ".XBSE"}[symbol[:2]]


def fact(
    value, refs, *, status="source_observed", reason="retrospective source observation", raw=None
):
    return {
        "value": value,
        "status": status,
        "source_refs": list(dict.fromkeys(refs)),
        "raw_value": raw,
        "reason": reason,
        "historical_available_at": None,
    }


def flag(row, field, refs, *, invert=False):
    raw = row.get(field) if row else None
    if raw in ("0", "1"):
        return fact(raw == ("0" if invert else "1"), refs, raw=raw)
    reason = (
        "row_missing_unknown"
        if row is None
        else "source_null"
        if raw is None
        else "unsupported_enum"
    )
    return fact(None, refs, status="unavailable", raw=raw, reason=reason)


def price(row, field, refs):
    raw = row.get(field) if row else None
    if raw is not None and raw != "":
        value = Decimal(raw)
        if value.is_finite() and value > 0:
            return fact(str(value), refs, raw=raw)
    return fact(None, refs, status="unavailable", raw=raw, reason="source_reference_unavailable")


class Sources:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.files, self.received = {}, []

    def add(self, name):
        p = self.root / name
        require(
            not Path(name).is_absolute()
            and ".." not in Path(name).parts
            and p.resolve().is_relative_to(self.root),
            "HISTORICAL_FILE",
            "Unsafe source path",
        )
        raw = p.read_bytes()
        self.files[name] = raw
        return raw

    def capture(self, name):
        raw = self.add(name)
        receipt_name = name + ".receipt.json"
        receipt = json.loads(self.add(receipt_name))
        require(
            receipt["status"] == 200
            and receipt["sha256"] == hashlib.sha256(raw).hexdigest()
            and receipt["bytes"] == len(raw),
            "HISTORICAL_INTEGRITY",
            "Invalid HTTP receipt",
        )
        start, end = timestamp(receipt["captured_start"]), timestamp(receipt["captured_end"])
        require(start <= end, "HISTORICAL_CLOCK", "Reversed acquisition clock")
        self.received.append(end)
        return raw, receipt, [name, receipt_name]

    def archive(self, name, sql, *, rowlimit=False):
        raw, receipt, refs = self.capture(name)
        parsed = urlparse(receipt["url"])
        require(
            parsed.scheme == "https"
            and parsed.hostname == "www.dolthub.com"
            and parsed.path == "/api/v1alpha1/chenditc/investment_data/master"
            and parse_qs(parsed.query) == {"q": [sql]},
            "HISTORICAL_SOURCE",
            "Unexpected archive query identity",
        )
        doc = json.loads(raw)
        require(
            doc["repository_owner"] == "chenditc"
            and doc["repository_name"] == "investment_data"
            and doc["sql_query"] == sql
            and doc["commit_ref"] == "master"
            and doc["query_execution_status"]
            in ({"Success", "RowLimit"} if rowlimit else {"Success"}),
            "HISTORICAL_SOURCE",
            "Failed/foreign/truncated archive response",
        )
        require(
            isinstance(doc["rows"], list) and len(doc["rows"]) <= 1000,
            "HISTORICAL_SOURCE",
            "Invalid archive rows",
        )
        return doc["rows"], refs


def panel_sql(panel, commit, kind):
    table, fields = (
        ("bao_a_stock_eod_info", "symbol,tradedate,tradestatus,is_st,preclose")
        if kind == "status"
        else ("final_a_stock_limit", "symbol,tradedate,pre_close,up_limit,down_limit")
    )
    where = "symbol IN (" + ",".join("'" + s + "'" for s in panel["symbols"]) + ")"
    where += f" AND tradedate BETWEEN '{panel['start']}' AND '{panel['end']}'"
    return f"SELECT {fields} FROM {table} AS OF '{commit}' WHERE {where} ORDER BY symbol,tradedate LIMIT 501"


def validate_panel(rows, panel):
    keys = [(r["symbol"], day(r["tradedate"])) for r in rows]
    require(
        len(rows) < 501
        and keys == sorted(set(keys))
        and all(s in panel["symbols"] and panel["start"] <= d <= panel["end"] for s, d in keys),
        "HISTORICAL_SOURCE",
        "Duplicate, escaped, unordered or truncated panel",
    )
    return {key: r for key, r in zip(keys, rows)}


def build(source_root, destination):
    sources = Sources(source_root)
    scope = json.loads(sources.add("scope.json"))
    curation = json.loads(sources.add("curation.json"))
    commit = scope["source_commit"]
    require(re.fullmatch(r"[0-9a-v]{32}", commit), "HISTORICAL_SOURCE", "Invalid fixed Dolt commit")
    require(1 <= len(scope["panels"]) <= 30, "HISTORICAL_SCOPE", "Bounded panels required")
    symbols = sorted({s for p in scope["panels"] for s in p["symbols"]})
    require(len(symbols) <= 100, "HISTORICAL_SCOPE", "Too many securities")
    for p in scope["panels"]:
        require(
            re.fullmatch(r"[a-z0-9-]+", p["id"])
            and p["start"] <= p["end"]
            and (date.fromisoformat(p["end"]) - date.fromisoformat(p["start"])).days < 100,
            "HISTORICAL_SCOPE",
            "Invalid panel",
        )
        for s in p["symbols"]:
            convert(s)
    for name in curation["documents"]:
        sources.capture(name)
    for name in curation.get("auxiliary_files", []):
        sources.add(name)
    codes = sorted(s[2:] + "." + s[:2] for s in symbols)
    sql = f"SELECT * FROM ts_a_stock_list AS OF '{commit}' WHERE ts_code IN ("
    # The exact captured ordering is preserved in the input manifest.
    master_codes = curation["master_codes"]
    require(sorted(master_codes) == codes, "HISTORICAL_SOURCE", "Master query differs from scope")
    sql += ",".join("'" + s + "'" for s in master_codes) + ") ORDER BY ts_code LIMIT 31"
    master, master_refs = sources.archive(curation["master_capture"], sql)
    require(
        len(master) < 31
        and len({r["ts_code"] for r in master}) == len(master)
        and all(r["ts_code"] in codes for r in master),
        "HISTORICAL_SOURCE",
        "Foreign/duplicate master",
    )
    by_code = {r["ts_code"]: r for r in master}
    instruments = []
    for symbol in symbols:
        sec = convert(symbol)
        raw = by_code.get(symbol[2:] + "." + symbol[:2])
        row = {
            "security": sec,
            "source_refs": master_refs,
            "source_fields": raw,
            "board": curation["boards"][sec],
            "listing_date_scope": "this_exchange_symbol_only",
        }
        for output, source in (
            ("initial_listing_date", "list_date"),
            ("delisting_date", "delist_date"),
        ):
            value = raw.get(source) if raw else None
            row[output] = fact(
                value,
                master_refs,
                raw=value,
                status="source_observed" if value else "unavailable",
                reason="retrospective dated master fact"
                if value
                else "blank_is_unknown_not_continued_listing",
            )
        override = curation.get("instrument_documents", {}).get(sec, {})
        for key, item in override.items():
            require(
                key in {"initial_listing_date", "delisting_date"},
                "HISTORICAL_SOURCE",
                "Unknown override",
            )
            if row[key]["value"] is not None:
                require(
                    row[key]["value"] == item["value"],
                    "HISTORICAL_SOURCE",
                    "Master/document conflict",
                )
            row[key] = fact(
                item["value"],
                item["source_refs"],
                status="document_extracted",
                reason=item["locator"],
                raw=item["value"],
            )
            row["source_refs"] = list(dict.fromkeys(row["source_refs"] + item["source_refs"]))
        instruments.append(row)
    rules = curation["rules"]
    states = []
    for panel in scope["panels"]:
        rows, refs = sources.archive(panel["status_capture"], panel_sql(panel, commit, "status"))
        statuses = validate_panel(rows, panel)
        rows, limit_refs = sources.archive(
            panel["limits_capture"], panel_sql(panel, commit, "limits")
        )
        limits = validate_panel(rows, panel)
        for symbol in panel["symbols"]:
            for offset in range(
                (date.fromisoformat(panel["end"]) - date.fromisoformat(panel["start"])).days + 1
            ):
                label = (date.fromisoformat(panel["start"]) + timedelta(days=offset)).isoformat()
                key, sec = (symbol, label), convert(symbol)
                raw, claimed = statuses.get(key), limits.get(key)
                row = {
                    "security": sec,
                    "date": label,
                    "panel": panel["id"],
                    "board": curation["boards"][sec],
                    "suspended": flag(raw, "tradestatus", refs, invert=True),
                    "is_st": flag(raw, "is_st", refs),
                    "reference_price": price(raw, "preclose", refs),
                    "source_fields": raw,
                    "intraday_halts_verified": False,
                    "reference_basis": "archive_preclose_unadjusted_CNY_per_share",
                }
                matches = [
                    r for r in rules if sec in r["securities"] and r["start"] <= label <= r["end"]
                ]
                require(len(matches) <= 1, "HISTORICAL_SOURCE", "Overlapping rule curation")
                rule = (
                    dict(matches[0])
                    if matches
                    else {
                        "kind": "unknown",
                        "ratio": None,
                        "depends_on_st": False,
                        "source_refs": refs,
                        "reason": "No applicable rule evidence",
                    }
                )
                if rule["kind"] == "ordinary" and rule["depends_on_st"]:
                    st = row["is_st"]["value"]
                    # Curated/event ratios stay fixed unless the owner explicitly
                    # selected the generic board rule's daily ST dependency.
                    policy = rule.get("st_policy", "fixed_rule")
                    require(
                        policy in {"fixed_rule", "fixed_event", "daily_status"},
                        "HISTORICAL_SOURCE",
                        "Unknown ST rule policy",
                    )
                    if policy == "daily_status" and st is not None:
                        rule.update(ratio="0.05" if st else "0.10")
                    if st is None:
                        rule.update(kind="unknown", ratio=None, reason="ST dependency unknown")
                row["rule"] = rule
                band_refs = list(dict.fromkeys(refs + rule["source_refs"]))
                values, status = (None, None), "unavailable"
                if rule["kind"] == "ordinary" and row["reference_price"]["value"] is not None:
                    values = ordinary_limits(row["reference_price"]["value"], rule["ratio"])
                    status = "derived"
                elif rule["kind"] == "unlimited":
                    status = "not_applicable"
                for field, value in zip(("upper_limit", "lower_limit"), values):
                    row[field] = fact(
                        value,
                        band_refs,
                        status=status,
                        reason="explicit ordinary board/IPO rule model; exchange exceptions not certified",
                    )
                validation = "source_missing" if claimed is None else "model_unavailable"
                if claimed is not None and rule["kind"] == "unlimited":
                    validation = "conflict_with_unlimited_rule"
                elif claimed is not None and status == "derived":
                    match = (
                        Decimal(claimed["up_limit"]) == Decimal(values[0])
                        and Decimal(claimed["down_limit"]) == Decimal(values[1])
                        and Decimal(claimed["pre_close"])
                        == Decimal(row["reference_price"]["value"])
                    )
                    validation = "model_agreement" if match else "conflict_with_rule_model"
                row["source_claimed_limits"] = {
                    "raw_fields": claimed,
                    "source_refs": limit_refs,
                    "validation": validation,
                    "execution_usable": False,
                }
                states.append(row)
    populations = []
    for definition in scope["universe_sources"]:
        label = day(definition["date"])
        count_sql = f"SELECT COUNT(*) AS row_count FROM bao_a_stock_eod_info AS OF '{commit}' WHERE tradedate='{label}'"
        counts, refs = sources.archive(definition["count"], count_sql)
        require(len(counts) == 1, "HISTORICAL_COVERAGE", "Missing population count")
        count = int(counts[0]["row_count"])
        require(0 <= count <= 10000, "HISTORICAL_SCOPE", "Population exceeds bound")
        raw_rows, normalized, last = [], [], None
        for i, path in enumerate(definition["pages"]):
            sql = f"SELECT symbol,tradedate,tradestatus,is_st FROM bao_a_stock_eod_info AS OF '{commit}' WHERE tradedate='{label}'"
            sql += (
                f" AND symbol>'{last}' ORDER BY symbol LIMIT 900"
                if i
                else " ORDER BY symbol LIMIT 10001"
            )
            rows, page_refs = sources.archive(path, sql, rowlimit=i == 0)
            refs += page_refs
            keys = [r["symbol"] for r in rows]
            require(
                keys == sorted(set(keys))
                and all(r["tradedate"] == label for r in rows)
                and (i == 0 or bool(keys) and keys[0] > last),
                "HISTORICAL_COVERAGE",
                "Duplicate, unordered or escaped population page",
            )
            if keys:
                last = keys[-1]
            raw_rows += rows
            normalized += [
                {
                    "security": convert(r["symbol"]),
                    "suspended": flag(r, "tradestatus", page_refs, invert=True),
                    "is_st": flag(r, "is_st", page_refs),
                }
                for r in rows
            ]
        require(
            len(raw_rows) == count and len({r["symbol"] for r in raw_rows}) == count,
            "HISTORICAL_COVERAGE",
            "Population page total differs from pinned source count",
        )
        populations.append(
            {
                "date": label,
                "rows": normalized,
                "source_row_count": count,
                "source_query_complete": True,
                "market_complete": False,
                "source_refs": refs,
            }
        )
    by_population = {(p["date"], r["security"]): r for p in populations for r in p["rows"]}
    for row in states:
        row["dependency_conflicts"] = conflicts = _state_dependency_conflicts(
            row,
            by_population.get((row["date"], row["security"])),
            curation.get("security_events", []),
        )
        if conflicts:
            refs = list(
                dict.fromkeys(
                    ref
                    for conflict in conflicts
                    for claim in conflict["claims"]
                    for ref in claim["source_refs"]
                )
            )
            for field in ("upper_limit", "lower_limit"):
                row[field] = fact(
                    None,
                    refs,
                    status="unavailable",
                    reason="Conflicting bound model dependencies; no source selected",
                )
            row["source_claimed_limits"]["validation"] = "conflict_with_dependencies"
    data = {
        "schema": DATA_SCHEMA,
        "scope": scope,
        "states": states,
        "instruments": instruments,
        "universes": populations,
        "actions": curation["actions"],
        "security_events": curation.get("security_events", []),
    }
    sources.files["data.json"] = canonical(data)
    binding = {
        "schema": SCHEMA,
        "data": "data.json",
        "historical_pit": False,
        "execution_permission": False,
        "received_at": max(sources.received).isoformat(),
        "source_commit": commit,
        "files": [
            {"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for path, raw in sorted(sources.files.items())
        ],
    }
    target = Path(destination)
    require(not target.exists(), "HISTORICAL_FILE", "Refusing to overwrite a package")
    target.mkdir(parents=True)
    for path, raw in sources.files.items():
        p = target / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
    raw = canonical(binding)
    (target / "binding.json").write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    view = HistoricalMarketView(target / "binding.json", expected_binding_sha256=sha)
    return {"binding_sha256": sha, "descriptor": view.descriptor(), "coverage": view.coverage()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root")
    parser.add_argument("destination")
    args = parser.parse_args()
    print(json.dumps(build(args.source_root, args.destination), ensure_ascii=False, indent=2))
