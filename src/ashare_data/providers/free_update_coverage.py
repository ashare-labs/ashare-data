"""Reproducible live date-window coverage trials, separate from daily deployment.

All five stages make real requests, checkpointed by stage-specific job IDs. The
verifier re-reads their immutable HTTP bodies, database links and public query
results. Extending a historical date window is not a future publication cycle.
"""
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

from ashare_data.providers.free_updates import catalog, digest, encoded, implementation_identity, plan, source_date

STAGES = ("seed", "seed_repeat", "delta", "full", "full_repeat")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def recipe(rows, *, as_of, seed_end, delta_start, page_size=100):
    from ashare_data.providers.free_update_macro_history import window
    bounds=window({"start_date":delta_start,"end_date":seed_end,"page_size":page_size})
    require(source_date(as_of) == as_of and bounds["end_date"] < as_of,
            "coverage requires exact as-of after the seed date")
    require(rows and len(set(rows)) == len(rows),"coverage requires distinct explicit menu rows")
    config={"schema":"ad.free-update-history-trial.v1","implementation_sha256":implementation_identity(),
        "workbook_sha256":catalog()["workbook_sha256"],"rows":sorted(rows),"as_of":as_of,
        "seed_end":bounds["end_date"],"delta_start":bounds["start_date"],"page_size":page_size}
    config["recipe_id"]=digest(config)
    return config


def stage_plan(config, stage):
    require(stage in STAGES,"unknown history trial stage")
    window={"page_size":config["page_size"]}
    if stage in {"seed","seed_repeat"}:
        window["end_date"]=config["seed_end"]
    if stage == "delta":
        window.update(start_date=config["delta_start"],end_date=config["as_of"])
    prepared=plan(rows=config["rows"],as_of=config["as_of"],source_window=window)
    require(len(prepared["jobs"]) == len(config["rows"]) and all(len(i["job_ids"]) == 1 for i in prepared["items"]),
            "history trial requires one complete direct report per row")
    mapping={}
    for job in prepared["jobs"]:
        old=job["job_id"]
        job["verification_trial"]={"recipe_id":config["recipe_id"],"stage":stage}
        job["job_id"]=digest({k:v for k,v in job.items() if k not in {"job_id","menu_rows"}})
        mapping[old]=job["job_id"]
    for item in prepared["items"]:
        item["job_ids"]=[mapping[key] for key in item["job_ids"]]
    return prepared


class CapturedResponses:
    """Offline integrity check of a recorded live run; never a new live result."""
    def __init__(self, root, result, start, end):
        self.root,self.result,self.start,self.end=root,result,start,end
        self.index=0
        self.dataset_metadata={}
        self.receipts=[]

    def get(self, url, headers=None):
        return self._read(url, "http")

    def get_curl(self, url):
        return self.get(url)

    def native(self, url):
        return self._read(url, "tdx-sdk")

    def tdx(self, url):
        return self._read(url, "tdx")

    def _read(self, url, protocol):
        receipts=self.result["receipts"]
        require(self.index < len(receipts),"live pagination receipt absent")
        receipt=receipts[self.index]
        self.index+=1
        require(receipt["url"] == url and receipt.get("protocol") == protocol and receipt["status"] == 200,
                "live request URL, protocol or successful response mismatch")
        received=datetime.fromisoformat(receipt["received_at"])
        require(self.start <= received <= self.end,"receipt time outside its live run")
        body=gzip.decompress((self.root/"raw"/(receipt["sha256"]+".gz")).read_bytes())
        require(hashlib.sha256(body).hexdigest() == receipt["sha256"],"live raw response hash mismatch")
        self.receipts.append(receipt)
        return body

    def post(self, url, parameters):
        index = self.index
        body = self.get(url)
        receipt = self.result["receipts"][index]
        require(receipt.get("request_method") == "POST" and receipt.get("request_body_sha256") ==
                hashlib.sha256(urlencode(parameters).encode()).hexdigest(), "live POST method/body differs from requested page or scope")
        return body

    def post_json(self, url, parameters):
        index = self.index
        body = self.get(url)
        receipt = self.result["receipts"][index]
        require(receipt.get("request_method") == "POST" and receipt.get("request_body_sha256") ==
                hashlib.sha256(json.dumps(parameters, ensure_ascii=False).encode()).hexdigest(),
                "live JSON POST method/body differs from requested page or scope")
        return body


