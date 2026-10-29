"""Verified native macro bundles composed by the existing coordinated writer.

Preparation never writes the selected release root. Publication needs the caller's
live controller/driver handle, the same writer handle, and a fresh parent binding.
This module allocates no instrument IDs and never fills canonical CMES fields.
"""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from ashare_data.core.model import require
from ashare_data.providers.free_updates import (
    catalog,
    digest,
    encoded,
    implementation_identity,
    source_date,
)
from ashare_data.providers.free_update_handoff import read_bundle
from ashare_data.storage.market_release import (
    MarketRelease,
    ReleaseWriter,
    atomic_write,
    connection,
    sha_file,
)

CONTRACT = "native_macro_sources"
VERSION = "ad.native-macro-release.v1"
CANDIDATE = "ad.native-macro-candidate.v1"
SOURCE = "eastmoney_datacenter"
TABLE = "macro.native_observations"
MAX_ROWS = 100000
MAX_BYTES = 128 * 1024**2
COLUMNS = "bundle_id,sequence,menu_row,native_series_id,business_key,period,observed_at,function,source,source_report,payload_sha256,payload_json,units_json,capture_id"
DDL = """CREATE TABLE observations(bundle_id VARCHAR,sequence BIGINT,menu_row INTEGER,
 native_series_id VARCHAR,business_key VARCHAR,period DATE,observed_at TIMESTAMPTZ,
 function VARCHAR,source VARCHAR,source_report VARCHAR,payload_sha256 VARCHAR,
 payload_json VARCHAR,units_json VARCHAR,capture_id VARCHAR)"""


def mapping_identity():
    root = Path(__file__).resolve().parents[1]
    names = (
        "storage/native_macro_release.py",
        "product/native_macro.py",
        "storage/market_release.py",
        "cli/native_macro.py",
    )
    return digest({name: sha_file(root / name) for name in names})


def verify_object(root, item):
    path = (Path(root) / item["path"]).resolve()
    require(path.is_relative_to(Path(root).resolve()), "MACRO_PATH", "Object escaped its release")
    require(
        path.stat().st_size == item["bytes"] and sha_file(path) == item["sha256"],
        "MACRO_OBJECT",
        "Immutable macro object changed",
    )
    return path


def checked_bundle(path):
    """Reparse the captured HTTP bodies; never trust a declared semantic flag."""
    from ashare_data.providers.free_update_coverage import CapturedResponses
    from ashare_data.providers.free_update_macro_history import collect
    from ashare_data.providers.free_update_semantics import validate_semantics

    path = Path(path).resolve()
    manifest, snapshot = read_bundle(path)
    require(
        manifest["implementation_sha256"] == implementation_identity()
        and manifest["workbook_sha256"] == catalog()["workbook_sha256"],
        "MACRO_PRODUCER",
        "Review a bundle from the current verified collector",
    )
    require(
        0 < len(snapshot) <= MAX_ROWS
        and len(manifest["files"]) <= 500
        and sum((path / n).stat().st_size for n in manifest["files"]) <= MAX_BYTES,
        "MACRO_BOUND",
        "Split an oversized macro handoff",
    )
    specs = json.loads(
        Path(__file__).parents[1].joinpath("providers/public-macro-requests.json").read_text()
    )["specs"]
    expected, contracts, checks = [], {}, {}
    menu = {r["excel_row"]: r for r in catalog()["items"]}
    for check in manifest["checks"]:
        row, function = check["excel_row"], check["function"]
        require(
            row not in contracts
            and row in menu
            and len(menu[row]["calls"]) == 1
            and menu[row]["calls"][0]["function"] == function,
            "MACRO_SCOPE",
            "Native source/menu mapping changed",
        )
        require(
            specs.get(function, {}).get("kind") == "eastmoney_datacenter",
            "MACRO_SOURCE",
            "Only reviewed native macro reports are supported",
        )
        run = json.loads((path / "runs" / (check["run_id"] + ".json")).read_text())
        require(
            run["status"] == "received"
            and run["implementation_sha256"] == implementation_identity()
            and run["finished_at"] == check["observed_at"],
            "MACRO_CAPTURE",
            "Capture identity changed",
        )
        replay = CapturedResponses(
            path,
            run,
            datetime.fromisoformat(run["started_at"]),
            datetime.fromisoformat(run["finished_at"]),
        )
        parameters = run["coverage"]["requested_window"]
        records = collect(replay, specs[function], parameters)
        job = {"function": function, "parameters": parameters}
        semantics = validate_semantics(records, job, run["observation_as_of"])
        require(
            replay.index == len(run["receipts"])
            and records == run["records"]
            and replay.dataset_metadata == run["coverage"] == check["coverage"]
            and run["coverage"]["complete_history"]
            and semantics == check["semantics"]
            and semantics["contract_reviewed"]
            and not semantics["gaps"],
            "MACRO_REPLAY",
            "Raw response, complete range or semantic replay differs",
        )
        definition = {
            "source": SOURCE,
            "function": function,
            "source_report": run["coverage"]["report"],
            "units": semantics["units"],
            "business_key": semantics["business_key"],
            "timezone": "Asia/Shanghai",
            "frequency": "source_statistical_release",
            "price_adjustment": "not_applicable",
            "instrument_id": None,
        }
        definition["native_series_id"] = digest(definition)
        contracts[str(row)] = definition
        checks[row] = check
        for record in records:
            key = {k: record[k] for k in semantics["business_key"]}
            expected.append(
                {
                    "key": digest([row, function, list(key.values())]),
                    "menu_row": row,
                    "function": function,
                    "business_key": key,
                    "payload_sha256": digest(record),
                    "data": record,
                }
            )
    require(
        sorted(contracts, key=int) == list(map(str, sorted(manifest["menu_rows"])))
        and sorted(expected, key=lambda r: r["key"]) == snapshot,
        "MACRO_SNAPSHOT",
        "Bundle snapshot or business keys differ from raw source",
    )
    return manifest, snapshot, contracts, checks


