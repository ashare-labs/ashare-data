"""Publish compact unit evidence beside unchanged frozen financial observations."""

from copy import deepcopy
import json
from pathlib import Path
import shutil

from ashare_data.core.model import require
from ashare_data.financials import tables as t
from ashare_data.financials import unit_resolution as units
from ashare_data.storage.financial_release import checked_path
from ashare_data.storage.market_release import (
    MarketRelease, ReleaseWriter, sha_file,
)

CONTRACT = "financial_unit_resolution"
SCHEMA = "ad.financial-unit-publication.v1"
LAYERS_SCHEMA = "ad.financial-unit-publication-set.v1"


def _verified(root, item):
    path = checked_path(root, item["path"])
    require(path.stat().st_size == item["bytes"] and sha_file(path) == item["sha256"],
            "FIN_UNIT_EVIDENCE", "Published unit evidence changed")
    return path


def verify_contract(root, manifest):
    """Verify the immutable proof closure without accessing original source paths."""
    contract = manifest[CONTRACT]
    if contract.get("schema") == LAYERS_SCHEMA:
        return _verify_layers(root, manifest, contract)
    require(contract["schema"] == SCHEMA, "FIN_UNIT_PUBLICATION", "Unknown unit publication")
    evidence = {(x["path"], x["sha256"], x["bytes"]) for x in manifest["evidence"]}
    items = [contract["candidate"], *contract["dependencies"], *contract["code_evidence"]]
    require(all((x["path"], x["sha256"], x["bytes"]) in evidence for x in items),
            "FIN_UNIT_PUBLICATION", "Unit proof is absent from the release file list")
    value = json.loads(_verified(root, contract["candidate"]).read_bytes())
    identity = dict(value)
    candidate_id = identity.pop("candidate_id")
    require(value["schema"] == units.SCHEMA
            and t.identity("unit-candidate", identity) == candidate_id
            and value["implementation_sha256"] == sha_file(units.__file__)
            and contract["candidate_id"] == candidate_id,
            "FIN_UNIT_CANDIDATE", "Unit candidate implementation or identity differs")
    dependencies = {x["original_path"]: x for x in contract["dependencies"]}
    require(len(dependencies) == len(contract["dependencies"]),
            "FIN_UNIT_PUBLICATION", "Duplicate unit proof paths")

    def reference(item):
        found = dependencies.get(item["path"])
        require(found is not None and all(found[k] == item[k] for k in ("sha256", "bytes")),
                "FIN_UNIT_EVIDENCE", "Published dependency does not match its original reference")
        return _verified(root, found)

    base = json.loads(reference(value["base_candidate"]).read_bytes())
    audit = json.loads(reference(value["audit"]).read_bytes())
    review = json.loads(reference(value["review"]).read_bytes())
    for item in review["evidence"]:
        reference(item)
    expected_paths = {value[k]["path"] for k in ("base_candidate", "audit", "review")}
    expected_paths.update(x["path"] for x in review["evidence"])
    require(set(dependencies) == expected_paths,
            "FIN_UNIT_PUBLICATION", "Unexpected or incomplete unit proof closure")
    require(base["candidate_id"] == value["base_candidate_id"]
            and base["mapping_sha256"] == value["base_mapping_sha256"]
            and audit["captures"] == value["captures"]
            and audit["mapping_sha256"] == value["base_mapping_sha256"]
            and review["audit_sha256"] == value["audit"]["sha256"]
            and review["historical_visibility_added"] is False,
            "FIN_UNIT_SCOPE", "Unit proof does not bind the audited extraction")
    require(contract["coverage"] == value["coverage"]
            and not value["original_observations_changed"]
            and not value["financial_schema_changed"],
            "FIN_UNIT_PUBLICATION", "Unit publication cannot rewrite source observations")
    code = {x["role"]: x for x in contract["code_evidence"]}
    require(len(code) == len(contract["code_evidence"])
            and code["unit_implementation"]["sha256"] == value["implementation_sha256"],
            "FIN_UNIT_PUBLICATION", "Missing frozen unit implementation")
    if review.get("derivation_script_sha256"):
        require(code.get("review_derivation", {}).get("sha256") == review["derivation_script_sha256"],
                "FIN_UNIT_PUBLICATION", "Missing reviewed definition producer")
    for item in code.values():
        _verified(root, item)
    return value


