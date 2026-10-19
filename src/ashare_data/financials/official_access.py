"""Field-reviewed official versions backed by existing compressed batch assets."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from ashare_data.financials.expansion import digest, encoded
from ashare_data.financials.ledger import FinancialLedger, FIELDS


def verified_source(root, key):
    if Path(key).name != key:
        raise ValueError("Invalid source receipt key")
    receipt = json.loads((root / "receipts" / (key + ".json")).read_text())
    path = (root / receipt["path"]).resolve()
    if not path.is_relative_to(root.resolve()) or receipt["state"] != "complete":
        raise ValueError("Invalid official evidence path or state")
    raw = gzip.decompress(path.read_bytes())
    if len(raw) != receipt["bytes"] or digest(raw) != receipt["sha256"]:
        raise ValueError("Official evidence checksum mismatch")
    return receipt


def reviewed_ledger(root, review):
    """Build the existing arithmetic ledger from exact, reviewed archive cells.

    The review is field/version-bound, not a blanket issuer approval. Its explicit
    replacement edges require captured supporting notices. Date-only visibility
    continues to fail strict mode. This performs no database import or asset copy.
    """
    root = Path(root).resolve()
    if review.get("schema") != "ad.official-field-review.v1":
        raise ValueError("Unsupported field review")
    reports, observations = {}, []
    for item in review["fields"]:
        if item["field"] not in FIELDS:
            raise ValueError("Ledger currently accepts reviewed YTD flows only")
        path = root / "documents" / (item["symbol"] + "-" + item["announcement_id"] + ".json.gz")
        if not path.resolve().is_relative_to(root):
            raise ValueError("Invalid reviewed document path")
        blob = path.read_bytes()
        if digest(blob) != item["document_derivative_sha256"]:
            raise ValueError("Reviewed derivative changed; repeat field review")
        document = json.loads(gzip.decompress(blob))
        report = document["report"]
        pdf = verified_source(root, document["pdf_receipt"])
        verified_source(root, report["index_receipt"])
        if pdf["sha256"] != item["document_sha256"] or pdf["sha256"] != document["document_sha256"]:
            raise ValueError("Reviewed original PDF changed")
        candidates = [
            r for r in document["cells"] if all(r[k] == item[k] for k in ("field", "value", "page"))
        ]
        if len(candidates) != 1 or candidates[0]["review_status"] == "ambiguous_duplicate_field":
            raise ValueError("Reviewed cell is absent or ambiguous")
        cell = candidates[0]
        if not item.get("layout_reviewed"):
            raise ValueError("Source layout review required")
        for supporting in item.get("supporting_receipts", []):
            verified_source(root, supporting)
        if item.get("replaces") and not item.get("supporting_receipts"):
            raise ValueError("Replacement needs its captured correction evidence")
        rid = item["announcement_id"]
        reports[rid] = {
            "security": cell["security"],
            "published_date": report["published_date"],
            "publication_precision": report["publication_precision"],
            "published_at": None,
            "available_at": None,
            "historical_first_publication_proven": False,
            "complete_revision_history": False,
            "sha256": pdf["sha256"],
            "captured_at": document["observed_at"],
            "source_url": report["source_url"],
        }
        observations.append(
            {
                k: cell[k]
                for k in (
                    "security",
                    "field",
                    "value",
                    "period_start",
                    "period_end",
                    "basis",
                    "scope",
                    "unit",
                    "currency",
                    "page",
                )
            }
            | {
                "record_id": item["record_id"],
                "report_id": rid,
                "replaces_in_this_pack": item.get("replaces"),
                "replacement_scope_reviewed": bool(item.get("replaces") and item.get("supporting_receipts")),
                "field_review_sha256": digest(encoded(item)),
                "source_receipt": document["pdf_receipt"],
                "historical_pit_certified": False,
            }
        )
    return FinancialLedger(
        {"schema": "ad.financial-ledger.v1", "reports": reports, "observations": observations}
    )