def current_parent(parent):
    current = MarketRelease(parent.root)
    require(
        current.release_id == parent.release_id and current.sha256 == parent.sha256,
        "MACRO_PARENT",
        "Parent advanced; prepare a new candidate against the current release and SHA",
    )
    return {"root": str(parent.root), "release_id": parent.release_id, "sha256": parent.sha256}


def read_candidate(path):
    root = Path(path).resolve()
    value = json.loads((root / "candidate.json").read_text())
    require(
        value["schema"] == CANDIDATE
        and value["mapping_sha256"] == mapping_identity()
        and value["candidate_id"]
        == digest({k: v for k, v in value.items() if k != "candidate_id"}),
        "MACRO_CANDIDATE",
        "Candidate schema, mapping or hash changed",
    )
    for item in value["objects"]:
        verify_object(root, item)
    return value


def prepare(parent, bundle, output):
    """Build a bounded unpublished candidate outside the selected release root."""
    binding = current_parent(parent)
    bundle = Path(bundle).resolve()
    manifest, snapshot, definitions, checks = checked_bundle(bundle)
    root = Path(output).resolve()
    require(
        not root.is_relative_to(parent.root), "MACRO_OUTPUT", "Prepare outside the release root"
    )
    prior = parent.manifest.get(CONTRACT, {"schema": VERSION, "chains": {}})
    scope = digest(manifest["menu_rows"])
    chain = prior["chains"].get(
        scope, {"menu_rows": manifest["menu_rows"], "definitions": definitions, "bundles": []}
    )
    require(
        chain["definitions"] == definitions,
        "MACRO_UNITS",
        "Source definition or unit changed; explicit review required",
    )
    if any(b["bundle_id"] == manifest["bundle_id"] for b in chain["bundles"]):
        return {"already_consumed": True, "bundle_id": manifest["bundle_id"], "parent": binding}
    for key, other in prior["chains"].items():
        require(
            key == scope or not set(other["menu_rows"]) & set(manifest["menu_rows"]),
            "MACRO_SCOPE",
            "Another chain already owns a selected native menu row",
        )
    tail = chain["bundles"][-1] if chain["bundles"] else None
    require(
        manifest["previous_bundle"] == (tail["bundle_id"] if tail else None),
        "MACRO_PREDECESSOR",
        "Consume the complete staging predecessor chain in order",
    )
    previous = parent.native_macro().snapshot(manifest["menu_rows"]) if tail else {}
    now = {r["key"]: r for r in snapshot}
    require(
        set(previous) <= set(now),
        "MACRO_HISTORY",
        "Input removed previous periods; do not delete history",
    )
    changed = [
        r
        for r in snapshot
        if previous.get(r["key"], {}).get("payload_sha256") != r["payload_sha256"]
    ]
    require(
        changed == json.loads((bundle / "delta.json").read_text()),
        "MACRO_DELTA",
        "Delta differs from the published source snapshot",
    )
    if tail:
        for row in manifest["menu_rows"]:
            before = datetime.fromisoformat(tail["observed_at"][str(row)])
            after = datetime.fromisoformat(checks[row]["observed_at"])
            require(after >= before, "MACRO_TIME", "Capture order regressed")
            if any(r["menu_row"] == row for r in changed):
                require(
                    after > before, "MACRO_TIME", "Conflicting revision at the same capture instant"
                )
    require(
        not root.exists() or not any(root.iterdir()),
        "MACRO_OUTPUT",
        "Use a new empty candidate directory",
    )
    sequence = len(chain["bundles"]) + 1
    objects, archive, data_files = [], {}, []
    with ReleaseWriter(
        root, categorized=True, free_reserve_bytes=0, budget_bytes=MAX_BYTES * 3
    ) as writer:
        for name in sorted([*manifest["files"], "manifest.json"]):
            src = bundle / name
            require(
                src.resolve().is_relative_to(bundle), "MACRO_PATH", "Bundle file escaped its root"
            )
            temp = writer.work / (hashlib.sha256(name.encode()).hexdigest() + ".copy")
            shutil.copyfile(src, temp)
            item = writer.object(temp, src.suffix.lstrip("."), table="macro.source_evidence")
            item.update(kind="native_macro_evidence", table="macro.source_evidence")
            archive[name] = item
            if item not in objects:
                objects.append(item)
        if changed:
            with closing(connection(memory="256MB", threads=1)) as c:
                c.execute(DDL)
                rows = []
                for row in changed:
                    number = row["menu_row"]
                    definition = definitions[str(number)]
                    check = checks[number]
                    rows.append(
                        (
                            manifest["bundle_id"],
                            sequence,
                            number,
                            definition["native_series_id"],
                            row["key"],
                            source_date(row["data"]["REPORT_DATE"]),
                            check["observed_at"],
                            row["function"],
                            SOURCE,
                            definition["source_report"],
                            row["payload_sha256"],
                            encoded(row["data"]).decode(),
                            encoded(definition["units"]).decode(),
                            check["run_id"],
                        )
                    )
                c.executemany(
                    "INSERT INTO observations VALUES (" + ",".join("?" for _ in range(14)) + ")",
                    rows,
                )
                item = writer.parquet(
                    c,
                    "SELECT " + COLUMNS + " FROM observations ORDER BY business_key",
                    [],
                    "native-macro",
                    table=TABLE,
                )
                item.update(
                    kind="native_macro_table",
                    table=TABLE,
                    rows=len(rows),
                    duplicate_keys=0,
                    bundle_id=manifest["bundle_id"],
                    sequence=sequence,
                )
                data_files.append(item)
                objects.append(item)
    value = {
        "schema": CANDIDATE,
        "mapping_sha256": mapping_identity(),
        "parent": binding,
        "scope_id": scope,
        "menu_rows": manifest["menu_rows"],
        "definitions": definitions,
        "bundle_id": manifest["bundle_id"],
        "previous_bundle": manifest["previous_bundle"],
        "producer_sha256": manifest["implementation_sha256"],
        "sequence": sequence,
        "observed_at": {str(row): check["observed_at"] for row, check in checks.items()},
        "coverage": {str(row): check["coverage"] for row, check in checks.items()},
        "archive": archive,
        "data_files": data_files,
        "objects": objects,
        "counts": {
            "input_rows": len(snapshot),
            "adopted": len(changed),
            "equal_duplicate": len(snapshot) - len(changed),
            "quarantined": 0,
        },
        "historical_pit_certified": False,
        "mode": "append native-source revisions, preserve prior releases",
    }
    value["candidate_id"] = digest(value)
    atomic_write(root / "candidate.json", encoded(value))
    return value


