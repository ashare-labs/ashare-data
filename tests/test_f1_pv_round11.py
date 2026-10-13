"""Synthetic archives exercise binding and status gates without private market data."""

from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json

import pytest

from ashare_data import f1
from ashare_data import f1_pv_round11 as scope
from ashare_data.f1_pv_semantic_contract import ContractError, digest, payload
from test_f1_pv_semantic_contract import fixture_bundle


def _bytes(value):
    return json.dumps(value, sort_keys=True).encode()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def archive(tmp_path, monkeypatch):
    bundle = fixture_bundle()
    days = [(date(2024, 4, 19) - timedelta(days=59 - k)).isoformat() for k in range(60)]
    bundle.update(security="300124.XSHE", sessions=days)
    evidence = {f"sources/{key}.json": _bytes({"synthetic": key}) for key in "abcd"}
    hashes = [_hash(value) for value in evidence.values()]
    contract = bundle["contract"]
    contract.update(
        snapshot_id="Sina:" + hashes[0],
        snapshot_version="sha256:" + hashes[0],
        snapshot_sha256=hashes[0],
        anchor_version=hashes[1],
        calendar_sha256=hashes[2],
        normal_state_sha256=hashes[3],
        evidence_sha256=hashes,
    )
    for day, row in zip(days, bundle["rows"], strict=True):
        row.update(date=day, source_ref=contract["snapshot_id"])
    bundle["factors"][0].update(date=days[0], value="1")
    contract["payload_sha256"] = digest(payload(bundle))
    reference = deepcopy(bundle)
    reference["factors"] = []
    reference["contract"].update(
        provider="JQ_public_price",
        source_role="reference",
        construction="materialized_post",
        snapshot_id="JQ_public_price:" + hashes[0],
        anchor_version=hashes[0],
        anchor_kind="opaque_post_price_snapshot_sha256",
        input_precision="binary64_api_post_round_false",
        stored_price_unit="CNY_per_post_adjusted_share",
        stored_adjustment="post",
    )
    for row in reference["rows"]:
        row["source_ref"] = reference["contract"]["snapshot_id"]
        row["prices"] = {key: float(value).hex() for key, value in row["prices"].items()}
    reference["contract"]["payload_sha256"] = digest(payload(reference))
    evidence[scope._BUNDLES["independent_source"]] = _bytes(bundle)
    evidence[scope._BUNDLES["reference_semantics"]] = _bytes({"bundle": reference})
    for name, data in evidence.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "schema": "ad.pv.huichuan-package.v1",
        "commit": scope.SOURCE_COMMIT,
        "files": {
            name: {"bytes": len(data), "sha256": _hash(data)} for name, data in evidence.items()
        },
    }
    encoded = _bytes(manifest)
    (tmp_path / "package-manifest.json").write_bytes(encoded)
    # Only synthetic test archives replace the pinned external trust anchor.
    monkeypatch.setattr(scope, "ARCHIVE_MANIFEST_SHA256", _hash(encoded))
    return tmp_path


def run(archive, **kwargs):
    params = dict(
        definition=f1.PV_RESEARCH_DEFINITION,
        mode="reference_semantics",
        fields=["Variance20", "ATR6", "ATR14"],
    )
    params.update(kwargs)
    return f1.pv_round11_candidate(archive, **params)


@pytest.mark.parametrize(
    "mode,matched", [("reference_semantics", True), ("independent_source", False)]
)
def test_modes_preserve_empirical_scope_and_unique_original_slots(archive, mode, matched):
    result = run(archive, mode=mode)
    assert result["archive_binding"]["actual_source_files_verified"] is True
    review = result["empirical_review"]
    assert [row["mode_bit_match"] for row in review["fields"]] == [matched] * 3
    assert review["conditional_original_slots_with_support"] == 2
    assert review["independent_JQ_accepted_original_slots_added"] == 0
    assert review["earlier_Vanke_target_role"] == "development_selected_rule_evidence"
    assert review["source_compatibility"] == {"status": "blocked", "issue": "ADJ-INPUT-001"}
    for key in [
        "new_inputs_certified",
        "all_four_candidate_confirmed",
        "vendor_generation_rule_certified",
        "automatic_source_fallback",
        "jq_equivalent",
        "strategy_visible",
        "production_eligible",
    ]:
        assert review[key] is False
    subset = run(archive, mode=mode, fields=["ATR6", "ATR14"])
    assert subset["empirical_review"]["conditional_original_slots_with_support"] == 1
    assert subset["calculation"]["rows"] == result["calculation"]["rows"][1:]


