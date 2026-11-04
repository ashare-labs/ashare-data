"""Pinned readers retain small verified ancestor links without evicting raw bytes."""

from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from ashare_data.core.model import DataError
from ashare_data.storage import market_release as module
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter
from ashare_data.storage.read_replicas import ReadReplica
from test_market_release import chunk, manifest, metadata
from test_read_replicas import copy_replica


@pytest.fixture(params=[False, True], ids=["source", "replica"])
def chain(tmp_path, monkeypatch, request):
    monkeypatch.delenv("ASHARE_MARKET_READ_REPLICAS", raising=False)
    monkeypatch.delenv("ASHARE_MARKET_VERIFY_CACHE", raising=False)
    root = tmp_path / "market"
    with ReleaseWriter(root, free_reserve_bytes=0) as writer:
        meta = metadata(writer)
        first = writer.publish(manifest(meta, [chunk(writer, 10)]), expected_parent=None)
        second = writer.publish(manifest(meta, [chunk(writer, 20)]), expected_parent=1)
    directory = tmp_path / "replicas"
    paths = [root / first["manifest"], root / second["manifest"]]
    if request.param:
        paths = [copy_replica(directory, path, pointer["sha256"], ".json")
                 for path, pointer in zip(paths, [first, second])]
        copy_replica(directory, root / meta["path"], meta["sha256"], ".sqlite3")
        monkeypatch.setenv("ASHARE_MARKET_READ_REPLICAS", str(directory))
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(tmp_path / "verified"))
    monkeypatch.setattr(module, "_manifest_cache", None)
    monkeypatch.setattr(module, "_parent_link_cache", OrderedDict())
    return SimpleNamespace(root=root, meta=meta, first=first, second=second,
                           selected_path=paths[0], parent_path=paths[1],
                           replicas=request.param, directory=directory)


def measure(chain, monkeypatch):
    reads, parses = Counter(), Counter()
    parent_raw = chain.parent_path.read_bytes()
    read_bytes, loads = Path.read_bytes, json.loads

    def read(path):
        reads[path] += 1
        return read_bytes(path)

    def parse(value, *args, **kwargs):
        if value == parent_raw:
            parses["parent"] += 1
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(module.json, "loads", parse)
    return reads, parses


def test_repeated_pin_keeps_one_parent_link_and_selected_raw_bytes(chain, monkeypatch):
    reads, parses = measure(chain, monkeypatch)
    readers = [MarketRelease(chain.root, 1) for _ in range(4)]
    assert reads[chain.parent_path] == reads[chain.selected_path] == 1
    assert parses["parent"] == 1
    assert module._manifest_cache[0][0] == chain.selected_path
    assert list(module._parent_link_cache.values()) == [(1, chain.first["sha256"])]
    assert all(type(link) is tuple and type(link[0]) is int and type(link[1]) is str
               for link in module._parent_link_cache.values())
    readers[0].manifest["rules"]["fixed"] = "caller mutation"
    assert all(reader.manifest["rules"]["fixed"] == "version-one" for reader in readers[1:])
    assert len({id(reader.manifest) for reader in readers}) == 4


