"""Admit valid frozen free-stockdb supplements without intraday source splicing.

Every original native key is classified. New formal rows keep binary64 source
values and source labels; overlap is resolved by a frozen whole-security-day
selection, never by guessed timestamp shifts or numeric rounding.
"""

import argparse
from collections import defaultdict, Counter
from datetime import datetime
from functools import lru_cache
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

import pandas as pd

from ashare_data.storage.market_release import (
    ReleaseWriter,
    canonical,
    atomic_write,
    sha_file,
    sql_literal,
    FIELDS,
)
from repair_market_coverage import Repair, fact, disposition_counts

CORE = ("open", "high", "low", "close", "volume", "amount")
# Labels repeat across securities; cache only the pure, unchanged datetime parser.
_source_datetime = lru_cache(maxsize=200000)(datetime.strptime)


def native_record(key, row, table, code):
    assert key == f"{table}:{code}:{row['date']}" and row["code"] == code
    reason = None
    raw = [row.get(k) for k in CORE]
    try:
        label = _source_datetime(str(row["date"]), "%Y%m%d" if table == "日k" else "%Y%m%d%H%M%S")
    except (ValueError, TypeError):
        label = None
        reason = "invalid_native_label"
    if not all(v is None or (type(v) in (int, float)) for v in raw):
        reason = "unsupported_numeric_type"
    elif any(
        v is not None and (not math.isfinite(v) or (type(v) is int and float(v) != v)) for v in raw
    ):
        reason = "nonfinite_or_inexact_binary64_projection"
    elif (
        any(v is None for v in raw[:4])
        or min(raw[:4]) <= 0
        or raw[1] < max(raw[0], raw[3])
        or raw[2] > min(raw[0], raw[3])
    ):
        reason = "invalid_ohlc"
    elif raw[4] is not None and (
        raw[4] < 0 or int(raw[4]) != raw[4] or raw[4] > 9223372036854775807
    ):
        reason = "invalid_share_quantity"
    elif raw[5] is not None and raw[5] < 0:
        reason = "negative_amount"
    # Units are source-family evidence. VWAP is a diagnostic, not a price rewrite.
    vwap = None if not raw[4] or raw[5] is None else raw[5] / raw[4]
    return dict(
        source_code=code,
        source_key=key,
        native_label=str(row["date"]),
        native_time=label,
        reason=reason,
        **dict(zip(FIELDS, raw)),
        vwap_in_envelope=(
            vwap is None
            or raw[2] is None
            or raw[1] is None
            or raw[2] * 0.98 <= vwap <= raw[1] * 1.02
        ),
    )


