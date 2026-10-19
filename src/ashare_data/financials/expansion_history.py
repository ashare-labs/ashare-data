"""Bind one provider field/version to a verified official historical ledger.

Bindings are reviewed local inputs, not automatic date inference. Unrelated
unproved reports do not veto a field; relevant version uncertainty still does.
No source files, collectors or deployed databases are modified here.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from ashare_data.financials.ledger import FinancialLedger, verify_pack_files
from ashare_data.financials.validation import AcceptanceError


def inside(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("historical evidence escapes its reviewed asset root")
    return path


def verified_bytes(root, ref):
    raw = inside(root, ref["path"]).read_bytes()
    if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("historical evidence checksum mismatch")
    return raw


def bound_history(asset_root, row, task_id, field, as_of):
    """Return this exact field's strict as-of evidence, or a bounded denial.

The registry links an existing official ledger and release proof by hash; a
supplier's booleans or date labels cannot confer historical availability.
"""
    root = Path(asset_root)
    path = root / "historical-bindings.json"
    if not path.exists():
        return {"allowed": False, "reason": "field_has_no_verified_historical_binding"}
    registry = json.loads(path.read_text())
    if registry.get("schema") != "ad.expansion-historical-bindings.v1":
        raise ValueError("unsupported historical binding registry")
    matches = [b for b in registry["bindings"] if b.get("task_id") == task_id
               and b.get("field") == field and all(b.get(k) == row.get(k) for k in
                   ("response_sha256", "source_revision_id", "source_locator"))]
    if len(matches) > 1:
        raise ValueError("ambiguous historical field binding")
    if not matches:
        return {"allowed": False, "reason": "field_has_no_verified_historical_binding"}
    binding = matches[0]
    pack = json.loads(verified_bytes(root, binding["ledger"]))
    evidence_root = inside(root, binding["ledger"]["evidence_root"])
    verify_pack_files(pack, evidence_root)
    ledger = FinancialLedger(pack)
    try:
        selected = ledger.select(row["security"], field, row["period_end"],
                                 as_of=as_of, mode="strict")
    except AcceptanceError as error:
        return {"allowed": False, "reason": "official_version_unavailable:" + str(error)}
    if selected["record_id"] != binding["official_record_id"]:
        return {"allowed": False, "reason": "snapshot_is_not_the_asof_official_version"}
    metric = row.get("metrics", {}).get(field, {})
    if (metric.get("state") != "source_observed" or metric.get("unit") != selected["unit"]
            or Decimal(metric["value"]) != Decimal(selected["value"])
            or any(row.get(k) != selected.get(k) for k in
                   ("security", "period_start", "period_end", "basis", "scope", "currency"))):
        raise ValueError("official/provider field value or reporting scope mismatch")
    report = ledger.reports[selected["report_id"]]
    if report.get("security") != row["security"]:
        raise ValueError("official report issuer/security mismatch")
    # A captured current index and PDF alone are not historical release proof.
    proof = json.loads(verified_bytes(root, binding["release_proof"]))
    required = {
        "schema": "ad.official-report-release-proof.v1",
        "report_id": selected["report_id"], "document_sha256": report["sha256"],
        "published_at": report["published_at"], "available_at": report["available_at"],
        "historical_first_publication_proven": True,
    }
    if any(proof.get(k) != v for k, v in required.items()):
        raise ValueError("release proof does not bind the selected official version")
    if proof.get("evidence_kind") not in {"contemporaneous_release_capture", "verified_historical_archive"}:
        raise ValueError("current index labels are not historical release evidence")
    from ashare_data.financials.validation import instant
    if instant(proof["revision_inventory_complete_through"]) < instant(as_of):
        return {"allowed": False, "reason": "revision_evidence_does_not_cover_requested_cutoff"}
    if not proof.get("sources"):
        raise ValueError("release proof requires captured supporting sources")
    for ref in proof["sources"]:
        verified_bytes(root, ref)
    return {
        "allowed": True, "reason": None, "official_record_id": selected["record_id"],
        "official_report_id": selected["report_id"], "document_sha256": report["sha256"],
        "published_at": report["published_at"], "available_at": report["available_at"],
        "revision_inventory_complete_through": proof["revision_inventory_complete_through"],
        "ledger_sha256": binding["ledger"]["sha256"],
        "release_proof_sha256": binding["release_proof"]["sha256"],
        "value": selected["value"], "field": field,
        "historical_pit_certified": False,
    }