def checked_run(root, db, job, as_of):
    from ashare_data.providers.free_update_macro_history import collect as parse_history
    from ashare_data.providers.free_update_semantics import validate_semantics
    candidates=db.execute("SELECT * FROM runs WHERE job_id=? AND as_of=? ORDER BY started_at DESC",
                          (job["job_id"],as_of)).fetchall()
    require(candidates and candidates[0]["status"] == "received","live trial stage incomplete or latest attempt failed")
    run=dict(candidates[0])
    require(json.loads(run["job_json"]) == job,"live trial job changed")
    result=json.loads((root/"runs"/(run["run_id"]+".json")).read_text())
    require(result["status"] == "received" and result.get("implementation_sha256") == implementation_identity(),
            "live trial result implementation mismatch")
    evidence=CapturedResponses(root,result,datetime.fromisoformat(run["started_at"]),datetime.fromisoformat(run["finished_at"]))
    specs=json.loads(Path(__file__).with_name("public-macro-requests.json").read_text())["specs"]
    records=parse_history(evidence,specs[job["function"]],job["parameters"])
    require(records == result["records"] and evidence.dataset_metadata == result["coverage"],
            "parsed live raw bodies differ from persisted result/coverage")
    require(evidence.index == len(result["receipts"]),"unexplained extra HTTP receipts")
    semantics=validate_semantics(records,job,result["observation_as_of"])
    require(semantics.get("contract_reviewed") and not semantics.get("gaps"),"live trial semantic acceptance incomplete")
    payloads={digest(r) for r in records}
    saved=db.execute("SELECT series_id,payload_sha256 FROM run_records WHERE run_id=?",(run["run_id"],)).fetchall()
    require(payloads == {r["payload_sha256"] for r in saved} and len({r["series_id"] for r in saved}) == 1,
            "live trial database record links mismatch")
    return {"run_id":run["run_id"],"job_id":job["job_id"],"finished_at":run["finished_at"],
        "records":len(records),"inserted":run["inserted"],"requests":len(result["receipts"]),
        "coverage":result["coverage"],"semantics":semantics,"payloads":payloads,
        "series_id":saved[0]["series_id"]}


def verify_trial(trial_root):
    from ashare_data.storage.free_updates import connect, query
    root=Path(trial_root).resolve()
    config=json.loads((root/"recipe.json").read_text())
    expected=recipe(config["rows"],as_of=config["as_of"],seed_end=config["seed_end"],
                    delta_start=config["delta_start"],page_size=config["page_size"])
    require(config == expected,"coverage recipe changed or implementation is no longer current")
    plans={s:stage_plan(config,s) for s in STAGES}
    output=[]
    with connect(root,readonly=True) as db:
        for row in config["rows"]:
            item={"excel_row":row,"coverage_verified":False,"recipe_id":config["recipe_id"],
                  "implementation_sha256":config["implementation_sha256"],"daily_deployed":False}
            try:
                stages={s:checked_run(root,db,next(j for j in p["jobs"] if row in j["menu_rows"]),config["as_of"])
                        for s,p in plans.items()}
                seed,repeat,delta,full,again=[stages[s] for s in STAGES]
                require(seed["payloads"] == repeat["payloads"] and repeat["inserted"] == 0,"live seed repeat changed")
                require(seed["inserted"] == len(seed["payloads"]),"trial did not start with empty history")
                added=delta["payloads"]-seed["payloads"]
                overlap=delta["payloads"] & seed["payloads"]
                require(added and overlap and delta["inserted"] == len(added),"live delta did not exercise overlapping insertion")
                require(full["payloads"] == seed["payloads"] | delta["payloads"] and full["inserted"] == 0,
                        "seed plus real delta does not reconcile with complete live history")
                require(full["payloads"] == again["payloads"] and again["inserted"] == 0,"live complete-history repeat changed")
                require(full["coverage"]["complete_history"] and again["coverage"]["complete_history"],
                        "full-history stage was date limited")
                require(len({v["series_id"] for v in stages.values()}) == 1,"date windows or page sizes split the series")
                known=set()
                for stage in STAGES:
                    value=stages[stage]
                    known |= value["payloads"]
                    observed=query(root,row,limit=100000,as_of=value["finished_at"])
                    require({v["payload_sha256"] for v in observed} == known,"received-by query lost or leaked observations")
                item.update(coverage_verified=True,scope="来源实际可得的全部统计期、页面及请求字段；不含源未提供的早期历史/首次发布版本",
                    full_records=full["records"],source_count=full["coverage"]["source_count"],
                    first_source_period=full["coverage"]["first_source_period"],last_source_period=full["coverage"]["last_source_period"],
                    full_pages=full["coverage"]["pages_received"],delta_new_records=len(added),delta_overlap_records=len(overlap),
                    real_requests=sum(v["requests"] for v in stages.values()),
                    live_historical_window_extension=True,real_cross_publication_cycle=False,
                    raw_count_and_pagination=True,persistence_readback=True,received_by_query=True,
                    live_unchanged_repeats=2,stages={s:{k:v for k,v in value.items() if k!="payloads"} for s,value in stages.items()},
                    limitations=full["semantics"]["limitations"]+["同一次工作期间真实请求的历史窗口增量；尚未观察下一次实际发布和定时任务运行"])
            except (ValueError,OSError,KeyError,TypeError) as exc:
                item["error"]=type(exc).__name__+": "+str(exc)
            output.append(item)
    return {"schema":"ad.free-update-history-verification.v1","recipe":config,
            "coverage_verified_count":sum(i["coverage_verified"] for i in output),"items":output}


