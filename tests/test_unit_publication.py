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


def test_disjoint_increment_retains_old_dates_and_previous_releases(tmp_path):
    from ashare_data.financials import unit_resolution as units
    parent, first_root, first = published_base(tmp_path)
    publish(parent, first_root)
    r1 = MarketRelease(parent.root)
    first_contract = deepcopy(r1.manifest[publication.CONTRACT])
    review_path = tmp_path / 'review-next.json'
    review = json.loads(Path(first['review']['path']).read_text())
    review['available_from'] = '2026-10-22T10:00:00+00:00'
    definition = dict(review['definitions'][0], source_field='MINY', source_title='少数股东综合收益',
                      anchor_label='少数股东综合收益', unit='CNY')
    review['definitions'] = [definition]
    review_path.write_text(json.dumps(review))
    next_root = tmp_path / 'next-units'
    units.prepare(Path(first['base_candidate']['path']).parent,
                  first['audit']['path'], review_path, next_root)
    result = publish(r1, next_root)
    r2 = MarketRelease(parent.root)
    assert r2.manifest[publication.CONTRACT]['layers'][0] == first_contract
    assert result['unit_coverage']['numeric_unit_recoverable'] == 4
    assert publish(r1, next_root)['already_applied']
    cutoff = '2026-10-21T00:00:00+00:00'
    assert point(r2.financial(), 'BASICEPS', '基本每股收益', as_of=cutoff)['value'] == '0.104900'
    assert point(r2.financial(), 'MINY', '少数股东综合收益', as_of=cutoff)['value'] is None
    assert point(r2.financial(), 'MINY', '少数股东综合收益',
                 as_of='2026-10-23T00:00:00+00:00')['value'] == '-101000000.000000'
    assert point(MarketRelease(parent.root, r1.release_id).financial(), 'MINY',
                 '少数股东综合收益', as_of='2026-10-23T00:00:00+00:00')['value'] is None
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        dropped = deepcopy(r2.manifest)
        dropped[publication.CONTRACT] = first_contract
        with pytest.raises(DataError, match='immutable unit contract'):
            writer.publish(dropped, expected_parent=r2.release_id, verify_unchanged=False)


def test_increment_cannot_reinterpret_a_previously_published_rule(tmp_path):
    from ashare_data.financials import unit_resolution as units
    parent, root, candidate = published_base(tmp_path)
    publish(parent, root)
    old = MarketRelease(parent.root)
    review = json.loads(Path(candidate['review']['path']).read_text())
    review['available_from'] = '2026-10-22T10:00:00+00:00'
    review['definitions'] = [review['definitions'][0]]
    path = tmp_path/'overlap-review.json'
    path.write_text(json.dumps(review))
    overlap = tmp_path/'overlap-units'
    units.prepare(Path(candidate['base_candidate']['path']).parent,
                  candidate['audit']['path'], path, overlap)
    with pytest.raises(DataError, match='overlaps an existing rule'):
        publish(old, overlap)
    assert MarketRelease(parent.root).sha256 == old.sha256


def test_closed_append_validation_matches_complete_result_and_rejects_changed_keys(tmp_path):
    from ashare_data.storage.financial_increment import validate_append
    from ashare_data.storage.financial_release import table_views, validate_tables
    from ashare_data.storage.market_release import connection
    parent, _, _ = published_base(tmp_path)
    files = [f for f in parent.manifest['evidence'] if f.get('kind') == 'financial_table']
    with parent.metadata() as db, connection() as c:
        result = validate_append(c, db, parent, files, parent.verify)
        table_views(c, files, parent.verify)
        full = validate_tables(c, db)
    assert result['table_rows'] == full['table_rows']
    assert result['input_rows'] == full['input_rows']
    assert not any(result['appended_rows'].values())
    altered = deepcopy(files)
    item = next(f for f in altered if f['table'] == 'financial.observations')
    target = tmp_path/'changed-values.parquet'
    with connection() as c:
        c.read_parquet(str(parent.verify(item))).create_view('old')
        c.execute("COPY (SELECT * REPLACE ('7' AS canonical_value_decimal_text) FROM old) TO ? (FORMAT PARQUET)", [str(target)])
    item['path'] = str(target)
    with parent.metadata() as db, connection() as c:
        with pytest.raises(DataError, match='Existing identity has changed values'):
            validate_append(c, db, parent, altered, lambda f: target if f is item else parent.verify(f))
