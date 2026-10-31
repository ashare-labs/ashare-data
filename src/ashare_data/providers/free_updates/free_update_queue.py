"""Immutable explicit-universe plans with bounded, cross-day resumable execution.

The queue is stored inside an isolated observation store. It never installs a
timer, creates a production release, or equates a supplied universe with the
whole market. Raw receipts and SQLite run checkpoints remain the authority.
"""
import hashlib
import json
import os
from pathlib import Path

from ashare_data.providers.free_updates import digest, encoded, implementation_identity, plan


def prepare(config_path, *, as_of=None):
    path = Path(config_path).resolve()
    config = json.loads(path.read_text())
    if config.get("schema") != "ad.free-update-queue-config.v1":
        raise ValueError("unsupported queue configuration")
    root = (path.parent/Path(config["root"])).resolve()
    if root.exists() and any(root.iterdir()):
        from ashare_data.storage.free_updates import connect
        connect(root, readonly=True).close()
    if not config.get("rows"):
        raise ValueError("queue requires explicit menu rows")
    context = config.get("context", {})
    universe = None
    if "context_file" in config:
        if "context" in config:
            raise ValueError("use one context source")
        body = (path.parent/config["context_file"]).read_bytes()
        sha = hashlib.sha256(body).hexdigest()
        if sha != config.get("context_sha256"):
            raise ValueError("universe manifest hash changed or is missing")
        manifest = json.loads(body)
        if manifest.get("schema") != "ad.free-update-universe.v1" or not manifest.get("contexts"):
            raise ValueError("invalid universe manifest")
        context = manifest["contexts"]
        universe = {k:manifest.get(k) for k in ("scope","source_receipts","coverage_limitations","observed_at")}
        universe.update(sha256=sha, contexts=len(context))
    prepared = plan(rows=config["rows"], context=context, as_of=as_of or config.get("as_of"),
                    lookback_days=config.get("lookback_days",14))
    if any(i["missing_context"] for i in prepared["items"]):
        raise ValueError("queue requires every selected context dimension")
    limits = {"timeout":config.get("timeout",75), "workers":config.get("workers",2),
              "max_jobs":config.get("max_jobs",20), "attempts_per_day":config.get("attempts_per_day",2)}
    if (not 1 <= limits["timeout"] <= 300 or not 1 <= limits["workers"] <= 3
            or not 1 <= limits["attempts_per_day"] <= 3 or limits["max_jobs"] < 1):
        raise ValueError("invalid queue execution limits")
    content = {"schema":"ad.free-update-queue.v1", "plan":prepared, "limits":limits,
               "universe":universe, "implementation_sha256":implementation_identity(),
               "coverage_claim":"all explicitly frozen jobs only; whole-market and historical coverage require separate evidence"}
    content["queue_id"] = digest(content)
    return root, content


def save(config_path, *, as_of=None):
    from ashare_data.storage.free_updates import writer
    root, queue = prepare(config_path, as_of=as_of)
    with writer(root):
        folder = root/"queues"
        folder.mkdir(exist_ok=True)
        target = folder/(queue["queue_id"]+".json")
        if target.exists():
            if target.read_bytes() != encoded(queue):
                raise ValueError("existing immutable queue content differs")
        else:
            temporary=folder/(queue["queue_id"]+".next")
            with temporary.open("wb") as stream:
                stream.write(encoded(queue))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary,target)
    return target


def read(queue_path):
    path = Path(queue_path).resolve()
    queue = json.loads(path.read_text())
    content = {k:v for k,v in queue.items() if k != "queue_id"}
    if queue.get("schema") != "ad.free-update-queue.v1" or digest(content) != queue.get("queue_id"):
        raise ValueError("queue hash/schema mismatch")
    if queue["implementation_sha256"] != implementation_identity():
        raise ValueError("implementation changed; preserve this queue and prepare a new verified version")
    if path.parent.name != "queues" or path.name != queue["queue_id"]+".json":
        raise ValueError("queue must reside in its isolated observation store")
    return path.parent.parent, queue


def run(queue_path, *, execute=False, progress=None):
    from ashare_data.storage.free_updates import collect, connect
    root, queue = read(queue_path)
    prepared = queue["plan"]
    with connect(root,readonly=True) as db:
        done = {r[0] for r in db.execute("SELECT DISTINCT job_id FROM runs WHERE as_of=? AND status='received'",(prepared["as_of"],))}
    ids = {j["job_id"] for j in prepared["jobs"]}
    result = {"queue_id":queue["queue_id"], "root":str(root), "as_of":prepared["as_of"],
        "jobs":len(ids), "received_jobs":len(ids & done), "pending_jobs":len(ids-done),
        "dry_run":not execute, "limits":queue["limits"], "universe":queue["universe"],
        "production_publication":False, "timer_installed":False,
        "all_requested_jobs_received":bool(ids) and ids <= done, "full_market_accepted":False}
    if execute:
        results = collect(root,prepared,progress=progress,**queue["limits"])
        result = run(queue_path)
        result.update(dry_run=False, attempted=sum("run_id" in r for r in results))
    return result