class PublishedUnits(units.UnitCandidate):
    """The reviewed implementation bound to a self-contained published proof."""

    published = True

    def __init__(self, release):
        self.value = verify_contract(release.root, release.manifest)
        self.release_id = release.release_id
        self.manifest_sha256 = release.sha256


def freeze(writer, source, kind):
    """Retain a small evidence copy; never move or delete its original."""
    source = Path(source)
    digest = sha_file(source)
    suffix = source.suffix.lstrip(".") or "bin"
    target = writer.root / "objects" / (digest + "." + suffix)
    if target.exists():
        require(sha_file(target) == digest, "MARKET_INTEGRITY", "Existing proof object changed")
        item = {"path": str(target.relative_to(writer.root)), "sha256": digest,
                "bytes": target.stat().st_size}
    else:
        staged = writer.root / "work" / ("unit-proof-" + digest + "." + suffix)
        if staged.exists():
            require(sha_file(staged) == digest, "FIN_UNIT_EVIDENCE", "Incomplete proof copy differs")
        else:
            shutil.copyfile(source, staged)
        item = writer.object(staged, suffix)
    return dict(item, kind=kind)


def compose(parent, candidate_root, *, expected_sha256, calendar_plan=None, review_script=None,
            free_reserve_bytes=150 * 1024**3):
    """Publish one checked unit candidate, optionally with a bounded calendar extension."""
    candidate = units.UnitCandidate(candidate_root, expected_sha256=expected_sha256)
    candidate.bind(parent.financial())
    calendar = None
    if calendar_plan is not None:
        from ashare_data.storage.calendar_release import verify_plan
        calendar = verify_plan(parent, calendar_plan)
    operation = t.identity("unit-publication", parent.sha256, expected_sha256,
                           sha_file(calendar_plan) if calendar_plan else None)
    current = MarketRelease(parent.root)
    if current.manifest.get(CONTRACT, {}).get("operation_id") == operation:
        verify_contract(current.root, current.manifest)
        return {"unchanged": True, "already_applied": True, "release": current.receipt()}
    require(current.sha256 == parent.sha256, "MARKET_PARENT_CHANGED", "Unit parent advanced")
    if CONTRACT in parent.manifest:
        return _compose_increment(parent, candidate_root, expected_sha256=expected_sha256,
                                  review_script=review_script, calendar_plan=calendar_plan,
                                  free_reserve_bytes=free_reserve_bytes)
    refs = [candidate.value[k] for k in ("base_candidate", "audit", "review")]
    review = json.loads(Path(candidate.value["review"]["path"]).read_bytes())
    refs += review["evidence"]
    code = {"unit_implementation": Path(units.__file__),
            "publication_implementation": Path(__file__)}
    if review.get("derivation_script_sha256"):
        require(review_script is not None and sha_file(review_script) == review["derivation_script_sha256"],
                "FIN_UNIT_PUBLICATION", "Supply the checksum-matching reviewed definition producer")
        code["review_derivation"] = Path(review_script)
    needed = sum(x["bytes"] for x in refs) + (Path(candidate_root) / "candidate.json").stat().st_size
    needed += sum(p.stat().st_size for p in code.values())
    if calendar:
        needed += calendar["metadata"]["bytes"] + sum(x["bytes"] for x in calendar["evidence"])
    require(needed < 512 * 1024**2, "MARKET_CAPACITY", "Unit/calendar increment exceeds 512 MiB")
    require(shutil.disk_usage(parent.root).free > needed + free_reserve_bytes,
            "MARKET_CAPACITY", "Keep the 150 GiB free-space reserve")
    with ReleaseWriter(parent.root, retain_work_files=True,
                       free_reserve_bytes=free_reserve_bytes) as writer:
        current = MarketRelease(parent.root)
        if current.manifest.get(CONTRACT, {}).get("operation_id") == operation:
            verify_contract(current.root, current.manifest)
            return {"unchanged": True, "already_applied": True, "release": current.receipt()}
        require(current.sha256 == parent.sha256, "MARKET_PARENT_CHANGED", "Unit parent advanced")
        value = deepcopy(parent.manifest)
        dependencies = []
        for item in refs:
            frozen = freeze(writer, units.verify_file(item), "financial_unit_evidence")
            dependencies.append(dict(frozen, original_path=item["path"]))
        frozen_candidate = freeze(writer, Path(candidate_root) / "candidate.json", "financial_unit_candidate")
        code_evidence = [dict(freeze(writer, path, "financial_unit_code"), role=role)
                         for role, path in code.items()]
        value[CONTRACT] = {
            "schema": SCHEMA, "operation_id": operation,
            "candidate": frozen_candidate, "candidate_id": candidate.value["candidate_id"],
            "dependencies": dependencies, "code_evidence": code_evidence,
            "coverage": candidate.value["coverage"],
            "base_release_id": parent.release_id, "base_manifest_sha256": parent.sha256,
            "raw_observations_unchanged": True, "historical_visibility_added": False,
            "source_originals": "Inherited source_assets and capture hashes remain separately retained; no raw history copy",
        }
        additions = [frozen_candidate, *dependencies, *code_evidence]
        if calendar:
            value["metadata"] = freeze(writer, calendar["metadata"]["path"], "calendar_metadata")
            proofs = [freeze(writer, x["path"], "calendar_extension_evidence")
                      for x in calendar["evidence"]]
            plan_object = freeze(writer, calendar_plan, "calendar_extension_plan")
            value["calendar_extension"] = {
                "schema": calendar["schema"], "plan": plan_object,
                "base_release_id": parent.release_id, "base_manifest_sha256": parent.sha256,
                "added_sessions": calendar["added_sessions"], "proofs": proofs,
                "calendar_scope": calendar["calendar_scope"],
                "historical_pit_certified": False, "security_tradability_proven": False,
            }
            additions += [plan_object, *proofs]
        existing = {x["path"] for x in value["evidence"]}
        for item in additions:
            if item["path"] not in existing:
                value["evidence"].append(item)
                existing.add(item["path"])
        value["change_set"] = {
            "kind": "reviewed_financial_units_and_calendar",
            "operation_id": operation, "market_fact_rows_rewritten": 0,
            "financial_observation_rows_rewritten": 0,
            "numeric_units_resolved": candidate.value["coverage"]["numeric_unit_recoverable"],
            "calendar_sessions_added": len(calendar["added_sessions"]) if calendar else 0,
        }
        capacity = value.setdefault("capacity", {})
        old_owned = capacity.pop("current_owned_bytes", None)
        capacity.update(unit_calendar_increment_budget_bytes=512 * 1024**2,
                        unit_calendar_retained_input_bytes=needed)
        if old_owned is not None:
            capacity.update(previous_release_owned_bytes=old_owned,
                            current_owned_bytes_upper_bound=old_owned + needed)
        verify_contract(writer.root, value)
        published = writer.publish(value, expected_parent=parent.release_id, verify_unchanged=False)
    return {"unchanged": False, "release": published, "operation_id": operation,
            "unit_coverage": candidate.value["coverage"],
            "calendar_sessions_added": len(calendar["added_sessions"]) if calendar else 0}


