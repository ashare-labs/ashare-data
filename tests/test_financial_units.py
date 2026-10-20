from copy import deepcopy
import json
from pathlib import Path

import pytest

from ashare_data.core.model import DataError
from ashare_data.financials import tables as t
from ashare_data.financials import unit_resolution as units
from ashare_data.product.financial_release import FinancialTables
from ashare_data.storage.financial_release import compose, prepare
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, canonical, sha_file
from test_financial_release import batch, market, report


def fixture(tmp_path, *, reasons=()):
    parent = market(tmp_path / "market")
    row = report()
    row["source_issues"] = list(reasons)
    row["fields"] += [
        dict(source_field="BASICEPS", source_title="基本每股收益", ordinal=5,
             raw_value="0.104900", value="0.104900", state="source_observed"),
        dict(source_field="ZERO", source_title="零金额", ordinal=6,
             raw_value="0.000000", value="0.000000", state="source_observed"),
        dict(source_field="MINY", source_title="少数股东综合收益", ordinal=7,
             raw_value="-101000000.000000", value="-101000000.000000", state="source_observed"),
    ]
    source = batch(tmp_path / "source", [row])
    base_root = tmp_path / "base"
    base = prepare(parent, base_root, [source])
    financial = FinancialTables.prepared(base_root, parent)
    with financial.sql() as c:
        capture = dict(zip(
            ("capture_id", "receipt_sha256", "response_sha256"),
            c.execute("SELECT capture_id,receipt_sha256,response_sha256 FROM meta.financial_captures").fetchone(),
        ))
    groups = []
    definitions = []
    for item in row["fields"]:
        if item["source_field"] in {"P", "R"}:
            continue
        precision = "ps4" if item["source_field"] in {"BASICEPS", "MINY"} else "f2"
        groups.append({
            "key": ["lrb", item["source_field"], item["source_title"], precision, "lrb", "CNY",
                    "合并期末", "unverified", item["state"], item["value"] is not None],
            "rows": 1,
        })
        if item["source_field"] == "MINY":
            continue
        definitions.append({
            "statement": "lrb", "source_field": item["source_field"], "source_title": item["source_title"],
            "unit": "CNY/share" if item["source_field"] == "BASICEPS" else "CNY",
            "scale_decimal_text": "1", "reviewed": True, "anchor_document_sha256": "fixture-document",
            "anchor_page": 1, "anchor_label": item["source_title"],
        })
    audit = dict(
        schema=units.AUDIT_SCHEMA, candidate_id=base["candidate_id"],
        mapping_sha256=base["mapping_sha256"],
        fields=base["validation"]["table_rows"]["financial.observations"],
        batch_key=base["batches"][0]["batch_key"], captures=[capture], groups=groups,
    )
    audit_path = tmp_path / "audit.json"
    audit_path.write_bytes(canonical(audit))
    evidence = tmp_path / "definition.txt"
    evidence.write_text("Fixture statement header; money CNY and EPS CNY/share")
    review = dict(
        schema=units.REVIEW_SCHEMA, method="reviewed_statement_headers_and_native_provider_display",
        audit_sha256=sha_file(audit_path), historical_visibility_added=False,
        evidence=[units.file_reference(evidence)], reviewed_document_sha256=["fixture-document"],
        available_from="2026-10-20T10:00:00+00:00", definitions=definitions,
    )
    review_path = tmp_path / "review.json"
    review_path.write_bytes(canonical(review))
    output = tmp_path / "units"
    value = units.prepare(base_root, audit_path, review_path, output)
    resolved = financial.with_units(output, expected_sha256=sha_file(output / "candidate.json"))
    return financial, resolved, output, value, audit_path, review_path


def point(reader, field="unknown", title="未知单位", **extra):
    return reader.source_point(
        "600000.XSHG", field, "2024-03-31", source="sina", statement="lrb", title=title,
        as_of=extra.pop("as_of", "2026-10-21T00:00:00+00:00"),
        mode=extra.pop("mode", "source_research"), **extra,
    )


def test_unit_annotations_preserve_originals_exact_decimal_and_missing(tmp_path):
    original, resolved, _, candidate, _, _ = fixture(tmp_path)
    assert candidate["coverage"]["numeric_unit_recoverable"] == 3
    assert candidate["coverage"]["numeric_unit_unverified_remaining"] == 1
    assert point(original)["status"] == "unavailable"
    assert point(resolved)["value"] == "1.25"
    eps = point(resolved, "BASICEPS", "基本每股收益")
    assert (eps["value"], eps["unit"]) == ("0.104900", "CNY/share")
    assert point(resolved, "ZERO", "零金额")["value"] == "0.000000"
    assert point(resolved, "empty", "空字符串")["value"] is None
    assert point(resolved, "null", "空值")["value"] is None
    # ps4 is not a per-share rule: an unreviewed ps4 amount remains unknown.
    assert point(resolved, "MINY", "少数股东综合收益")["value"] is None
    assert eps["context_id"] != eps["base_context_id"]
    assert eps["unit_candidate_published"] is False
    with original.sql() as c:
        before = c.execute("SELECT * FROM financial.observations ORDER BY observation_id").fetchall()
    with resolved.sql() as c:
        after = c.execute("SELECT * FROM financial.observations ORDER BY observation_id").fetchall()
        assert c.execute("""SELECT count(*) FROM financial.unit_resolved_observations
            WHERE unit_ready_value_decimal_text IS NOT NULL""").fetchone()[0] == 3
        assert c.execute("SELECT count(*) FROM financial.visibility").fetchone()[0] == 0
    assert before == after
    # Existing canonical values and formulas retain their original path.
    assert resolved.point("600000.XSHG", "net_profit", "2024-03-31", source="sina",
                          as_of="2026-10-21T00:00:00+00:00", mode="source_research")["value"] == (
                              "1234567890123456789.000000001")


