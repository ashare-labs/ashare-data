"""Preverified local replicas preserve the published snapshot and fail closed.

All objects are small, locally published fixtures. No external data directory,
deployment or service is used, and production readers never create a replica.
"""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_data.core.model import DataError
from ashare_data.storage import market_release as module
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter
from ashare_data.storage.read_replicas import ReadReplica, read_replica
from ashare_data.storage.verified_objects import fingerprint, verify_cached
from test_market_release import chunk, manifest, metadata


@pytest.fixture
def published(tmp_path, monkeypatch):
    monkeypatch.delenv("ASHARE_MARKET_READ_REPLICAS", raising=False)
    monkeypatch.delenv("ASHARE_MARKET_VERIFY_CACHE", raising=False)
    monkeypatch.setattr(module, "_manifest_cache", None)
    root = tmp_path / "market"
    with ReleaseWriter(root, free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        bar = chunk(writer, 10)
        pointer = writer.publish(manifest(meta, [bar]), expected_parent=None)
    cache = tmp_path / "verified"
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(cache))
    return SimpleNamespace(root=root, meta=meta, bar=bar, pointer=pointer,
                           directory=tmp_path / "replicas", cache=cache)


def copy_replica(directory, source, expected, suffix, mode=0o400):
    directory.mkdir(exist_ok=True, mode=0o700)
    target = directory / (expected + suffix)
    target.write_bytes(source.read_bytes())
    target.chmod(mode)
    return target


def install(published, monkeypatch, mode=0o400):
    published.manifest_replica = copy_replica(
        published.directory, published.root / published.pointer["manifest"],
        published.pointer["sha256"], ".json", mode)
    published.metadata_replica = copy_replica(
        published.directory, published.root / published.meta["path"],
        published.meta["sha256"], ".sqlite3", mode)
    monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(published.directory))
    return published


def prices(reader):
    return reader.read("bars_1d", "000001.XSHE", "2024-01-02", "2024-01-02")


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_same_snapshot_without_touching_original_objects(published, monkeypatch, mode):
    original = MarketRelease(published.root)
    expected = prices(original)
    install(published, monkeypatch, mode)
    source_paths = {published.root / published.meta["path"],
                    published.root / published.pointer["manifest"]}
    for path in source_paths:
        path.unlink()
    # A local replica must not even resolve/stat/open the slow original object.
    # CURRENT and price parquet remain on the original published root.
    for name in ("resolve", "stat", "open"):
        previous = getattr(Path, name)

        def guarded(path, *args, _previous=previous, **kwargs):
            assert path not in source_paths, f"original object touched: {path}"
            return _previous(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded)
    replica = MarketRelease(published.root)
    assert replica.metadata_path == published.metadata_replica
    assert replica.manifest == original.manifest
    assert replica.receipt() == original.receipt()
    assert replica.identifiers == original.identifiers
    assert replica.instruments == original.instruments
    assert prices(replica) == expected
    assert replica.verify(replica.files[0]) == published.root / published.bar["path"]
    assert {p.name for p in published.directory.iterdir()} == {
        published.manifest_replica.name, published.metadata_replica.name}


def test_absent_replica_uses_original_without_creating_any_replica(published, monkeypatch):
    original = MarketRelease(published.root)
    assert not published.directory.exists()
    published.directory.mkdir(mode=0o700)
    monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(published.directory))
    fallback = MarketRelease(published.root)
    assert fallback.metadata_path == original.metadata_path
    assert fallback.receipt() == original.receipt()
    assert prices(fallback) == prices(original)
    assert list(published.directory.iterdir()) == []


@pytest.mark.parametrize("kind", ["metadata", "manifest"])
def test_each_replica_type_is_optional_independently(published, monkeypatch, kind):
    if kind == "metadata":
        copied = copy_replica(published.directory, published.root / published.meta["path"],
                              published.meta["sha256"], ".sqlite3")
    else:
        copy_replica(published.directory, published.root / published.pointer["manifest"],
                     published.pointer["sha256"], ".json")
        copied = published.root / published.meta["path"]
    monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(published.directory))
    reader = MarketRelease(published.root)
    assert reader.metadata_path == copied
    assert prices(reader)[0]["close_price"] == 10


