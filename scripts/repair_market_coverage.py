"""Resumable R1 coverage repair. Raw assets and all published objects stay immutable.

Each instrument is bounded. Source locators, exact numeric roundtrips, dispositions
and the frozen rules are retained. No account or financial-data writes occur.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import resource
import sqlite3

import pandas as pd

from ashare_data.storage.market_release import (
    MarketRelease,
    ReleaseWriter,
    atomic_write,
    canonical,
    connection,
    fingerprint,
    sha_file,
    sql_literal,
    DISPOSITIONS,
    FIELDS,
)

VERSION = "coverage-repair-v2"
EARLY_ELIGIBLE = """a.source='sohu' AND a.volume_shares>=0 AND a.turnover_cny>=0
 AND a.low_price>0 AND a.high_price>=greatest(a.open_price,a.close_price)
 AND a.low_price<=least(a.open_price,a.close_price)
 AND EXISTS (SELECT 1 FROM early_values b WHERE a.date=b.date AND b.source IN ('sina','tencent')
 AND a.open_price=b.open_price AND a.high_price=b.high_price AND a.low_price=b.low_price AND a.close_price=b.close_price
 AND abs(a.volume_shares-b.volume_shares)<=100)
 AND ((a.volume_shares=0 AND a.turnover_cny=0) OR
 (a.volume_shares>0 AND a.turnover_cny>0
 AND a.turnover_cny>=a.low_price*greatest(a.volume_shares-100,0)-100
 AND a.turnover_cny<=a.high_price*(a.volume_shares+100)+100))"""


def disposition_counts(c, table):
    result = dict(
        c.execute("select disposition,count(*) from " + table + " group by disposition").fetchall()
    )
    result["input_rows"] = sum(result.values())
    for key in DISPOSITIONS:
        result.setdefault(key, 0)
    return result


def fact(writer, c, query, name, table, series, instrument):
    key = "trade_date" + (",label_at" if table == "bars_1m" else "")
    count, first, last = c.execute(
        "select count(*),min(trade_date),max(trade_date) from (" + query + ")"
    ).fetchone()
    if not count:
        return None
    duplicates = c.execute(
        "select count(*) from (select "
        + key
        + ",count(*) n from ("
        + query
        + ") group by "
        + key
        + " having n>1)"
    ).fetchone()[0]
    assert duplicates == 0
    item = writer.parquet(c, query, [], name)
    item.update(
        table=table,
        series_id=series,
        instrument_id=instrument,
        rows=count,
        first_date=str(first),
        last_date=str(last),
        duplicate_keys=0,
    )
    return item


class Repair:
    def __init__(self, root, audit, entry, writer):
        self.root, self.audit, self.entry, self.w = root, audit, entry, writer
        audit.mkdir(parents=True, exist_ok=True)
        self.r = MarketRelease(root, 1)
        assert self.r.sha256 == "99f0ed21634408c7b325f851518f9db5ed596484c1b0a143dc11c79e6c543391"
        self.doc = json.loads(entry.read_bytes())
        self.daily = Path(self.doc["assets"]["main"]["path"]) / "daily.sqlite3"
        self.c = connection(audit / "spill", memory="1GB", threads=1)
        self.db = sqlite3.connect(audit / "repair-checkpoint.sqlite3")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS batches(key TEXT PRIMARY KEY,body TEXT); CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,body TEXT);"
        )
        self.parser_hash = sha_file(Path(__file__))
        self.setting("parser_sha256:" + self.parser_hash, self.parser_hash)
        self.setting("rule_contract", {"version": VERSION, "early_eligible": EARLY_ELIGIBLE})
        atomic_write(
            self.audit / ("early-parser-" + self.parser_hash + ".py"), Path(__file__).read_bytes()
        )
        with self.r.metadata() as d:
            self.daily_asset = json.loads(
                d.execute(
                    "select body from source_assets where asset_id='daily-sqlite'"
                ).fetchone()[0]
            )
        self.files = {(x["table"], x["series_id"], x["instrument_id"]): x for x in self.r.files}
        self.original_evidence = {
            (x.get("kind"), x.get("series_id"), x.get("instrument_id")): x
            for x in self.r.manifest["evidence"]
        }

    def setting(self, key, value=None):
        row = self.db.execute("select body from settings where key=?", (key,)).fetchone()
        if row:
            prior = json.loads(row[0])
            assert value is None or prior == value, "Changed frozen setting: " + key
            return prior
        assert value is not None
        self.db.execute("insert into settings values (?,?)", (key, canonical(value).decode()))
        self.db.commit()
        return value

    def done(self, key):
        return self.db.execute("select 1 from batches where key=?", (key,)).fetchone() is not None

    def finish(self, key, value):
        value.update(
            parser_sha256=self.parser_hash,
            rule_version=VERSION,
            completed_at=datetime.now(timezone.utc).isoformat(),
            peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
        self.db.execute("insert into batches values (?,?)", (key, canonical(value).decode()))
        self.db.commit()

    def source_check(self):
        for path, fp in self.daily_asset["fingerprints"].items():
            assert fingerprint(path) == fp, "Daily original changed"

    def early(self):
        self.source_check()
        with sqlite3.connect(self.daily.as_uri() + "?mode=ro", uri=True) as d:
            d.row_factory = sqlite3.Row
            d.execute("BEGIN")
            groups = defaultdict(list)
            for (symbol,) in d.execute("select distinct symbol from bars where date<'2010-01-01'"):
                security = symbol[2:] + (".XSHG" if symbol.startswith("sh") else ".XSHE")
                groups[self.r.identifiers[security]].append(symbol)
            for pos, (instrument, symbols) in enumerate(sorted(groups.items())):
                key = "early:" + str(instrument)
                if self.done(key):
                    continue
                if pos % 50 == 0:
                    self.w.budget()
                    self.source_check()
                rows = [
                    dict(x)
                    for x in d.execute(
                        "select * from bars where date<'2010-01-01' and symbol in ("
                        + ",".join("?" for _ in symbols)
                        + ") order by date,symbol,source",
                        symbols,
                    )
                ]
                self.c.register("early_raw", pd.DataFrame(rows))
                expr = ",".join(
                    'cast("' + raw + '" as DECIMAL(38,12)) ' + target
                    for raw, target in zip(
                        ("open", "high", "low", "close", "volume", "amount"), FIELDS
                    )
                )
                self.c.execute(
                    "create or replace temp table early_values as select *,"
                    + expr
                    + " from early_raw"
                )
                self.c.execute(
                    "create or replace temp table early_selected as select * from early_values a where "
                    + EARLY_ELIGIBLE
                    + " qualify row_number() over(partition by date order by symbol)=1"
                )
                same = " AND ".join("a." + k + " IS NOT DISTINCT FROM b." + k for k in FIELDS)
                self.c.execute(
                    """create or replace temp table early_classified as select a.*,case
                  when b.date is null then 'quarantined'
                  when a.symbol=b.symbol and a.source=b.source then 'adopted'
                  when """
                    + same
                    + """ then 'equal_duplicate' else 'resolved_unused' end disposition,
                  case when b.date is null then 'historical_denomination_or_units_not_corroborated_per_record'
                  when a.symbol=b.symbol and a.source=b.source then 'provider_declared_units_cross_source_and_turnover_corroborated'
                  else 'corroborated_sohu_whole_observation_selected' end reason
                  from early_values a left join early_selected b using(date)"""
                )
                batch = 300000 + instrument
                query = f"""select 1::USMALLINT series_id,{instrument}::UINTEGER instrument_id,cast(date as DATE) trade_date,
                  open_price,high_price,low_price,close_price,cast(volume_shares as BIGINT) volume_shares,turnover_cny,
                  {batch}::UINTEGER batch_id,symbol source_symbol,source,row_hash,response_id,observed_at,preclose,tradestatus,is_st
                  from early_selected"""
                new_rows = self.c.execute("select count(*) from early_selected").fetchone()[0]
                original = self.files.get(("bars_1d", 1, instrument))
                files = []
                if new_rows:
                    complete = query
                    if original:
                        complete = (
                            "select * from read_parquet("
                            + sql_literal(self.r.verify(original))
                            + ") UNION ALL "
                            + query
                        )
                    f = fact(
                        self.w,
                        self.c,
                        complete + " ORDER BY trade_date",
                        "early-fact-" + str(instrument),
                        "bars_1d",
                        1,
                        instrument,
                    )
                    files.append(f)
                ledger_query = "select symbol source_symbol,date,source,row_hash,disposition,reason from early_classified"
                previous = self.original_evidence.get(("input_dispositions", 1, instrument))
                if previous:
                    ledger_query = (
                        "select * from read_parquet("
                        + sql_literal(self.r.verify(previous))
                        + ") where date>='2010-01-01' UNION ALL "
                        + ledger_query
                    )
                ledger = self.w.parquet(
                    self.c,
                    ledger_query + " ORDER BY source_symbol,date,source",
                    [],
                    "early-ledger-" + str(instrument),
                )
                ledger.update(
                    kind="input_dispositions",
                    series_id=1,
                    instrument_id=instrument,
                    check_version=VERSION,
                )
                counts = disposition_counts(self.c, "early_classified")
                assert counts["input_rows"] == len(rows)
                first, last = self.c.execute(
                    "select min(date),max(date) from early_selected"
                ).fetchone()
                self.finish(
                    key,
                    {
                        "source": "early_daily_sqlite",
                        "files": files,
                        "evidence": [ledger],
                        "batch_id": batch if new_rows else None,
                        "instrument_id": instrument,
                        "source_symbols": symbols,
                        "input_rows_sha256": hashlib.sha256(canonical(rows)).hexdigest(),
                        "input_dispositions": counts,
                        "first_adopted": first,
                        "last_adopted": last,
                        "new_rows": new_rows,
                        "source_asset": "daily-sqlite",
                    },
                )
                self.c.unregister("early_raw")
                if pos % 50 == 0:
                    print("EARLY", pos + 1, len(groups), new_rows, counts, flush=True)
        self.source_check()
        totals = Counter()
        for (raw,) in self.db.execute("select body from batches where key like 'early:%'"):
            totals.update(json.loads(raw)["input_dispositions"])
        atomic_write(
            self.audit / "early-repair-summary.json",
            canonical(
                {
                    "counts": totals,
                    "rule": EARLY_ELIGIBLE,
                    "source_units_evidence": "sohu-history-unit-evidence.json",
                    "status": "staged_unpublished",
                }
            ),
        )
        print("EARLY DONE", totals, flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--entry", type=Path, required=True)
    p.add_argument("--phase", choices=["early"], required=True)
    a = p.parse_args()
    with ReleaseWriter(a.root) as writer:
        m = Repair(a.root, a.audit, a.entry, writer)
        with (m.daily.parent / "writer.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            m.early()


if __name__ == "__main__":
    main()