@dataclass(frozen=True)
class Boundary:
    root: Path
    candidate_id: str
    parent_id: int
    parent_sha256: str
    pid: int
    driver: object
    writer_handle: object


def _boundary_check(writer, parent, candidate, permit):
    require(
        isinstance(permit, Boundary)
        and permit.root == writer.root
        and permit.pid == os.getpid()
        and permit.candidate_id == candidate["candidate_id"]
        and permit.parent_id == parent["release_id"]
        and permit.parent_sha256 == parent["sha256"]
        and permit.writer_handle is writer.lock,
        "MACRO_BOUNDARY",
        "Use this live controller's explicit candidate and parent boundary",
    )
    require(
        writer.lock is not None and not writer.lock.closed and not permit.driver.closed,
        "MACRO_BOUNDARY",
        "Controller or writer handle was released",
    )
    stat = (writer.root / "driver.lock").stat()
    held = os.fstat(permit.driver.fileno())
    require(
        (stat.st_dev, stat.st_ino) == (held.st_dev, held.st_ino),
        "MACRO_BOUNDARY",
        "Controller handle does not identify the existing driver lock",
    )
    # A free driver lock is a failure, never permission to become its owner.
    with (writer.root / "driver.lock").open("rb") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            require(
                False, "MACRO_BOUNDARY", "The existing controller must already hold driver.lock"
            )
    require(
        not any(
            (writer.root / n).exists()
            for n in (".work/import.json", "prepared.json", ".work/prepared.json")
        ),
        "MACRO_BOUNDARY",
        "Existing import/prepared work has not reached a released boundary",
    )
    actual = json.loads((writer.root / "CURRENT.json").read_text())
    require(
        actual["release_id"] == parent["release_id"] and actual["sha256"] == parent["sha256"],
        "MACRO_PARENT",
        "Parent advanced; prepare again",
    )