def test_complete_real_verification_receipt_can_be_reused(published, monkeypatch):
    install(published, monkeypatch)
    path = published.metadata_replica
    # This is the same full-hash API used to prepare a large replica offline.
    expected_fp = verify_cached(path, published.meta["sha256"], published.meta["bytes"],
                                module.sha_file)
    receipt = json.loads(next(published.cache.glob("*.json")).read_bytes())
    assert receipt["path"] == str(path)
    assert receipt["fingerprint"] == expected_fp == fingerprint(path)
    assert receipt["path"] != str(published.root / published.meta["path"])

    def unexpected_hash(_):
        pytest.fail("unchanged fully verified metadata was hashed again")

    monkeypatch.setattr(module, "sha_file", unexpected_hash)
    first, second = MarketRelease(published.root), MarketRelease(published.root)
    assert first.metadata_path == second.metadata_path == path
    assert first.manifest == second.manifest
    first.manifest["rules"]["fixed"] = "private caller mutation"
    assert second.manifest["rules"]["fixed"] == "version-one"


@pytest.mark.parametrize("kind,change", [
    ("metadata", "same_size_wrong_sha"), ("metadata", "wrong_size"),
    ("manifest", "same_size_wrong_sha"), ("manifest", "wrong_size")])
def test_bad_present_replica_is_rejected_without_source_fallback(published, monkeypatch, kind, change):
    install(published, monkeypatch)
    path = getattr(published, kind + "_replica")
    raw = path.read_bytes()
    path.chmod(0o600)
    path.write_bytes(b"x" + raw[1:] if change == "same_size_wrong_sha" else raw + b"bad")
    with pytest.raises(DataError, match="(Immutable|manifest chain changed)"):
        MarketRelease(published.root)
    assert not list(published.cache.glob("*.json"))


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o777, 0o1700])
def test_insecure_replica_directory_rejected_even_with_no_matching_object(tmp_path, monkeypatch, mode):
    directory = tmp_path / "replicas"
    directory.mkdir(mode=0o700)
    directory.chmod(mode)
    monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(directory))
    with pytest.raises(DataError, match="private, owned and not a symlink"):
        read_replica("0" * 64, ".json")


@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "relative"])
def test_invalid_configured_directory_rejected(tmp_path, monkeypatch, kind):
    directory = tmp_path / "replicas"
    if kind == "file":
        directory.write_bytes(b"not a directory")
    elif kind == "symlink":
        actual = tmp_path / "actual"
        actual.mkdir(mode=0o700)
        directory.symlink_to(actual, target_is_directory=True)
    elif kind == "relative":
        directory = Path("relative-replicas")
    monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(directory))
    with pytest.raises(DataError, match="Replica directory"):
        read_replica("0" * 64, ".json")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o700, 0o000])
def test_insecure_replica_file_rejected(published, monkeypatch, mode):
    install(published, monkeypatch)
    published.metadata_replica.chmod(mode)
    with pytest.raises(DataError, match="private owned regular file"):
        MarketRelease(published.root)


@pytest.mark.parametrize("bits", [0o4000, 0o2000, 0o1000])
def test_special_replica_permission_bits_are_rejected(published, monkeypatch, bits):
    install(published, monkeypatch)
    previous = Path.lstat

    def special(path):
        info = previous(path)
        if path == published.metadata_replica:
            # Some filesystems clear setuid on non-executable regular files.
            # Supply that filesystem state directly to exercise the guard.
            fields = list(info)
            fields[0] |= bits
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", special)
    with pytest.raises(DataError, match="private owned regular file"):
        MarketRelease(published.root)


@pytest.mark.parametrize("kind", ["symlink", "broken_symlink", "directory"])
def test_nonregular_replica_rejected(published, monkeypatch, kind):
    install(published, monkeypatch)
    path = published.metadata_replica
    path.unlink()
    if kind == "directory":
        path.mkdir(mode=0o700)
    else:
        target = (published.root / published.meta["path"] if kind == "symlink"
                  else published.root / "missing")
        path.symlink_to(target)
    with pytest.raises(DataError, match="private owned regular file"):
        MarketRelease(published.root)


