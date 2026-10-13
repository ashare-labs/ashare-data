"""Seal the fixed free-source supplement onto the reviewed minute base.

This is bounded package assembly, not a provider or a data admission model.
All original files and claims remain available; only gate explanations and
source references change. State facts, replay defaults, clocks and scope do not.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from ashare_data import MinuteEvidenceView, seal_minute_evidence
from ashare_data.model import canonical

BASE_SHA = "04b46591ea45e74b7abcc223c94ce11b80642f0d8199a1c00a3cdcf104fe0091"
SUPPLEMENT_SHA = "656021201a1afce5d2b1f666f635cb82f7479a8662238f920b0889dc46b69a07"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verified_binding(root, expected):
    raw = (root / "binding.json").read_bytes()
    assert digest(raw) == expected, "Input binding changed"
    binding = json.loads(raw)
    for entry in binding["files"]:
        path = root / entry["path"]
        assert not path.is_symlink() and path.resolve().is_relative_to(root.resolve())
        data = path.read_bytes()
        assert len(data) == entry["bytes"] and digest(data) == entry["sha256"]
    MinuteEvidenceView(root / "binding.json", expected_binding_sha256=expected)
    return binding


def build(base, supplement, reanalysis, before_report, destination, received_at):
    base, supplement, reanalysis, before_report, destination = map(
        Path, (base, supplement, reanalysis, before_report, destination)
    )
    original = verified_binding(base, BASE_SHA)
    extra = verified_binding(supplement, SUPPLEMENT_SHA)
    proof = json.loads((reanalysis / "report.json").read_bytes())
    assert proof["passed"] and proof["parser_issue_resolved"] and not proof["network_used"]
    assert len(proof["cases"]) == 15 and sum(c["native_empty"] for c in proof["cases"]) == 5
    assert not proof["full_action_absence"] and proof["historical_available_at"] is None
    before = json.loads(before_report.read_bytes())
    assert before["frozen_commit"] == "d851553f9aa18f4347a58aea37cc19813dfe5392"
    assert sum(c["outcome"] == "rejected" for c in before["cases"]) == 5
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".minute-supplement-", dir=destination.parent) as tmp:
        stage = Path(tmp)
        for entry in original["files"]:
            target = stage / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(base / entry["path"], target)
        additions = [e for e in extra["files"] if e["path"].startswith("supplement/")]
        assert len(additions) == 24
        for entry in additions:
            target = stage / entry["path"]
            assert not target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(supplement / entry["path"], target)
        shutil.copytree(reanalysis, stage / "parser")
        shutil.copyfile(before_report, stage / "parser/before-parser.json")
        data = json.loads((base / "data.json").read_bytes())
        supplemental = json.loads((supplement / "data.json").read_bytes())["minute_evidence"]
        spec = data["minute_evidence"]
        replacements = {c["gate_id"]: c for c in supplemental["assessments"]}
        # Keep the r2 contract and its explicit opt-in status from the fixed base.
        for gate in spec["assessments"]:
            if gate["gate_id"] in {"G00", "G03", "G04", "G06", "G07", "G10"}:
                updated = replacements[gate["gate_id"]]
                assert gate["status"] == updated["status"]
                gate.update(updated)
            if gate["gate_id"] == "G10":
                gate["reason"] = (
                    "Preserved 281 TDX action/share-capital records, 12 BaoStock dividend rows "
                    "with share-market fields, 254 paginated CNINFO announcements and issuer PDFs. "
                    "Five real native-empty BaoStock responses now validate as complete_empty_unknown "
                    "within their echoed query scopes; old failed receipts remain preserved. "
                    "Missing credit/tradability clocks and source-domain completeness still prevent "
                    "full action-absence proof."
                )
                gate["source_refs"] += ["parser/report.json", "parser/before-parser.json"]
        spec["actions"]["source_refs"] += [
            "supplement/action-coverage-supplement.json",
            "supplement/acquisition.tar.gz",
            "parser/report.json",
            "parser/before-parser.json",
        ]
        spec["actions"]["unknown_domains"] += (
            " Broader TDX records, CNINFO announcements and bounded BaoStock native-empty queries "
            "add source observations only; missing payment/credit/tradability dates, source omissions "
            "and historical visibility are not resolved."
        )
        (stage / "data.json").write_bytes(canonical(data))
        (stage / "parser/integration.json").write_bytes(
            canonical(
                dict(
                    schema="ad.minute-free-source-integration.v1",
                    base_binding_sha256=BASE_SHA,
                    supplement_binding_sha256=SUPPLEMENT_SHA,
                    scope_changed=False,
                    original_source_rows=1428,
                    target_labels=1422,
                    normalized_actions_changed=False,
                    historical_facts_changed=False,
                    consumer_contract_changed=False,
                    source_clocks_changed=False,
                    parser_scope="type14 dividend and type16 adjust-factor verified native-empty schema",
                    full_action_absence=False,
                    execution_permission=False,
                )
            )
        )
        inventory = {k: v for k, v in original.items() if k != "files"}
        inventory["received_at"] = received_at
        inventory["files"] = [
            dict(
                path=str(p.relative_to(stage)),
                bytes=p.stat().st_size,
                sha256=digest(p.read_bytes()),
            )
            for p in sorted(stage.rglob("*"))
            if p.is_file()
        ]
        (stage / "inventory.json").write_bytes(canonical(inventory))
        result = seal_minute_evidence(stage, destination)
        result["base_binding_sha256"] = BASE_SHA
        result["source_supplement_binding_sha256"] = SUPPLEMENT_SHA
        result["bound_files"] = len(inventory["files"])
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "supplement", "reanalysis", "before-report", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--received-at", default=None)
    args = parser.parse_args()
    result = build(
        args.base,
        args.supplement,
        args.reanalysis,
        args.before_report,
        args.output,
        args.received_at or datetime.now(timezone.utc).isoformat(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