def controller_boundary(writer, parent, candidate, *, driver, coordination):
    """Called inside the existing controller, with its already-held driver handle.

    There is no CLI that creates a production permit or acquires a driver lock.
    An idle root writer lock alone never satisfies this contract.
    """
    require(
        coordination.get("writer_pid") == os.getpid()
        and coordination.get("boundary_available") is True
        and coordination.get("publication_readback_verified") is True
        and coordination.get("published", {}).get("release_id") == parent.release_id
        and coordination.get("published", {}).get("sha256") == parent.sha256,
        "MACRO_BOUNDARY",
        "Existing controller must explicitly confirm completed publication and readback",
    )
    permit = Boundary(
        writer.root,
        candidate["candidate_id"],
        parent.release_id,
        parent.sha256,
        os.getpid(),
        driver,
        writer.lock,
    )
    _boundary_check(writer, candidate["parent"], candidate, permit)
    return permit


def compose(writer, parent, candidate_root, *, boundary):
    """Append to a copy of the parent's manifest; do not publish or replace IDs."""
    root = Path(candidate_root).resolve()
    candidate = read_candidate(root)
    require(
        writer.root == parent.root and getattr(writer, "categorized", False),
        "MACRO_WRITER",
        "Use the existing categorized common writer",
    )
    require(
        candidate["parent"] == current_parent(parent),
        "MACRO_PARENT",
        "Candidate is bound to another parent",
    )
    _boundary_check(writer, candidate["parent"], candidate, boundary)
    previous = parent.manifest.get(CONTRACT, {"schema": VERSION, "chains": {}})
    contract = json.loads(json.dumps(previous))
    chain = contract["chains"].setdefault(
        candidate["scope_id"],
        {
            "menu_rows": candidate["menu_rows"],
            "definitions": candidate["definitions"],
            "bundles": [],
        },
    )
    require(
        len(chain["bundles"]) + 1 == candidate["sequence"],
        "MACRO_PREDECESSOR",
        "Chain changed after preparation",
    )
    evidence = list(parent.manifest.get("evidence", []))
    for item in candidate["objects"]:
        source = verify_object(root, item)
        with tempfile.NamedTemporaryFile(dir=writer.work, delete=False) as stream:
            temp = Path(stream.name)
        try:
            shutil.copyfile(source, temp)
            actual = writer.object(temp, source.suffix.lstrip("."), table=item["table"])
            require(
                all(actual[k] == item[k] for k in ("path", "sha256", "bytes")),
                "MACRO_LAYOUT",
                "Object layout differs from categorized candidate",
            )
        finally:
            temp.unlink(missing_ok=True)
        if item not in evidence:
            evidence.append(item)
    entry = {
        k: candidate[k]
        for k in (
            "candidate_id",
            "mapping_sha256",
            "bundle_id",
            "previous_bundle",
            "producer_sha256",
            "sequence",
            "observed_at",
            "coverage",
            "archive",
            "data_files",
            "counts",
            "parent",
        )
    }
    chain["bundles"].append(entry)
    with tempfile.NamedTemporaryFile(dir=writer.work, suffix=".sqlite3", delete=False) as stream:
        path = Path(stream.name)
    try:
        with closing(sqlite3.connect(path)) as db:
            with parent.metadata() as original:
                original.backup(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS release_contracts(name TEXT PRIMARY KEY,body TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO release_contracts VALUES (?,?)",
                (CONTRACT, encoded(contract).decode()),
            )
            db.commit()
        metadata = writer.object(path, "sqlite3")
    finally:
        path.unlink(missing_ok=True)
    counts = dict(parent.manifest["input_dispositions"])
    counts["native_macro:" + candidate["bundle_id"]] = candidate["counts"]
    value = dict(
        parent.manifest,
        metadata=metadata,
        evidence=evidence,
        input_dispositions=counts,
        **{CONTRACT: contract},
        change_set={"kind": CONTRACT, "candidate_id": candidate["candidate_id"]},
    )
    verify_contract(writer.root, value)
    return value


def verify_contract(root, manifest):
    contract = manifest.get(CONTRACT)
    native = [
        f for f in manifest.get("evidence", []) if f.get("kind", "").startswith("native_macro_")
    ]
    require(
        bool(contract) or not native, "MACRO_CONTRACT", "Native macro objects need their contract"
    )
    if not contract:
        return
    require(contract["schema"] == VERSION, "MACRO_CONTRACT", "Unknown native macro schema")
    objects, rows = [], set()
    for scope, chain in contract["chains"].items():
        from ashare_data.providers.free_update_macro_contracts import require_customs_units
        for definition in chain["definitions"].values():
            require_customs_units(definition["function"], definition.get("units", {}))
        require(
            scope == digest(chain["menu_rows"]) and not rows & set(chain["menu_rows"]),
            "MACRO_SCOPE",
            "Overlapping native source scopes",
        )
        rows.update(chain["menu_rows"])
        previous = None
        for n, bundle in enumerate(chain["bundles"], 1):
            require(
                bundle["sequence"] == n and bundle["previous_bundle"] == previous,
                "MACRO_PREDECESSOR",
                "Broken published bundle chain",
            )
            previous = bundle["bundle_id"]
            counts = bundle["counts"]
            require(
                counts["input_rows"]
                == counts["adopted"] + counts["equal_duplicate"] + counts["quarantined"],
                "MACRO_ACCOUNTING",
                "Native source input dispositions do not balance",
            )
            require(
                sum(f["rows"] for f in bundle["data_files"]) == counts["adopted"],
                "MACRO_ACCOUNTING",
                "Adopted rows differ from immutable data",
            )
            objects.extend(bundle["archive"].values())
            objects.extend(bundle["data_files"])
            for item in bundle["data_files"]:
                require(
                    item["table"] == TABLE
                    and item["kind"] == "native_macro_table"
                    and item["key_value_mismatches"] == item["duplicate_keys"] == 0,
                    "MACRO_TABLE",
                    "Unreconciled native macro table",
                )
                with closing(connection(memory="256MB", threads=1)) as c:
                    records = c.execute(
                        "SELECT " + COLUMNS + " FROM read_parquet(?)",
                        [str(verify_object(root, item))],
                    ).fetchall()
                require(
                    len(records) == item["rows"] and len({r[4] for r in records}) == len(records),
                    "MACRO_TABLE",
                    "Native row count or business key changed",
                )
                for row in records:
                    (
                        bid,
                        seq,
                        number,
                        series,
                        key,
                        period,
                        observed,
                        function,
                        source,
                        report,
                        payload_sha,
                        payload,
                        units,
                        capture,
                    ) = row
                    definition = chain["definitions"].get(str(number), {})
                    data = json.loads(payload)
                    require(
                        bid == bundle["bundle_id"]
                        and seq == n
                        and source == SOURCE
                        and definition.get("function") == function
                        and definition.get("source_report") == report
                        and definition.get("native_series_id") == series
                        and definition.get("units") == json.loads(units)
                        and digest(data) == payload_sha
                        and key
                        == digest([number, function, [data[k] for k in definition["business_key"]]])
                        and period.isoformat() == source_date(data["REPORT_DATE"])
                        and observed == datetime.fromisoformat(bundle["observed_at"][str(number)])
                        and capture,
                        "MACRO_TABLE",
                        "Native identity, period, unit, capture or payload changed",
                    )
    require(
        {digest(x) for x in objects} == {digest(x) for x in native},
        "MACRO_CONTRACT",
        "Native object set differs from its contract",
    )
    for item in {x["path"]: x for x in objects}.values():
        verify_object(root, item)
    metadata = verify_object(root, manifest["metadata"])
    with closing(sqlite3.connect(metadata.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        stored = db.execute(
            "SELECT body FROM release_contracts WHERE name=?", (CONTRACT,)
        ).fetchone()
        require(
            stored and json.loads(stored[0]) == contract,
            "MACRO_METADATA",
            "Frozen macro metadata differs",
        )


def publication_guard(writer, manifest, prior, pointer, permit):
    before = (prior or {}).get(CONTRACT)
    after = manifest.get(CONTRACT)
    if (
        not before
        and not after
        and not any(
            x.get("kind", "").startswith("native_macro_") for x in manifest.get("evidence", [])
        )
    ):
        return
    verify_contract(writer.root, manifest)
    if before:
        require(after is not None, "MACRO_HISTORY", "Preserve the published native macro contract")
        for scope, chain in before["chains"].items():
            target = after["chains"].get(scope, {})
            require(
                target.get("definitions") == chain["definitions"]
                and target.get("bundles", [])[: len(chain["bundles"])] == chain["bundles"],
                "MACRO_HISTORY",
                "Preserve native source identities and every previous bundle",
            )
    if before != after:
        require(
            pointer is not None,
            "MACRO_PARENT",
            "Publish native macro increments onto an existing common release",
        )
        old_ids = {
            b["candidate_id"]
            for c in (before or {"chains": {}})["chains"].values()
            for b in c["bundles"]
        }
        added = [
            b
            for c in after["chains"].values()
            for b in c["bundles"]
            if b["candidate_id"] not in old_ids
        ]
        require(
            len(added) == 1, "MACRO_BOUNDARY", "One immutable candidate per coordinated boundary"
        )
        candidate = added[0]
        _boundary_check(writer, candidate["parent"], candidate, permit)
        require(
            manifest["files"] == prior["files"]
            and all(item in manifest.get("evidence", []) for item in prior.get("evidence", [])),
            "MACRO_HISTORY",
            "Macro consumption must preserve every existing market/evidence object",
        )
        path = verify_object(writer.root, manifest["metadata"])
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute(
                "ATTACH DATABASE ? AS previous",
                ((writer.root / prior["metadata"]["path"]).as_uri() + "?mode=ro&immutable=1",),
            )
            for table in ("release_instruments", "release_identifiers"):
                missing = db.execute(
                    "SELECT count(*) FROM (SELECT * FROM previous."
                    + table
                    + " EXCEPT SELECT * FROM main."
                    + table
                    + ")"
                ).fetchone()[0]
                extra = db.execute(
                    "SELECT count(*) FROM (SELECT * FROM main."
                    + table
                    + " EXCEPT SELECT * FROM previous."
                    + table
                    + ")"
                ).fetchone()[0]
                require(
                    missing == extra == 0,
                    "MACRO_IDENTITIES",
                    "Macro consumption must preserve existing security identities exactly",
                )
