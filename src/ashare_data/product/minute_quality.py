"""Read-time missing quotes, backed by the release's existing invalidity evidence."""

import json
from datetime import timezone
from zoneinfo import ZoneInfo

from ashare_data.core.model import require
from ashare_data.research.minute_policy import HARD_ACTIVITY, native_denials
from ashare_data.storage.market_release import connection

QUOTE_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def standard_quote(row):
    """An invalid observation is one unavailable quote; conflict alone is not invalidity.

    An OHLC contradiction does not identify which price is wrong. Preserve the
    source/time/reasons, but do not offer any part of that quote as a normal bar.
    Explicit native-source readers retain the original values for diagnosis.
    """
    invalid_price = row.get("price_status") == "invalid"
    activity = set(row.get("diagnostics", [])) & HARD_ACTIVITY
    fields = (
        list(QUOTE_FIELDS)
        if invalid_price
        else [
            f for f in ("volume", "amount") if any(reason.endswith("_" + f) for reason in activity)
        ]
    )
    if not fields:
        return row
    return {
        **row,
        **dict.fromkeys(fields),
        "quote_status": "missing" if invalid_price else "partial_missing",
        "missing_reason": "confirmed_invalid_price" if invalid_price else "unavailable_activity",
        "missing_fields": fields,
        "use_decision": native_denials(
            {**row, "price_status": row.get("price_status", "unverified")}
        ),
    }


def released_invalid_slots(release, security, series_ids, start, end):
    """Recover only known, quarantined invalid labels, without opening raw bars.

    The HF issue ledger has native (UTC+8) timestamps and source row numbers.
    Join those to the frozen review using source hash AND row range. A quarantine
    or a reference conflict by itself is insufficient evidence of a bad price.
    Other issue formats are deliberately outside this reader's contract.
    """
    instrument = release.identifiers[security]
    issues = [
        item
        for item in release.manifest.get("evidence", [])
        if item.get("kind") == "issues"
        and item.get("instrument_id") == instrument
        and item.get("series_id") == 2
        and 2 in series_ids
        and item.get("check_version") in {"hf-observation-v1", "hf-research-restrictions-v2"}
    ]
    if not issues:
        return []
    with release.metadata() as db:
        if not db.execute(
            "select 1 from sqlite_master where type='table' and name='release_contracts'"
        ).fetchone():
            return []
        contract = db.execute(
            "select body from release_contracts where name='source_observation_readers'"
        ).fetchone()
        if not contract:
            return []
        binding = json.loads(contract[0])["minute_sources"].get("neigezhu")
        if not binding:
            return []
        evidence = {item["sha256"]: item for item in release.manifest.get("evidence", [])}
        review_sha = binding["review"]["sha256"]
        require(review_sha in evidence, "MARKET_MINUTE_EVIDENCE", "Frozen review is absent")
        review = json.loads(release.verify(evidence[review_sha]).read_bytes())
        require(
            review["schema"] == "neigezhu-quality-overlay.v1"
            and review["manifest_sha256"] == binding["manifest"]["sha256"],
            "MARKET_MINUTE_EVIDENCE",
            "Review must bind the frozen source manifest",
        )
        rules = [r for r in review["ranges"] if r["price_status"] == "invalid"]
        if not rules:
            return []
        local = ZoneInfo("Asia/Singapore")  # Same native-label conversion as the HF publisher.
        bounds = [t.astimezone(local).replace(tzinfo=None) for t in (start, end)]
        slots = {}
        c = connection(memory="128MB", threads=1)
        try:
            for item in issues:
                cursor = c.execute(
                    "select asset_id,file_row_number,timestamp from read_parquet(?) "
                    "where disposition='quarantined' and timestamp between ? and ?",
                    [str(release.verify(item)), *bounds],
                )
                records = (row for batch in iter(lambda: cursor.fetchmany(4096), []) for row in batch)
                for asset_id, source_row, label in records:
                    asset_record = db.execute(
                        "select body from source_assets where asset_id=?", (asset_id,)
                    ).fetchone()
                    require(
                        asset_record is not None, "MARKET_MINUTE_EVIDENCE", "Source asset absent"
                    )
                    asset = json.loads(asset_record[0])
                    matched = [
                        r
                        for r in rules
                        if asset["sha256"] == r["source_sha256"]
                        and asset["path"].endswith("/" + r["path"])
                        and r["first_row"] <= source_row <= r["last_row"]
                    ]
                    if not matched:
                        continue
                    require(
                        asset["instrument_id"] == instrument
                        and asset["source_version"] == review["manifest_sha256"],
                        "MARKET_MINUTE_EVIDENCE",
                        "Source asset differs from frozen review",
                    )
                    label = label.replace(tzinfo=local)
                    published_label = label.astimezone(timezone.utc).isoformat()
                    row = slots.setdefault(
                        label,
                        {
                            "security": security,
                            "instrument_id": instrument,
                            "series_id": 2,
                            "source": "neigezhu",
                            "trade_date": str(label.date()),
                            "source_label": published_label,
                            "label_at": published_label,
                            "price_status": "invalid",
                            "interval_status": "unknown",
                            "historical_pit": False,
                            "diagnostics": [],
                            "missing_evidence": [],
                        },
                    )
                    row["diagnostics"] = sorted(
                        set(row["diagnostics"]) | {r["rule"] for r in matched}
                    )
                    row["missing_evidence"].append(
                        {
                            "source_path": asset["path"],
                            "source_file_sha256": asset["sha256"],
                            "source_row": source_row,
                            "source_label": label.isoformat(),
                            "review_sha256": review_sha,
                            "issues_sha256": item["sha256"],
                            "disposition": "quarantined",
                        }
                    )
        finally:
            c.close()
    return [standard_quote(row) for row in slots.values()]
