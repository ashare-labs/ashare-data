"""Frozen, evidence-qualified corrections to captured price-limit coverage.

Source receipts retain their original code-based denominator. This release-bound
view explains candidate omissions using dated identifiers and existing facts.
A suspended day is not executable; its statutory limit is still unknown.
"""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from ashare_data.core.model import require
from ashare_data.storage.market_release import connection, sha_file
from ashare_data.storage.unit_publication import freeze

CONTRACT = "reference_coverage_resolution"
SCHEMA = "ad.reference-coverage-resolution.v1"
STATES = {
    "outside_identifier_period",
    "covered_identity_alias",
    "suspended_not_executable",
    "unresolved_source_gap",
    "outside_listing_period",
}


def classify(missing, limits, statuses, *, listing_ends=None):
    """Exclusive dispositions; require an exact frozen identity/day match."""
    listing_ends = listing_ends or {}
    indexed = {}
    for name, rows in (("limits", limits), ("statuses", statuses)):
        lookup = {}
        for row in rows:
            key = (row["requested_security"], str(row["trade_date"])[:10])
            lookup.setdefault(key, []).append(row)
        indexed[name] = lookup
    output, seen = [], set()
    for row in missing:
        key = (row["security"], row["trade_date"])
        require(key not in seen, "FIN_COVERAGE", "Duplicate candidate security-day")
        seen.add(key)
        available = indexed["limits"].get(key, [])
        status = indexed["statuses"].get(key, [])
        require(
            all(r["instrument_id"] == row["instrument_id"] for r in available + status),
            "FIN_COVERAGE",
            "Evidence identity differs",
        )
        same_values = {(r["up_limit"], r["down_limit"], r["unit"]) for r in available}
        flags = {str(r["tradestatus"]) for r in status if r["tradestatus"] is not None}
        if row["outside_code_interval"]:
            disposition = "outside_identifier_period"
        elif (
            row["security"] in listing_ends
            and row["trade_date"] >= listing_ends[row["security"]]["valid_to"]
        ):
            disposition = "outside_listing_period"
        elif len(same_values) == 1 and all(
            r["unit"] == "CNY/share"
            and r["up_limit"] is not None
            and r["down_limit"] is not None
            and Decimal(r["up_limit"]) >= Decimal(r["down_limit"]) > 0
            for r in available
        ):
            disposition = "covered_identity_alias"
        elif flags == {"0"}:
            disposition = "suspended_not_executable"
        else:
            disposition = "unresolved_source_gap"
        output.append(
            dict(
                row,
                disposition=disposition,
                limit_observation_ids=json.dumps(sorted(r["observation_id"] for r in available)),
                limit_source_securities=json.dumps(sorted({r["security"] for r in available})),
                status_row_hashes=json.dumps(sorted(r["row_hash"] for r in status)),
                status_sources=json.dumps(sorted({r["source"] for r in status})),
                source_status="0" if flags == {"0"} else None,
                listing_end_evidence=json.dumps(listing_ends.get(row["security"])),
                no_price_limit_inferred=False,
                historical_pit_certified=False,
            )
        )
    return output


def append_to_manifest(writer, parent, manifest, *, rows_path, summary_path, proof_paths):
    """Publish one small immutable correction ledger; no source facts are changed."""
    require(CONTRACT not in parent.manifest, "FIN_COVERAGE", "Coverage correction already exists")
    summary = json.loads(Path(summary_path).read_bytes())
    require(
        summary["parent_manifest_sha256"] == parent.sha256,
        "FIN_COVERAGE",
        "Coverage parent differs",
    )
    value = deepcopy(manifest)
    ledger = freeze(writer, rows_path, "reference_coverage_rows")
    report = freeze(writer, summary_path, "reference_coverage_summary")
    proofs = [freeze(writer, p, "reference_coverage_evidence") for p in proof_paths]
    value[CONTRACT] = dict(
        schema=SCHEMA,
        base_release_id=parent.release_id,
        base_manifest_sha256=parent.sha256,
        rows=ledger,
        summary=report,
        proofs=proofs,
        scope="previously_unvisited_17448_reference_windows",
        raw_receipts_unchanged=True,
        source_alias_not_exchange_code=True,
    )
    existing = {x["path"] for x in value["evidence"]}
    for item in [ledger, report, *proofs]:
        if item["path"] not in existing:
            value["evidence"].append(item)
            existing.add(item["path"])
    validate(writer.root, value)
    return value


def validate(root, manifest):
    contract = manifest[CONTRACT]
    require(contract["schema"] == SCHEMA, "FIN_COVERAGE", "Unknown coverage contract")
    evidence = {(x["path"], x["sha256"], x["bytes"]) for x in manifest["evidence"]}
    for item in [contract["rows"], contract["summary"], *contract["proofs"]]:
        path = Path(root) / item["path"]
        require(
            (item["path"], item["sha256"], item["bytes"]) in evidence
            and path.resolve().is_relative_to(Path(root).resolve())
            and path.stat().st_size == item["bytes"]
            and sha_file(path) == item["sha256"],
            "FIN_COVERAGE",
            "Coverage evidence differs",
        )
    summary = json.loads((Path(root) / contract["summary"]["path"]).read_bytes())
    with connection() as c:
        c.read_parquet(str(Path(root) / contract["rows"]["path"])).create_view("coverage")
        counts = dict(
            c.execute("SELECT disposition,count(*) FROM coverage GROUP BY disposition").fetchall()
        )
        duplicate = c.execute(
            "SELECT count(*) FROM (SELECT security,trade_date FROM coverage GROUP BY ALL HAVING count(*)<>1)"
        ).fetchone()[0]
        invalid = c.execute(
            "SELECT count(*) FROM coverage WHERE (disposition='covered_identity_alias' AND limit_observation_ids='[]') OR (disposition='suspended_not_executable' AND source_status IS DISTINCT FROM '0') OR (disposition='outside_identifier_period' AND NOT outside_code_interval) OR (disposition='outside_listing_period' AND listing_end_evidence='null') OR no_price_limit_inferred OR historical_pit_certified"
        ).fetchone()[0]
    require(
        not duplicate
        and not invalid
        and set(counts) <= STATES
        and counts == summary["dispositions"]
        and sum(counts.values()) == summary["input_candidate_days"],
        "FIN_COVERAGE",
        "Coverage dispositions do not reconcile",
    )
    return summary


def rows(release, *, security=None, day=None):
    contract = release.manifest.get(CONTRACT)
    if contract is None:
        return []
    path = release.verify(contract["rows"])
    with connection() as c:
        c.read_parquet(str(path)).create_view("coverage")
        terms, params = [], []
        if security is not None:
            terms.append("security=?")
            params.append(security)
        if day is not None:
            terms.append("trade_date=?")
            params.append(day)
        cursor = c.execute(
            "SELECT * FROM coverage"
            + (" WHERE " + " AND ".join(terms) if terms else "")
            + " ORDER BY security,trade_date",
            params,
        )
        return [dict(zip([x[0] for x in cursor.description], r)) for r in cursor.fetchall()]


def summary(release):
    contract = release.manifest.get(CONTRACT)
    if contract is None:
        return {"status": "unreviewed", "release_id": release.release_id}
    return json.loads(release.verify(contract["summary"]).read_bytes())
