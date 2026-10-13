"""Research calculations bound to the independently reviewed round11 archive.

The unmodified arithmetic engine validates self-described contracts. This entry
point additionally authenticates the actual archived bytes against the reviewed
manifest. Its finite observations never certify supplier semantics or new data.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .f1_pv_semantic_contract import DEFINITION, FIELDS, evaluate, require

SOURCE_COMMIT = "523b84a8cffef3ba8bc20b642277cb1b21850978"
ARCHIVE_MANIFEST_SHA256 = "5a76a3db1d689c8c722fa699ca02b2aa1c1240b51780d112c3813b5efd99825b"
CONDITIONALLY_SUPPORTED_FIELDS = ("Variance20", "ATR6", "ATR14")
_TARGET = {"security": "300124.XSHE", "date": "2024-04-19"}
_SLOTS = {"Variance20": "PV02", "ATR6": "PV03", "ATR14": "PV03", "EMA5": "PV04"}
_BUNDLES = {
    "reference_semantics": "results/prediction-lock.json",
    "independent_source": "frozen-round9/results/holdout-independent-bundle.json",
}


def _local_bytes(root, name):
    relative = Path(name)
    require(not relative.is_absolute() and ".." not in relative.parts, "UNSAFE_ARCHIVE_PATH")
    path = root
    for part in relative.parts:
        path /= part
        require(not path.is_symlink(), "SYMLINKED_ARCHIVE_PATH")
    require(path.is_file(), "ARCHIVE_FILE_REQUIRED: " + name)
    return path.read_bytes()


def _archive(root):
    root = Path(root).resolve()
    manifest_bytes = _local_bytes(root, "package-manifest.json")
    require(
        hashlib.sha256(manifest_bytes).hexdigest() == ARCHIVE_MANIFEST_SHA256,
        "REVIEWED_ARCHIVE_MANIFEST_REQUIRED",
    )
    manifest = json.loads(manifest_bytes)
    require(
        manifest["schema"] == "ad.pv.huichuan-package.v1" and manifest["commit"] == SOURCE_COMMIT,
        "REVIEWED_ARCHIVE_IDENTITY_REQUIRED",
    )
    contents = {}
    for name, expected in manifest["files"].items():
        data = _local_bytes(root, name)
        require(
            len(data) == expected["bytes"]
            and hashlib.sha256(data).hexdigest() == expected["sha256"],
            "ARCHIVE_FILE_CHANGED: " + name,
        )
        contents[name] = data
    return manifest, contents


def _candidate(archive, *, definition, mode, fields, allowed):
    require(definition == DEFINITION, "VERSIONED_RESEARCH_DEFINITION_REQUIRED")
    require(mode in _BUNDLES, "EXPLICIT_SOURCE_MODE_REQUIRED")
    require(
        isinstance(fields, (list, tuple))
        and fields
        and all(isinstance(field, str) and field in allowed for field in fields)
        and len(set(fields)) == len(fields),
        "EXPLICIT_SUPPORTED_FIELDS_REQUIRED; EMA5 needs pv_round11_ema5_local_candidate",
    )
    manifest, contents = _archive(archive)
    saved = json.loads(contents[_BUNDLES[mode]])
    bundle = saved["bundle"] if mode == "reference_semantics" else saved
    require(
        bundle["security"] == _TARGET["security"] and bundle["sessions"][-1] == _TARGET["date"],
        "REVIEWED_TARGET_REQUIRED",
    )
    # These hashes must identify verified files, not merely agree with each other
    # inside a caller-supplied contract. The pinned manifest also binds the exact
    # derived bundle to the reviewed source reconstruction.
    archived_hashes = {entry["sha256"] for entry in manifest["files"].values()}
    require(
        set(bundle["contract"]["evidence_sha256"]) <= archived_hashes,
        "SOURCE_EVIDENCE_BYTES_REQUIRED",
    )
    calculation = evaluate(
        bundle, definition=definition, mode=mode, end_date=_TARGET["date"], fields=fields
    )
    statuses = [
        {
            "field": field,
            "original_slot": _SLOTS[field],
            "conditional_status": (
                "supported_at_frozen_target"
                if field in CONDITIONALLY_SUPPORTED_FIELDS
                else "fixed_equivalence_path_refuted_at_frozen_target"
            ),
            "conditional_bit_match": field in CONDITIONALLY_SUPPORTED_FIELDS,
            "independent_bit_match": False,
            "mode_bit_match": mode == "reference_semantics"
            and field in CONDITIONALLY_SUPPORTED_FIELDS,
        }
        for field in fields
    ]
    return {
        "calculation": calculation,
        "archive_binding": {
            "source_commit": SOURCE_COMMIT,
            "manifest_sha256": ARCHIVE_MANIFEST_SHA256,
            "verified_files": len(contents),
            "bundle_path": _BUNDLES[mode],
            "actual_source_files_verified": True,
        },
        "empirical_review": {
            "target": deepcopy(_TARGET),
            "fields": statuses,
            "conditional_original_slots_with_support": len(
                {_SLOTS[field] for field in fields if field in CONDITIONALLY_SUPPORTED_FIELDS}
            ),
            "independent_JQ_accepted_original_slots_added": 0,
            "new_confirmatory_targets": 1,
            "earlier_Vanke_target_role": "development_selected_rule_evidence",
            "new_inputs_certified": False,
            "all_four_candidate_confirmed": False,
            "vendor_generation_rule_certified": False,
            "automatic_source_fallback": False,
            "source_compatibility": {"status": "blocked", "issue": "ADJ-INPUT-001"},
            "jq_equivalent": False,
            "strategy_visible": False,
            "production_eligible": False,
        },
    }


def pv_round11_candidate(archive, *, definition, mode, fields):
    """Evaluate requested Variance20/ATR6/ATR14 fields in the sealed target archive.

    Both modes are research-only. Conditional observations apply to supplied JQ
    public prices; native Sina prices retain their independent 0/4 counterexample.
    No network, fallback, new tolerance, or reference-derived scaling is used.
    """
    return _candidate(
        archive,
        definition=definition,
        mode=mode,
        fields=fields,
        allowed=CONDITIONALLY_SUPPORTED_FIELDS,
    )


def pv_round11_ema5_local_candidate(archive, *, definition, mode, fields):
    """Compute only explicitly requested EMA5; its frozen equivalence path failed."""
    return _candidate(
        archive, definition=definition, mode=mode, fields=fields, allowed=(FIELDS[-1],)
    )
