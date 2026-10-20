"""Map existing CNINFO archives and reviewed daily evidence into shared tables.

The ordinary archive keeps cell candidates quarantined. Historical eligibility is
created only through the existing official_daily verification path, with typed
field-scoped replacement links and separately versioned visibility evidence.
"""

from collections import Counter, defaultdict
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
import re

from ashare_data.core.model import require
from ashare_data.financials import tables as t
from ashare_data.financials import expansion as e
from ashare_data.financials import official_daily
from ashare_data.financials.official_access import verified_source
from ashare_data.financials.official_expansion import report_identity
from ashare_data.storage.market_release import sha_file


def new_batch(builder, key, body):
    old = builder.db.execute("SELECT batch_id FROM data_batches WHERE batch_key=?", (key,)).fetchone()
    if old:
        return old[0]
    number = builder.db.execute("SELECT coalesce(max(batch_id),0)+1 FROM data_batches").fetchone()[0]
    builder.db.execute("INSERT INTO data_batches VALUES (?,?,?,?,?,?)",
                       (number, key, 0, sha_file(Path(official_daily.__file__)), builder.mapping, t.text(body)))
    return number


def asset(builder, batch, path, kind, expected=None):
    aid = builder.asset(path, kind=kind, expected=expected)
    builder.db.execute("INSERT OR IGNORE INTO batch_inputs VALUES (?,?)", (batch, aid))
    return aid


def source_capture(builder, projection, root, key, batch_id, batch_key):
    receipt = verified_source(root, key)
    receipt_path = root / "receipts" / (key + ".json")
    raw_asset = asset(builder, batch_id, root / receipt["path"], "official_raw")
    asset(builder, batch_id, receipt_path, "official_receipt")
    capture = t.identity("capture", batch_key, key, sha_file(receipt_path))
    projection.add("meta.financial_captures", capture_id=capture, batch_key=batch_key,
                   task_id=key, source="cninfo", raw_asset_id=raw_asset,
                   response_sha256=receipt["sha256"], receipt_sha256=sha_file(receipt_path),
                   observed_at=receipt.get("observed_at"), source_state=receipt["state"], source_rows=0)
    return capture, raw_asset


def disclosure(projection, issuer, item, capture=None):
    eid = t.identity("official-event", issuer, str(item["announcement_id"]))
    projection.add("financial.disclosure_events", event_id=eid, issuer_id=issuer, source="cninfo",
                   native_event_id=str(item["announcement_id"]), title=item.get("title"),
                   published_date=item["published_date"], precision="date",
                   event_kind="revision_notice" if item.get("revision_label") else "official_disclosure",
                   visibility_basis="current_official_index_date")
    if capture:
        projection.link("disclosure", eid, "capture", capture, "captured_in")
    return eid


def document_row(document, *, reviewed=None):
    """Preserve one PDF content version even when different fields are extracted."""
    selected = document["cells"] if reviewed is None else reviewed
    fields, metrics = [], {}
    for ordinal, cell in enumerate(document["cells"]):
        if cell not in selected:
            continue
        field = cell["field"]
        statement = cell.get("statement") or ("llb" if field == "net_operate_cash_flow" else "lrb")
        fields.append(dict(source_field=field, source_title=field, ordinal=ordinal,
                           raw_value=cell.get("source_line", cell["value"]), value=cell["value"],
                           state="source_observed", context={
                               k: v for k, v in dict(cell, statement=statement, column_role="current_period").items()
                               if k in {"statement", "period_start", "period_end", "basis", "scope", "currency", "column_role"}
                           }))
        # Ambiguous same-field candidates remain long-table observations, never metrics.
        if sum(x["field"] == field for x in selected) == 1:
            metrics[field] = dict(source_ordinal=ordinal, source_field=field, unit=cell["unit"], value=cell["value"])
    report = document["report"]
    return dict(schema="ad.financial-source-report.v1", source="cninfo",
                security=document.get("security") or selected[0]["security"], statement="document",
                period_end=report["period_end"], period_start=None, basis="per_cell", scope="consolidated",
                currency="CNY", source_revision_id=document["document_sha256"],
                content_projection="official_pdf_content", version_kind="official_document_hash",
                source_metadata={"content_sha256": document["document_sha256"]},
                source_locator="pdf:" + report["announcement_id"], fields=fields, metrics=metrics,
                source_issues=[] if reviewed is not None else ["official_cell_layout_review_required"])


