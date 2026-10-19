"""Read provider snapshots through explicit time, code and field-use constraints.

This module does not change captures, the active collector, or FinancialLedger.
Audit access preserves questionable records. Calculation access returns one
checked value, never an unrestricted bag of source fields.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path

from ashare_data.financials.expansion import load_batch, security, verify_receipt
from ashare_data.financials.expansion_history import bound_history

SHANGHAI = timezone(timedelta(hours=8))
PURPOSES = ("historical_pit", "source_research", "listed_security", "source_audit")
SCHEMAS = {"ad.financial-source-report.v1", "ad.corporate-source-observation.v1"}
SHARE_COMPONENTS = {"float_a_shares", "float_b_shares", "float_h_shares"}


class SourceUseDenied(ValueError):
    def __init__(self, reasons, decision=None):
        self.reasons = list(dict.fromkeys(reasons))
        self.decision = decision
        super().__init__("; ".join(self.reasons))


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("a timezone-aware timestamp is required")
    return result


def announcement_day(row):
    if row["schema"] == "ad.financial-source-report.v1":
        return row.get("announcement_date")
    item = row.get("observation", {})
    return item.get("announcement_date") or item.get("fields", {}).get("announcement_date")


def share_overlap(row):
    """Use recorded precision to avoid declaring harmless rounding a conflict."""
    item = row.get("observation", {})
    fields = item.get("fields", {})
    if row.get("category") != "shares" or fields.get("total_shares") is None:
        return False
    present = [name for name in SHARE_COMPONENTS if fields.get(name) is not None]
    excess = sum((Decimal(fields[name]) for name in present), Decimal(0)) - Decimal(fields["total_shares"])
    # Half a reported quantum per printed component and total; never manufacture values.
    quantum = item.get("source_quantum_shares", {})
    tolerance = sum((Decimal(quantum.get(name) or 0) / 2 for name in present + ["total_shares"]), Decimal(0))
    return excess > tolerance


def assess(row, task, *, purpose="historical_pit", as_of=None, field=None, review=None, history=None):
    if purpose not in PURPOSES:
        raise ValueError("unknown use purpose")
    if row.get("schema") not in SCHEMAS:
        raise ValueError("unsupported snapshot schema")
    if row.get("security") != security(task["symbol"]):
        raise ValueError("record and source task security disagree")
    cutoff = timestamp(as_of) if as_of is not None else None
    if purpose != "source_audit" and cutoff is None:
        raise ValueError("calculation access requires as_of; audit access is explicitly separate")
    captured = timestamp(row["observed_at"])
    announced = announcement_day(row)
    announced = date.fromisoformat(announced) if announced else None
    financial = row["schema"] == "ad.financial-source-report.v1"
    proof_by = None
    if review and field in review.get("verified_fields", {}):
        value = row.get("metrics", {}).get(field, {}).get("value")
        if value is not None and Decimal(value) == Decimal(review["verified_fields"][field]["value"]):
            proof_by = timestamp(review["evidence_available_by"])
    observed_official_value = bool(proof_by and cutoff and cutoff >= proof_by)
    historical_value = bool(purpose == "historical_pit" and history and history.get("allowed"))
    period = row.get("period_end") if financial else row.get("observation", {}).get("date")
    code_exit = bool(period and task.get("out") and period >= task["out"])
    code_before_ipo = bool(period and task.get("ipo") and period < task["ipo"])
    findings = []
    if announced and announced > captured.astimezone(SHANGHAI).date():
        findings.append("source_date_label_after_capture_day")
    if code_exit:
        findings.append("period_outside_source_code_lifetime")
    if code_before_ipo:
        findings.append("period_before_source_code_ipo")
    overlap = share_overlap(row)
    if overlap:
        findings.append("source_share_class_overlap_unresolved")
    if row.get("period_after_code_exit") != code_exit and financial:
        findings.append("stored_lifetime_flag_disagrees_with_task")

    reasons = []
    if purpose != "source_audit":
        if cutoff < captured and not historical_value:
            reasons.append("snapshot_not_yet_captured_at_requested_time")
        if announced and announced > cutoff.astimezone(SHANGHAI).date() and not (observed_official_value or historical_value):
            reasons.append("announcement_date_after_requested_day")
        if "source_date_label_after_capture_day" in findings and not (observed_official_value or historical_value):
            # A later wall clock alone cannot repair a suspect historical date/version.
            reasons.append("source_announcement_after_capture_day_requires_review")
        if purpose == "historical_pit" and not historical_value:
            # Only a checked official field/version binding can supply historical evidence.
            reasons.append((history or {}).get("reason") or "field_has_no_verified_historical_binding")
        if purpose in {"listed_security", "historical_pit"} and (code_exit or code_before_ipo):
            reasons.append("report_not_applicable_to_requested_code_lifetime")
        if financial and announced is None and not historical_value:
            reasons.append("source_announcement_date_unknown")
        if not financial and period and date.fromisoformat(period) > cutoff.astimezone(SHANGHAI).date():
            reasons.append("source_effective_date_after_requested_day")
        if not financial and row.get("category") != "shares":
            reasons.append("corporate_action_calculation_requires_its_event_specific_contract")
        if overlap and field in SHARE_COMPONENTS:
            reasons.append("share_class_value_not_independently_usable")
        if field is None:
            reasons.append("choose_one_canonical_field_for_calculation")
    return {
        "schema": "ad.source-use-decision.v1", "purpose": purpose,
        "allowed": not reasons, "reasons": list(dict.fromkeys(reasons)),
        "findings": findings, "historical_pit_certified": False,
        "code_mapping_applied": False, "carry_forward_authorized": False,
        "record_invalidated": False,
        "official_value_observed_by": proof_by.isoformat() if proof_by else None,
        "disclosure_context": review.get("disclosure_context") if review else None,
        "date_label_overridden_for_current_research_only": observed_official_value and purpose in {"source_research", "listed_security"},
        "historical_field_evidence": history if purpose == "historical_pit" else None,
        "time_basis": ("verified official field/version historical availability" if historical_value
                       else "capture cutoff for this source snapshot, not reconstructed public availability"),
    }


class SnapshotReader:
    """The supported downstream entry point for financial/reference expansion."""
    def __init__(self, root):
        self.root, self.manifest, tasks = load_batch(root)
        self.tasks = {task["id"]: task for task in tasks}
        universe = json.loads((self.root / "universe.json").read_text())
        self.code_events = universe.get("code_events", [])
        self.asset_root = self.root.parent.parent
        self.reviews = []
        review_path = self.asset_root / "usage-reviews.json"
        if review_path.exists():
            content = json.loads(review_path.read_text())
            if content.get("schema") != "ad.expansion-usage-reviews.v1":
                raise ValueError("unsupported source-use review schema")
            self.reviews = content["records"]

    def _review(self, row, task_id):
        matches = [review for review in self.reviews if review.get("task_id") == task_id
                   and all(review.get(key) == row.get(key) for key in
                           ("response_sha256", "source_revision_id", "source_locator"))]
        if len(matches) > 1:
            raise ValueError("ambiguous source-use reviews")
        if not matches:
            return None
        review = matches[0]
        if review.get("outcome") != "official_current_document_values_verified_no_first_release_claim":
            raise ValueError("unsupported source-use review outcome")
        if review.get("disclosure_context") not in {"current_period", "later_report_comparative_column"}:
            raise ValueError("unknown reviewed disclosure context")
        if not review.get("evidence") or not review.get("verified_fields"):
            raise ValueError("review requires bounded official evidence and specific verified fields")
        clocks = []
        for evidence in review["evidence"]:
            path = (self.asset_root / evidence["path"]).resolve()
            if not path.is_relative_to(self.asset_root.resolve()):
                raise ValueError("review evidence escapes the asset root")
            raw = path.read_bytes()
            if len(raw) != evidence["bytes"] or hashlib.sha256(raw).hexdigest() != evidence["sha256"]:
                raise ValueError("review evidence checksum mismatch")
            clocks.append(timestamp(evidence["received_at"]))
        if timestamp(review["evidence_available_by"]) < max(clocks):
            raise ValueError("review cannot predate its captured official evidence")
        return review

    def read(self, task_id, *, period=None, locator=None, field=None,
             purpose="historical_pit", as_of=None):
        if task_id not in self.tasks or (period is None) == (locator is None):
            raise ValueError("choose a known task and exactly one period or source locator")
        task = self.tasks[task_id]
        receipt_path = self.root / "receipts" / (task_id + ".json")
        if not receipt_path.exists():
            raise SourceUseDenied(["task_has_not_been_captured"])
        receipt = json.loads(receipt_path.read_text())
        if receipt["state"] not in {"complete", "partial_source"}:
            raise SourceUseDenied(["task_has_no_accepted_normalized_records:" + receipt["state"]])
        verify_receipt(self.root, receipt)
        selected = []
        with gzip.open(self.root / receipt["normalized"]["path"], "rt") as stream:
            for line in stream:
                row = json.loads(line)
                row_locator = row.get("source_locator") or row.get("observation", {}).get("locator")
                if (period is not None and row.get("period_end") == period) or (locator is not None and row_locator == locator):
                    selected.append(row)
        if len(selected) != 1:
            raise SourceUseDenied(["source_record_missing_or_ambiguous"])
        row = selected[0]
        if row["observed_at"] != receipt["observed_at"] or row["response_sha256"] != receipt["attempts"][-1]["response_sha256"]:
            raise ValueError("record provenance differs from its verified receipt")
        review = self._review(row, task_id)
        history = (bound_history(self.asset_root, row, task_id, field, as_of)
                   if purpose == "historical_pit" and as_of is not None
                   and row["schema"] == "ad.financial-source-report.v1" else None)
        decision = assess(row, task, purpose=purpose, as_of=as_of, field=field, review=review, history=history)
        provenance = {"task_id": task_id, "security": row["security"],
                      "source_locator": row.get("source_locator") or row.get("observation", {}).get("locator"),
                      "source_revision_id": row["source_revision_id"],
                      "response_sha256": row["response_sha256"], "observed_at": row["observed_at"],
                      "known_code_events": [event for event in self.code_events if event["old_symbol"] == task["symbol"]]}
        if row["schema"] == "ad.financial-source-report.v1":
            provenance.update({key: row[key] for key in ("period_start", "period_end", "basis", "scope")})
        if not decision["allowed"]:
            raise SourceUseDenied(decision["reasons"], decision)
        if purpose == "source_audit":
            return {"decision": decision, "provenance": provenance, "source_record": row,
                    "calculation_value_exposed": False}
        if row["schema"] == "ad.financial-source-report.v1":
            value = row["metrics"].get(field)
            if not value or value.get("state") != "source_observed" or value.get("unit") != "CNY":
                raise SourceUseDenied(["canonical_monetary_value_or_unit_unavailable"])
            amount, unit = value["value"], value["unit"]
        else:
            amount = row["observation"]["fields"].get(field)
            if amount is None or field not in {"total_shares", "float_a_shares", "float_b_shares", "float_h_shares", "restricted_a_shares", "preferred_shares"}:
                raise SourceUseDenied(["canonical_share_value_unavailable"])
            unit = "shares"
        if not Decimal(amount).is_finite():
            raise SourceUseDenied(["nonfinite_value"])
        return {"decision": decision, "provenance": provenance,
                "field": field, "value": amount, "unit": unit,
                "identity_basis": "provider request code; no unreviewed stable issuer remapping"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--task", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--period")
    selection.add_argument("--locator")
    parser.add_argument("--field")
    parser.add_argument("--purpose", choices=PURPOSES, default="historical_pit")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    try:
        result = SnapshotReader(args.root).read(args.task, period=args.period, locator=args.locator,
            field=args.field, purpose=args.purpose, as_of=args.as_of)
    except SourceUseDenied as error:
        print(json.dumps({"allowed": False, "reasons": error.reasons, "decision": error.decision}, ensure_ascii=False))
        return 2
    except ValueError as error:
        print(json.dumps({"allowed": False, "reasons": [str(error)]}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