def layers(contract):
    return contract["layers"] if contract.get("schema") == LAYERS_SCHEMA else [contract]


def preserves_prior(previous, current):
    """A new publication may append one proof, never reinterpret old unit rules."""
    if current == previous:
        return True
    if not current or current.get("schema") != LAYERS_SCHEMA:
        return False
    before, after = layers(previous), current.get("layers", [])
    return len(after) == len(before) + 1 and after[:-1] == before


def _verify_layers(root, manifest, contract):
    entries = contract.get("layers", [])
    require(len(entries) >= 2 and all(x.get("schema") == SCHEMA for x in entries),
            "FIN_UNIT_PUBLICATION", "Unit increments need a flat immutable proof list")
    values = [verify_contract(root, dict(manifest, **{CONTRACT: layer})) for layer in entries]
    ids = [v["candidate_id"] for v in values]
    require(len(set(ids)) == len(ids), "FIN_UNIT_PUBLICATION", "Duplicate unit increment")
    merged = deepcopy(values[0])
    rules, seen = [], set()
    for value in values:
        for key in ("base_candidate_id", "base_mapping_sha256", "base_batch_key", "captures", "base_table_objects"):
            require(value[key] == merged[key], "FIN_UNIT_SCOPE", "Unit increments must share the audited extraction")
        require(value["coverage"]["numeric_unit_unverified_before"] == merged["coverage"]["numeric_unit_unverified_before"],
                "FIN_UNIT_SCOPE", "Unit audit populations differ")
        for rule in value["rules"]:
            key = tuple(rule[k] for k in ("source", "statement", "source_field", "source_title", "currency", "scope"))
            require(key not in seen, "FIN_UNIT_HISTORY", "Unit increment overlaps an existing rule")
            seen.add(key)
            rules.append(rule)
    recovered = sum(v["coverage"]["numeric_unit_recoverable"] for v in values)
    total = merged["coverage"]["numeric_unit_unverified_before"]
    require(recovered <= total, "FIN_UNIT_SCOPE", "Resolved units exceed the audited population")
    by_statement = {}
    for value in values:
        for key, count in value["coverage"]["recovered_by_statement"].items():
            by_statement[key] = by_statement.get(key, 0) + count
    merged.update(candidate_id=t.identity("unit-layer-set", ids), rules=rules)
    merged["coverage"] = dict(merged["coverage"], numeric_unit_recoverable=recovered,
        numeric_unit_unverified_remaining=total-recovered, rules=len(rules), recovered_by_statement=by_statement)
    require(contract["candidate_id"] == merged["candidate_id"] and contract["coverage"] == merged["coverage"],
            "FIN_UNIT_PUBLICATION", "Combined unit proof identity or totals differ")
    return merged


