"""Three additional Sina amount definitions backed by a reviewed 2026 report."""

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
from decimal import Decimal
from ashare_data.financials.unit_resolution import file_reference, prepare
from ashare_data.storage.financial_release import immutable_json
from ashare_data.storage.market_release import sha_file

ANCHOR = "da5b6faed07c4ad3cf20b7252718b8f6c9e95bea0384ebe1a36bee450fcc75cc"
FIELDS = [
    ("OTHERINCO", "其他收益", "28706580.99", 115794),
    ("INTERESTEXPENSE", "利息费用", "3113705.96", 103569),
    ("DEVEEXPE", "研发费用", "130510600.79", 99654),
]


def build(root, output):
    root, output = Path(root), Path(output)
    audit = root / "coordination/unit-evidence-20261020/metadata-audit/audit.json"
    prior = json.loads(
        (root / "unit-candidates/20261020-sina-reviewed-v1/candidate.json").read_bytes()
    )
    official = root / "batches/20261019-official-v1"
    rp = official / "receipts/pdf-1225175081.json"
    receipt = json.loads(rp.read_bytes())
    pdf = official / receipt["path"]
    assert (
        hashlib.sha256(gzip.decompress(pdf.read_bytes())).hexdigest() == receipt["sha256"] == ANCHOR
    )
    source = root / "batches/20261019-public-v1/normalized/sina_income-sh600126.jsonl.gz"
    report = next(
        json.loads(line)
        for line in gzip.open(source, "rt")
        if json.loads(line)["period_end"] == "2026-03-31"
    )
    definitions = []
    for field, title, value, count in FIELDS:
        item = next(
            x for x in report["fields"] if x["source_field"] == field and x["source_title"] == title
        )
        assert Decimal(item["value"]) == Decimal(value)
        definitions.append(
            dict(
                statement="lrb",
                source_field=field,
                source_title=title,
                unit="CNY",
                scale_decimal_text="1",
                reviewed=True,
                anchor_document_sha256=ANCHOR,
                anchor_announcement_id="1225175081",
                anchor_page=9,
                header_page=8,
                anchor_label=title,
                native_title_match="exact",
                unit_only_not_canonical_semantic_equivalence=True,
                exact_decimal_sample=dict(
                    security=report["security"], period="2026-03-31", value=value
                ),
            )
        )
    evidence = [file_reference(pdf), file_reference(rp), file_reference(source)]
    for relative in (
        "coordination/unit-evidence-20261020/sina-cncw2022.js.gz",
        "coordination/unit-evidence-20261020/sina-cncw2022.receipt.json",
    ):
        evidence.append(file_reference(root / relative))
    review_path = output / "review.json"
    if not review_path.exists():
        immutable_json(
            review_path,
            dict(
                schema="ad.financial-unit-definition-review.v1",
                method="reviewed_statement_headers_and_native_provider_display",
                audit_sha256=sha_file(audit),
                available_from=datetime.now(timezone.utc).isoformat(),
                historical_visibility_added=False,
                definitions=definitions,
                evidence=evidence,
                reviewed_document_sha256=[ANCHOR],
                reviewed_pages={"1225175081": [8, 9]},
                derivation_script_sha256=sha_file(__file__),
                evidence_boundary=[
                    "合并利润表 单位：元 币种：人民币; exact three row labels on following page.",
                    "Sina display precision and abbreviations do not scale native values.",
                    "Only exact audited fields, CNY consolidated context and captured extraction qualify.",
                    "This resolves units, not canonical metric equivalence or historical visibility.",
                ],
            ),
        )
    result = prepare(
        Path(prior["base_candidate"]["path"]).parent, audit, review_path, output / "candidate"
    )
    assert [(r["source_field"], r["numeric_rows"]) for r in result["rules"]] == [
        (x[0], x[3]) for x in FIELDS
    ]
    assert result["coverage"]["numeric_unit_recoverable"] == 319017
    return result["coverage"]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    Path(a.output).mkdir(parents=True, exist_ok=True)
    print(json.dumps(build(a.root, a.output)))
