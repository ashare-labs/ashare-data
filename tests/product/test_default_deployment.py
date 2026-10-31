"""User defaults select the existing library without creating another."""
import argparse
import json

import pytest

from ashare_data import DataError
from ashare_data.product import deployment_path
from ashare_data.product.configuration import load_profile
from ashare_data.storage.local_daily import LocalDailyStore, add_parser, command


def test_user_config_maintenance_read_and_explicit_root(tmp_path, monkeypatch):
    library = tmp_path / "real-library"
    LocalDailyStore(library).close()
    config = tmp_path / "config" / "ashare" / "deployment.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({
        "schema": "ashare-deployment.v1", "data_root": "../..",
        "assets": {"main": {"kind": "local_daily", "path": "real-library"}},
        "profiles": {"product": {"kind": "product", "daily_asset": "main"}},
    }))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent.parent))
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    before = (library / "daily.sqlite3").read_bytes()
    assert command(parser.parse_args(["local-daily", "status"]))
    assert (library / "daily.sqlite3").read_bytes() == before
    monkeypatch.setenv("ASHARE_DEPLOYMENT", str(tmp_path / "missing.json"))
    with pytest.raises(DataError):
        command(parser.parse_args(["local-daily", "status"]))
    assert command(parser.parse_args(["local-daily", "status", "--root", str(library)]))
    assert not (tmp_path / "missing.json").exists()


def test_discovery_precedence_and_broken_link(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert deployment_path() is None
    link = tmp_path / "ashare" / "deployment.json"
    link.parent.mkdir()
    link.symlink_to(tmp_path / "missing.json")
    assert deployment_path() == link
    with pytest.raises(DataError):
        load_profile()
    monkeypatch.setenv("ASHARE_DATA_ROOT", str(tmp_path / "root"))
    assert deployment_path() == tmp_path / "root" / "deployment.json"
    monkeypatch.setenv("ASHARE_DEPLOYMENT", str(tmp_path / "env.json"))
    assert deployment_path() == tmp_path / "env.json"
    assert deployment_path(tmp_path / "explicit.json") == tmp_path / "explicit.json"
