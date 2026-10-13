"""Evidence identity and immutable publication guards for the offline builder."""

import json
from pathlib import Path
import runpy

import pytest


@pytest.fixture(scope="module")
def builder():
    return runpy.run_path(str(Path(__file__).parents[1] / "scripts/build_daily_research.py"))


def test_html_identity_does_not_accept_a_symbol_mentioned_elsewhere(tmp_path, builder):
    page = tmp_path / "profile.html"
    page.write_text('var page_symbol = "sz000001"; <p>sz000651</p>')
    assert builder["parse_tables"](page, "sz000001") == {}
    with pytest.raises(ValueError, match="security identity"):
        builder["parse_tables"](page, "sz000651")
    page.write_text('var page_symbol = "sz000001"; var fullcode="sz000651";')
    with pytest.raises(ValueError, match="security identity"):
        builder["parse_tables"](page, "sz000001")


def test_fixed_outputs_cannot_be_replaced_by_a_new_revision(tmp_path, builder):
    target = tmp_path / "binding.json"
    builder["write_fixed"](target, b"fixed version")
    builder["write_fixed"](target, b"fixed version")
    with pytest.raises(ValueError, match="Refusing to replace"):
        builder["write_fixed"](target, b"new version")
    assert target.read_bytes() == b"fixed version"


@pytest.mark.parametrize("name", ["../binding.json", "facts.json", "raw/source.json"])
def test_output_must_not_overwrite_inputs_or_escape_root(tmp_path, builder, name):
    with pytest.raises(ValueError, match="Output must"):
        builder["build"](tmp_path, name)
    assert not list(tmp_path.iterdir())


def test_foreign_manifest_baseline_is_rejected_before_building(tmp_path, builder):
    (tmp_path / "strategy-manifest.json").write_text(json.dumps({"baselines": {"ad": "wrong"}}))
    (tmp_path / "facts.json").write_text("{}")
    with pytest.raises(ValueError, match="baselines"):
        builder["build"](tmp_path)
    assert not (tmp_path / "binding.json").exists()
