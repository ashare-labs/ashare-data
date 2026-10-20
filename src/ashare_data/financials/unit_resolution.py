"""Small, evidence-bound unit overlays for already frozen financial observations.

The overlay never rewrites observations, assigns canonical field semantics, or
grants historical visibility. Its rules apply only to the audited input objects,
capture receipts, source field/title, statement, currency and extraction mapping.
"""

from collections import Counter, defaultdict
import copy
import json
from pathlib import Path

from ashare_data.core.model import require
from ashare_data.financials import tables as t
from ashare_data.storage.financial_release import immutable_json
from ashare_data.storage.market_release import sha_file, sql_literal

SCHEMA = "ad.financial-unit-candidate.v1"
AUDIT_SCHEMA = "ad.sina-unit-metadata-audit.v1"
REVIEW_SCHEMA = "ad.financial-unit-definition-review.v1"


def file_reference(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha_file(path), "bytes": path.stat().st_size}


def verify_file(item):
    path = Path(item["path"])
    require(path.is_file() and path.stat().st_size == item["bytes"]
            and sha_file(path) == item["sha256"], "FIN_UNIT_EVIDENCE", "Unit evidence changed")
    return path


def prepare(candidate_root, audit_path, review_path, output):
    """Build only reviewed unit rules and small capture bindings, without copying data."""
    candidate_path = Path(candidate_root) / "candidate.json"
    base = json.loads(candidate_path.read_bytes())
    audit = json.loads(Path(audit_path).read_bytes())
    review = json.loads(Path(review_path).read_bytes())
    require(audit["schema"] == AUDIT_SCHEMA and review["schema"] == REVIEW_SCHEMA,
            "FIN_UNIT_SCHEMA", "Unknown unit audit or review")
    require(audit["candidate_id"] == base["candidate_id"]
            and audit["mapping_sha256"] == base["mapping_sha256"]
            and audit["fields"] == base["validation"]["table_rows"]["financial.observations"],
            "FIN_UNIT_SCOPE", "Unit audit does not bind the frozen candidate")
    require(review["audit_sha256"] == sha_file(audit_path)
            and review["method"] == "reviewed_statement_headers_and_native_provider_display"
            and review["historical_visibility_added"] is False,
            "FIN_UNIT_EVIDENCE", "Unit definitions require the exact audited source and reviewed evidence")
    for item in review["evidence"]:
        verify_file(item)
    groups = defaultdict(list)
    for group in audit["groups"]:
        key = group["key"]
        if key[7] == "unverified":
            groups[tuple(key[:3])].append(group)
    rules, seen = [], set()
    for definition in review["definitions"]:
        key = tuple(definition[name] for name in ("statement", "source_field", "source_title"))
        require(key not in seen and key in groups, "FIN_UNIT_DEFINITION", "Duplicate or unaudited field definition")
        seen.add(key)
        require(definition["unit"] in {"CNY", "CNY/share"}
                and definition["scale_decimal_text"] == "1"
                and definition["anchor_document_sha256"] in review["reviewed_document_sha256"]
                and definition["anchor_page"] >= 1 and definition["anchor_label"]
                and definition["reviewed"] is True,
                "FIN_UNIT_DEFINITION", "Unsupported or unreviewed unit rule")
        # Precision is display metadata, not a unit. A rule requires independent
        # statement-header evidence, and the complete audited metadata signature.
        require(all(g["key"][3] in {"f2", "ps4"}
                    and g["key"][4] == key[0]
                    and g["key"][5:7] == ["CNY", "合并期末"]
                    for g in groups[key]),
                "FIN_UNIT_METADATA", "Mixed or unsupported source metadata; keep this field unverified")
        value = dict(definition, source="sina", currency="CNY", scope="consolidated",
                     numeric_rows=sum(g["rows"] for g in groups[key] if g["key"][9]),
                     missing_rows=sum(g["rows"] for g in groups[key] if not g["key"][9]),
                     available_from=review["available_from"],
                     evidence_kind="reviewed_source_definition_not_explicit_native_unit")
        value["rule_id"] = t.identity("unit-rule", value, sha_file(review_path))
        rules.append(value)
    require(rules, "FIN_UNIT_DEFINITION", "No supported reviewed unit definitions")
    total = sum(g["rows"] for values in groups.values() for g in values if g["key"][9])
    recovered = sum(r["numeric_rows"] for r in rules)
    by_statement = Counter()
    for rule in rules:
        by_statement[rule["statement"]] += rule["numeric_rows"]
    result = {
        "schema": SCHEMA, "base_candidate": file_reference(candidate_path),
        "base_candidate_id": base["candidate_id"], "base_mapping_sha256": base["mapping_sha256"],
        "base_batch_key": audit["batch_key"], "audit": file_reference(audit_path),
        "review": file_reference(review_path), "rules": rules, "captures": audit["captures"],
        "base_table_objects": [
            {k: item[k] for k in ("table", "sha256", "bytes", "rows", "partition_key")}
            for item in base["files"]
        ],
        "coverage": {
            "numeric_unit_unverified_before": total,
            "numeric_unit_recoverable": recovered,
            "numeric_unit_unverified_remaining": total - recovered,
            "recovered_by_statement": dict(by_statement),
            "rules": len(rules),
            "missing_values_filled": 0, "historical_visibility_added": 0,
        },
        "published": False, "financial_schema_changed": False,
        "original_observations_changed": False,
        "implementation_sha256": sha_file(__file__),
    }
    result["candidate_id"] = t.identity("unit-candidate", result)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    immutable_json(output / "candidate.json", result)
    return result


