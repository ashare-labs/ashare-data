"""Bounded raw-to-normalized key/value reconciliation for one collected market day.

Reads only the selected updater root, requires its writer lock and performs no network requests.
The report is small; raw captures and candidate files are not copied.
"""

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import time
from functools import lru_cache
from urllib.parse import parse_qs, urlsplit

from ashare_data.market.increment import normalize_values, sohu_capture
from ashare_data.providers.cmes import native_security
from ashare_data.providers.daily_increment import assess_minutes, equity_security
from ashare_data.storage.daily_increment import IncrementStore
from ashare_data.storage.market_release import FIELDS, MarketRelease, atomic_write, canonical


def values(row):
    from decimal import Decimal
    return tuple(None if row.get(k) is None else Decimal(str(row[k])) for k in FIELDS)


def verify(root, day, candidate=None):
    started = time.monotonic()
    store = IncrementStore(root)
    counts, dispositions, patterns = Counter(), Counter(), Counter()
    missing_assets = []
    try:
        with store.writer():
            store.register_request_cache(day)
            @lru_cache(maxsize=2)
            def capture(asset):
                receipt_row = store.db.execute("SELECT body FROM captures WHERE sha256=?", (asset,)).fetchone()
                if receipt_row is None:
                    return None
                receipt = json.loads(receipt_row[0])
                path = Path(receipt.get("path", receipt.get("capture_path", "")))
                raw = gzip.decompress(path.read_bytes())
                assert hashlib.sha256(raw).hexdigest() == asset, "raw capture changed"
                contexts = [json.loads(r[0]) for r in store.db.execute("SELECT body FROM capture_requests WHERE asset_id=?", (asset,))]
                return receipt, raw, contexts
            coverage = store.db.execute("SELECT * FROM coverage WHERE day=? AND kind IN ('daily','minute') ORDER BY kind,security", (day,)).fetchall()
            for number, entry in enumerate(coverage, 1):
                body = json.loads(entry["body"])
                asset = body.get("source_asset_id")
                frequency = "1d" if entry["kind"] == "daily" else "1m"
                if not asset:
                    counts["unavailable_or_no_capture"] += 1
                    continue
                retained = capture(asset)
                if retained is None:
                    missing_assets.append([entry["security"], asset])
                    continue
                receipt, raw, contexts = retained
                counts["verified_captures"] += 1
                if frequency == "1m":
                    symbol = native_security(entry["security"]).replace(".", "").lower()
                    assert any(parse_qs(urlsplit(r.get("url", "")).query).get("symbol") == [symbol] for r in contexts), "minute request identity missing"
                    parsed, summary = assess_minutes(raw, day, store.config["minute_window"], trade_status=body.get("trade_status"))
                    for key in ("input_rows", "adopted", "equal_duplicate", "resolved_unused", "quarantined", "format_rejected"):
                        dispositions[key] += summary[key]
                        assert summary[key] == body[key], (entry["security"], key, "input accounting differs")
                    expected = {(str(r["label_at"]), values(r)) for r in parsed if r["disposition"] == "adopted"}
                    source = "sina"
                    patterns[json.dumps(dict(observed=summary["observed_count"], missing=summary["missing_regular_labels"],
                                             only_closing_auction=summary["missing_labels"] == [day + " 14:58:00", day + " 14:59:00"]), sort_keys=True)] += 1
                elif body.get("source") == "sohu":
                    symbol = native_security(entry["security"]).replace(".", "").lower()
                    parsed, native = sohu_capture(raw, receipt, symbol, day, day, source_block=body["source_block"])
                    expected = {("", values(normalize_values({k: str(r[k]) if r.get(k) is not None else None
                                                             for k in ("open", "high", "low", "close", "volume", "amount")}))) for r in parsed}
                    source = "sohu"
                    counts["daily_native_rows"] += len(native)
                elif body.get("source") == "baostock":
                    parsed = [r for r in json.loads(raw)["rows"] if equity_security(r.get("code")) == entry["security"] and r.get("date") == day]
                    expected = {("", values(normalize_values(r))) for r in parsed}
                    source = "baostock"
                    counts["daily_native_rows"] += len(parsed)
                else:
                    counts["unsupported_verifier_source"] += 1
                    continue
                actual = {(r["label_at"], values(dict(r))) for r in store.db.execute(
                    "SELECT * FROM observations WHERE source=? AND frequency=? AND security=? AND trade_date=?",
                    (source, frequency, entry["security"], day))}
                assert expected <= actual, (entry["security"], "raw key/value missing or differs")
                counts["verified_key_values"] += len(expected)
                counts["retained_other_versions"] += len(actual - expected)
                if number % 500 == 0:
                    print(json.dumps(dict(event="raw_reconciliation", completed=number, total=len(coverage))), flush=True)
            assert not missing_assets, "referenced raw captures missing"
            assert dispositions["input_rows"] == sum(dispositions[k] for k in ("adopted", "equal_duplicate", "resolved_unused", "quarantined", "format_rejected"))
            duplicate_keys = store.db.execute("SELECT count(*) FROM (SELECT source,frequency,security,trade_date,label_at,row_hash,count(*) n "
                                              "FROM observations WHERE trade_date=? GROUP BY 1,2,3,4,5,6 HAVING n>1)", (day,)).fetchone()[0]
            assert duplicate_keys == 0
            result = dict(day=day, counts=dict(counts), minute_raw_dispositions=dict(dispositions),
                          minute_label_patterns=[dict(json.loads(k), securities=v) for k, v in patterns.items()],
                          duplicate_key_value_rows=duplicate_keys, coverage=store.report(day),
                          elapsed_seconds=time.monotonic() - started, task_bytes=store.budget())
            bao_accounting = store.root / "runs" / day / "baostock-accounting.json"
            if bao_accounting.exists():
                result["baostock_raw_dispositions"] = json.loads(bao_accounting.read_bytes())
                for value in result["baostock_raw_dispositions"].values():
                    assert value["input_rows"] == sum(value[k] for k in ("adopted", "equal_duplicate", "resolved_unused", "quarantined", "format_rejected"))
            if candidate:
                release = MarketRelease(candidate)
                result["candidate"] = dict(release.receipt(), files=len(release.files),
                                           rows=sum(f["rows"] for f in release.files),
                                           mismatches=sum(f["key_value_mismatches"] for f in release.files))
                assert result["candidate"]["mismatches"] == 0
            return result
    finally:
        store.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--candidate")
    args = p.parse_args()
    result = verify(args.root, args.date, args.candidate)
    path = Path(args.root) / "evidence" / ("acceptance-" + args.date + ".json")
    atomic_write(path, canonical(result))
    print(json.dumps(dict(report=str(path), **result), ensure_ascii=False))
