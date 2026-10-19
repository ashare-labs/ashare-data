"""Bounded, resumable, read-only free-stockdb key/value audit. No source is rewritten.

Unproven units/label semantics remain quarantined at the sealed original asset;
overlap is measured row by row, never promoted into equivalence from total counts.
Requires the optional msgpack==1.1.2 tooling dependency, not the product runtime.
"""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import urllib.parse
import urllib.request

import msgpack
import pandas as pd

from ashare_data.storage.market_release import (
    canonical,
    atomic_write,
    sha_file,
    fingerprint,
    connection,
)


def get(query, limit=None):
    params = {"cmd": "get", "t": query}
    if limit is not None:
        params["num"] = str(limit)
    url = "http://127.0.0.1:7899/?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                raw = response.read(64 * 1024**2 + 1)
            if len(raw) > 64 * 1024**2:
                raise ValueError("64 MiB read bound exceeded; split source range")
            value = msgpack.unpackb(raw, raw=False, strict_map_key=True)
            return value, hashlib.sha256(raw).hexdigest(), len(raw)
        except (TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def inventory(root):
    return {
        str(p.relative_to(root)): fingerprint(p)
        for name in ("data", "data1")
        for p in sorted((root / name).glob("*"))
        if p.is_file() and p.name not in ("LOCK", "LOG", "LOG.old")
    }


def pairs(value, table, code):
    if isinstance(value, dict):
        return [(f"{table}:{code}:{value['date']}", value)]
    if not isinstance(value, list):
        raise ValueError("Unknown source envelope")
    return value


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--entry", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    a = p.parse_args()
    a.root.mkdir(parents=True, exist_ok=True)
    doc = json.loads(a.entry.read_bytes())
    source = Path(doc["assets"]["free-stockdb-external"]["path"]) / "stockdb"
    hf = Path(doc["assets"]["minute-hf-download"]["path"])
    db = sqlite3.connect(a.root / "checkpoint.sqlite3")
    db.executescript(
        "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE IF NOT EXISTS batches(key TEXT PRIMARY KEY,value TEXT);"
    )

    def setting(key, value=None):
        r = db.execute("select value from metadata where key=?", (key,)).fetchone()
        if r:
            prior = json.loads(r[0])
            assert value is None or prior == value, key + " changed"
            return prior
        if value is None:
            return None
        db.execute("insert into metadata values (?,?)", (key, canonical(value).decode()))
        db.commit()
        return value

    prior = setting("source_inventory", inventory(source))
    if setting("source_hashes") is None:
        hashes = {name: sha_file(source / name) for name in prior}
        assert prior == inventory(source), "Source changed during sealing"
        setting("source_hashes", hashes)
    probe = json.loads((a.root.parent / "stockdb-probe.json").read_bytes())
    codes = {
        v for key, values in probe["股票代码"].items() if key not in ("1", "5") for v in values
    }
    codes.update(v[1] for v in probe["退市*"] if v[1][0] not in ("1", "5"))
    codes.update(p.stem for p in (hf / "data/stock_1m").glob("*/*.parquet"))
    daily = Path(doc["assets"]["main"]["path"]) / "daily.sqlite3"
    d = sqlite3.connect(daily.as_uri() + "?mode=ro", uri=True)
    d.row_factory = sqlite3.Row
    codes.update(r[0][2:] for r in d.execute("select symbol from instruments"))
    codes = setting("equity_codes", sorted(codes))
    setting(
        "scope",
        {
            "equity_codes": len(codes),
            "excluded_prefixes": ["1", "5"],
            "universe_completeness": "source-directory union, not independently certified all historical equities",
            "disposition": "all rows quarantined at sealed original LevelDB until units and interval semantics are evidenced",
            "source_version": hashlib.sha256(canonical(setting("source_hashes"))).hexdigest(),
            "consumer": "release quality/source inventory; no unsupported cross-source splice",
        },
    )
    c = connection(a.root / "spill", memory="1GB", threads=1)
    for pos, code in enumerate(codes):
        if pos % 20 == 0:
            assert inventory(source) == prior, "Original LevelDB changed: stop before publish"
            totals = Counter()
            for (v,) in db.execute("select value from batches"):
                value = json.loads(v)
                totals.update({value["table"]: value["rows"]})
            atomic_write(
                a.root / "progress.json",
                canonical({"position": pos, "securities": len(codes), "input_rows": totals}),
            )
        for table in ("日k", "分钟k"):
            index = f"{table}:{code}:range"
            info = setting(index)
            if info is None:
                first, _, _ = get(f"{table}:{code}:*", 1)
                last, _, _ = get(f"{table}:{code}:*", -1)
                first = pairs(first, table, code)
                last = pairs(last, table, code)
                info = setting(
                    index,
                    {
                        "first": first[0][1]["date"] if first else None,
                        "last": last[-1][1]["date"] if last else None,
                    },
                )
            if info["first"] is None:
                continue
            years = (
                ["all"]
                if table == "日k"
                else list(range(int(str(info["first"])[:4]), int(str(info["last"])[:4]) + 1))
            )
            for year in years:
                key = f"{table}:{code}:{year}"
                if db.execute("select 1 from batches where key=?", (key,)).fetchone():
                    continue
                query = f"{table}:{code}:" + ("*" if year == "all" else str(year) + "*")
                value, raw_hash, raw_bytes = get(query)
                entries = pairs(value, table, code)
                seen = set()
                records = []
                invalid = 0
                for source_key, row in entries:
                    # Verify every source key and every core field's representation, including null.
                    assert source_key == f"{table}:{code}:{row['date']}" and row["code"] == code
                    assert source_key not in seen, "Duplicate native KV key"
                    seen.add(source_key)
                    date = str(row["date"])
                    form = "%Y%m%d" if table == "日k" else "%Y%m%d%H%M%S"
                    try:
                        label = datetime.strptime(date, form)
                    except ValueError:
                        invalid += 1
                        continue
                    numeric = [
                        row.get(k) for k in ("open", "high", "low", "close", "volume", "amount")
                    ]
                    assert all(
                        v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))
                        for v in numeric
                    ), "Unexpected native number"
                    records.append((label, *numeric))
                frame = pd.DataFrame(
                    records, columns=["label", "open", "high", "low", "close", "volume", "amount"]
                )
                c.register("stock", frame)
                overlap = equal = 0
                if table == "分钟k":
                    exchange = (
                        "SH"
                        if code.startswith("6")
                        else "SZ"
                        if code.startswith(("0", "3"))
                        else "BJ"
                    )
                    path = hf / f"data/stock_1m/{exchange}/{code}.parquet"
                    if path.exists() and records:
                        cond = " AND ".join(
                            f's."{f}" IS NOT DISTINCT FROM h."{g}"'
                            for f, g in zip(
                                ("open", "high", "low", "close", "volume", "amount"),
                                ("open", "high", "low", "close", "volume", "turnover"),
                            )
                        )
                        overlap, equal = c.execute(
                            "select count(*),count(*) filter(where "
                            + cond
                            + ") from stock s join read_parquet(?) h on s.label=h.timestamp",
                            [str(path)],
                        ).fetchone()
                elif records:
                    symbol = ("sh" if code.startswith("6") else "sz") + code
                    raw_daily = [
                        dict(r)
                        for r in d.execute(
                            "select date,source,open,high,low,close,volume,amount from bars where symbol=?",
                            (symbol,),
                        )
                    ]
                    if raw_daily:
                        daily_frame = pd.DataFrame(raw_daily)
                        c.register("original_daily", daily_frame)
                        overlap = c.execute(
                            "select count(distinct s.label) from stock s join original_daily o on cast(s.label as date)=cast(o.date as date)"
                        ).fetchone()[0]
                        # Daily decimal text versus binary floating is not exact semantic equality.
                        equal = None
                        c.unregister("original_daily")
                result = {
                    "source": "free_stockdb",
                    "table": table,
                    "code": code,
                    "year": year,
                    "query": query,
                    "rows": len(entries),
                    "all_keys_unique": True,
                    "format_rejected": invalid,
                    "quarantined": len(entries) - invalid,
                    "adopted": 0,
                    "equal_duplicate": 0,
                    "resolved_unused": 0,
                    "same_label_overlap_rows": overlap,
                    "same_label_equal_native_numeric_rows": equal,
                    "all_input_keys_and_core_fields_checked": True,
                    "raw_response_sha256": raw_hash,
                    "raw_response_bytes": raw_bytes,
                    "source_version": setting("scope")["source_version"],
                    "destination": "unchanged sealed original LevelDB KV records; explicit unresolved semantic quarantine",
                    "key_value_evidence": "all native keys checked against payload code/date; all core field types inspected; matched keys compared field-by-field; numeric equality does not certify common intervals/units",
                }
                db.execute("insert into batches values (?,?)", (key, canonical(result).decode()))
                db.commit()
                c.unregister("stock")
        if pos % 25 == 0:
            print("STOCKDB", pos + 1, len(codes), code, flush=True)
    assert inventory(source) == prior
    report = {
        "scope": setting("scope"),
        "source_asset": {"root": str(source), "files": setting("source_hashes")},
        "tables": {},
        "completed": True,
    }
    for (v,) in db.execute("select value from batches"):
        row = json.loads(v)
        totals = report["tables"].setdefault(row["table"], Counter())
        totals.update(
            {
                k: row[k]
                for k in (
                    "rows",
                    "quarantined",
                    "format_rejected",
                    "adopted",
                    "equal_duplicate",
                    "resolved_unused",
                    "same_label_overlap_rows",
                )
            }
        )
        if row["same_label_equal_native_numeric_rows"] is not None:
            totals.update(
                {
                    "same_label_equal_native_numeric_rows": row[
                        "same_label_equal_native_numeric_rows"
                    ]
                }
            )
    if "日k" in report["tables"]:
        report["tables"]["日k"]["same_label_equal_native_numeric_rows"] = None
    report["overlap_scope"] = "Same source code/label diagnostic; daily binary versus decimal equality was not assessed. Stable identity mappings are fixed separately in the release."
    atomic_write(a.root / "report.json", canonical(report))
    print(json.dumps(report["tables"]), flush=True)


if __name__ == "__main__":
    main()