def registered_coverage(root):
    root=Path(root).resolve()
    path=root/"coverage-verification.json"
    if not path.exists():
        return {},[]
    index=json.loads(path.read_text())
    verified,errors={},[]
    for identity in index.get("trial_ids",[]):
        try:
            require(len(identity) == 64 and all(c in "0123456789abcdef" for c in identity),"invalid trial identifier")
            trial=(root/"coverage-trials"/identity).resolve()
            require(trial.parent == (root/"coverage-trials").resolve(),"trial must stay in the isolated observation store")
            result=verify_trial(trial)
            for item in result["items"]:
                if item["coverage_verified"]:
                    verified[item["excel_row"]]=item
                else:
                    errors.append({"trial_id":identity,"excel_row":item["excel_row"],"error":item["error"]})
        except (ValueError,OSError,KeyError,TypeError) as exc:
            errors.append({"trial_id":identity,"error":str(exc)})
    return verified,errors


def run_trial(root, config, *, execute=False, timeout=75, progress=None):
    from ashare_data.storage.free_updates import collect, connect
    root=Path(root).resolve()
    require(config["implementation_sha256"] == implementation_identity(),"trial implementation changed")
    require(1 <= timeout <= 300,"trial timeout must be in 1..300")
    trial=root/"coverage-trials"/config["recipe_id"]
    plans={s:stage_plan(config,s) for s in STAGES}
    if not execute:
        return {"recipe":config,"trial_root":str(trial),"jobs":sum(len(p["jobs"]) for p in plans.values()),
                "dry_run":True,"daily_deployed":False}
    with connect(root,readonly=True):
        pass
    connect(trial).close()
    recipe_path=trial/"recipe.json"
    if recipe_path.exists():
        require(json.loads(recipe_path.read_text()) == config,"existing immutable trial recipe differs")
    else:
        recipe_path.write_bytes(encoded(config))
    for stage,prepared in plans.items():
        results=collect(trial,prepared,timeout=timeout,workers=1,attempts_per_day=2,
                        progress=(lambda value,s=stage: progress({"stage":s,**value})) if progress else None)
        if any(r["status"] not in {"received","already_received"} for r in results):
            return {"recipe":config,"trial_root":str(trial),"stopped_stage":stage,
                    "coverage_verified_count":0,"pending":True,"daily_deployed":False}
    result=verify_trial(trial)
    (trial/"verification.json").write_bytes(encoded(result))
    path=root/"coverage-verification.json"
    index=json.loads(path.read_text()) if path.exists() else {"schema":"ad.free-update-coverage-index.v1","trial_ids":[]}
    if config["recipe_id"] not in index["trial_ids"]:
        index["trial_ids"].append(config["recipe_id"])
    temporary=path.with_suffix(".next.json")
    temporary.write_bytes(encoded(index))
    temporary.replace(path)
    return result
