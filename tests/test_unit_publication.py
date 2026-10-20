"""Frozen unit publications survive source-proof relocation and preserve release history."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from ashare_data.core.model import DataError
from ashare_data.product.financial_release import main
from ashare_data.storage.financial_release import compose as compose_tables
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, sha_file
from ashare_data.storage import unit_publication as publication
from test_financial_units import fixture, point


def published_base(tmp_path):
    original, _, unit_root, candidate, _, _ = fixture(tmp_path)
    parent = original.release
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        value = compose_tables(writer, parent, original.candidate_root,
                               expected_parent_sha256=parent.sha256)
        writer.publish(value, expected_parent=parent.release_id)
    return MarketRelease(parent.root), unit_root, candidate


def publish(parent, unit_root, **kwargs):
    return publication.compose(parent, unit_root, expected_sha256=sha_file(unit_root / "candidate.json"),
                               free_reserve_bytes=0, **kwargs)


def test_published_units_are_default_immutable_and_reproducible_offline(tmp_path, capsys):
    parent, unit_root, candidate = published_base(tmp_path)
    old_value = point(parent.financial(), "BASICEPS", "基本每股收益")
    assert old_value["value"] is None
    result = publish(parent, unit_root)
    current = MarketRelease(parent.root)
    assert current.release_id == parent.release_id + 1
    assert current.manifest["files"] == parent.manifest["files"]
    assert current.manifest["metadata"] == parent.manifest["metadata"]
    assert current.manifest["financial_reference_tables"] == parent.manifest["financial_reference_tables"]
    assert current.manifest["change_set"]["financial_observation_rows_rewritten"] == 0
    assert result["unit_coverage"]["numeric_unit_recoverable"] == 3
    assert publish(parent, unit_root)["already_applied"]
    # Retain originals under new names: published reading must not need original proof paths.
    for item in current.manifest[publication.CONTRACT]["dependencies"]:
        p = Path(item["original_path"])
        p.rename(p.with_name(p.name + ".retained-offline"))
    reader = current.financial()
    value = point(reader, "BASICEPS", "基本每股收益")
    assert (value["value"], value["unit"]) == ("0.104900", "CNY/share")
    assert value["unit_candidate_published"] is True
    assert value["unit_candidate_id"] == candidate["candidate_id"]
    assert point(reader, "MINY", "少数股东综合收益")["value"] is None
    assert point(reader, "empty", "空字符串")["value"] is None
    assert point(reader, mode="historical_pit")["status"] == "visibility_unproven"
    assert "unit_definition_not_captured_at_cutoff" in point(
        reader, as_of="2026-10-20T09:59:59+00:00")["reasons"]
    assert point(MarketRelease(parent.root, parent.release_id).financial(),
                 "BASICEPS", "基本每股收益") == old_value
    main(["--market-root", str(parent.root), "--release-id", str(current.release_id),
          "--manifest-sha256", current.sha256, "source-query", "--security", "600000.XSHG",
          "--field", "BASICEPS", "--title", "基本每股收益", "--statement", "lrb",
          "--period", "2024-03-31", "--source", "sina", "--mode", "source_research",
          "--as-of", "2026-10-21T00:00:00+00:00"])
    assert json.loads(capsys.readouterr().out) == value


def test_failed_publication_is_recoverable_and_does_not_change_current(tmp_path, monkeypatch):
    parent, unit_root, _ = published_base(tmp_path)
    original = ReleaseWriter.publish
    def interrupted(*args, **kwargs):
        raise RuntimeError("simulated failure before commit")
    monkeypatch.setattr(ReleaseWriter, "publish", interrupted)
    with pytest.raises(RuntimeError, match="before commit"):
        publish(parent, unit_root)
    assert MarketRelease(parent.root).sha256 == parent.sha256
    monkeypatch.setattr(ReleaseWriter, "publish", original)
    assert publish(parent, unit_root)["unchanged"] is False
    assert point(MarketRelease(parent.root).financial())["value"] == "1.25"


def test_proof_tamper_and_dropped_contract_are_rejected(tmp_path):
    parent, unit_root, _ = published_base(tmp_path)
    publish(parent, unit_root)
    current = MarketRelease(parent.root)
    without = deepcopy(current.manifest)
    without.pop(publication.CONTRACT)
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        with pytest.raises(DataError, match="immutable unit contract"):
            writer.publish(without, expected_parent=current.release_id, verify_unchanged=False)
    assert MarketRelease(parent.root).sha256 == current.sha256
    proof = parent.root / current.manifest[publication.CONTRACT]["dependencies"][0]["path"]
    proof.write_bytes(proof.read_bytes() + b" ")
    with pytest.raises(DataError, match="evidence changed"):
        MarketRelease(parent.root).financial()