def import_daily_review(builder, specification_path):
    """Bounded verified fields, safe to stage separately from the still-active archive."""
    from ashare_data.storage.financial_release import immutable_json
    specification_path = Path(specification_path).resolve()
    spec = json.loads(specification_path.read_text())
    root = Path(spec["official_root"]).resolve()
    review_path = Path(spec["review"]).resolve()
    require(sha_file(review_path) == spec["review_sha256"], "FIN_REVIEW", "Explicit review hash changed")
    review = json.loads(review_path.read_text())
    correction = json.loads(Path(spec["correction_reviews"]).read_text()) if spec.get("correction_reviews") else None
    verified = official_daily.reader(root, review, reference_root=spec["reference_root"],
                                    reference_tasks=spec["reference_tasks"], correction_reviews=correction)
    ledger = verified._ledger  # Reuse the existing verified contract, not raw review booleans.
    key = t.identity("official-reviewed-batch", sha_file(specification_path), sha_file(review_path), builder.mapping)
    checkpoint = builder.root / ("batch-" + key.split(":")[1] + ".json")
    if checkpoint.exists():
        return json.loads(checkpoint.read_text())
    batch_id = new_batch(builder, key, {"kind": "reviewed_official_daily", "specification": spec})
    evidence = asset(builder, batch_id, review_path, "official_field_review", spec["review_sha256"])
    asset(builder, batch_id, specification_path, "daily_review_specification")
    if spec.get("correction_reviews"):
        asset(builder, batch_id, Path(spec["correction_reviews"]), "correction_scope_review")
        evidence_root = Path(correction["evidence_root"])
        for item in correction["items"]:
            asset(builder, batch_id, evidence_root / "receipts" / ("pdf-" + item["announcement_id"] + ".json"),
                  "correction_receipt")
            receipt = verified_source(evidence_root, "pdf-" + item["announcement_id"])
            asset(builder, batch_id, evidence_root / receipt["path"], "correction_original")
    ref_root = Path(spec["reference_root"])
    for task_id in spec["reference_tasks"]:
        rp = ref_root / "receipts" / (task_id + ".json")
        receipt = json.loads(rp.read_text())
        asset(builder, batch_id, rp, "session_evidence_receipt")
        for kind in ("raw", "normalized"):
            artifact = receipt[kind]
            asset(builder, batch_id, ref_root / artifact["path"], "session_" + kind, artifact["sha256"])
    projection = t.Projection(instruments=builder.instruments, register_issuer=builder.issuer, mapping_sha256=builder.mapping)
    indices, issuer_ids, captures = {}, {}, {}
    for symbol in sorted({x["symbol"] for x in review["fields"]}):
        index, _ = official_daily._index(root, symbol)
        indices[symbol] = index
        asset(builder, batch_id, root / "indices" / (symbol + ".json.gz"), "reviewed_official_index")
        for source in index["sources"]:
            captures[source], _ = source_capture(builder, projection, root, source, batch_id, key)
    observation_ids, relation_ids = {}, {}
    for aid in ledger.reports:
        items = [x for x in review["fields"] if x["announcement_id"] == aid]
        symbol = items[0]["symbol"]
        path = root / "documents" / (symbol + "-" + aid + ".json.gz")
        artifact = asset(builder, batch_id, path, "reviewed_document", items[0]["document_derivative_sha256"])
        doc = json.loads(gzip.decompress(path.read_bytes()))
        index = indices[symbol]
        official = next(x for x in index["announcements"] if x["announcement_id"] == aid)
        current_cells = [cell for cell in doc["cells"] if any(
            all(cell[k] == item[k] for k in ("field", "value", "page")) for item in items)]
        row = document_row(doc, reviewed=current_cells)
        end = row["period_end"]
        task = dict(kind="cninfo_review", source_issuer_key=index["issuer"]["orgId"],
                    ipo=end, out=(date.fromisoformat(end) + timedelta(days=1)).isoformat(),
                    relation_role="reviewed_report_for_security", relation_status="verified_report_security_relation",
                    evidence_asset_id=evidence)
        issuer = builder.issuer("cninfo", row["security"], end, task)
        issuer_ids[symbol] = issuer
        relation = t.identity("issuer-instrument", issuer, builder.instruments[row["security"]],
                              task["ipo"], task["out"], task["relation_role"], evidence)
        relation_ids[aid] = relation
        capture, _ = source_capture(builder, projection, root, "pdf-" + aid, batch_id, key)
        for source in {s for item in items for s in item.get("supporting_receipts", [])}:
            source_capture(builder, projection, root, source, batch_id, key)
        parser = t.identity("official-reviewed-parser", sha_file(Path(__file__)), sha_file(review_path))
        rid = projection.report(row, task, capture, parser)
        event_id = disclosure(projection, issuer, official, captures[official["index_receipt"]])
        projection.link("report", rid, "disclosure", event_id, "disclosed_in", issuer_id=issuer, period_end=end)
        gate = ledger.reports[aid]["daily_visibility"]
        for item in items:
            matching = [o for o in projection.rows["financial.observations"]
                        if o["report_version_id"] == rid and o["canonical_field"] == item["field"]]
            require(len(matching) == 1, "FIN_REVIEW", "Reviewed field mapping is ambiguous")
            observation_ids[item["record_id"]] = matching[0]["observation_id"]
            if gate.get("available_from"):
                proof = dict(gate, official_document_asset_id=artifact, review_asset_id=evidence)
                projection.add("financial.visibility",
                               visibility_id=t.identity("visibility", matching[0]["observation_id"], proof),
                               observation_id=matching[0]["observation_id"], event_id=event_id,
                               issuer_relation_id=relation, policy="verified_official_daily",
                               available_from=gate["available_from"], evidence_asset_id=evidence,
                               evidence_json=t.text(proof))
    for record in ledger.observations:
        parent = record.get("replaces_in_this_pack")
        if parent:
            issuer = next(o["issuer_id"] for o in projection.rows["financial.observations"]
                          if o["observation_id"] == observation_ids[record["record_id"]])
            gate = ledger.reports[record["report_id"]]["daily_visibility"]
            projection.link("observation", observation_ids[record["record_id"]],
                            "observation", observation_ids[parent], "replaces",
                            issuer_id=issuer, period_end=record["period_end"], field=record["field"],
                            available_at=gate.get("available_from"), evidence_asset_id=evidence,
                            scope_state="reviewed_exact")
    for blocker in ledger.daily_blockers:
        symbol = "sh" + blocker["security"][:6] if blocker["security"].endswith("XSHG") else "sz" + blocker["security"][:6]
        item = next(x for x in indices[symbol]["announcements"] if x["announcement_id"] == blocker["announcement_id"])
        issuer = issuer_ids[symbol]
        event = disclosure(projection, issuer, item, captures[item["index_receipt"]])
        for record in ledger.observations:
            if all(record[k] == blocker[k] for k in ("security", "period_end", "field")):
                projection.link("disclosure", event, "observation", observation_ids[record["record_id"]],
                                "unresolved_revision", issuer_id=issuer, period_end=record["period_end"],
                                field=record["field"], available_at=blocker["blocked_from"],
                                evidence_asset_id=evidence, scope_state="unreviewed")
    projection.add("meta.financial_coverage", coverage_id=t.identity("coverage", key), batch_key=key,
                   task_id="reviewed_fields", source="cninfo", source_state="reviewed_bounded_fields",
                   input_rows=len(ledger.observations), mapped_rows=len(ledger.observations), quarantined_rows=0,
                   coverage_json=t.text({"reviewed_fields": len(ledger.observations),
                                         "full_archive": False, "strict_historical_pit": False}))
    files = builder.flush(projection.rows, key, 0)
    result = dict(batch_key=key, source_root=str(root), files=files,
                  task_states={"reviewed_fields": len(ledger.observations)}, tasks=len(ledger.observations),
                  receipt_inventory_sha256=verified._pack_sha256)
    builder.db.commit()
    immutable_json(checkpoint, result)
    return result


