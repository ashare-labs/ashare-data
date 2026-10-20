"""Bounded append-only market calendar corrections from retained source evidence."""

from datetime import date, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from ashare_data.core.model import require
from ashare_data.financials import tables as t
from ashare_data.storage.market_release import canonical, sha_file

SCHEMA = "ad.market-calendar-extension.v1"
SCOPE = "Sina Shanghai common SH/SZ session calendar, corroborated by official XSHE monthly flags; no security tradability or historical PIT certification"


def reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha_file(path), "bytes": path.stat().st_size}


def verify_file(item):
    path = Path(item["path"])
    require(path.stat().st_size == item["bytes"] and sha_file(path) == item["sha256"],
            "CALENDAR_EVIDENCE", "Calendar source or prepared metadata changed")
    return path


def _proof(parent, *, end, sina_raw, sina_response_id, szse_root):
    from ashare_data.providers import daily_sources
    from ashare_data.financials import expansion

    end_day = date.fromisoformat(end)
    with parent.metadata() as old:
        latest = old.execute("SELECT max(date) FROM calendar").fetchone()[0]
        response = old.execute("SELECT * FROM responses WHERE id=?", (sina_response_id,)).fetchone()
    require(response is not None and response[1] == "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"
            and response[5] == 200, "CALENDAR_EVIDENCE", "Use the frozen source calendar receipt")
    first_day = date.fromisoformat(latest) + timedelta(days=1)
    require(first_day <= end_day and (end_day - first_day).days < 31
            and first_day.strftime("%Y-%m") == end[:7],
            "CALENDAR_SCOPE", "Only extend a bounded current month")
    raw = gzip.decompress(Path(sina_raw).read_bytes())
    require(hashlib.sha256(raw).hexdigest() == response[4] and len(raw) == response[7],
            "CALENDAR_EVIDENCE", "Sina original does not match the frozen response")
    source_days = [x[:10] for x in daily_sources.decode_sina(raw, "datelist")]
    require(source_days == sorted(set(source_days)) and source_days[-1] >= end,
            "CALENDAR_EVIDENCE", "Source calendar is incomplete or ambiguous")
    root = Path(szse_root)
    receipt_path = root / "receipts" / ("szse_calendar-" + end[:7] + ".json")
    receipt = json.loads(receipt_path.read_bytes())
    attempt = receipt["attempts"][-1]
    require(attempt["http_status"] == 200 and attempt["url"] ==
            "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=" + end[:7],
            "CALENDAR_EVIDENCE", "Use the captured official monthly calendar endpoint")
    official_raw = root / receipt["raw"]["path"]
    require(sha_file(official_raw) == receipt["raw"]["sha256"]
            and official_raw.stat().st_size == receipt["raw"]["bytes"],
            "CALENDAR_EVIDENCE", "Official month original changed")
    official_body = gzip.decompress(official_raw.read_bytes())
    require(hashlib.sha256(official_body).hexdigest() == receipt["attempts"][-1]["response_sha256"],
            "CALENDAR_EVIDENCE", "Official response body differs from its capture")
    official, coverage = expansion.parse_calendar(
        official_body, {"month": end[:7]}, first_day.isoformat(), end,
    )
    require(coverage["source_month_complete"], "CALENDAR_EVIDENCE", "Official month has missing dates")
    flags = {x["date"]: x["is_open"] for x in official}
    all_dates = [(first_day + timedelta(days=i)).isoformat()
                 for i in range((end_day - first_day).days + 1)]
    require(set(flags) == set(all_dates) and all(flags[d] == (d in source_days) for d in all_dates),
            "CALENDAR_CONFLICT", "Common calendar and official XSHE flags differ")
    added = [{"date": d, "source": "sina_trading_dates_sh", "receipt": sina_response_id}
             for d in all_dates if flags[d]]
    require(added, "CALENDAR_SCOPE", "No new evidenced sessions")
    evidence_paths = [
        Path(sina_raw), official_raw, receipt_path, Path(__file__),
        Path(daily_sources.__file__), Path(daily_sources.__file__).with_name("sina-decode.js"),
        Path(expansion.__file__),
    ]
    evidence = [reference(p) for p in evidence_paths]
    proof = {
        "schema": SCHEMA, "parent_release_id": parent.release_id,
        "parent_manifest_sha256": parent.sha256, "calendar_scope": SCOPE,
        "inputs": {"sina_raw": str(Path(sina_raw).resolve()),
                   "sina_response_id": sina_response_id, "szse_root": str(root.resolve())},
        "first": first_day.isoformat(), "last": end, "added_sessions": added,
        "official_flags": official, "source_coverage": coverage, "evidence": evidence,
        "sina_received_at": response[3], "szse_received_at": receipt["observed_at"],
        "new_network_requests": 0, "historical_pit_certified": False,
        "security_tradability_proven": False, "source_originals_modified": False,
    }
    return proof


