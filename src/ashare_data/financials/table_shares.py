"""Import existing reviewed share resolutions without replacing negative source cells."""

from collections import Counter
from datetime import datetime
import json
from pathlib import Path

from ashare_data.financials import tables as t
from ashare_data.financials.share_resolution import ShareResolutionReader, captured
from ashare_data.financials.table_official import asset, new_batch
from ashare_data.storage.market_release import sha_file


def import_resolutions(builder, root):
    from ashare_data.storage.financial_release import immutable_json
    root = Path(root).resolve()
    reader = ShareResolutionReader(root)
    key = t.identity("share-resolution-batch", sha_file(root / "manifest.json"), builder.mapping)
    checkpoint = builder.root / ("batch-" + key.split(":")[1] + ".json")
    if checkpoint.exists():
        return json.loads(checkpoint.read_text())
    batch_id = new_batch(builder, key, {"kind": "existing_reviewed_share_resolutions", "source": reader.manifest})
    asset(builder, batch_id, root / "manifest.json", "share_resolution_manifest")
    resolved_asset = asset(builder, batch_id, root / reader.manifest["records"]["path"], "share_resolution_records",
                           reader.manifest["records"]["sha256"])
    projection = t.Projection(instruments=builder.instruments, register_issuer=builder.issuer,
                              mapping_sha256=builder.mapping)
    states, cached = Counter(), {}

    def source(source_reader, task_id):
        cache_key = (str(source_reader.root), task_id)
        if cache_key not in cached:
            _, receipt, _, checksum = captured(source_reader.root, task_id)
            raw_id = asset(builder, batch_id, source_reader.root / receipt["raw"]["path"], "share_resolution_raw")
            normalized_id = asset(builder, batch_id, source_reader.root / receipt["normalized"]["path"], "share_resolution_normalized")
            asset(builder, batch_id, source_reader.root / "receipts" / (task_id + ".json"), "share_resolution_receipt", checksum)
            cid = t.identity("capture", key, cache_key, checksum)
            projection.add("meta.financial_captures", capture_id=cid, batch_key=key, task_id=task_id,
                           source="sina_share_evidence", raw_asset_id=raw_id, normalized_asset_id=normalized_id,
                           response_sha256=receipt["attempts"][-1]["response_sha256"], receipt_sha256=checksum,
                           observed_at=receipt["observed_at"], source_state=receipt["state"], source_rows=0)
            cached[cache_key] = (cid, receipt)
        return cached[cache_key]

    for item in reader.records:
        builder.db.execute("INSERT OR IGNORE INTO source_aliases VALUES (?,?,?,?)",
                           (reader.manifest["records"]["sha256"], item["security"],
                            builder.instruments[item["security"]], "verified exact share point; no issuer/code continuity assertion"))
        states[item["status"]] += 1
        structure_capture, sr = source(reader.structure, item["structure_task"])
        if item["status"] != "source_value_recovered":
            reader.read(item["security"], item["date"], field=item["field"], purpose="source_audit")
            projection.quarantine(dict(item, schema="ad.share-resolution-candidate.v1"), structure_capture, item["status"])
            continue
        result = reader.read(item["security"], item["date"], field=item["field"],
                             purpose="source_research", as_of=reader.manifest["at"])
        float_capture, fr = source(reader.floats, item["float_task"])
        # Both observations are required: use the later evidence capture clock.
        observed = max((sr["observed_at"], fr["observed_at"]), key=datetime.fromisoformat)
        cid = t.identity("capture", key, item["security"], item["date"], item["field"])
        projection.add("meta.financial_captures", capture_id=cid, batch_key=key,
                       task_id=t.identity("resolution", item), source="sina_reviewed_share_point",
                       normalized_asset_id=resolved_asset, observed_at=observed,
                       source_state="verified_source_point_no_historical_pit", source_rows=1)
        for evidence_capture in (structure_capture, float_capture):
            projection.link("capture", cid, "capture", evidence_capture, "proof_input")
        row = dict(schema="ad.corporate-source-observation.v1", source="sina_reviewed_share_point",
                   security=item["security"], source_revision_id=t.identity("share-resolution", item), category="shares",
                   observation=dict(date=item["date"], fields={item["field"]: result["value"]},
                                    native_fields={"value_source": item["value_source"], "original_source_gap": item["source_gap"]}),
                   quality_flags=[])
        projection.reference(row, {"kind": "share_resolution"}, cid)
    recovered = states["source_value_recovered"]
    projection.add("meta.financial_coverage", coverage_id=t.identity("coverage", key), batch_key=key,
                   task_id="existing_share_resolutions", source="sina_reviewed_share_point",
                   source_state="closed_with_explicit_unresolved_cells", input_rows=len(reader.records),
                   mapped_rows=recovered, quarantined_rows=len(reader.records) - recovered,
                   coverage_json=t.text(dict(states)))
    files = builder.flush(projection.rows, key, 0)
    result = dict(batch_key=key, source_root=str(root), files=files, task_states=dict(states),
                  tasks=len(reader.records), receipt_inventory_sha256=reader.manifest["records"]["sha256"])
    builder.db.commit()
    immutable_json(checkpoint, result)
    return result