def append_to_manifest(writer, parent, manifest, candidate_root, *, expected_sha256, review_script=None):
    """Add a disjoint, audited unit layer to a caller-owned common-release draft."""
    require(writer.lock is not None and writer.root == parent.root, "FIN_WRITER", "Use the locked common writer")
    require(CONTRACT in parent.manifest and manifest.get(CONTRACT) == parent.manifest[CONTRACT],
            "FIN_UNIT_HISTORY", "Preserve the immutable unit contract before appending")
    candidate = units.UnitCandidate(candidate_root, expected_sha256=expected_sha256)
    candidate.bind(parent.financial())
    require(candidate.value["candidate_id"] not in [x["candidate_id"] for x in layers(parent.manifest[CONTRACT])],
            "FIN_UNIT_PUBLICATION", "Unit increment is already present")
    refs = [candidate.value[k] for k in ("base_candidate", "audit", "review")]
    review = json.loads(Path(candidate.value["review"]["path"]).read_bytes())
    refs += review["evidence"]
    code = {"unit_implementation": Path(units.__file__), "publication_implementation": Path(__file__)}
    if review.get("derivation_script_sha256"):
        require(review_script is not None and sha_file(review_script) == review["derivation_script_sha256"],
                "FIN_UNIT_PUBLICATION", "Supply the checksum-matching definition producer")
        code["review_derivation"] = Path(review_script)
    require(sum(x["bytes"] for x in refs) + (Path(candidate_root)/"candidate.json").stat().st_size < 512*1024**2,
            "MARKET_CAPACITY", "Unit increment exceeds 512 MiB")
    dependencies = [dict(freeze(writer, units.verify_file(item), "financial_unit_evidence"),
                         original_path=item["path"]) for item in refs]
    frozen = freeze(writer, Path(candidate_root)/"candidate.json", "financial_unit_candidate")
    code_evidence = [dict(freeze(writer, path, "financial_unit_code"), role=role) for role,path in code.items()]
    operation = t.identity("unit-publication", parent.sha256, expected_sha256, None)
    layer = dict(schema=SCHEMA, operation_id=operation, candidate=frozen,
                 candidate_id=candidate.value["candidate_id"], dependencies=dependencies,
                 code_evidence=code_evidence, coverage=candidate.value["coverage"],
                 base_release_id=parent.release_id, base_manifest_sha256=parent.sha256,
                 raw_observations_unchanged=True, historical_visibility_added=False)
    value = deepcopy(manifest)
    present = {x["path"] for x in value["evidence"]}
    for item in [frozen, *dependencies, *code_evidence]:
        if item["path"] not in present:
            value["evidence"].append(item)
            present.add(item["path"])
    entries = [*layers(parent.manifest[CONTRACT]), layer]
    total = parent.manifest[CONTRACT]["coverage"]
    coverage = dict(total)
    coverage["rules"] += candidate.value["coverage"]["rules"]
    coverage["numeric_unit_recoverable"] += candidate.value["coverage"]["numeric_unit_recoverable"]
    coverage["numeric_unit_unverified_remaining"] -= candidate.value["coverage"]["numeric_unit_recoverable"]
    coverage["recovered_by_statement"] = dict(total["recovered_by_statement"])
    for key,count in candidate.value["coverage"]["recovered_by_statement"].items():
        coverage["recovered_by_statement"][key] = coverage["recovered_by_statement"].get(key,0)+count
    value[CONTRACT] = dict(schema=LAYERS_SCHEMA, layers=entries, operation_id=operation,
        candidate_id=t.identity("unit-layer-set", [x["candidate_id"] for x in entries]),
        coverage=coverage, raw_observations_unchanged=True, historical_visibility_added=False,
        base_release_id=parent.release_id, base_manifest_sha256=parent.sha256)
    verify_contract(writer.root, value)
    return value