def prepare(parent, output, *, end, sina_raw, sina_response_id, szse_root):
    """Reuse retained originals; perform no HTTP or source-store mutation."""
    output = Path(output)
    if (output / "plan.json").exists():
        return verify_plan(parent, output / "plan.json")
    require(not output.exists(), "CALENDAR_OUTPUT", "Use a new controlled preparation directory")
    proof = _proof(parent, end=end, sina_raw=sina_raw,
                   sina_response_id=sina_response_id, szse_root=szse_root)
    added, evidence = proof["added_sessions"], proof["evidence"]
    mapping = hashlib.sha256(canonical(proof)).hexdigest()
    output.mkdir(parents=True)
    metadata = output / "metadata.sqlite3"
    shutil.copyfile(parent.metadata_path, metadata)
    expected = {name: [] for name in ("calendar", "metadata", "source_assets", "data_batches", "batch_inputs")}
    with sqlite3.connect(metadata) as db:
        db.execute("PRAGMA foreign_keys=ON")
        batch_id = db.execute("SELECT coalesce(max(batch_id),0)+1 FROM data_batches").fetchone()[0]
        batch_key = t.identity("calendar-batch", parent.sha256, mapping)
        body = canonical(dict(proof, mapping_sha256=mapping)).decode()
        batch = [batch_id, batch_key, parent.release_id + 1, sha_file(__file__), mapping, body]
        db.execute("INSERT INTO data_batches VALUES(?,?,?,?,?,?)", batch)
        expected["data_batches"].append(batch)
        for item in evidence:
            asset = t.identity("calendar-source-asset", item)
            asset_body = canonical(dict(item, kind="calendar_extension_evidence",
                                        retention="original_retained_and_publication_copy")).decode()
            db.execute("INSERT INTO source_assets VALUES(?,?)", (asset, asset_body))
            db.execute("INSERT INTO batch_inputs VALUES(?,?)", (batch_id, asset))
            expected["source_assets"].append([asset, asset_body])
            expected["batch_inputs"].append([batch_id, asset])
        for item in added:
            row = [item[k] for k in ("date", "source", "receipt")]
            db.execute("INSERT INTO calendar VALUES(?,?,?)", row)
            expected["calendar"].append(row)
        marker = ["calendar_extension:" + mapping, canonical({"batch_id": batch_id, "mapping_sha256": mapping}).decode()]
        db.execute("INSERT INTO metadata VALUES(?,?)", marker)
        expected["metadata"].append(marker)
    plan = dict(proof, mapping_sha256=mapping, metadata=reference(metadata),
                expected_insertions=expected)
    (output / "plan.json").write_bytes(canonical(plan))
    return verify_plan(parent, output / "plan.json")


def verify_plan(parent, path):
    """Verify exact metadata deltas; original rows and unrelated tables must match."""
    plan = json.loads(Path(path).read_bytes())
    require(plan["schema"] == SCHEMA and plan["parent_manifest_sha256"] == parent.sha256
            and plan["parent_release_id"] == parent.release_id,
            "CALENDAR_PARENT", "Calendar plan belongs to another frozen parent")
    for item in plan["evidence"]:
        verify_file(item)
    metadata = verify_file(plan["metadata"])
    proof = {k: v for k, v in plan.items()
             if k not in {"mapping_sha256", "metadata", "expected_insertions"}}
    require(hashlib.sha256(canonical(proof)).hexdigest() == plan["mapping_sha256"],
            "CALENDAR_EVIDENCE", "Calendar mapping changed")
    require(proof == _proof(parent, end=plan["last"], **plan["inputs"]),
            "CALENDAR_EVIDENCE", "Calendar mapping differs from retained source values")
    expected_rows = {name: [] for name in ("calendar", "metadata", "source_assets", "data_batches", "batch_inputs")}
    with parent.metadata() as old:
        batch_id = old.execute("SELECT coalesce(max(batch_id),0)+1 FROM data_batches").fetchone()[0]
    mapping = plan["mapping_sha256"]
    batch_key = t.identity("calendar-batch", parent.sha256, mapping)
    expected_rows["data_batches"] = [[batch_id, batch_key, parent.release_id + 1, sha_file(__file__),
                                     mapping, canonical(dict(proof, mapping_sha256=mapping)).decode()]]
    for item in plan["evidence"]:
        asset = t.identity("calendar-source-asset", item)
        body = canonical(dict(item, kind="calendar_extension_evidence",
                              retention="original_retained_and_publication_copy")).decode()
        expected_rows["source_assets"].append([asset, body])
        expected_rows["batch_inputs"].append([batch_id, asset])
    expected_rows["calendar"] = [[item[k] for k in ("date", "source", "receipt")]
                                 for item in plan["added_sessions"]]
    expected_rows["metadata"] = [["calendar_extension:" + mapping,
                                  canonical({"batch_id": batch_id, "mapping_sha256": mapping}).decode()]]
    require(plan["expected_insertions"] == expected_rows,
            "CALENDAR_METADATA", "Declared insertions do not match the source correction")
    uri = metadata.as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as db:
        db.execute("ATTACH DATABASE ? AS previous",
                   (parent.metadata_path.as_uri() + "?mode=ro&immutable=1",))
        tables = list(db.execute("SELECT name,sql FROM previous.sqlite_master WHERE type='table'"))
        require(tables == list(db.execute("SELECT name,sql FROM main.sqlite_master WHERE type='table'")),
                "CALENDAR_METADATA", "Calendar update changed metadata schemas")
        for table, _ in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            require(db.execute("SELECT count(*) FROM (SELECT * FROM previous." + quoted
                               + " EXCEPT SELECT * FROM main." + quoted + ")").fetchone()[0] == 0,
                    "CALENDAR_METADATA", "Calendar update removed or changed old rows: " + table)
            actual = [list(x) for x in db.execute("SELECT * FROM main." + quoted
                                                + " EXCEPT SELECT * FROM previous." + quoted)]
            expected = plan["expected_insertions"].get(table, [])
            require(sorted(actual) == sorted(expected),
                    "CALENDAR_METADATA", "Unexpected new metadata rows: " + table)
        require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                "CALENDAR_METADATA", "Prepared metadata failed integrity check")
        for item in plan["added_sessions"]:
            require(db.execute("SELECT source,receipt FROM calendar WHERE date=?", (item["date"],)).fetchone()
                    == (item["source"], item["receipt"]),
                    "CALENDAR_METADATA", "A calendar session differs from the verified mapping")
    return plan
