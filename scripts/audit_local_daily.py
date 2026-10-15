#!/usr/bin/env python3
"""Offline integrity/count audit of a real local library; never fetch or repair data."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

from ashare_data.local_daily import DEFAULT_ROOT, LocalDailyStore


def audit(root):
    store = LocalDailyStore(root, readonly=True)
    try:
        store.db.execute("BEGIN")  # One consistent snapshot while another process keeps collecting.
        result = store.summary()
        result["sqlite_quick_check"] = [r[0] for r in store.db.execute("PRAGMA quick_check")]
        result["raw_objects_checked"] = 0
        result["raw_object_errors"] = []
        for (sha,) in store.db.execute(
            "SELECT DISTINCT sha256 FROM responses WHERE sha256 IS NOT NULL"
        ):
            path = store.root / "raw" / (sha + ".gz")
            try:
                actual = hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest()
                if actual != sha:
                    raise ValueError("SHA256 mismatch")
                result["raw_objects_checked"] += 1
            except (OSError, ValueError) as error:
                result["raw_object_errors"].append(dict(sha256=sha, error=str(error)))
        result["source_date_duplicates"] = store.db.execute(
            "SELECT count(*) FROM (SELECT symbol,date,source,count(*) n FROM bars GROUP BY symbol,date,source HAVING n>1)"
        ).fetchone()[0]
        result["non_session_source_observations"] = [
            dict(r)
            for r in store.db.execute(
                "SELECT symbol,date,source,response_id FROM bars b WHERE date BETWEEN ? AND ? "
                "AND NOT EXISTS (SELECT 1 FROM calendar c WHERE c.date=b.date) ORDER BY symbol,date,source",
                (store.meta("start"), store.meta("end")),
            )
        ]
        result["non_session_observation_policy"] = (
            "Retained as source evidence, excluded from get_price and calendar coverage; no automatic date correction."
        )
        result["retired_codes_with_bars"] = store.db.execute(
            "SELECT count(*) FROM instruments i WHERE out IS NOT NULL AND EXISTS (SELECT 1 FROM bars b WHERE b.symbol=i.symbol)"
        ).fetchone()[0]
        result["delisted_with_bars"] = result["retired_codes_with_bars"] - sum(
            bool(
                store.db.execute(
                    "SELECT 1 FROM instruments i WHERE symbol=? AND out IS NOT NULL AND EXISTS (SELECT 1 FROM bars b WHERE b.symbol=i.symbol)",
                    (e["old_symbol"],),
                ).fetchone()
            )
            for e in store.code_events()
        )
        result["unattempted_symbols"] = [
            r[0]
            for r in store.db.execute(
                "SELECT symbol FROM instruments i WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.symbol=i.symbol AND j.kind='bars' AND (j.attempts>0 OR j.state IN ('done','empty_unknown','failed','blocked'))) ORDER BY symbol"
            )
        ]
        result["bao_captures_checked"] = 0
        result["bao_capture_errors"] = []
        if store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_captures'"
        ).fetchone():
            from ashare_data.local_baostock import read_capture, verify_capture

            for row in store.db.execute("SELECT id,path FROM source_captures"):
                try:
                    envelope = read_capture(row["path"])
                    verify_capture(envelope)
                    if envelope["receipt"]["receipt_id"] != row["id"]:
                        raise ValueError("database/capture identity mismatch")
                    result["bao_captures_checked"] += 1
                except (OSError, ValueError) as error:
                    result["bao_capture_errors"].append(dict(id=row["id"], error=str(error)))
        result["request_failures"] = [
            dict(r)
            for r in store.db.execute(
                "SELECT symbol,kind,source,start,end,state,error FROM jobs WHERE state IN ('failed','blocked') ORDER BY symbol,kind"
            )
        ]
        result["successful_empty_response_jobs"] = store.db.execute(
            "SELECT count(*) FROM jobs WHERE state='empty_unknown'"
        ).fetchone()[0]
        result["empty_response_details"] = (
            "jobs.csv; a successful empty source response is not proof of a suspension or an acquisition failure"
        )
        crosscheck = store.root / "reports" / "sample-price-crosscheck.json"
        if crosscheck.exists():
            checked = json.loads(crosscheck.read_text())
            result["independent_crosscheck"] = dict(
                report=str(crosscheck),
                at=checked["at"],
                bar_snapshot_at=checked.get("bar_snapshot_at"),
                read_consistency=checked.get("read_consistency"),
                scope_note=checked.get("scope_note"),
                sample_definition_report=checked.get("sample_definition_report"),
                sample_definition_report_at=checked.get("sample_definition_report_at"),
                securities=len(checked["sample_symbols"]),
                third_source_windows=checked["window_count"],
                checked_price_field_disagreements=len(checked["disagreements"]),
                request_errors=checked["request_errors"],
                source_consistency_counts=checked["counts"],
                interpretation=checked["interpretation"],
            )
        result["crosscheck_snapshot_policy"] = (
            "Cross-check statistics retain their own snapshot time and sample scope; they are not "
            "coverage or an exchange truth certificate. The early mixed-source crosscheck.json probe "
            "is excluded from this audit."
        )
        return result
    finally:
        store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = p.parse_args()
    result = audit(args.root)
    (args.root / "reports" / "integrity.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(
        bool(
            result["sqlite_quick_check"] != ["ok"]
            or result["raw_object_errors"]
            or result["bao_capture_errors"]
            or result["source_date_duplicates"]
        )
    )


if __name__ == "__main__":
    sys.exit(main())
