"""Practical daily PIT from reviewed official versions and existing trading evidence.

This reconstructs day-level eligibility, not first-publication clocks or a complete
historical archive. Missing calendar dates can delay eligibility, never advance it.
No network, database import, or duplicate source-asset storage is performed.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import re

from ashare_data.financials import expansion as e
from ashare_data.financials.expansion import digest, encoded, load_batch, security, verify_receipt
from ashare_data.financials.ledger import (
    AcceptanceError,
    FIELDS,
    SHANGHAI,
    FinancialLedger,
    instant,
    verify_pack_files,
)
from ashare_data.financials.limit_expansion import parse as parse_reference
from ashare_data.financials.official_expansion import report_identity
from ashare_data.financials.pit import FinancialPIT


def traded_sessions(root, task_ids):
    """Use positive, non-paused trades in explicitly selected cached daily windows.

    A provider-reported trade is evidence of an open exchange session, not an
    official full calendar. An absent/suspended symbol never proves market closure.
    """
    root, _, tasks = e.load_batch(root)
    by_id = {t["id"]: t for t in tasks}
    sessions = {}
    for key in dict.fromkeys(task_ids):
        task = by_id[key]
        if task["kind"] != "zzshare_daily_reference":
            raise ValueError("Explicit daily reference tasks required")
        receipt = json.loads((root / "receipts" / (key + ".json")).read_text())
        if receipt["state"] != "complete":
            raise ValueError("Trading evidence task is incomplete")
        e.verify_receipt(root, receipt)
        raw = gzip.decompress((root / receipt["raw"]["path"]).read_bytes())
        rows, _ = parse_reference(raw, task)
        source_rows = json.loads(raw, parse_float=Decimal)["data"]["list"]
        exchange = e.security(task["symbol"]).split(".")[1]
        for row, source in zip(rows, source_rows, strict=True):
            volume = Decimal(str(source.get("volume", "NaN")))
            if row["fields"]["is_paused"]["value"] != 0 or not volume.is_finite() or volume <= 0:
                continue
            sessions.setdefault(exchange, {}).setdefault(row["trade_date"], {
                "source": "zzshare_cached_positive_unpaused_trade",
                "source_security": row["security"],
                "task_id": key,
                "source_locator": row["source_locator"],
                "response_sha256": e.digest(raw),
                "receipt_sha256": e.digest(e.encoded(receipt)),
                "official_exchange_calendar": False,
            })
    return sessions


def calendar_sessions(root):
    """Verify complete captured calendar responses; no weekday or suspension inference."""
    from ashare_data.financials.tushare_reference import calendar_rows
    root, _, tasks = e.load_batch(root)
    sessions = {}
    for task in tasks:
        if task['kind'] != 'tushare_trade_cal':
            raise ValueError('Explicit exchange calendar tasks required')
        path = root / 'receipts' / (task['id'] + '.json')
        receipt = json.loads(path.read_bytes())
        if receipt['state'] != 'complete':
            raise ValueError('Calendar response is incomplete')
        e.verify_receipt(root, receipt)
        raw = gzip.decompress((root / receipt['raw']['path']).read_bytes())
        rows, coverage = calendar_rows(raw, task)
        if coverage['missing_calendar_dates']:
            raise ValueError('Calendar response omits dates')
        for row in rows:
            if row['is_open']:
                exchange = {'SSE':'XSHG','SZSE':'XSHE'}[row['exchange']]
                proof = dict(source='tushare_captured_exchange_calendar', task_id=task['id'],
                    source_locator=row['source_locator'], response_sha256=e.digest(raw),
                    receipt_sha256=e.digest(path.read_bytes()), official_exchange_calendar=False)
                if row['date'] in sessions.setdefault(exchange, {}):
                    raise ValueError('Overlapping calendar tasks')
                sessions[exchange][row['date']] = proof
    if not sessions:
        raise ValueError('Empty calendar source')
    return sessions


def _source_json(root, key):
    receipt = verified_source(root, key)
    return json.loads(gzip.decompress((root / receipt["path"]).read_bytes())), receipt


def _index(root, symbol):
    path = root / "indices" / (symbol + ".json.gz")
    blob = path.read_bytes()
    index = json.loads(gzip.decompress(blob))
    if index["security"] != e.security(symbol):
        raise ValueError("Index/security mismatch")
    queries = index["queries"]
    if {q["kind"] for q in queries} != {"periodic", "correction", "revision"} or not all(
        q["pagination_complete_for_query"] for q in queries
    ):
        raise ValueError("Known revision searches are incomplete")
    identity, _ = _source_json(root, "identity-" + symbol)
    exact = [r for r in identity if r.get("code") == symbol[2:] and r.get("category") == "A股"]
    if len(exact) != 1 or exact[0] != index["issuer"]:
        raise ValueError("Official issuer identity mismatch")
    sources = {key: _source_json(root, key) for key in index["sources"] if key.startswith("index-")}
    source_ids = {str(r["announcementId"]) for payload, _ in sources.values()
                  for r in (payload.get("announcements") or [])}
    if source_ids != {item["announcement_id"] for item in index["announcements"]}:
        raise ValueError("Known source versions missing from derived index")
    for item in index["announcements"]:
        payload, receipt = sources[item["index_receipt"]]
        found = [r for r in (payload.get("announcements") or [])
                 if str(r["announcementId"]) == item["announcement_id"]]
        if len(found) != 1:
            raise ValueError("Official index row absent or ambiguous")
        raw = found[0]
        if (raw["secCode"], raw["orgId"]) != (symbol[2:], index["issuer"]["orgId"]):
            raise ValueError("Cross-issuer official index evidence")
        canonical = report_identity(raw)
        if any(item[k] != v for k, v in canonical.items()):
            raise ValueError("Official publication date/version differs from captured index")
        item["index_response_sha256"] = receipt["sha256"]
    return index, e.digest(blob)


def _after(sessions, exchange, day):
    dates = sorted(sessions.get(exchange, {}))
    position = bisect_right(dates, day)
    if position == len(dates):
        return None
    session = dates[position]
    return {
        "available_from": datetime.combine(date.fromisoformat(session), time.min, SHANGHAI).isoformat(),
        "session_date": session,
        "session_evidence": sessions[exchange][session],
        "first_actual_session_proven": False,
        "missing_sessions_policy": "delay_to_next_evidenced_session; never_infer_closed_days",
    }


def _correction_scopes(indices, reviewed_fields, specification):
    if specification is None:
        return {}
    if specification.get("schema") != "ad.official-correction-scope-review.v1":
        raise ValueError("Unknown correction scope review")
    evidence_root = Path(specification["evidence_root"]).resolve()
    exclusions = {}
    for item in specification["items"]:
        index, _ = indices[item["symbol"]]
        notice = next(a for a in index["announcements"] if a["announcement_id"] == item["announcement_id"])
        if (not item.get("layout_reviewed") or not isinstance(item.get("page"), int) or item["page"] < 1
                or not notice["revision_label"] or notice["period_end"] is not None
                or item["index_response_sha256"] != notice["index_response_sha256"]):
            raise ValueError("Correction scope lacks exact reviewed index/page binding")
        pdf = verified_source(evidence_root, "pdf-" + item["announcement_id"])
        if pdf["sha256"] != item["document_sha256"] or pdf["request"]["url"] != notice["source_url"]:
            raise ValueError("Correction scope original PDF differs")
        for field in item["unaffected_fields"]:
            key = (index["security"], field["period_end"], field["field"])
            if key not in reviewed_fields:
                raise ValueError("Correction exclusion must bind an existing reviewed field and period")
            exclusions[key + (item["announcement_id"],)] = dict(
                announcement_id=item["announcement_id"], document_sha256=pdf["sha256"], page=item["page"],
                source_url=notice["source_url"], review_sha256=e.digest(e.encoded(item)),
                reviewed_change=item["reviewed_change"], field_scope="explicitly_unaffected",
            )
    return exclusions


def reader(root, review, *, reference_root, reference_tasks, correction_reviews=None, calendar_root=None):
    """Build FinancialPIT with verified day-level source and known-version boundaries.

    Review binds exact PDF/page/field/scope and replacement notices. Known missing
    versions or potentially relevant unscoped corrections block only their affected
    historical interval. Unknown undiscovered revisions are a stated archive limit,
    not a requirement to prove a negative before any historical query is usable.
    """
    root = Path(root).resolve()
    ledger = reviewed_ledger(root, review)
    sessions = calendar_sessions(calendar_root) if calendar_root else traded_sessions(reference_root, reference_tasks)
    indices = {s: _index(root, s) for s in {r["symbol"] for r in review["fields"]}}
    reviewed_fields = {}
    for item in review["fields"]:
        cell = ledger.by_id[item["record_id"]]
        key = (cell["security"], cell["period_end"], cell["field"])
        reviewed_fields.setdefault(key, set()).update(
            [item["announcement_id"]] + [s.removeprefix("pdf-") for s in item.get("supporting_receipts", [])]
        )
    exclusions = _correction_scopes(indices, reviewed_fields, correction_reviews)
    blockers = {}
    for item in review["fields"]:
        cell = ledger.by_id[item["record_id"]]
        index, index_sha = indices[item["symbol"]]
        indexed = {a["announcement_id"]: a for a in index["announcements"]}
        official = indexed[item["announcement_id"]]
        report = ledger.reports[cell["report_id"]]
        if (cell["security"] != index["security"] or cell["period_end"] != official["period_end"]
                or report["published_date"] != official["published_date"]
                or report["source_url"] != official["source_url"]):
            raise ValueError("Reviewed cell/report identity does not match official index")
        pdf = verified_source(root, "pdf-" + item["announcement_id"])
        if pdf.get("request", {}).get("url") != official["source_url"]:
            raise ValueError("Captured PDF URL does not match official index")
        for supporting in item.get("supporting_receipts", []):
            aid = supporting.removeprefix("pdf-")
            notice = indexed.get(aid)
            if not notice or notice["period_end"] != cell["period_end"] or not notice["revision_label"]:
                raise ValueError("Correction notice scope lacks an official period binding")
            receipt = verified_source(root, supporting)
            if receipt.get("request", {}).get("url") != notice["source_url"]:
                raise ValueError("Correction source URL mismatch")
        exchange = cell["security"].split(".")[1]
        gate = _after(sessions, exchange, report["published_date"])
        report.update(title=official["title"], period_end=official["period_end"],
                      index_sha256=official["index_response_sha256"], daily_visibility={
            **(gate or {}),
            "official_announcement_id": item["announcement_id"],
            "official_published_date": report["published_date"],
            "publication_precision": "date",
            "index_derivative_sha256": index_sha,
            "index_response_sha256": official["index_response_sha256"],
            "evidence_boundary": "current_official_archive; reviewed_field_scope_and_known_versions",
            "unknown_revision_absence_asserted": False,
            "historical_capture_asserted": False,
        })
        key = (cell["security"], cell["period_end"], cell["field"])
        report["daily_visibility"]["reviewed_unrelated_corrections"] = [v for k, v in exclusions.items() if k[:3] == key]
        for candidate in index["announcements"]:
            aid = candidate["announcement_id"]
            relevant = (candidate["period_end"] == cell["period_end"] and (
                candidate["full_report_candidate"] or candidate["revision_label"]))
            unscoped = (candidate["period_end"] is None and candidate["revision_label"] and
                        re.search(r"财务|报表|会计差错|定期报告|^更正公告$", candidate["title"]))
            if unscoped and key + (aid,) in exclusions:
                continue
            if (not (relevant or unscoped) or aid in reviewed_fields[key]
                    or candidate["published_date"] < cell["period_end"]):
                continue
            # Block from the following calendar day even if the session archive is
            # sparse: a known unresolved correction must not keep a stale value alive.
            day = date.fromisoformat(candidate["published_date"]) + timedelta(days=1)
            blockers[key + (aid,)] = dict(
                security=cell["security"], period_end=cell["period_end"], field=cell["field"],
                announcement_id=aid, title=candidate["title"],
                blocked_from=datetime.combine(day, time.min, SHANGHAI).isoformat(),
                reason="known_version_or_correction_scope_unreviewed",
            )
    pack = dict(schema="ad.financial-ledger.v1", reports=ledger.reports,
                observations=ledger.observations, known_unresolved_revisions=list(blockers.values()))
    result = FinancialPIT(pack)
    result._files_verified = True
    result._pack_sha256 = e.digest(e.encoded(pack))
    return result


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


if __name__ == "__main__":
    raise SystemExit(main())