def test_new_unit_evidence_has_separate_cutoff_and_never_grants_pit(tmp_path):
    _, resolved, _, _, _, _ = fixture(tmp_path)
    early = point(resolved, as_of="2026-10-20T09:59:59+00:00")
    assert early["value"] is None
    assert "unit_definition_not_captured_at_cutoff" in early["reasons"]
    historical = point(resolved, mode="historical_pit")
    assert historical["status"] == "visibility_unproven"
    assert "historical_visibility_unproven" in historical["reasons"]
    assert point(resolved, title="另一标题")["status"] == "missing"


def test_units_do_not_remove_other_quality_failures(tmp_path):
    _, resolved, _, _, _, _ = fixture(tmp_path, reasons=("source_requires_review",))
    value = point(resolved)
    assert value["value"] is None and "source_requires_review" in value["reasons"]


def test_hashes_and_audited_base_are_required(tmp_path):
    original, _, root, _, _, review = fixture(tmp_path)
    with pytest.raises(DataError, match="hash differs"):
        original.with_units(root, expected_sha256="wrong")
    other = deepcopy(original)
    other.files = other.files[:-1]
    candidate = units.UnitCandidate(root, expected_sha256=sha_file(root / "candidate.json"))
    with pytest.raises(DataError, match="audited frozen source"):
        candidate.bind(other)
    evidence = Path(json.loads(review.read_bytes())["evidence"][0]["path"])
    evidence.write_text("changed")
    with pytest.raises(DataError, match="evidence changed"):
        original.with_units(root, expected_sha256=sha_file(root / "candidate.json"))


@pytest.mark.parametrize("change", ["precision", "currency", "source", "scope"])
def test_unreviewed_metadata_variants_cannot_receive_a_unit(tmp_path, change):
    _, _, _, _, audit_path, review_path = fixture(tmp_path)
    audit = json.loads(audit_path.read_bytes())
    slot, value = {"precision": (3, "p2"), "currency": (5, "USD"),
                   "source": (4, "fzb"), "scope": (6, "母公司")}[change]
    audit["groups"][0]["key"][slot] = value
    audit_path.write_bytes(canonical(audit))
    review = json.loads(review_path.read_bytes())
    review["audit_sha256"] = sha_file(audit_path)
    review_path.write_bytes(canonical(review))
    with pytest.raises(DataError, match="Mixed or unsupported"):
        units.prepare(tmp_path / "base", audit_path, review_path, tmp_path / "rejected")


def test_immutable_unit_candidate_rejects_changed_rules(tmp_path):
    _, _, root, _, audit_path, review_path = fixture(tmp_path)
    review = json.loads(review_path.read_bytes())
    review["definitions"][0]["unit"] = "percent"
    review_path.write_bytes(canonical(review))
    with pytest.raises(DataError, match="Unsupported or unreviewed"):
        units.prepare(tmp_path / "base", audit_path, review_path, root)


def test_resolved_context_retains_the_original_dimension_contract(tmp_path):
    _, resolved, _, _, _, _ = fixture(tmp_path)
    value = point(resolved)
    with resolved.sql() as c:
        raw = c.execute("SELECT raw_context_json FROM financial.observations WHERE source_field='unknown'").fetchone()[0]
    context = json.loads(raw)
    context["unit"] = "CNY"
    assert value["context_id"] == t.identity("context", context)


def test_unit_overlay_can_bind_published_base_without_changing_publication(tmp_path):
    original, _, root, _, _, _ = fixture(tmp_path)
    parent = original.release
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        manifest = compose(writer, parent, original.candidate_root, expected_parent_sha256=parent.sha256)
        writer.publish(manifest, expected_parent=parent.release_id)
    published = MarketRelease(parent.root)
    before = published.sha256
    reviewed = published.financial().with_units(root, expected_sha256=sha_file(root / "candidate.json"))
    value = point(reviewed, "BASICEPS", "基本每股收益")
    assert (value["value"], value["unit"]) == ("0.104900", "CNY/share")
    assert value["financial_published"] is True and value["unit_candidate_published"] is False
    assert MarketRelease(parent.root).sha256 == before
