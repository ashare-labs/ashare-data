"""Synthetic routing fixtures; market acceptance is a separately sealed run."""

import hashlib
import json
import shutil

import pytest

from ashare_data import DataError
from ashare_data.cli import main
from ashare_data.product import LocalMinuteProduct


@pytest.fixture
def deployment(tmp_path):
    from test_neigezhu import NativeViewTests

    native = NativeViewTests()
    native.setUp()
    try:
        root = tmp_path / "layout" / "assets"
        shutil.copytree(native.root, root)
        review = root / "review.json"
        review.write_text(
            json.dumps(
                {
                    "schema": "neigezhu-quality-overlay.v1",
                    "manifest_sha256": native.sha,
                    "ranges": [],
                }
            )
        )
        tdx = root / "tdx.json"
        tdx.write_text(
            json.dumps(
                {
                    "schema": "ad-tdx-minute-session.v1",
                    "minute": [
                        {
                            "security": "600000.XSHG",
                            "source_label": "2026-06-02T09:31:00+08:00",
                            "open": "12.34",
                            "evidence": {"source": "synthetic-routing-fixture"},
                        }
                    ],
                }
            )
        )

        def spec(reader, asset):
            return {
                "reader": reader,
                "asset": asset,
                "coverage": "synthetic routing fixture",
                "limits": [],
            }

        sources = {
            "tdx": {
                **spec("tdx-session", "minute-tdx"),
                "content": {"sha256": hashlib.sha256(tdx.read_bytes()).hexdigest()},
            },
            "neigezhu": {
                **spec("neigezhu-native", "minute-hf-download"),
                "manifest": {"path": "manifest.csv", "sha256": native.sha},
                "review": {
                    "path": "review.json",
                    "sha256": hashlib.sha256(review.read_bytes()).hexdigest(),
                },
            },
        }
        entry = root.parent / "deployment.json"
        entry.write_text(
            json.dumps(
                {
                    "schema": "ashare-deployment.v1",
                    "data_root": "assets",
                    "assets": {
                        "minute-tdx": {"kind": "reference", "path": "tdx.json"},
                        "minute-hf-download": {"kind": "reference", "path": "."},
                    },
                    "profiles": {"product": {"kind": "product", "minute_sources": sources}},
                }
            )
        )
        yield entry
    finally:
        native.tearDown()


def query(owner, **kw):
    return owner.prices(
        "600000.XSHG", "2026-06-02T09:30:00+08:00", "2026-06-02T09:34:00+08:00", **kw
    )


def test_default_tdx_and_explicit_native_preserve_values_and_denials(deployment):
    owner = LocalMinuteProduct(deployment)
    original = json.loads((deployment.parent / "assets/tdx.json").read_text())["minute"]
    assert query(owner)["rows"] == original
    result = query(owner, source="neigezhu")
    assert result["source"] == "neigezhu"
    assert [r["source_row"] for r in result["rows"]] == [0, 1, 2, 3]
    assert result["rows"][0]["source_label"].endswith("09:30:00+08:00")
    assert result["rows"][1]["high"] == 9
    assert result["rows"][1]["use_decision"]["execution"] == "deny"
    assert result["rows"][3]["volume"] == 0
    assert result["report"]["repaired_rows"] == 0
    assert not result["binding"]["strict_execution_admitted"]


def test_selected_failure_has_no_fallback_and_does_not_disable_other_source(deployment):
    document = json.loads(deployment.read_text())
    del document["profiles"]["product"]["minute_sources"]["neigezhu"]["review"]
    deployment.write_text(json.dumps(document))
    owner = LocalMinuteProduct(deployment)
    listing = owner.sources()
    assert listing["default_source"] == "tdx"
    assert [s["status"] for s in listing["sources"]] == [
        "registered_unopened",
        "registration_error",
    ]
    assert query(owner)["rows"][0]["open"] == "12.34"
    assert query(owner, source="auto")["source"] == "tdx"
    for source in ("neigezhu", "missing"):
        with pytest.raises(DataError):
            query(owner, source=source)


def test_auto_native_masks_invalid_but_explicit_diagnostics_keep_raw(deployment):
    document = json.loads(deployment.read_text())
    document["profiles"]["product"]["default_minute_source"] = "neigezhu"
    deployment.write_text(json.dumps(document))
    owner = LocalMinuteProduct(deployment)
    normal, raw = query(owner), query(owner, source="neigezhu")
    assert len(normal["rows"]) == len(raw["rows"]) == 4
    assert [r["source_label"] for r in normal["rows"]] == [r["source_label"] for r in raw["rows"]]
    assert normal["rows"][1]["high"] is None and raw["rows"][1]["high"] == 9
    assert normal["rows"][1]["missing_reason"] == "confirmed_invalid_price"
    assert normal["rows"][1]["source_row"] == raw["rows"][1]["source_row"]
    assert normal["rows"][3]["volume"] == 0  # Zero activity alone is not invalidity.


def test_content_and_review_pins_fail_closed(deployment):
    owner = LocalMinuteProduct(deployment)
    (deployment.parent / "assets/review.json").write_text("{}")
    with pytest.raises(DataError, match="Pinned quality review"):
        query(owner, source="neigezhu")
    assert query(owner)["rows"]
    (deployment.parent / "assets/tdx.json").write_text("{}")
    with pytest.raises(DataError, match="TDX session changed"):
        query(owner)


