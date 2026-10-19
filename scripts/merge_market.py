"""Resumable, source-preserving first market release. Run with explicit entry/root.

Sources are read only. No financial/reference/factor values or account state are imported.
Every input bar has exactly one disposition; quarantine points to sealed source assets.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
from pathlib import Path
import resource
import sqlite3
import time

import pandas as pd

from ashare_data.storage.market_release import (
    ReleaseWriter,
    connection,
    canonical,
    sha_file,
    fingerprint,
    sql_literal,
    atomic_write,
    DISPOSITIONS,
    FIELDS,
)
from ashare_data.storage.local_daily import LocalDailyStore

RULE = "market-merge-20261019.v1"
SOURCE_PRIORITY = {"baostock": 0, "eastmoney": 1, "sina": 2, "sohu": 3, "tencent": 4}
SMALL_TABLES = (
    "metadata",
    "responses",
    "instruments",
    "listing_facts",
    "calendar",
    "code_events",
    "coverage",
)


def sid(symbol):
    if symbol.startswith(("sh", "sz")):
        return symbol[2:] + (".XSHG" if symbol[:2] == "sh" else ".XSHE")
    exchange, code = symbol.split(".")
    return (
        code + ({"SH": ".XSHG", "SZ": ".XSHE"}.get(exchange, ""))
        if exchange != "BJ"
        else "BJ." + code
    )


def source_stats(path):
    return {str(p): fingerprint(p) for p in (path, Path(str(path) + "-wal")) if p.exists()}


class Merge:
    def __init__(self, entry, root):
        self.entry = entry.resolve()
        self.root = root.resolve()
        self.document = json.loads(entry.read_bytes())
        base = Path(self.document["data_root"])
        self.base = (base if base.is_absolute() else entry.parent / base).resolve()
        self.profile = self.document["profiles"]["product"]
        self.daily = self.asset(self.profile["daily_asset"])
        self.hf = self.asset("minute-hf-download")
        self.w = ReleaseWriter(root)
        self.work = root / "work"
        self.db = sqlite3.connect(self.work / "checkpoint.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batches(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        self.c = connection(self.work / "spill", memory="2GB", threads=2)
        self.rule_hash = hashlib.sha256(canonical(self.contracts())).hexdigest()
        self.parser_sha256 = sha_file(Path(__file__))
        parser_copy = self.work / "parser.py"
        parser_copy.write_bytes(Path(__file__).read_bytes())
        self.parser_object = self.w.object(parser_copy, "py")

    def asset(self, name):
        p = Path(self.document["assets"][name]["path"])
        return (p if p.is_absolute() else self.base / p).resolve()

    def setting(self, key, value=None):
        old = self.db.execute("select value from config where key=?", (key,)).fetchone()
        if old is not None:
            result = json.loads(old[0])
            if value is not None and result != value:
                raise ValueError("Frozen setting changed: " + key)
            return result
        if value is None:
            return None
        self.db.execute("insert into config values (?,?)", (key, canonical(value).decode()))
        self.db.commit()
        return value

    def done(self, key):
        r = self.db.execute("select value from batches where key=?", (key,)).fetchone()
        if not r:
            return None
        result = json.loads(r[0])
        if result["rule_hash"] != self.rule_hash:
            raise ValueError("Rule changed: start a new explicit build")
        for item in result.get("files", []) + result.get("evidence", []):
            if not (self.root / item["path"]).exists():
                raise ValueError("Checkpoint object missing")
        return result

    def finish(self, key, value):
        value.update(
            rule_hash=self.rule_hash,
            parser_sha256=self.parser_sha256,
            parser_object=self.parser_object,
            completed_at=datetime.now(timezone.utc).isoformat(),
            peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
        self.db.execute("insert into batches values (?,?)", (key, canonical(value).decode()))
        self.db.commit()

    def contracts(self):
        return {
            "rule_version": RULE,
            "parser_version": RULE,
            "schema_version": 1,
            "daily": {
                "priority": SOURCE_PRIORITY,
                "whole_observation": True,
                "field_filling": False,
                "reason": "Existing compatible reader priority; BaoStock retains exact integer share counts/amounts; Sohu display-lot and amount rounding is not substituted",
                "conflicts": "research selection by frozen priority, not a claim of exchange truth",
                "price": "DECIMAL(38,12), exact roundtrip required",
                "volume": "BIGINT only if exact integral",
                "unknown": "Pre-2010 units/basis remain quarantined in the original source; no silent unit certification",
                "identities": "Retained issuer code-event chains; source-file aliases are separately bound to the source snapshot",
                "series": {
                    "series_id": 1,
                    "frequency": "1d",
                    "price_basis": "unadjusted",
                    "volume_unit": "share",
                    "amount_unit": "CNY",
                    "semantic_status": "partially_verified",
                },
            },
            "hf": {
                "source_timezone": "Asia/Singapore",
                "canonical_timezone": "UTC",
                "price": "DOUBLE unchanged",
                "volume": "BIGINT unchanged",
                "amount": "DOUBLE unchanged",
                "scale_factors": {"price": 1, "volume": 1, "amount": 1},
                "source_declaration": "README.md and metadata/source_provenance.json, pinned in evidence",
                "series": {
                    "series_id": 2,
                    "frequency": "1m",
                    "price_basis": "unadjusted",
                    "basis_evidence_status": "source_declared",
                    "volume_unit": "share",
                    "amount_unit": "CNY",
                    "label_role": "unknown",
                    "interval_closure": "unknown",
                    "semantic_status": "partially_verified",
                },
                "bad_values": "Nonfinite/missing/nonpositive prices, invalid OHLC, negative quantities, or pinned review restrictions -> quarantine; no fabricated corrections",
                "duplicate_rule": "Exact-value duplicates choose lexicographically first source path; conflicting keys quarantine all candidates",
            },
            "stockdb": {
                "source_snapshot": "Immutable inventory/hash of original LevelDB data and data1; serialized bounded local raw-key reads",
                "admission": "quarantine until quantity units and interval contract are evidenced; full key overlap comparison is not semantic equivalence",
                "scope": "Equity codes in frozen source directory + delisted directory + daily/HF inventories; ETF prefixes 1/5 excluded explicitly",
            },
            "future_financial": {
                "enabled": False,
                "source_revision_version_separate_from_parser_version": True,
                "fact_context_key": ["report_version", "extraction_version", "context_id"],
            },
            "pit": "Only local release reproducibility; no strict historical market PIT",
            "snapshot": "content addressed Parquet file manifest with retained replaced chunks; no sparse-history hybrid",
        }

    def prepare(self):
        self.setting("contracts", self.contracts())
        self.setting("entry", str(self.entry))
        self.setting("source_daily_stats", source_stats(self.daily / "daily.sqlite3"))
        if self.setting("source_daily_hashes") is None:
            self.setting(
                "source_daily_hashes", {p: sha_file(p) for p in self.setting("source_daily_stats")}
            )
        spec = self.profile["minute_sources"]["neigezhu"]
        raw = Path(spec["manifest"]["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == spec["manifest"]["sha256"]
        self.source_manifest = [
            r
            for r in csv.DictReader(io.StringIO(raw.decode()))
            if r["path"].startswith("data/stock_1m/")
        ]
        self.setting("hf_manifest_sha256", spec["manifest"]["sha256"])
        self.overlay = json.loads(Path(spec["review"]["path"]).read_bytes())
        assert sha_file(spec["review"]["path"]) == spec["review"]["sha256"]
        self.restrictions = defaultdict(list)
        for row in self.overlay["ranges"]:
            self.restrictions[row["path"]].append(row)
        self.coverage = list(
            csv.DictReader((self.hf / "metadata/coverage_by_instrument.csv").open())
        )
        for name, path in [
            ("hf-file-manifest", Path(spec["manifest"]["path"])),
            ("hf-quality-overlay", Path(spec["review"]["path"])),
            ("hf-readme", self.hf / "README.md"),
            ("hf-provenance", self.hf / "metadata/source_provenance.json"),
            ("hf-coverage", self.hf / "metadata/coverage_by_instrument.csv"),
            ("hf-calendar", self.hf / "metadata/market_calendar.csv"),
            ("hf-missing-days", self.hf / "metadata/no_bar_day_classification.csv"),
            ("hf-absent", self.hf / "metadata/absent_instruments.csv"),
        ]:
            target = self.work / (name + path.suffix)
            target.write_bytes(path.read_bytes())
            self.setting(name, self.w.object(target, path.suffix.lstrip(".")))
        s = sqlite3.connect((self.daily / "daily.sqlite3").as_uri() + "?mode=ro", uri=True)
        self.daily_symbols = self.setting("daily_symbols")
        if self.daily_symbols is None:
            self.daily_symbols = [r[0] for r in s.execute("select distinct symbol from bars")]
            self.setting("daily_symbols", self.daily_symbols)
        events = [json.loads(r[0]) for r in s.execute("select body from code_events")]
        symbols = set(self.daily_symbols)
        symbols.update(
            r["path"].split("/")[-2].lower() + Path(r["path"]).stem for r in self.source_manifest
        )
        stock_probe = self.root.parent / "migration-20261019/stockdb-probe.json"
        if stock_probe.exists():
            raw = json.loads(stock_probe.read_text())
            codes = {
                v for k, values in raw["股票代码"].items() if k not in ("1", "5") for v in values
            }
            codes.update(v[1] for v in raw.get("退市*", []) if v[1][0] not in ("1", "5"))
            for code in codes:
                symbols.add(
                    (
                        "sh"
                        if code.startswith("6")
                        else "sz"
                        if code.startswith(("0", "3"))
                        else "bj"
                    )
                    + code
                )
            self.setting("stock_codes", sorted(codes))
        self.parents = {x: x for x in symbols}
        for e in events:
            self.parents[e["new_symbol"]] = e["old_symbol"]

        def root(symbol):
            seen = set()
            while self.parents[symbol] != symbol:
                if symbol in seen:
                    raise ValueError("Cyclic identity evidence")
                seen.add(symbol)
                symbol = self.parents[symbol]
            return symbol

        self.ids = {symbol: i + 1 for i, symbol in enumerate(sorted({root(s) for s in symbols}))}
        self.symbol_ids = {symbol: self.ids[root(symbol)] for symbol in sorted(symbols)}
        self.setting("symbol_ids", self.symbol_ids)
        self.events = events
        ref = self.work / "reference.sqlite3"
        if not ref.exists():
            d = sqlite3.connect(ref)
            s.execute("BEGIN")
            for table in SMALL_TABLES:
                schema = s.execute(
                    "select sql from sqlite_master where name=?", (table,)
                ).fetchone()[0]
                d.execute(schema)
                rows = s.execute("select * from " + table).fetchall()
                if rows:
                    d.executemany(
                        "insert into " + table + " values (" + ",".join("?" for _ in rows[0]) + ")",
                        rows,
                    )
            d.executescript("""CREATE TABLE release_instruments(instrument_id INTEGER PRIMARY KEY,security TEXT UNIQUE,name TEXT,identity_status TEXT,venue_id TEXT,asset_class TEXT);
            CREATE TABLE release_identifiers(security TEXT PRIMARY KEY,instrument_id INTEGER,namespace TEXT,valid_from TEXT,valid_to TEXT,validity_precision TEXT,source_version TEXT);
            CREATE TABLE source_assets(asset_id TEXT PRIMARY KEY,body TEXT);
            CREATE TABLE series_contracts(series_id INTEGER PRIMARY KEY,body TEXT);
            CREATE TABLE release_batches(batch_key TEXT PRIMARY KEY,body TEXT);
            """)
            names = dict(s.execute("select symbol,name from instruments"))
            for symbol, i in self.ids.items():
                public = sid(symbol) if not symbol.startswith("bj") else "BJ." + symbol[2:]
                d.execute(
                    "insert into release_instruments values (?,?,?,?,?,?)",
                    (
                        i,
                        public,
                        names.get(symbol, ""),
                        "source_snapshot; historical code reuse not fully evidenced",
                        symbol[:2].upper(),
                        "equity",
                    ),
                )
            for symbol, i in self.symbol_ids.items():
                public = sid(symbol) if not symbol.startswith("bj") else "BJ." + symbol[2:]
                begins = [e["effective_date"] for e in events if e["new_symbol"] == symbol]
                ends = [e["effective_date"] for e in events if e["old_symbol"] == symbol]
                d.execute(
                    "insert into release_identifiers values (?,?,?,?,?,?,?)",
                    (
                        public,
                        i,
                        "venue_equity_code",
                        max(begins) if begins else None,
                        min(ends) if ends else None,
                        "issuer_notice" if begins or ends else "unknown",
                        RULE,
                    ),
                )
            for v in (self.contracts()["daily"]["series"], self.contracts()["hf"]["series"]):
                d.execute(
                    "insert into series_contracts values (?,?)",
                    (v["series_id"], canonical(v).decode()),
                )
            d.commit()
            d.close()
        s.close()
        self.reference = ref

    def source_check(self):
        assert self.setting("source_daily_stats") == source_stats(self.daily / "daily.sqlite3"), (
            "Daily source changed"
        )

    def stats(self, table="classified"):
        counts = dict(
            self.c.execute(
                "select disposition,count(*) from " + table + " group by disposition"
            ).fetchall()
        )
        return {"input_rows": sum(counts.values()), **{k: counts.get(k, 0) for k in DISPOSITIONS}}

    def fact(self, query, name, table, instrument_id, series_id):
        count, unique, first, last = self.c.execute(
            "select count(*),count(distinct "
            + ("trade_date" if table == "bars_1d" else "label_at")
            + "),min(trade_date),max(trade_date) from ("
            + query
            + ")"
        ).fetchone()
        assert count == unique
        if not count:
            return None
        obj = self.w.parquet(self.c, query, [], name)
        return {
            **obj,
            "table": table,
            "instrument_id": instrument_id,
            "series_id": series_id,
            "rows": count,
            "duplicate_keys": count - unique,
            "first_date": str(first),
            "last_date": str(last),
        }

    def daily_merge(self):
        library = LocalDailyStore(self.daily, readonly=True)
        library.db.execute("BEGIN")
        groups = defaultdict(list)
        for symbol in self.daily_symbols:
            groups[self.symbol_ids[symbol]].append(symbol)
        calendar = [r[0] for r in library.db.execute("select date from calendar order by date")]
        for pos, (i, symbols) in enumerate(sorted(groups.items())):
            key = f"daily:{i}"
            if self.done(key):
                continue
            if pos % 20 == 0:
                self.w.budget()
                self.source_check()
            started = time.monotonic()
            allowed = set()
            for symbol in symbols:
                allowed.update(library.expected(symbol, "1990-01-01", "2100-01-01", calendar))
            rows = [
                dict(r)
                for r in library.db.execute(
                    "select * from bars where symbol in ("
                    + ",".join("?" for _ in symbols)
                    + ") order by symbol,date,source",
                    symbols,
                )
            ]
            if not rows:
                continue
            native_dates = {
                symbol: set(library.expected(symbol, "1990-01-01", "2100-01-01", calendar))
                for symbol in symbols
            }
            for r in rows:
                r["priority"] = SOURCE_PRIORITY.get(r["source"], 99)
                r["in_scope"] = r["date"] in allowed
                r["native_code_valid"] = r["date"] in native_dates[r["symbol"]]
            frame = pd.DataFrame(rows)
            self.c.register("raw_daily", frame)
            expressions = ",".join(
                f'try_cast("{a}" as DECIMAL(38,12)) as {b}'
                for a, b in zip(("open", "high", "low", "close", "volume", "amount"), FIELDS)
            )
            self.c.execute(
                "create or replace temp table normalized as select *, "
                + expressions
                + " from raw_daily"
            )
            # Decimal strings are already normalized by original adapters. Reject any precision loss.
            checks = []
            for raw, column in zip(("open", "high", "low", "close", "volume", "amount"), FIELDS):
                checks.append(
                    f'''("{raw}" is not null and ({column} is null or length(rtrim(split_part(cast("{raw}" as varchar),'.',2),'0'))>12 or contains(lower(cast("{raw}" as varchar)),'e')))'''
                )
            error = " OR ".join(checks)
            self.c.execute(
                """create or replace temp table assessed as select *, case
             when try_cast(date as date) is null or priority=99 or """
                + error
                + """ then 'invalid_format_or_precision'
             when date<'2010-01-01' then 'early_units_and_basis_unverified'
             when not in_scope then 'outside_frozen_listing_or_calendar'
             when open_price is null or high_price is null or low_price is null or close_price is null
               or least(open_price,high_price,low_price,close_price)<=0
               or high_price<greatest(open_price,close_price) or low_price>least(open_price,close_price)
               or high_price<low_price or volume_shares<0 or turnover_cny<0
               or volume_shares!=trunc(volume_shares) then 'invalid_core_value'
             else null end as reason from normalized"""
            )
            self.c.execute("""create or replace temp table selected as select * from assessed where reason is null
             qualify row_number() over(partition by date order by priority,native_code_valid desc,symbol)=1""")
            equality = " AND ".join(f"a.{f} IS NOT DISTINCT FROM b.{f}" for f in FIELDS)
            self.c.execute(
                """create or replace temp table classified as select a.*,case
             when a.reason='invalid_format_or_precision' then 'format_rejected'
             when a.reason='outside_frozen_listing_or_calendar' then 'resolved_unused'
             when a.reason is not null then 'quarantined'
             when a.symbol=b.symbol and a.source=b.source then 'adopted'
             when """
                + equality
                + """ then 'equal_duplicate' else 'resolved_unused' end as disposition
             from assessed a left join selected b using(date)"""
            )
            query = f"""select 1::USMALLINT as series_id,{i}::UINTEGER as instrument_id,cast(date as date) as trade_date,
             open_price,high_price,low_price,close_price,cast(volume_shares as BIGINT) as volume_shares,turnover_cny,
             {i}::UINTEGER as batch_id,symbol as source_symbol,source,row_hash,response_id,observed_at,
             preclose,tradestatus,is_st from selected order by date"""
            fact = self.fact(query, f"daily-{i}", "bars_1d", i, 1)
            disposition = self.w.parquet(
                self.c,
                """select symbol as source_symbol,date,source,row_hash,
             disposition,coalesce(reason,case when disposition='resolved_unused' then 'lower_priority_conflicting_whole_observation'
             else disposition end) as reason from classified order by symbol,date,source""",
                [],
                f"daily-disposition-{i}",
            )
            # Explicit frozen denominator, including completely missing security-days.
            expected = pd.DataFrame(
                {"trade_date": calendar, "expected": [d in allowed for d in calendar]}
            )
            self.c.register("expected_days", expected)
            quality = self.w.parquet(
                self.c,
                f"""select 1::USMALLINT as series_id,{i}::UINTEGER as instrument_id,
             cast(e.trade_date as date) as trade_date,case when expected then 'expected' else 'not_expected' end as expected_state,
             case when expected then 1 else 0 end as expected_count,case when s.date is null then 0 else 1 end as observed_count,
             case when expected and s.date is null then 1 else 0 end as missing_count,
             'frozen provider calendar and retained listing facts; not exchange/PIT certification' as evidence
             from expected_days e left join selected s on e.trade_date=s.date order by e.trade_date""",
                [],
                f"daily-coverage-{i}",
            )
            counts = self.stats()
            assert counts["input_rows"] == len(rows)
            self.finish(
                key,
                {
                    "source": "daily_sqlite",
                    "files": [fact] if fact else [],
                    "evidence": [disposition, quality],
                    "input_dispositions": counts,
                    "source_symbols": symbols,
                    "input_values_sha256": hashlib.sha256(canonical(rows)).hexdigest(),
                    "source_asset": self.setting("source_daily_stats"),
                    "seconds": time.monotonic() - started,
                },
            )
            self.c.unregister("raw_daily")
            self.c.unregister("expected_days")
            if pos % 25 == 0:
                print("DAILY", pos + 1, len(groups), "last", key, counts, flush=True)
        library.close()
        self.source_check()

    def hf_merge(self):
        groups = defaultdict(list)
        for index, r in enumerate(self.source_manifest):
            symbol = r["path"].split("/")[-2].lower() + Path(r["path"]).stem
            groups[self.symbol_ids[symbol]].append({**r, "asset_id": 100000 + index})
        for pos, (i, items) in enumerate(sorted(groups.items())):
            key = f"hf:{i}"
            if self.done(key):
                continue
            if pos % 20 == 0:
                self.w.budget()
            started = time.monotonic()
            pieces = []
            source_info = []
            for r in items:
                p = self.hf / r["path"]
                before = fingerprint(p)
                assert (
                    before[0] == int(r["observed_bytes"]) and sha_file(p) == r["computed_hash"]
                ), "HF source differs from frozen audit"
                restricted = (
                    " OR ".join(
                        f"(file_row_number between {x['first_row']} and {x['last_row']})"
                        for x in self.restrictions[r["path"]]
                    )
                    or "false"
                )
                pieces.append(f"""select *,{r["asset_id"]}::UINTEGER as asset_id,({restricted}) as restricted
                    from read_parquet({sql_literal(p)},file_row_number=true)""")
                source_info.append(
                    {
                        "asset_id": r["asset_id"],
                        "path": str(p),
                        "sha256": r["computed_hash"],
                        "fingerprint": before,
                        "source_alias": p.stem,
                        "instrument_id": i,
                        "source_version": self.setting("hf_manifest_sha256"),
                    }
                )
            self.c.execute("create or replace temp table native as " + " UNION ALL ".join(pieces))
            self.c.execute("""create or replace temp table assessed as select *,
              case when timestamp is null or date_trunc('minute',timestamp)!=timestamp then 'invalid_label'
               when restricted then 'pinned_review_restriction'
               when open is null or high is null or low is null or close is null
                 or not isfinite(open) or not isfinite(high) or not isfinite(low) or not isfinite(close)
                 or least(open,high,low,close)<=0 or high<greatest(open,close) or low>least(open,close) or high<low
                 or volume<0 or turnover<0 or (turnover is not null and not isfinite(turnover)) then 'invalid_core_value'
               else null end as reason from native""")
            # File path and payload identities are both checked; timezone conversion is only an offset.
            for r in items:
                ex = r["path"].split("/")[-2]
                code = Path(r["path"]).stem
                assert (
                    self.c.execute(
                        "select count(*) from native where asset_id=? and (symbol is distinct from ? or exchange is distinct from ?)",
                        [r["asset_id"], code, ex],
                    ).fetchone()[0]
                    == 0
                )
            differences = " OR ".join(
                f'(min("{f}") over w is distinct from max("{f}") over w OR (count("{f}") over w not in (0,count(*) over w)))'
                for f in ("open", "high", "low", "close", "volume", "turnover")
            )
            self.c.execute(
                """create or replace temp table ranked as select *, row_number() over w as rank,
             count(*) over w as key_count,("""
                + differences
                + """) as conflict from assessed
             window w as (partition by timestamp order by asset_id,file_row_number rows between unbounded preceding and unbounded following)"""
            )
            self.c.execute("""create or replace temp table classified as select *,case
             when reason='invalid_label' then 'format_rejected'
             when reason is not null or conflict then 'quarantined'
             when rank=1 then 'adopted' else 'equal_duplicate' end as disposition from ranked""")
            query = f"""select 2::USMALLINT as series_id,{i}::UINTEGER as instrument_id,cast(timestamp as date) as trade_date,
             timestamp at time zone 'Asia/Singapore' as label_at,open as open_price,high as high_price,low as low_price,
             close as close_price,volume as volume_shares,turnover as turnover_cny,asset_id as batch_id
             from classified where disposition='adopted' order by timestamp"""
            fact = self.fact(query, f"hf-{i}", "bars_1m", i, 2)
            exception = self.w.parquet(
                self.c,
                """select asset_id,file_row_number,timestamp,disposition,
             coalesce(reason,case when conflict then 'conflicting_duplicate_key' else 'equal_duplicate' end) as reason
             from classified where disposition!='adopted' order by asset_id,file_row_number""",
                [],
                f"hf-exceptions-{i}",
            )
            quality = self.w.parquet(
                self.c,
                f"""select 2::USMALLINT as series_id,{i}::UINTEGER as instrument_id,
             cast(timestamp as date) as trade_date,'unknown' as expected_state,NULL::INTEGER as expected_count,
             count(*) as source_observed_count,count(*) filter(where disposition='adopted') as observed_count,
             NULL::INTEGER as missing_count,count(*) filter(where volume=0) as zero_volume_count,
             min(cast(timestamp as time)) as first_label,max(cast(timestamp as time)) as last_label
             from classified group by trade_date order by trade_date""",
                [],
                f"hf-coverage-{i}",
            )
            counts = self.stats()
            assert counts["input_rows"] == sum(int(r["decoded_rows"]) for r in items)
            for asset in source_info:
                assert fingerprint(asset["path"]) == asset["fingerprint"], (
                    "Source changed during batch"
                )
            self.finish(
                key,
                {
                    "source": "hf",
                    "files": [fact] if fact else [],
                    "evidence": [exception, quality],
                    "input_dispositions": counts,
                    "source_assets": source_info,
                    "seconds": time.monotonic() - started,
                    "accounting_rule": "Every source row evaluated once; adopted are in fact; all other row locators in exception file",
                },
            )
            if pos % 10 == 0:
                print(
                    "HF",
                    pos + 1,
                    len(groups),
                    "last",
                    key,
                    counts,
                    "seconds",
                    round(time.monotonic() - started, 2),
                    flush=True,
                )

    def report(self):
        by_source = {}
        files = []
        batches = []
        evidence = []
        for key, body in self.db.execute("select key,value from batches order by key"):
            v = json.loads(body)
            batches.append((key, v))
            files.extend(v.get("files", []))
            evidence.extend(v.get("evidence", []))
            counts = by_source.setdefault(v["source"], Counter())
            counts.update(v["input_dispositions"])
        report = {
            "schema": "ad-market-build-progress.v1",
            "rule_version": RULE,
            "input_dispositions": by_source,
            "target_unique_keys": dict(
                Counter(
                    {
                        t: sum(f["rows"] for f in files if f["table"] == t)
                        for t in ("bars_1d", "bars_1m")
                    }
                )
            ),
            "completed_batches": len(batches),
            "file_count": len(files),
            "bytes": self.w.budget(),
        }
        atomic_write(self.root / "BUILD-PROGRESS.json", canonical(report))
        print(json.dumps(report, default=dict), flush=True)
        return report, files, evidence, batches


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--entry", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("phase", choices=["prepare", "daily", "hf", "report", "core"])
    a = p.parse_args()
    with ReleaseWriter(a.root) as writer:
        m = Merge(a.entry, a.root)
        m.w = writer
        with (m.daily / "writer.lock").open("rb") as source_lock:
            fcntl.flock(source_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            m.prepare()
            if a.phase in ("daily", "core"):
                m.daily_merge()
            if a.phase in ("hf", "core"):
                m.hf_merge()
            m.report()


if __name__ == "__main__":
    main()