class StockRepair(Repair):
    def prepare(self, old_audit, tooling):
        sys.path.insert(0, str(tooling))
        import audit_stockdb_merge as api

        self.api = api
        self.parser_hash = sha_file(Path(__file__))
        self.setting("stockdb_parser_sha256:" + self.parser_hash, self.parser_hash)
        atomic_write(
            self.audit / ("stockdb-parser-" + self.parser_hash + ".py"), Path(__file__).read_bytes()
        )
        self.old = sqlite3.connect(old_audit.as_uri() + "?mode=ro", uri=True)
        self.old_settings = {
            k: json.loads(v) for k, v in self.old.execute("select key,value from metadata")
        }
        self.stock_root = Path(self.doc["assets"]["free-stockdb-external"]["path"]) / "stockdb"
        self.original_inventory = self.old_settings["source_inventory"]
        assert api.inventory(self.stock_root) == self.original_inventory, "Frozen LevelDB changed"
        self.groups = defaultdict(list)
        for code in self.old_settings["equity_codes"]:
            sec = (
                code + ".XSHG"
                if code.startswith("6")
                else code + ".XSHE"
                if code.startswith(("0", "3"))
                else "BJ." + code
            )
            self.groups[self.r.identifiers[sec]].append(code)
        # Early stage must finish before the complement of its accepted keys is taken.
        assert (self.audit / "early-repair-summary.json").exists()
        for (raw,) in self.db.execute("select body from batches where key like 'early:%'"):
            for item in json.loads(raw)["files"]:
                self.files[(item["table"], item["series_id"], item["instrument_id"])] = item

    def read(self, code, table):
        info = self.old_settings.get(f"{table}:{code}:range")
        if not info or info["first"] is None:
            return [], []
        years = (
            ["all"]
            if table == "日k"
            else list(range(int(str(info["first"])[:4]), int(str(info["last"])[:4]) + 1))
        )
        rows = []
        inputs = []
        for year in years:
            batch_key = f"{table}:{code}:{year}"
            previous = self.old.execute(
                "select value from batches where key=?", (batch_key,)
            ).fetchone()
            assert previous is not None
            previous = json.loads(previous[0])
            response, sha, n = self.api.get(previous["query"])
            pairs = self.api.pairs(response, table, code)
            assert sha == previous["raw_response_sha256"] and len(pairs) == previous["rows"], (
                "Source response changed: " + batch_key
            )
            records = [native_record(key, value, table, code) for key, value in pairs]
            assert len({r["source_key"] for r in records}) == len(records)
            rows.extend(records)
            inputs.append(
                {
                    "query": previous["query"],
                    "input_rows": len(records),
                    "response_sha256": sha,
                    "response_bytes": n,
                }
            )
        return rows, inputs

    def run(self, limit=None):
        start = time.monotonic()
        for pos, (instrument, codes) in enumerate(sorted(self.groups.items())):
            if limit is not None and pos >= limit:
                break
            if pos % 25 == 0:
                self.w.budget()
                assert self.api.inventory(self.stock_root) == self.original_inventory
                atomic_write(
                    self.audit / "stockdb-repair-progress.json",
                    canonical(
                        {
                            "position": pos,
                            "instruments": len(self.groups),
                            "elapsed_seconds": time.monotonic() - start,
                            "status": "staged_unpublished",
                        }
                    ),
                )
            for table, series, batch_base in [("日k", 5, 400000), ("分钟k", 4, 500000)]:
                key = "stockdb:" + str(series) + ":" + str(instrument)
                if self.done(key):
                    continue
                rows = []
                inputs = []
                for code in codes:
                    values, receipts = self.read(code, table)
                    rows.extend(values)
                    inputs.extend(receipts)
                if not rows:
                    self.finish(
                        key,
                        {
                            "source": "free_stockdb",
                            "table": table,
                            "series_id": series,
                            "instrument_id": instrument,
                            "files": [],
                            "evidence": [],
                            "input_dispositions": dict(
                                input_rows=0,
                                adopted=0,
                                equal_duplicate=0,
                                resolved_unused=0,
                                quarantined=0,
                                format_rejected=0,
                            ),
                            "inputs": inputs,
                        },
                    )
                    continue
                frame = pd.DataFrame(rows)
                # Explicit coercions preserve all original binary values and integral quantities.
                self.c.register("stock_native", frame)
                cast = ",".join(
                    "cast("
                    + field
                    + " as "
                    + ("BIGINT" if field == "volume_shares" else "DOUBLE")
                    + ") "
                    + field
                    for field in FIELDS
                )
                self.c.execute(
                    "create or replace temp table stock_values as select source_code,source_key,native_label,native_time,cast(native_time as DATE) trade_date,cast(reason as VARCHAR) reason,vwap_in_envelope,"
                    + cast
                    + " from stock_native"
                )
                original = (
                    self.files.get(("bars_1d", 1, instrument))
                    if table == "日k"
                    else self.files.get(("bars_1m", 2, instrument))
                )
                relation = (
                    "select NULL::DATE trade_date where false"
                    if original is None
                    else "select distinct trade_date from read_parquet("
                    + sql_literal(self.r.verify(original))
                    + ")"
                )
                # A new staged early object is not in R1's manifest but is still verified by hash.
                self.c.execute("create or replace temp table primary_days as " + relation)
                if table == "日k" and any(
                    r["native_time"] and r["native_time"].year < 2010 for r in rows
                ):
                    symbols = [
                        (
                            "sh"
                            if code.startswith("6")
                            else "sz"
                            if code.startswith(("0", "3"))
                            else "bj"
                        )
                        + code
                        for code in codes
                    ]
                    with sqlite3.connect(self.daily.as_uri() + "?mode=ro", uri=True) as old_daily:
                        old_daily.row_factory = sqlite3.Row
                        reference = [
                            dict(r)
                            for r in old_daily.execute(
                                "select date,open,high,low,close,volume from bars where date<'2010-01-01' and symbol in ("
                                + ",".join("?" for _ in symbols)
                                + ")",
                                symbols,
                            )
                        ]
                    self.c.register(
                        "historical_reference",
                        pd.DataFrame(
                            reference, columns=["date", "open", "high", "low", "close", "volume"]
                        ),
                    )
                    matched = " AND ".join(
                        "a." + field + '=try_cast(b."' + raw + '" as DOUBLE)'
                        for raw, field in zip(CORE[:4], FIELDS[:4])
                    )
                    self.c.execute(
                        """update stock_values a set reason='historical_units_or_denomination_not_corroborated'
                      where reason is null and trade_date<DATE '2010-01-01'
                      and not exists(select 1 from primary_days p where p.trade_date=a.trade_date)
                      and not ((a.volume_shares=0 and a.turnover_cny=0 or a.volume_shares>0 and a.turnover_cny>0
                      and a.turnover_cny>=a.low_price*greatest(a.volume_shares-100,0)-100
                      and a.turnover_cny<=a.high_price*(a.volume_shares+100)+100)
                      and exists(select 1 from historical_reference b where try_cast(b.date as DATE)=a.trade_date and """
                        + matched
                        + " AND abs(a.volume_shares-try_cast(b.volume as DOUBLE))<=100))"
                    )
                    self.c.unregister("historical_reference")

                comparison = " OR ".join(
                    "min("
                    + field
                    + ") over w IS DISTINCT FROM max("
                    + field
                    + ") over w OR (count("
                    + field
                    + ") over w>0 AND count("
                    + field
                    + ") over w<count(*) over w)"
                    for field in FIELDS
                )
                self.c.execute(
                    "create or replace temp table stock_ranked as select *,row_number() over w rn,("
                    + comparison
                    + ") native_conflict from stock_values window w as (partition by native_time order by source_code rows between unbounded preceding and unbounded following)"
                )
                self.c.execute("""create or replace temp table stock_classified as select s.*,case
                  when reason IN ('invalid_native_label','unsupported_numeric_type','nonfinite_or_inexact_binary64_projection') then 'format_rejected'
                  when reason IS NOT NULL OR native_conflict then 'quarantined'
                  when p.trade_date IS NOT NULL then 'resolved_unused'
                  when rn>1 then 'equal_duplicate' else 'adopted' end disposition,
                  coalesce(reason,case when native_conflict then 'conflicting_native_alias_values'
                  when p.trade_date IS NOT NULL then 'primary_source_security_day_selected_no_intraday_splice'
                  when rn>1 then 'exact_native_alias_duplicate' else 'missing_security_day_source_label_supplement' end) disposition_reason
                  from stock_ranked s left join primary_days p using(trade_date)""")
                batch = batch_base + instrument
                values = ",".join(FIELDS)
                if table == "日k":
                    query = f"""select 5::USMALLINT series_id,{instrument}::UINTEGER instrument_id,trade_date,{values},{batch}::UINTEGER batch_id,
                      (case when starts_with(source_code,'6') then 'sh' when starts_with(source_code,'0') or starts_with(source_code,'3') then 'sz' else 'bj' end)||source_code source_symbol,
                      'free_stockdb'::VARCHAR AS source,NULL::VARCHAR row_hash,NULL::VARCHAR response_id,NULL::VARCHAR observed_at,
                      NULL::VARCHAR preclose,NULL::VARCHAR tradestatus,NULL::VARCHAR is_st
                      from stock_classified where disposition='adopted' ORDER BY trade_date"""
                    target = "bars_1d"
                else:
                    query = f"""select 4::USMALLINT series_id,{instrument}::UINTEGER instrument_id,trade_date,
                      timezone('Asia/Shanghai',native_time) label_at,{values},{batch}::UINTEGER batch_id
                      from stock_classified where disposition='adopted' ORDER BY label_at"""
                    target = "bars_1m"
                item = fact(
                    self.w,
                    self.c,
                    query,
                    "stockdb-fact-" + str(series) + "-" + str(instrument),
                    target,
                    series,
                    instrument,
                )
                ledger = self.w.parquet(
                    self.c,
                    "select source_code,native_label,disposition,disposition_reason from stock_classified ORDER BY source_code,native_label",
                    [],
                    "stockdb-ledger-" + str(series) + "-" + str(instrument),
                )
                ledger.update(
                    kind="input_dispositions",
                    series_id=series,
                    instrument_id=instrument,
                    source="free_stockdb",
                    check_version="stockdb-native-supplement-v2",
                )
                counts = disposition_counts(self.c, "stock_classified")
                assert counts["input_rows"] == len(rows)
                vwap_bad = self.c.execute(
                    "select count(*) from stock_classified where NOT vwap_in_envelope"
                ).fetchone()[0]
                self.finish(
                    key,
                    {
                        "source": "free_stockdb",
                        "table": table,
                        "series_id": series,
                        "instrument_id": instrument,
                        "source_codes": codes,
                        "batch_id": batch if item else None,
                        "source_asset": "free-stockdb",
                        "files": [item] if item else [],
                        "evidence": [ledger],
                        "input_dispositions": counts,
                        "inputs": inputs,
                        "vwap_envelope_diagnostic_outliers": vwap_bad,
                        "new_rows": item["rows"] if item else 0,
                    },
                )
                self.c.unregister("stock_native")
            if pos % 25 == 0:
                print(
                    "STOCK REPAIR",
                    pos + 1,
                    len(self.groups),
                    codes,
                    round(time.monotonic() - start, 1),
                    flush=True,
                )
        assert self.api.inventory(self.stock_root) == self.original_inventory
        total = Counter()
        by_table = defaultdict(Counter)
        for (raw,) in self.db.execute("select body from batches where key like 'stockdb:%'"):
            v = json.loads(raw)
            total.update(v["input_dispositions"])
            by_table[v["table"]].update(v["input_dispositions"])
        finished = self.db.execute(
            "select count(*) from batches where key like 'stockdb:%'"
        ).fetchone()[0] == 2 * len(self.groups)
        atomic_write(
            self.audit / "stockdb-repair-summary.json",
            canonical(
                {
                    "complete": finished,
                    "counts": total,
                    "by_table": by_table,
                    "status": "staged_unpublished",
                    "source_version": self.old_settings["scope"]["source_version"],
                }
            ),
        )
        print("STOCK DONE", finished, total, flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--entry", type=Path, required=True)
    p.add_argument("--old-audit", type=Path, required=True)
    p.add_argument("--tooling", type=Path, required=True)
    p.add_argument("--limit", type=int)
    a = p.parse_args()
    with ReleaseWriter(a.root) as writer:
        m = StockRepair(a.root, a.audit, a.entry, writer)
        m.prepare(a.old_audit, a.tooling)
        m.run(a.limit)


if __name__ == "__main__":
    main()