def import_archive(builder, root):
    """Whole closed index/document batch; no downloads or layout certification."""
    from ashare_data.storage.financial_release import closed_batch, immutable_json
    root = Path(root).resolve()
    with closed_batch(root):
        manifest = json.loads((root / "manifest.json").read_text())
        scope_raw = (root / "scope.json").read_bytes()
        require(manifest["schema"] == "ad.official-financial-batch.v1"
                and e.digest(scope_raw) == manifest["scope_sha256"], "FIN_SOURCE", "Official scope changed")
        scope = json.loads(scope_raw)
        for symbol in scope["symbols"]:
            path = root / "securities" / (symbol + ".json")
            require(path.exists() and json.loads(path.read_text())["state"] in {"complete", "parse_failed", "deferred"},
                    "FIN_BATCH_OPEN", "Official index batch still has unvisited securities")
        key = t.identity("official-archive-batch", str(root), sha_file(root / "manifest.json"), builder.mapping)
        checkpoint = builder.root / ("batch-" + key.split(":")[1] + ".json")
        if checkpoint.exists():
            return json.loads(checkpoint.read_text())
        batch_id = new_batch(builder, key, manifest)
        asset(builder, batch_id, root / "manifest.json", "official_batch")
        asset(builder, batch_id, root / "scope.json", "official_scope", manifest["scope_sha256"])
        receipts_by_symbol = defaultdict(list)
        for path in (root / "receipts").glob("*.json"):
            match = re.match(r"(?:identity|index)-(sh\d{6}|sz\d{6})(?:-|$)", path.stem)
            if match:
                receipts_by_symbol[match[1]].append(path.stem)
        buffer, files, states, ordinal = defaultdict(list), [], Counter(), 0
        for symbol in scope["symbols"]:
            status_path = root / "securities" / (symbol + ".json")
            status = json.loads(status_path.read_text())
            asset(builder, batch_id, status_path, "official_security_disposition")
            states[status["state"]] += 1
            projection = t.Projection(instruments=builder.instruments, register_issuer=builder.issuer,
                                      mapping_sha256=builder.mapping)
            index_path = root / "indices" / (symbol + ".json.gz")
            count = mapped = quarantined = 0
            if index_path.exists():
                index = json.loads(gzip.decompress(index_path.read_bytes()))
                asset(builder, batch_id, index_path, "official_index")
                task = dict(kind="cninfo_index", source_issuer_key=index["issuer"]["orgId"])
                issuer = builder.issuer("cninfo", index["security"], None, task)
                captured = {}
                payloads = {}
                for source in index["sources"]:
                    captured[source], _ = source_capture(builder, projection, root, source, batch_id, key)
                    receipt = verified_source(root, source)
                    payloads[source] = json.loads(gzip.decompress((root / receipt["path"]).read_bytes()))
                for item in index["announcements"]:
                    count += 1
                    capture = captured[item["index_receipt"]]
                    payload = payloads[item["index_receipt"]]
                    matching = [r for r in (payload.get("announcements") or [])
                                if str(r["announcementId"]) == item["announcement_id"]
                                and r.get("secCode") == symbol[2:] and r.get("orgId") == index["issuer"]["orgId"]]
                    if not matching or any(any(item.get(k) != v for k, v in report_identity(r).items())
                                           for r in matching):
                        projection.quarantine(dict(item, schema="ad.official-report-index.v1"), capture,
                                              "derived_announcement_does_not_match_source")
                        quarantined += 1
                        continue
                    event = disclosure(projection, issuer, item, capture)
                    mapped += 1
                    docpath = root / "documents" / (symbol + "-" + item["announcement_id"] + ".json.gz")
                    if not docpath.exists():
                        continue
                    doc = json.loads(gzip.decompress(docpath.read_bytes()))
                    asset(builder, batch_id, docpath, "official_document_derivative")
                    capture, _ = source_capture(builder, projection, root, doc["pdf_receipt"], batch_id, key)
                    require(doc["document_sha256"] == verified_source(root, doc["pdf_receipt"])["sha256"],
                            "FIN_SOURCE", "Document derivative/PDF content identity differs")
                    # All text candidates stay long-table retained rows. They are
                    # not automatically historical values or silently discarded.
                    row = document_row(doc)
                    rid = projection.report(row, task, capture,
                                            doc.get("normalizer_sha256", "unknown_official_extractor"))
                    projection.link("report", rid, "disclosure", event, "disclosed_in",
                                    issuer_id=issuer, period_end=item.get("period_end"))
                    count += 1 + len(doc["cells"])
                    mapped += 1 + len(doc["cells"])
            used = {x["task_id"] for x in projection.rows["meta.financial_captures"]}
            for source in receipts_by_symbol[symbol]:
                if source in used:
                    continue
                receipt_path = root / "receipts" / (source + ".json")
                receipt = json.loads(receipt_path.read_text())
                asset(builder, batch_id, receipt_path, "unparsed_official_receipt")
                if receipt.get("state") == "complete" and receipt.get("path"):
                    capture, aid = source_capture(builder, projection, root, source, batch_id, key)
                    projection.quarantine(
                        dict(schema="ad.official-unmapped-capture.v1", source_locator=source, raw_asset_id=aid),
                        capture, "captured_source_without_accepted_index; values_require_recovery")
            projection.add("meta.financial_coverage", coverage_id=t.identity("coverage", key, symbol),
                           batch_key=key, task_id=symbol, source="cninfo", security=e.security(symbol),
                           source_state=status["state"], input_rows=count, mapped_rows=mapped,
                           quarantined_rows=quarantined, coverage_json=t.text(status), error=status.get("error"))
            for name, rows in projection.rows.items():
                buffer[name].extend(rows)
            if sum(len(x) for x in buffer.values()) >= builder.rows_per_shard:
                files.extend(builder.flush(buffer, key, ordinal))
                ordinal += 1
                buffer.clear()
                builder.db.commit()
        if buffer:
            files.extend(builder.flush(buffer, key, ordinal))
        result = dict(batch_key=key, source_root=str(root), files=files, task_states=dict(states),
                      tasks=len(scope["symbols"]), receipt_inventory_sha256=t.identity("official_sources", states))
        builder.db.commit()
        immutable_json(checkpoint, result)
        return result