def test_product_cli_and_relative_layout_move(deployment, tmp_path, capsys, monkeypatch):
    moved = tmp_path / "moved"
    shutil.move(deployment.parent, moved)
    entry = moved / "deployment.json"
    monkeypatch.setenv("ASHARE_DEPLOYMENT", str(entry))
    assert main(["product", "minute-sources"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["sources"][1]["path"] == str(moved / "assets")
    assert (
        main(
            [
                "product",
                "minute",
                "--source",
                "neigezhu",
                "--security",
                "600000.XSHG",
                "--start",
                "2026-06-02T09:30:00+08:00",
                "--end",
                "2026-06-02T09:34:00+08:00",
            ]
        )
        == 0
    )
    assert len(json.loads(capsys.readouterr().out)["rows"]) == 4
    assert (
        main(
            [
                "product",
                "minute",
                "--source",
                "auto",
                "--security",
                "600000.XSHG",
                "--start",
                "2026-06-02T09:30:00+08:00",
                "--end",
                "2026-06-02T09:34:00+08:00",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["source"] == "tdx"


def test_empty_window_never_falls_back_or_initializes_state(deployment):
    owner = LocalMinuteProduct(deployment)
    result = owner.prices(
        "600000.XSHG", "2026-06-03T09:30:00+08:00", "2026-06-03T09:31:00+08:00", source="neigezhu"
    )
    assert result["rows"] == []
    assert not list(deployment.parent.rglob("*.sqlite*"))
    with pytest.raises(DataError, match="offset"):
        owner.prices("600000.XSHG", "2026-06-02T09:30", "2026-06-02T09:31")


def test_auto_means_fixed_default_and_never_falls_back(deployment):
    document = json.loads(deployment.read_text())
    document["profiles"]["product"]["default_minute_source"] = "neigezhu"
    del document["profiles"]["product"]["minute_sources"]["neigezhu"]["review"]
    deployment.write_text(json.dumps(document))
    owner = LocalMinuteProduct(deployment)
    with pytest.raises(DataError):
        query(owner, source="auto")
    assert query(owner, source="tdx")["rows"]


def test_explicit_native_research_survives_canonical_release_selection(deployment):
    owner = LocalMinuteProduct(deployment)
    owner.market_release = {"release_id": 2}
    owner.frozen_market_config = {"market_release": "must not be used for explicit native research"}
    result = query(owner, source="neigezhu")
    assert len(result["rows"]) == 4
    assert result["rows"][1]["high"] == 9
    assert result["rows"][1]["use_decision"]["execution"] == "deny"


def test_native_reader_keeps_pinned_root_after_entry_asset_changes(deployment):
    owner = LocalMinuteProduct(deployment)
    expected = query(owner, source="neigezhu")
    document = json.loads(deployment.read_text())
    document["assets"]["minute-hf-download"]["path"] = "unrelated-new-download"
    deployment.write_text(json.dumps(document))
    actual = query(owner, source="neigezhu")
    assert actual["rows"] == expected["rows"]
    assert actual["binding"] == expected["binding"]


def test_frozen_native_audit_resolves_inside_restored_release(deployment, tmp_path, monkeypatch):
    import sqlite3
    from ashare_data.storage.market_release import ReleaseWriter, MarketRelease
    from test_market_release import metadata

    original_root = tmp_path / "original-market"
    document = json.loads(deployment.read_text())
    native = document["profiles"]["product"]["minute_sources"]["neigezhu"]
    native = json.loads(json.dumps(native))
    with ReleaseWriter(original_root, free_reserve_bytes=0) as writer:
        evidence = []
        for name in ("manifest", "review"):
            src = deployment.parent / "assets" / native[name]["path"]
            evidence.append(writer.object(src, src.suffix.lstrip(".")))
            native[name]["path"] = "/retired-deployment/" + src.name
        spec = {
            "minute_sources": {"neigezhu": native},
            "assets": {
                "minute-hf-download": {
                    "kind": "reference",
                    "path": str(deployment.parent / "assets"),
                }
            },
        }
        initial = metadata(writer)
        meta_path = writer.root / "work/frozen-readers.sqlite3"
        shutil.copyfile(writer.root / initial["path"], meta_path)
        with sqlite3.connect(meta_path) as db:
            db.execute("CREATE TABLE release_contracts(name TEXT PRIMARY KEY,body TEXT)")
            db.execute(
                "INSERT INTO release_contracts VALUES('source_observation_readers',?)",
                (json.dumps(spec),),
            )
            db.commit()
        writer.publish(
            dict(
                metadata=writer.object(meta_path, "sqlite3"),
                files=[],
                evidence=evidence,
                input_dispositions={"source": {"input_rows": 0}},
            ),
            expected_parent=None,
        )
    restored_root = tmp_path / "restored-market"
    shutil.copytree(original_root, restored_root)
    release = MarketRelease(restored_root)
    pin = {"root": str(restored_root), "release_id": 1, "manifest_sha256": release.sha256}
    document["profiles"]["product"]["market_release"] = pin
    document["assets"]["minute-hf-download"]["path"] = "unrelated-current-download"
    deployment.write_text(json.dumps(document))
    monkeypatch.setattr(
        "ashare_data.product.configuration.resolve", lambda *a, **k: {"market_release": pin}
    )
    result = query(LocalMinuteProduct(deployment), source="neigezhu")
    assert len(result["rows"]) == 4
    assert result["binding"]["manifest"]["path"].startswith(str(restored_root / "objects"))
    assert result["rows"][1]["use_decision"]["execution"] == "deny"