class UnitCandidate:
    """Explicit opt-in to an unpublished, checksum-pinned unit candidate."""

    def __init__(self, root, *, expected_sha256):
        path = Path(root) / "candidate.json"
        require(sha_file(path) == expected_sha256, "FIN_UNIT_CANDIDATE", "Unit candidate hash differs")
        self.value = json.loads(path.read_bytes())
        value = dict(self.value)
        candidate_id = value.pop("candidate_id")
        require(value["schema"] == SCHEMA and t.identity("unit-candidate", value) == candidate_id
                and value["implementation_sha256"] == sha_file(__file__),
                "FIN_UNIT_CANDIDATE", "Unexpected unit candidate implementation or identity")
        for key in ("base_candidate", "audit", "review"):
            verify_file(value[key])
        review = json.loads(Path(value["review"]["path"]).read_bytes())
        for item in review["evidence"]:
            verify_file(item)

    def bind(self, financial):
        required = {(r["table"], r["sha256"], r["bytes"], r["rows"], r["partition_key"])
                    for r in self.value["base_table_objects"]}
        actual = {(r["table"], r["sha256"], r["bytes"], r["rows"], r["partition_key"])
                  for r in financial.files}
        require(required <= actual, "FIN_UNIT_SCOPE", "Reader lacks the audited frozen source objects")
        if financial.candidate:
            require(financial.candidate["candidate_id"] == self.value["base_candidate_id"],
                    "FIN_UNIT_SCOPE", "Unit overlay targets another prepared candidate")
        else:
            require(self.value["base_candidate_id"] in financial.release.manifest[
                "financial_reference_tables"]["candidate_ids"],
                "FIN_UNIT_SCOPE", "Audited base candidate is not in this publication")
        result = copy.copy(financial)
        result._unit_candidate = self
        return result

    def install(self, connection):
        """Expose annotations beside unchanged original columns; no implicit time gate."""
        import pandas as pd
        rules = [{k: r[k] for k in (
            "rule_id", "source", "statement", "source_field", "source_title",
            "currency", "scope", "unit", "scale_decimal_text", "available_from",
        )} for r in self.value["rules"]]
        captures = [{k: r[k] for k in ("capture_id", "receipt_sha256", "response_sha256")}
                    for r in self.value["captures"]]
        for name, records in (("_financial_unit_rules", rules), ("_financial_unit_scope", captures)):
            connection.register(name + "_input", pd.DataFrame(records))
            connection.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS SELECT * FROM {name}_input")
            connection.unregister(name + "_input")
        batch = sql_literal(self.value["base_batch_key"])
        mapping = sql_literal(self.value["base_mapping_sha256"])
        connection.execute(f"""CREATE OR REPLACE VIEW financial.unit_resolved_observations AS
          WITH scoped_reports AS (
            SELECT DISTINCT l.from_id AS report_version_id
            FROM financial.links l
            JOIN meta.financial_captures c ON c.capture_id=l.to_id
            JOIN _financial_unit_scope s USING(capture_id,receipt_sha256,response_sha256)
            WHERE l.from_type='report' AND l.to_type='capture' AND l.relation='captured_in'
              AND c.batch_key={batch}
          )
          SELECT o.*,u.rule_id AS unit_rule_id,u.unit AS resolved_unit,
            u.available_from AS unit_evidence_available_from,
            CASE WHEN u.rule_id IS NOT NULL AND o.value_state='source_observed'
                 AND o.quality_reasons_json='["unit_unverified"]'
              THEN o.value_decimal_text END AS unit_ready_value_decimal_text
          FROM financial.observations o
          JOIN financial.report_versions r USING(report_version_id)
          JOIN financial.extractions e USING(extraction_version_id)
          LEFT JOIN scoped_reports s USING(report_version_id)
          LEFT JOIN _financial_unit_rules u
            ON s.report_version_id IS NOT NULL AND r.source=u.source
            AND o.statement=u.statement AND o.source_field=u.source_field
            AND o.source_title=u.source_title AND o.currency=u.currency AND o.scope=u.scope
            AND e.mapping_sha256={mapping} AND o.unit_state='unverified'
            AND o.canonical_unit IS NULL AND o.canonical_value_decimal_text IS NULL
        """)
