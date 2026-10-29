"""Offline end-to-end publication of real captured macro bundles in a new fixture.

The host release and stock sentinel are synthetic. Macro rows and source evidence
are the explicitly supplied immutable captures. No network or production writer.
"""

from contextlib import contextmanager, closing
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from ashare_data.core.model import DataError
from ashare_data.providers.free_updates import digest, encoded
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection
from ashare_data.storage.native_macro_release import (
    prepare,
    compose,
    controller_boundary,
    mapping_identity,
    checked_bundle,
)


def seed(root):
    with ReleaseWriter(
        root, categorized=True, free_reserve_bytes=0, budget_bytes=512 * 1024**2
    ) as writer:
        path = writer.work / "synthetic-host.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,name TEXT);
                CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER);
                INSERT INTO release_instruments VALUES(42,'000001.XSHE','SYNTHETIC host sentinel');
                INSERT INTO release_identifiers VALUES('000001.XSHE',42);""")
        metadata = writer.object(path, "sqlite3")
        with closing(connection(memory="128MB", threads=1)) as c:
            item = writer.parquet(
                c,
                """SELECT 7::INTEGER series_id,42::INTEGER instrument_id,date '2024-01-02' trade_date,
                10::DECIMAL(18,6) open_price,10::DECIMAL(18,6) high_price,10::DECIMAL(18,6) low_price,
                10::DECIMAL(18,6) close_price,100::BIGINT volume_shares,1000::DOUBLE turnover_cny,1::INTEGER batch_id""",
                [],
                "synthetic-host",
                table="market.bars_1d",
            )
        item.update(
            table="bars_1d",
            series_id=7,
            instrument_id=42,
            rows=1,
            duplicate_keys=0,
            first_date="2024-01-02",
            last_date="2024-01-02",
        )
        return writer.publish(
            {
                "metadata": metadata,
                "files": [item],
                "input_dispositions": {"synthetic_host": {"input_rows": 1, "adopted": 1}},
                "rules": {"fixture": "synthetic host; never a production value"},
            },
            expected_parent=None,
        )


@contextmanager
def coordinated(parent, candidate):
    with (parent.root / "driver.lock").open("a+b") as driver:
        fcntl.flock(driver, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ReleaseWriter(
            parent.root, categorized=True, free_reserve_bytes=0, budget_bytes=512 * 1024**2
        ) as writer:
            status = {
                "writer_pid": os.getpid(),
                "boundary_available": True,
                "publication_readback_verified": True,
                "published": {"release_id": parent.release_id, "sha256": parent.sha256},
            }
            yield (
                writer,
                controller_boundary(writer, parent, candidate, driver=driver, coordination=status),
            )


def run(output, bundles):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError(
            "Use a new isolated validation directory; inspect previous evidence before rerunning"
        )
    if any(
        (ancestor / "CURRENT.json").exists() or (ancestor / "CATALOG.json").exists()
        for ancestor in output.parents
    ):
        raise ValueError("Validation cannot be created inside an existing release root")
    inputs = [checked_bundle(path) for path in bundles]
    assert len(inputs) == 2 and inputs[1][0]["previous_bundle"] == inputs[0][0]["bundle_id"]
    output.mkdir(parents=True)
    (output / "isolated-validation.json").write_bytes(
        encoded(
            {
                "purpose": "offline native macro publication validation",
                "host_release_synthetic": True,
                "real_source_bundle_ids": [x[0]["bundle_id"] for x in inputs],
                "network_requests": 0,
            }
        )
    )
    release_root = output / "release"
    seed_pointer = seed(release_root)
    original = MarketRelease(release_root)
    reports = []
    for n, (source, expected) in enumerate(zip(bundles, inputs), 1):
        parent = MarketRelease(release_root)
        candidate_root = output / ("candidate-" + str(n))
        candidate = prepare(parent, source, candidate_root)
        with coordinated(parent, candidate) as (writer, permit):
            value = compose(writer, parent, candidate_root, boundary=permit)
            if n == 2:
                import ashare_data.storage.market_release as module

                original_write = module.atomic_write

                def interrupt(path, body):
                    if Path(path).name == "CURRENT.json":
                        raise OSError("isolated injected failure before pointer publication")
                    return original_write(path, body)

                module.atomic_write = interrupt
                try:
                    try:
                        writer.publish(
                            value,
                            expected_parent=parent.release_id,
                            expected_parent_sha256=parent.sha256,
                            native_macro_boundary=permit,
                        )
                    except OSError as exc:
                        assert "isolated injected" in str(exc)
                    else:
                        raise AssertionError("injected interruption did not execute")
                    assert MarketRelease(release_root).sha256 == parent.sha256
                finally:
                    module.atomic_write = original_write
                retried = compose(writer, parent, candidate_root, boundary=permit)
                assert retried == value
            pointer = writer.publish(
                value,
                expected_parent=parent.release_id,
                expected_parent_sha256=parent.sha256,
                native_macro_boundary=permit,
            )
        published = MarketRelease(release_root, expected_manifest_sha256=pointer["sha256"])
        assert (
            published.identifiers == original.identifiers
            and published.manifest["files"] == original.manifest["files"]
        )
        reader = published.native_macro()
        snapshot = reader.snapshot(expected[0]["menu_rows"])
        assert len(snapshot) == len(expected[1])
        checks = []
        for source_row in expected[1]:
            found = snapshot[source_row["key"]]
            assert (
                found["data"] == source_row["data"]
                and found["payload_sha256"] == source_row["payload_sha256"]
            )
            assert found["units"] == expected[2][str(source_row["menu_row"])]["units"]
        for row in expected[0]["menu_rows"]:
            source_rows = [x for x in expected[1] if x["menu_row"] == row]
            records = reader.records(row, "1900-01-01", "2100-12-31")
            assert len(records) == len(source_rows)
            checks.append(
                {
                    "excel_row": row,
                    "read_back_records": len(records),
                    "payload_and_unit_mismatches": 0,
                }
            )
        replay = prepare(published, source, output / ("unused-repeat-" + str(n)))
        assert replay["already_consumed"] and not (output / ("unused-repeat-" + str(n))).exists()
        reports.append(
            {
                "release_id": published.release_id,
                "manifest_sha256": published.sha256,
                "candidate_id": candidate["candidate_id"],
                "bundle_id": candidate["bundle_id"],
                "counts": candidate["counts"],
                "queries": checks,
                "idempotent_handoff": True,
            }
        )
    pinned = MarketRelease(release_root, 2, expected_manifest_sha256=reports[0]["manifest_sha256"])
    assert len(pinned.native_macro().snapshot(inputs[0][0]["menu_rows"])) == len(inputs[0][1])
    try:
        prepare(original, bundles[0], output / "stale-output")
    except DataError as exc:
        assert "Parent advanced" in str(exc)
    else:
        raise AssertionError("stale parent was accepted")
    row = inputs[1][0]["menu_rows"][0]
    field = next(iter(inputs[1][2][str(row)]["units"]))
    point = MarketRelease(release_root).native_macro()
    latest = point.records(row, "1900-01-01", "2100-12-31")[-1]["period"]
    command = [
        sys.executable,
        "-I",
        "-m",
        "ashare_data.cli.native_macro",
        "point",
        "--root",
        str(release_root),
        "--release",
        "3",
        "--manifest-sha",
        reports[-1]["manifest_sha256"],
        "--row",
        str(row),
        "--field",
        field,
        "--period",
        latest,
    ]
    response = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert response.returncode == 0, response.stderr + response.stdout
    cli = json.loads(response.stdout)
    assert cli["value"] == point.point(row, field, latest)["value"]
    result = {
        "schema": "ad.native-macro-e2e-verification.v1",
        "mapping_sha256": mapping_identity(),
        "input_kind": "real captured macro HTTP responses replayed offline; synthetic host release/stock sentinel",
        "source_rows": len(inputs[0][1]),
        "scope_menu_rows": inputs[0][0]["menu_rows"],
        "network_requests": 0,
        "production_write_attempted": False,
        "isolated_release_root": str(release_root),
        "synthetic_seed": seed_pointer,
        "published": reports,
        "old_release_readback": True,
        "market_objects_and_security_ids_preserved": True,
        "stale_parent_rejected": True,
        "pointer_interruption_and_retry": True,
        "cli_query": cli,
        "categorized_layout": (release_root / "data/macro/native_observations").is_dir()
        and not (release_root / "objects").exists(),
        "real_cross_publication_cycle_observed": False,
        "timers_loaded": False,
    }
    result["verification_id"] = digest(result)
    (output / "verification.json").write_bytes(encoded(result))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--bundles", type=Path, nargs=2, required=True)
    args = p.parse_args()
    print(json.dumps(run(args.output, args.bundles), ensure_ascii=False))