def _compose_increment(parent, candidate_root, *, expected_sha256, review_script, calendar_plan,
                       free_reserve_bytes):
    require(calendar_plan is None, "FIN_UNIT_PUBLICATION", "Compose later calendar changes separately")
    operation = t.identity("unit-publication", parent.sha256, expected_sha256, None)
    with ReleaseWriter(parent.root, free_reserve_bytes=free_reserve_bytes) as writer:
        current = MarketRelease(parent.root)
        if current.manifest.get(CONTRACT, {}).get("operation_id") == operation:
            verify_contract(current.root, current.manifest)
            return dict(unchanged=True, already_applied=True, release=current.receipt())
        require(current.sha256 == parent.sha256, "MARKET_PARENT_CHANGED", "Unit parent advanced")
        value = append_to_manifest(writer, parent, parent.manifest, candidate_root,
                                   expected_sha256=expected_sha256, review_script=review_script)
        value["change_set"] = dict(kind="reviewed_financial_unit_increment", operation_id=operation,
            market_fact_rows_rewritten=0, financial_observation_rows_rewritten=0,
            numeric_units_resolved=value[CONTRACT]["coverage"]["numeric_unit_recoverable"]-
                                   parent.manifest[CONTRACT]["coverage"]["numeric_unit_recoverable"])
        published = writer.publish(value, expected_parent=parent.release_id, verify_unchanged=False)
    return dict(unchanged=False, release=published, operation_id=operation,
                unit_coverage=value[CONTRACT]["coverage"], calendar_sessions_added=0)