def test_concurrent_cold_pin_hashes_and_parses_each_parent_once(chain, monkeypatch):
    reads, parses = measure(chain, monkeypatch)
    barrier = Barrier(6)

    def read(_):
        barrier.wait(timeout=5)
        return MarketRelease(chain.root, 1, expected_manifest_sha256=chain.first["sha256"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        readers = list(pool.map(read, range(6)))
    assert reads[chain.parent_path] == reads[chain.selected_path] == 1
    assert parses["parent"] == 1
    assert {reader.sha256 for reader in readers} == {chain.first["sha256"]}
    assert len({id(reader.manifest) for reader in readers}) == 6


@pytest.mark.parametrize("change", ["same_size_restored_mtime", "replacement"])
def test_cached_parent_tampering_is_rehashed_and_rejected(chain, monkeypatch, change):
    reads, _ = measure(chain, monkeypatch)
    MarketRelease(chain.root, 1)
    path = chain.parent_path
    raw, before = path.read_bytes(), path.stat()
    changed = raw.replace(b"version-one", b"version-two")
    assert changed != raw and len(changed) == len(raw)
    before_reads = reads[path]
    if change == "replacement":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(changed)
        replacement.chmod(0o400)
        os.replace(replacement, path)
    else:
        path.chmod(0o600)
        path.write_bytes(changed)
        path.chmod(0o400)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(DataError, match="manifest chain changed"):
        MarketRelease(chain.root, 1)
    assert reads[path] == before_reads + 1


def test_current_advance_traverses_new_parent_and_preserves_existing_pin(chain, monkeypatch):
    fixed = MarketRelease(chain.root, 1)
    with ReleaseWriter(chain.root, free_reserve_bytes=0) as writer:
        third = writer.publish(manifest(chain.meta, [chunk(writer, 30)]), expected_parent=2)
    third_path = chain.root / third["manifest"]
    if chain.replicas:
        third_path = copy_replica(chain.directory, third_path, third["sha256"], ".json")
    reads, parses = measure(chain, monkeypatch)
    pinned = MarketRelease(chain.root, 1)
    assert reads[third_path] == 1
    assert reads[chain.parent_path] == reads[chain.selected_path] == parses["parent"] == 0
    assert fixed.sha256 == pinned.sha256 == chain.first["sha256"]
    assert MarketRelease(chain.root).sha256 == third["sha256"]
    with pytest.raises(DataError, match="Frozen release manifest changed"):
        MarketRelease(chain.root, 1, expected_manifest_sha256=third["sha256"])


def test_changed_current_hash_cannot_use_prior_verified_parent(chain):
    MarketRelease(chain.root, 1)
    (chain.root / "CURRENT.json").write_text(json.dumps({**chain.second, "sha256": "0" * 64}))
    with pytest.raises(DataError, match="manifest chain changed"):
        MarketRelease(chain.root, 1)


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_current_cannot_bind_valid_ancestor_sha_to_another_release_id(chain, warm):
    if warm:
        assert MarketRelease(chain.root, 1).sha256 == chain.first["sha256"]
    # With replicas this is the identical physical SHA-named object and
    # fingerprint cached for id 2; only CURRENT's claimed id changes to 3.
    misnamed = chain.root / "releases/00000003.json"
    misnamed.write_bytes(chain.parent_path.read_bytes())
    (chain.root / "CURRENT.json").write_text(json.dumps({
        **chain.second, "release_id": 3, "manifest": "releases/00000003.json"}))
    with pytest.raises(DataError, match="Unexpected parent release identity"):
        MarketRelease(chain.root, 1, expected_manifest_sha256=chain.first["sha256"])
    assert len(module._parent_link_cache) == int(warm)
    (chain.root / "CURRENT.json").write_text(json.dumps(chain.second))
    assert MarketRelease(chain.root, 1).sha256 == chain.first["sha256"]


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize("schema", ["ad-market-release.v0", None], ids=["wrong-schema", "missing-schema"])
def test_correctly_hashed_ancestor_with_invalid_schema_is_rejected(chain, warm, schema):
    if warm:
        MarketRelease(chain.root, 1)
    value = json.loads(chain.parent_path.read_bytes())
    if schema is None:
        value.pop("schema")
    else:
        value["schema"] = schema
    raw = module.canonical(value)
    expected = hashlib.sha256(raw).hexdigest()
    path = (chain.directory / (expected + ".json") if chain.replicas
            else chain.root / chain.second["manifest"])
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(raw)
    path.chmod(0o400)
    (chain.root / "CURRENT.json").write_text(json.dumps({**chain.second, "sha256": expected}))
    with pytest.raises(DataError, match="Unexpected parent release identity"):
        MarketRelease(chain.root, 1)
    assert len(module._parent_link_cache) == int(warm)


def test_selected_root_terminates_without_traversing_its_null_link(chain, monkeypatch):
    (chain.root / "CURRENT.json").write_text(json.dumps(chain.first))

    def unexpected(*args, **kwargs):
        pytest.fail("selected root must not traverse a terminal link")

    monkeypatch.setattr(module, "_parent_link", unexpected)
    root = MarketRelease(chain.root, 1, expected_manifest_sha256=chain.first["sha256"])
    assert root.manifest["parent_release_id"] is None
    assert root.manifest["parent_manifest_sha256"] is None


@pytest.mark.parametrize("link", [
    (0, "a" * 64), (-1, "a" * 64), (True, "a" * 64), (1.0, "a" * 64),
    (2, "a" * 64), (3, "a" * 64), (None, None),
    (1, "A" * 64), (1, "g" * 64), (1, "a" * 63), (1, "a" * 65),
    (1, "a" * 64 + "\n"), (1, None), (1, ["a" * 64]),
])
def test_invalid_link_is_rejected_after_full_ancestor_hash(tmp_path, monkeypatch, link):
    monkeypatch.delenv("ASHARE_MARKET_READ_REPLICAS", raising=False)
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(tmp_path / "verified"))
    monkeypatch.setattr(module, "_parent_link_cache", OrderedDict())
    path = tmp_path / "parent.json"
    raw = module.canonical({"schema": module.SCHEMA, "release_id": 2,
                            "parent_release_id": link[0], "parent_manifest_sha256": link[1]})
    path.write_bytes(raw)
    with pytest.raises(DataError, match="Invalid linear release chain"):
        module._parent_link(path, hashlib.sha256(raw).hexdigest(), 2)
    assert module._parent_link_cache == {}


def test_optout_clears_both_caches_and_rechecks_every_parent(chain, monkeypatch):
    reads, parses = measure(chain, monkeypatch)
    original = MarketRelease(chain.root, 1)
    assert module._parent_link_cache and module._manifest_cache is not None
    monkeypatch.delenv("ASHARE_MARKET_VERIFY_CACHE")
    for _ in range(2):
        assert MarketRelease(chain.root, 1).sha256 == original.sha256
    assert reads[chain.parent_path] == reads[chain.selected_path] == parses["parent"] == 3
    assert module._parent_link_cache == {} and module._manifest_cache is None


def test_parent_cache_hit_rechecks_actual_object_identity(chain, monkeypatch):
    MarketRelease(chain.root, 1)
    calls = []
    if chain.replicas:
        previous = ReadReplica.fingerprint

        def changed(replica):
            value = previous(replica)
            if replica.path == chain.parent_path:
                calls.append(replica.path)
                if len(calls) == 3:  # selection, before lookup, after lookup
                    return [value[0] + 1, *value[1:]]
            return value

        monkeypatch.setattr(ReadReplica, "fingerprint", changed)
    else:
        previous = module.fingerprint

        def changed(path):
            value = previous(path)
            if path == chain.parent_path:
                calls.append(path)
                if len(calls) == 2:
                    return [value[0] + 1, *value[1:]]
            return value

        monkeypatch.setattr(module, "fingerprint", changed)
    with pytest.raises(DataError, match="parent manifest changed during cache access"):
        MarketRelease(chain.root, 1)
    assert module._parent_link_cache == {}


def test_parent_changed_while_parsing_never_creates_link_cache(chain, monkeypatch):
    parent_raw = chain.parent_path.read_bytes()
    previous = json.loads

    def changing(value, *args, **kwargs):
        result = previous(value, *args, **kwargs)
        if value == parent_raw:
            chain.parent_path.chmod(0o600)
            chain.parent_path.write_bytes(parent_raw + b" ")
        return result

    monkeypatch.setattr(module.json, "loads", changing)
    with pytest.raises(DataError, match="parent manifest changed during parsing"):
        MarketRelease(chain.root, 1)
    assert module._parent_link_cache == {}


def test_parent_cache_is_bounded_lru_and_bound_to_current_id(tmp_path, monkeypatch):
    monkeypatch.delenv("ASHARE_MARKET_READ_REPLICAS", raising=False)
    monkeypatch.setenv("ASHARE_MARKET_VERIFY_CACHE", str(tmp_path / "verified"))
    monkeypatch.setattr(module, "_PARENT_CACHE_ENTRIES", 2)
    monkeypatch.setattr(module, "_parent_link_cache", OrderedDict())
    selected_cache = ("selected raw bytes remain independent", b"unchanged")
    monkeypatch.setattr(module, "_manifest_cache", selected_cache)
    items = []
    for index in range(3):
        path = tmp_path / f"{index}.json"
        raw = json.dumps({"schema": module.SCHEMA, "release_id": 2,
                          "parent_release_id": 1, "parent_manifest_sha256": "a" * 64,
                          "distinct_body": index}).encode()
        path.write_bytes(raw)
        items.append((path, hashlib.sha256(raw).hexdigest()))
    previous, reads = Path.read_bytes, Counter()

    def measured(path):
        reads[path] += 1
        return previous(path)

    monkeypatch.setattr(Path, "read_bytes", measured)
    for index in (0, 1, 0, 2, 1):
        assert module._parent_link(*items[index], 2) == (1, "a" * 64)
        assert len(module._parent_link_cache) <= 2
    assert [reads[path] for path, _ in items] == [1, 2, 1]
    assert module._manifest_cache is selected_cache
    with pytest.raises(DataError, match="Unexpected parent release identity"):
        module._parent_link(*items[1], 1)