def import_recovered_archive(builder, root):
    """Import a frozen repair package without adopting it into its live archive."""
    from ashare_data.financials import source_repair
    from ashare_data.storage.financial_release import closed_batch, checked_path, immutable_json
    root = Path(root).resolve()
    with closed_batch(root):
        manifest_path = root / "repair-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for entry in manifest.get("official", []):
            checked_path(root, entry["index"]["path"])
        manifest = source_repair.verify_inputs(root)
        original = Path(manifest["official_source"]).resolve()
        key = t.identity("recovered-official-batch", str(root), sha_file(manifest_path), builder.mapping)
        checkpoint = builder.root / ("batch-" + key.split(":")[1] + ".json")
        if checkpoint.exists():
            return json.loads(checkpoint.read_text())
        batch_id = new_batch(builder, key, dict(
            kind="bounded_recovered_official_index", repair_root=str(root),
            original_archive_closed=False, historical_pit_certified=False))
        asset(builder, batch_id, manifest_path, "official_repair_manifest")
        asset(builder, batch_id, original / "manifest.json", "official_batch",
              manifest["source_manifest_sha256"]["official"])
        buffer, files, states = defaultdict(list), [], Counter()
        for entry in manifest["official"]:
            path = checked_path(root, entry["index"]["path"])
            asset(builder, batch_id, path, "recovered_official_index", entry["index"]["sha256"])
            index = json.loads(gzip.decompress(path.read_bytes()))
            require(index["schema"] == "ad.recovered-official-index.v1"
                    and index["security"] == e.security(entry["symbol"])
                    and index["sources"] == entry["source_receipts"],
                    "FIN_SOURCE", "Recovered index identity or receipt inventory differs")
            projection = t.Projection(instruments=builder.instruments, register_issuer=builder.issuer,
                                      mapping_sha256=builder.mapping)
            issuer = builder.issuer("cninfo", index["security"], None, dict(
                kind="cninfo_recovered_index", source_issuer_key=index["issuer"]["orgId"]))
            captures, payloads = {}, {}
            for source, expected in index["sources"].items():
                receipt_path = original / "receipts" / (source + ".json")
                require(sha_file(receipt_path) == expected["receipt_sha256"],
                        "FIN_SOURCE", "Recovered source receipt changed")
                captures[source], _ = source_capture(builder, projection, original, source, batch_id, key)
                receipt = verified_source(original, source)
                require(receipt["sha256"] == expected["response_sha256"],
                        "FIN_SOURCE", "Recovered response binding differs")
                payloads[source] = json.loads(gzip.decompress((original / receipt["path"]).read_bytes()))
                require(sha_file(receipt_path) == expected["receipt_sha256"],
                        "FIN_SOURCE", "Recovered source receipt changed during import")
            count = mapped = quarantined = 0
            for item in index["announcements"]:
                count += 1
                source = item["index_receipt"]
                payload = payloads[source]
                matching = [row for row in (payload.get("announcements") or [])
                            if isinstance(row, dict) and str(row.get("announcementId")) == item["announcement_id"]
                            and row.get("secCode") == entry["symbol"][2:]
                            and row.get("orgId") == index["issuer"]["orgId"]]
                if (item["source_security"] != index["security"] or not matching
                        or any(any(item.get(k) != v for k, v in report_identity(row).items())
                               for row in matching)):
                    projection.quarantine(dict(item, schema=index["schema"]), captures[source],
                                          "recovered_announcement_does_not_match_source")
                    quarantined += 1
                    continue
                disclosure(projection, issuer, item, captures[source])
                mapped += 1
            rejected = [(row, "related_code_continuity_unproven")
                        for row in index["related_code_announcements"]]
            rejected += [(row, "conflicting_recovered_announcement")
                         for versions in index["conflicting_versions"].values() for row in versions]
            rejected += [(row, "rejected_source_announcement:" + row["reason"])
                         for row in index["quarantined_rows"]]
            for item, reason in rejected:
                source = item.get("index_receipt") or item.get("source", {}).get("receipt")
                require(source in captures, "FIN_SOURCE", "Recovered rejected row lacks source evidence")
                projection.quarantine(dict(item, schema=index["schema"]), captures[source], reason)
                count += 1
                quarantined += 1
            state = "recovered_complete_query_set" if index["query_set_complete"] else "recovered_with_query_gaps"
            states[state] += 1
            projection.add(
                "meta.financial_coverage", coverage_id=t.identity("coverage", key, entry["symbol"]),
                batch_key=key, task_id=entry["symbol"], source="cninfo_recovered",
                security=index["security"], source_state=state, input_rows=count, mapped_rows=mapped,
                quarantined_rows=quarantined, coverage_json=t.text(dict(
                    query_set_complete=index["query_set_complete"], query_gaps=entry["query_gaps"],
                    related_code_records=entry["related_code_records"], full_archive=False,
                    financial_fields_extracted=False, historical_pit_certified=False)))
            for name, rows in projection.rows.items():
                buffer[name].extend(rows)
            if sum(len(rows) for rows in buffer.values()) >= builder.rows_per_shard:
                files.extend(builder.flush(buffer, key, len(files)))
                buffer.clear()
                builder.db.commit()
        if buffer:
            files.extend(builder.flush(buffer, key, len(files)))
        result = dict(batch_key=key, source_root=str(root), files=files, tasks=len(manifest["official"]),
                      task_states=dict(states), receipt_inventory_sha256=t.identity(
                          "recovered-sources", [entry["source_receipts"] for entry in manifest["official"]]))
        builder.db.commit()
        immutable_json(checkpoint, result)
        return result
