"""Immutable macro staging bundles and read-only production handoff previews.

This producer writes only inside a marked observation store. It does not publish
CMES releases, acquire the production writer lock, or update any CURRENT file.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import tempfile

from ashare_data.providers.free_updates import catalog, digest, encoded, implementation_identity
from ashare_data.providers.free_updates.free_update_coverage import checked_run, registered_coverage, require
from ashare_data.storage.immutable import fsync_dir


def read_bundle(path, *, verify_parent=True):
    import gzip
    path=Path(path).resolve()
    manifest=json.loads((path/"manifest.json").read_text())
    require(manifest.get("schema")=="ad.free-source-handoff.v1","unsupported handoff bundle")
    require(digest({k:v for k,v in manifest.items() if k!="bundle_id"})==manifest["bundle_id"],"handoff manifest hash mismatch")
    require(path.name==manifest["bundle_id"],"handoff directory identity mismatch")
    for name,sha in manifest["files"].items():
        target=(path/name).resolve()
        require(target.is_relative_to(path),"handoff file escaped bundle")
        require(hashlib.sha256(target.read_bytes()).hexdigest()==sha,"handoff file hash mismatch: "+name)
    for receipt in manifest["raw_receipts"]:
        body=gzip.decompress((path/"raw"/(receipt["sha256"]+".gz")).read_bytes())
        require(hashlib.sha256(body).hexdigest()==receipt["sha256"],"handoff raw response hash mismatch")
    state=json.loads((path/"snapshot.json").read_text())
    delta=json.loads((path/"delta.json").read_text())
    require(len(state)==manifest["snapshot_records"] and len(delta)==manifest["delta_records"],"handoff counts mismatch")
    require(len({r["key"] for r in state})==len(state),"handoff duplicate business key")
    for row in state:
        require(digest(row["data"])==row["payload_sha256"],"handoff payload hash mismatch")
    require(all(r in state for r in delta),"handoff delta not present in snapshot")
    if manifest.get("previous_bundle") and verify_parent:
        parent=(path.parent/manifest["previous_bundle"]).resolve()
        require(parent.parent==path.parent,"handoff parent escaped outbox")
        parent_manifest,prior=read_bundle(parent,verify_parent=False)
        require(parent_manifest["menu_rows"]==manifest["menu_rows"],"handoff parent scope mismatch")
        before={r["key"]:r["payload_sha256"] for r in prior}
        require(set(before)<={r["key"] for r in state},"handoff removed historical business keys")
        require(delta==[r for r in state if before.get(r["key"])!=r["payload_sha256"]],"handoff delta differs from parent snapshot")
    return manifest,state


def prepare_bundle(root, rows, *, previous=None, execute=False, resume_latest=False):
    from ashare_data.storage.free_updates import connect, writer
    root=Path(root).resolve()
    require(rows and len(set(rows))==len(rows),"handoff needs distinct explicit rows")
    # A local staging lock coordinates exports with collection, never production.
    with (writer(root) if execute else closing(connect(root,readonly=True))) as db:
        proof=json.loads((root/"verification.json").read_text())
        require(proof["implementation_sha256"]==implementation_identity(),"core verification implementation changed")
        core={i["excel_row"] for i in proof["verified_menu_rows"]}
        covered,_=registered_coverage(root)
        require(set(rows)<=core & covered.keys(),"handoff requires core and full-source coverage acceptance for every row")
        checkpoint=root/"outbox"/("prepared-"+digest(sorted(rows))+".json")
        if resume_latest and checkpoint.exists():
            previous=json.loads(checkpoint.read_text())["bundle_id"]
        previous_manifest,previous_state=(None,[])
        if previous:
            candidate=(root/"outbox"/previous).resolve()
            require(candidate.parent==root/"outbox","previous handoff must stay in this staging outbox")
            previous_manifest,previous_state=read_bundle(candidate)
            require(previous_manifest["menu_rows"]==sorted(rows),"handoff row scope changed")
        snapshot,runs,raws,checks=[],{}, {},[]
        for row in sorted(rows):
            latest=db.execute("""SELECT r.* FROM menu_runs mr JOIN runs r USING(run_id)
                WHERE mr.excel_row=? ORDER BY r.started_at DESC LIMIT 1""",(row,)).fetchone()
            require(latest and latest["status"]=="received","latest selected capture failed or is absent")
            job=json.loads(latest["job_json"])
            require(job["implementation_sha256"]==implementation_identity(),"latest capture uses another implementation")
            check=checked_run(root,db,job,latest["as_of"])
            require(check["coverage"].get("complete_history"),"handoff currently requires complete bounded macro histories")
            value=json.loads((root/"runs"/(latest["run_id"]+".json")).read_text())
            key=check["semantics"]["business_key"]
            for record in value["records"]:
                snapshot.append({"key":digest([row,job["function"],[record[k] for k in key]]),
                    "menu_row":row,"function":job["function"],"business_key":{k:record[k] for k in key},
                    "payload_sha256":digest(record),"data":record})
            runs[latest["run_id"]]=value
            for receipt in value["receipts"]:
                raws[receipt["sha256"]]=receipt
            checks.append({"excel_row":row,"function":job["function"],"run_id":latest["run_id"],
                "observed_at":latest["finished_at"],"coverage":check["coverage"],"semantics":check["semantics"],
                "source_history_trial":covered[row]["recipe_id"]})
        snapshot.sort(key=lambda r:r["key"])
        before={r["key"]:r["payload_sha256"] for r in previous_state}
        current={r["key"]:r["payload_sha256"] for r in snapshot}
        missing=sorted(before.keys()-current.keys())
        require(not missing,"source dropped previously staged periods; review instead of deleting historical data")
        # Compare with the last snapshot, not first_seen: reverting to an older
        # value is still a new revision and must be delivered again.
        delta=[r for r in snapshot if before.get(r["key"])!=r["payload_sha256"]]
        if previous_manifest and previous_manifest["checks"]==checks and not delta:
            return {"bundle_id":previous_manifest["bundle_id"],"path":str(root/"outbox"/previous_manifest["bundle_id"]),
                "menu_rows":sorted(rows),"snapshot_records":len(snapshot),"delta_records":0,
                "dry_run":not execute,"production_published":False,"timer_installed":False,
                "already_prepared":True,"new_bundle_created":False}
        bodies={"snapshot.json":encoded(snapshot),"delta.json":encoded(delta)}
        for rid,value in runs.items():
            bodies["runs/"+rid+".json"]=encoded(value)
        for sha in raws:
            bodies["raw/"+sha+".gz"]=(root/"raw"/(sha+".gz")).read_bytes()
        manifest={"schema":"ad.free-source-handoff.v1","implementation_sha256":implementation_identity(),
            "workbook_sha256":catalog()["workbook_sha256"],"menu_rows":sorted(rows),
            "previous_bundle":previous_manifest["bundle_id"] if previous_manifest else None,
            "snapshot_records":len(snapshot),"delta_records":len(delta),"checks":checks,
            "raw_receipts":list(raws.values()),"files":{name:hashlib.sha256(body).hexdigest() for name,body in sorted(bodies.items())},
            "operation":"upsert native-source revisions by menu-row/function/business-key; no deletion",
            "production_published":False,"cross_publication_cycle_observed":False,
            "scope":"source-visible history at listed receipt times; not CMES-equivalent full history or PIT vintages"}
        manifest["bundle_id"]=digest(manifest)
        target=root/"outbox"/manifest["bundle_id"]
        result={"bundle_id":manifest["bundle_id"],"path":str(target),"menu_rows":sorted(rows),
            "snapshot_records":len(snapshot),"delta_records":len(delta),"bytes":sum(map(len,bodies.values())),
            "dry_run":not execute,"production_published":False,"timer_installed":False}
        if not execute:
            return result
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            read_bundle(target)
        else:
            pending=Path(tempfile.mkdtemp(prefix=".pending-",dir=target.parent))
            try:
                for name,body in {**bodies,"manifest.json":encoded(manifest)}.items():
                    file=pending/name
                    file.parent.mkdir(exist_ok=True)
                    with file.open("wb") as stream:
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                os.replace(pending,target)
                fsync_dir(target.parent)
            finally:
                if pending.exists():
                    shutil.rmtree(pending)
        read_bundle(target)
        if resume_latest:
            pending_state=checkpoint.with_suffix(".next.json")
            with pending_state.open("wb") as stream:
                stream.write(encoded({"bundle_id":manifest["bundle_id"],"menu_rows":sorted(rows),"production_acknowledged":False}))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending_state,checkpoint)
            fsync_dir(checkpoint.parent)
        return result


def intake_preview(bundle, coordination):
    manifest,_=read_bundle(bundle)
    status=json.loads(Path(coordination).read_text())
    return {"bundle_id":manifest["bundle_id"],"bundle_verified":True,
        "snapshot_records":manifest["snapshot_records"],"delta_records":manifest["delta_records"],
        "writer_pid":status.get("writer_pid"),"current_release":status.get("published"),
        "writer_boundary_available":status.get("boundary_available",False),
        "production_write_attempted":False,"production_publication_allowed":False,
        "pending_step":"existing writer must implement and review native macro handoff consumption, then publish under its own lock/parent check"}


def render_launch_agent(*, python, config, output, hour=21, minute=30):
    require(0<=hour<=23 and 0<=minute<=59,"invalid local schedule time")
    require(Path(python).is_file() and Path(config).is_file(),"explicit Python executable and config are required")
    target=Path(output)
    require(not target.exists(),"refuse to replace a prepared scheduler artifact")
    value={"Label":"local.ashare-data.free-source-staging",
        "ProgramArguments":[str(Path(python).absolute()),"-m","ashare_data.cli.free_updates","run-scheduled",
            "--config",str(Path(config).resolve()),"--execute"],
        "StartCalendarInterval":{"Hour":hour,"Minute":minute},"RunAtLoad":False,
        "ProcessType":"Background"}
    target.write_bytes(plistlib.dumps(value,sort_keys=True))
    return {"path":str(target),"timer_installed":False,"production_publication":False,
        "clock":"host local timezone; match intended time before installation",
        "pending_step":"install the verified wheel in the selected runtime, then explicitly load this plist for isolated staging"}