def test_failed_ema_requires_separate_explicit_candidate_entry(archive):
    with pytest.raises(ContractError, match="pv_round11_ema5_local_candidate"):
        run(archive, fields=["EMA5"])
    result = f1.pv_round11_ema5_local_candidate(
        archive, definition=f1.PV_RESEARCH_DEFINITION, mode="reference_semantics", fields=["EMA5"]
    )
    review = result["empirical_review"]
    assert review["conditional_original_slots_with_support"] == 0
    assert (
        review["fields"][0]["conditional_status"]
        == "fixed_equivalence_path_refuted_at_frozen_target"
    )
    assert review["fields"][0]["conditional_bit_match"] is False
    assert result["calculation"]["rows"][0]["seed"]["unknown_vendor_seed_covered"] is False
    with pytest.raises(ContractError, match="EXPLICIT_SUPPORTED_FIELDS_REQUIRED"):
        f1.pv_round11_ema5_local_candidate(
            archive,
            definition=f1.PV_RESEARCH_DEFINITION,
            mode="reference_semantics",
            fields=["ATR6"],
        )


@pytest.mark.parametrize("omitted", ["definition", "mode", "fields"])
def test_no_default_definition_source_or_fields(archive, omitted):
    params = dict(definition=f1.PV_RESEARCH_DEFINITION, mode="reference_semantics", fields=["ATR6"])
    del params[omitted]
    with pytest.raises(TypeError, match=omitted):
        f1.pv_round11_candidate(archive, **params)


@pytest.mark.parametrize("changed", ["sources/a.json", "results/prediction-lock.json"])
def test_changed_source_or_bundle_refused_even_with_unchanged_contract(archive, changed):
    (archive / changed).write_text("{}")
    with pytest.raises(ContractError, match="ARCHIVE_FILE_CHANGED"):
        run(archive)


def test_self_resealed_manifest_cannot_replace_reviewed_evidence(archive):
    path = archive / "package-manifest.json"
    manifest = json.loads(path.read_bytes())
    (archive / "sources/a.json").write_text("changed")
    manifest["files"]["sources/a.json"] = {"bytes": 7, "sha256": _hash(b"changed")}
    path.write_bytes(_bytes(manifest))
    with pytest.raises(ContractError, match="REVIEWED_ARCHIVE_MANIFEST_REQUIRED"):
        run(archive)


def test_symlinked_member_directory_is_not_archived_evidence(archive):
    original = archive / "sources"
    moved = archive / "moved-sources"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ContractError, match="SYMLINKED_ARCHIVE_PATH"):
        run(archive)


def test_missing_source_bytes_fail_closed(archive):
    (archive / "sources/b.json").unlink()
    with pytest.raises(ContractError, match="ARCHIVE_FILE_REQUIRED"):
        run(archive)


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"definition": "vendor-equivalent"}, "VERSIONED_RESEARCH_DEFINITION_REQUIRED"),
        ({"mode": "automatic"}, "EXPLICIT_SOURCE_MODE_REQUIRED"),
        ({"fields": []}, "EXPLICIT_SUPPORTED_FIELDS_REQUIRED"),
        ({"fields": ["ATR6", "ATR6"]}, "EXPLICIT_SUPPORTED_FIELDS_REQUIRED"),
        ({"fields": ["ROC120"]}, "EXPLICIT_SUPPORTED_FIELDS_REQUIRED"),
    ],
)
def test_unsupported_requests_refused(archive, kwargs, reason):
    with pytest.raises(ContractError, match=reason):
        run(archive, **kwargs)
