"""Practical daily PIT from reviewed official versions and existing trading evidence.

This reconstructs day-level eligibility, not first-publication clocks or a complete
historical archive. Missing calendar dates can delay eligibility, never advance it.
No network, database import, or duplicate source-asset storage is performed.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import gzip
import json
from pathlib import Path
import re

from ashare_data.financials import expansion as e
from ashare_data.financials.ledger import SHANGHAI
from ashare_data.financials.limit_expansion import parse as parse_reference
from ashare_data.financials.official_access import reviewed_ledger, verified_source
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


def reader(root, review, *, reference_root, reference_tasks, correction_reviews=None):
    """Build FinancialPIT with verified day-level source and known-version boundaries.

    Review binds exact PDF/page/field/scope and replacement notices. Known missing
    versions or potentially relevant unscoped corrections block only their affected
    historical interval. Unknown undiscovered revisions are a stated archive limit,
    not a requirement to prove a negative before any historical query is usable.
    """
    root = Path(root).resolve()
    ledger = reviewed_ledger(root, review)
    sessions = traded_sessions(reference_root, reference_tasks)
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