@pytest.mark.parametrize("kind", ["directory", "metadata_replica"])
def test_foreign_owned_replica_is_rejected(published, monkeypatch, kind):
    install(published, monkeypatch)
    target = getattr(published, kind)
    previous = Path.lstat

    def foreign(path):
        info = previous(path)
        if path == target:
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", foreign)
    with pytest.raises(DataError, match="owned"):
        MarketRelease(published.root)


def test_parent_alias_allowed_but_retargeting_a_selected_replica_is_rejected(published, monkeypatch):
    install(published, monkeypatch)
    alias = published.directory.parent / "alias"
    alias.symlink_to(published.directory.parent, target_is_directory=True)
    monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(alias / published.directory.name))
    reader = MarketRelease(published.root)
    assert reader.metadata_path == published.metadata_replica
    other = published.directory.parent / "other"
    other.mkdir()
    (other / published.directory.name).mkdir(mode=0o700)
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    with pytest.raises(DataError, match="Replica directory changed"):
        with reader.metadata():
            pytest.fail("retargeted directory was accepted")


def test_current_refresh_and_parent_chain_pin_do_not_mix_replicas(published, monkeypatch):
    with ReleaseWriter(published.root, free_reserve_bytes=0) as writer:
        second = writer.publish(manifest(published.meta, [chunk(writer, 20)]), expected_parent=1)
    install(published, monkeypatch)
    copy_replica(published.directory, published.root / second["manifest"], second["sha256"], ".json")
    (published.root / published.pointer["manifest"]).unlink()
    (published.root / second["manifest"]).unlink()
    (published.root / published.meta["path"]).unlink()
    current = published.root / "CURRENT.json"
    current.write_text(json.dumps(published.pointer))
    first = MarketRelease(published.root)
    current.write_text(json.dumps(second))
    latest = MarketRelease(published.root)
    pinned = MarketRelease(published.root, 1, expected_manifest_sha256=first.sha256)
    assert (first.release_id, latest.release_id, pinned.release_id) == (1, 2, 1)
    assert prices(first)[0]["close_price"] == prices(pinned)[0]["close_price"] == 10
    assert prices(latest)[0]["close_price"] == 20
    with pytest.raises(DataError, match="Frozen release manifest changed"):
        MarketRelease(published.root, 1, expected_manifest_sha256=latest.sha256)
    # Even a cached valid manifest is not valid under another CURRENT release id.
    current.write_text(json.dumps({**second, "sha256": first.sha256}))
    with pytest.raises(DataError, match="Unexpected release identity"):
        MarketRelease(published.root)


@pytest.mark.parametrize("kind", ["metadata", "manifest"])
def test_cached_replica_is_rehashed_after_same_size_restored_mtime_change(published, monkeypatch, kind):
    install(published, monkeypatch)
    MarketRelease(published.root)
    path = getattr(published, kind + "_replica")
    raw, before = path.read_bytes(), path.stat()
    path.chmod(0o600)
    path.write_bytes(b"x" + raw[1:])
    path.chmod(0o400)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert fingerprint(path) != [before.st_size, before.st_mtime_ns, before.st_ino,
                                 before.st_ctime_ns, before.st_dev]
    with pytest.raises(DataError, match="(Immutable object changed|manifest chain changed)"):
        MarketRelease(published.root)


@pytest.mark.parametrize("kind", ["missing", "mode", "symlink", "directory"])
def test_reader_rechecks_selected_replica_and_never_falls_back(published, monkeypatch, kind):
    install(published, monkeypatch)
    reader = MarketRelease(published.root)
    if kind == "directory":
        published.directory.rename(published.directory.with_name("moved"))
        published.directory.mkdir(mode=0o700)
    elif kind == "mode":
        published.metadata_replica.chmod(0o644)
    else:
        published.metadata_replica.unlink()
        if kind == "symlink":
            published.metadata_replica.symlink_to(published.root / published.meta["path"])
    with pytest.raises(DataError, match="Replica|replica"):
        with reader.metadata():
            pytest.fail("changed selected replica was accepted")


def test_manifest_replica_changed_during_read_is_not_cached(published, monkeypatch):
    install(published, monkeypatch)
    previous = Path.read_bytes

    def changing(path):
        raw = previous(path)
        if path == published.manifest_replica:
            path.chmod(0o600)
            path.write_bytes(raw + b" ")
        return raw

    monkeypatch.setattr(Path, "read_bytes", changing)
    with pytest.raises(DataError, match="during verification"):
        MarketRelease(published.root)
    assert module._manifest_cache is None


def test_metadata_replica_changed_during_hash_does_not_get_receipt(published, monkeypatch):
    install(published, monkeypatch)
    previous = module.sha_file

    def changing(path):
        digest = previous(path)
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b"changed")
        return digest

    monkeypatch.setattr(module, "sha_file", changing)
    with pytest.raises(DataError, match="during verification"):
        MarketRelease(published.root)
    assert not list(published.cache.glob("*.json"))


def test_metadata_replica_changed_during_query_is_rejected(published, monkeypatch):
    install(published, monkeypatch)
    reader = MarketRelease(published.root)
    with pytest.raises(DataError, match="Metadata changed during read"):
        with reader.metadata() as db:
            assert db.execute("SELECT count(*) FROM release_instruments").fetchone()[0] == 1
            published.metadata_replica.chmod(0o600)
            with published.metadata_replica.open("ab") as stream:
                stream.write(b"changed")


@pytest.mark.parametrize("kind", ["manifest", "metadata"])
def test_cache_hit_checks_replica_fingerprint_again_after_lookup(published, monkeypatch, kind):
    install(published, monkeypatch)
    reader = MarketRelease(published.root)
    target = getattr(published, kind + "_replica")
    previous, calls = ReadReplica.fingerprint, []

    def changed(replica):
        value = previous(replica)
        if replica.path == target:
            calls.append(target)
            # Manifest selection checks once before looking in its raw-byte
            # cache; an already-bound metadata reader starts at the lookup.
            after_lookup = 3 if kind == "manifest" else 2
            if len(calls) == after_lookup:
                return [value[0] + 1, *value[1:]]
        return value

    monkeypatch.setattr(ReadReplica, "fingerprint", changed)
    with pytest.raises(DataError, match="during cache access"):
        if kind == "manifest":
            MarketRelease(published.root)
        else:
            reader.verify(reader.manifest["metadata"])


@pytest.mark.parametrize("unsafe", ["/absolute.sqlite3", "../outside.sqlite3", "objects/../outside", ".", ""])
def test_metadata_descriptor_path_guard_still_precedes_replica_use(published, monkeypatch, unsafe):
    install(published, monkeypatch)
    reader = MarketRelease(published.root)
    reader.manifest["metadata"]["path"] = unsafe
    with pytest.raises(DataError, match="Object must be within"):
        reader.verify(reader.manifest["metadata"])


def test_disabling_verify_cache_still_checks_complete_replica_bytes(published, monkeypatch):
    install(published, monkeypatch)
    monkeypatch.delenv("ASHARE_MARKET_VERIFY_CACHE")
    previous, calls = module.sha_file, []

    def measured(path):
        calls.append(path)
        return previous(path)

    monkeypatch.setattr(module, "sha_file", measured)
    assert MarketRelease(published.root).sha256 == MarketRelease(published.root).sha256
    assert calls == [published.metadata_replica, published.metadata_replica]
    assert module._manifest_cache is None
    assert hashlib.sha256(published.manifest_replica.read_bytes()).hexdigest() == published.pointer["sha256"]


def test_cmes_catalog_consumer_metadata_projection(tmp_path, monkeypatch):
    root = tmp_path / "cmes-market"
    with ReleaseWriter(root, free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        bar = chunk(writer, 10)
        doc = manifest(meta, [bar])
        doc["cmes_catalog"] = {"schema": "ad-cmes-catalog.v1", "import_id": "cmes-import:test"}
        writer.publish(doc, expected_parent=None)
    replicas = tmp_path / "replicas"
    replicas.mkdir(mode=0o700)
    cache = tmp_path / "verified"
    monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(replicas))
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(cache))
    first = MarketRelease(root)
    proj_path = replicas / (meta["sha256"] + ".consumer.sqlite3")
    assert first.metadata_path == proj_path
    assert first.identifiers == {"000001.XSHE": 1}
    # Subsequent reads reuse the projection even if the original metadata file is unlinked.
    (root / meta["path"]).unlink()
    second = MarketRelease(root)
    assert second.metadata_path == proj_path
    assert second.identifiers == {"000001.XSHE": 1}

