"""Source-aware daily dispatch of frozen collectors; preview installs no timer.

Children retain their existing stores, implementation identities and checkpoints.
One shared lock and source cooldown registry cover all configured profiles. A
timer must explicitly invoke --execute. This recipe cannot publish production.
"""

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from zoneinfo import ZoneInfo
from uuid import uuid4


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("w") as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def checked(config_path):
    config = read(config_path)
    if config.get("schema") not in {"ad.daily-update-registry.v1", "ad.daily-update-registry.v2"}:
        raise ValueError("unsupported daily registry")
    parallel = config["schema"].endswith(".v2")
    scope = Path(config["isolated_workspace"]).resolve()
    root = Path(config["root"]).resolve()
    if not root.is_relative_to(scope) or any(
        (root / n).exists() for n in ("CURRENT", "CURRENT.json", "releases")
    ):
        raise ValueError("registry must remain in its isolated workspace")
    if sha(__file__) != config["dispatcher_sha256"]:
        raise ValueError("dispatcher differs from reviewed version")
    if not 1 <= config["chunks_per_run"] <= (100000 if parallel else 500) or not 60 <= config["seconds_per_run"] <= (86400 if parallel else 3600):
        raise ValueError("invalid daily run budget")
    if not 1 <= config["http_requests_per_day"] <= 1000 or not 1 <= config["child_jobs"] <= (120 if parallel else 5):
        raise ValueError("invalid request/job budget")
    if not (0 if parallel else 1) <= config["between_chunks_seconds"] <= 30:
        raise ValueError("invalid configured dispatch pacing")
    if parallel and (not 1 <= config.get("workers", 0) <= 64
                     or config.get("budget_scope") != "observation_store"):
        raise ValueError("parallel registry needs explicit workers and observation-store budgets")
    ids = set()
    manifests = set()
    for item in config["items"]:
        if not re.fullmatch("[a-z0-9-]+", item["id"]) or item["id"] in ids:
            raise ValueError("invalid or repeated profile id")
        ids.add(item["id"])
        if item["kind"] not in {"manual", "securities", "financial", "cached_macro", "billboard_queue"}:
            raise ValueError("unsupported collector kind")
        if "not_before_local" in item and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item["not_before_local"]):
            raise ValueError("invalid earliest collection time")
        if item["frequency"] not in {"daily", "weekly", "monthly"} or not item["sources"]:
            raise ValueError("explicit frequency and source families required")
        for key in ("profile", "runtime_manifest"):
            path = Path(item[key]).resolve()
            if not path.is_relative_to(scope) or sha(path) != item[key + "_sha256"]:
                raise ValueError("frozen file differs: " + key)
        profile = read(item["profile"])
        if Path(profile["root"]).resolve() != Path(item["observation_root"]).resolve():
            raise ValueError("observation root differs from reviewed profile")
        destination = Path(item["observation_root"]).resolve()
        if not destination.is_relative_to(scope) or any(
            (destination / n).exists() for n in ("CURRENT", "CURRENT.json", "releases")
        ):
            raise ValueError("production observation roots forbidden")
        if item["kind"] != "financial" and (
            profile.get("workers", 1) != 1 or profile.get("attempts_per_day", 2) != 1
        ):
            raise ValueError("daily registry requires serial one-attempt profiles")
        if item["kind"] == "manual" and not 1 <= profile["max_jobs"] <= (18 if parallel else 1):
            raise ValueError("manual child job count exceeds the collector contract")
        if item["kind"] in {"cached_macro", "billboard_queue"}:
            if not Path(profile["source_root"]).resolve().is_relative_to(scope):
                raise ValueError("cached source outside isolated workspace")
        if item.get("native_sdk_manifest"):
            binding = Path(item["native_sdk_manifest"]).resolve()
            if not binding.is_relative_to(scope) or sha(binding) != item["native_sdk_manifest_sha256"]:
                raise ValueError("native SDK binding changed")
            sdk = read(binding)
            sdk_root = Path(sdk["package_root"]).resolve()
            if not sdk_root.is_relative_to(scope) or profile.get("tdx", {}).get("sdk_sha256") != sdk["sdk_sha256"]:
                raise ValueError("native SDK scope differs")
            if profile["tdx"]["sdk_python"] != sdk["python"]:
                raise ValueError("native SDK interpreter differs")
            if Path(sdk["python"]).resolve() != Path(read(item["runtime_manifest"])["python"]).resolve():
                raise ValueError("native SDK needs a different interpreter authorization")
            for relative, digest in sdk["files"].items():
                path = (sdk_root / relative).resolve()
                if not path.is_relative_to(sdk_root) or sha(path) != digest:
                    raise ValueError("native SDK file changed")
        if not 30 <= item["child_timeout_seconds"] <= (7200 if parallel else 420):
            raise ValueError("child deadline out of bounds")
        if item["runtime_manifest"] not in manifests:
            frozen = read(item["runtime_manifest"])
            if config.get("enforce_request_admission") and "providers/free_update_budget.py" not in frozen["frozen_package_files"]:
                raise ValueError("scheduled runtime lacks per-request admission guard")
            origin = Path(frozen["package_origin"]).resolve().parent
            if not origin.is_relative_to(scope) or not Path(
                frozen["python"]
            ).absolute().is_relative_to(scope):
                raise ValueError("runtime outside isolated workspace")
            for path, digest in frozen["frozen_package_files"].items():
                if sha(origin / path) != digest:
                    raise ValueError("frozen runtime changed: " + path)
            manifests.add(item["runtime_manifest"])
        if item["kind"] == "financial":
            if sha(item["financial_entry"]) != item["financial_entry_sha256"]:
                raise ValueError("financial recipe changed")
            if sha(profile["source_universe"]) != profile["source_universe_sha256"]:
                raise ValueError("financial security identity changed")
    if not ids:
        raise ValueError("no configured profiles")
    return config


def blocked_sources(config, now, *, active_roots=()):
    """Read every store, including protected existing collectors, before dispatch.

    A cooldown is propagated by skipping a source family, never by moving a
    request to another hostname. Financial persistent refusals remain blocked.
    """
    blocked = {}
    roots = {i["observation_root"] for i in config["items"]} | set(
        config.get("cooldown_observation_roots", [])
    )
    active = {str(Path(root).resolve()) for root in active_roots}
    for root in roots:
        # Source and store leases already exclude a conflicting dispatch. Wait
        # until its child completes before reading a just-initializing database.
        if str(Path(root).resolve()) in active:
            continue
        path = Path(root) / "observations.sqlite"
        if path.exists():
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                for host, until, reason in db.execute(
                    "SELECT host,until_epoch,reason FROM cooldowns WHERE until_epoch>?",
                    (now.timestamp(),),
                ):
                    prior = blocked.get(host, {}).get("until_epoch", 0)
                    if until > prior:
                        blocked[host] = dict(until_epoch=until, reason=reason, store=root)
        path = Path(root) / "source-state.json"
        if path.exists():
            for host, status in read(path).items():
                until = status.get("cooldown_until", 0)
                if status.get("blocked") or until > now.timestamp():
                    previous = blocked.get(host, {})
                    blocked[host] = dict(
                        until_epoch=max(previous.get("until_epoch", 0), until),
                        permanent=bool(status.get("blocked") or previous.get("permanent")),
                        reason=status.get("blocked") or "financial cooldown",
                        store=root,
                    )
    return blocked


def source_block(item, blocked):
    # Recipe tags and actual receipt hosts must resolve to the same family.
    # A Sina refusal cannot be skipped merely because another recipe says
    # "sina" while the receipt says quotes.sina.cn or finance.sina.com.cn.
    aliases = {
        "sina": ("sina.com.cn", "sina.cn"),
        "tdx": ("tdx.com.cn",),
        "eltdx": ("tdx.com.cn",),
        "tdx-f10": ("tdx.com.cn",),
    }
    def domains(source):
        values = aliases.get(source, (source,))
        if source in {"sina.com.cn", "sina.cn"} or any(
            source.endswith("." + d) for d in aliases["sina"]
        ):
            values = (*values, *aliases["sina"])
        return values
    sources = {domain for source in item["sources"] for domain in domains(source)}
    return {
        h: v
        for h, v in blocked.items()
        if any(a == b or a.endswith("." + b) or b.endswith("." + a)
               for a in domains(h) for b in sources)
    }


def usage(config, day):
    requests = size = 0
    seen = set()
    for root in {i["observation_root"] for i in config["items"]}:
        database = Path(root) / "observations.sqlite"
        if database.exists():
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                for started, body in db.execute("SELECT started_at,result_json FROM runs"):
                    for receipt in json.loads(body).get("receipts", []):
                        stamp = receipt.get('received_at', started)
                        identity = (stamp, receipt.get('url'), receipt.get('sha256'),
                                    receipt.get('protocol'), receipt.get('request_method'))
                        if identity not in seen and datetime.fromisoformat(stamp).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() == day:
                            # Reparsed/derived records retain the same receipt.
                            # Count the HTTP attempt once across all stores.
                            seen.add(identity)
                            requests += 1
                            size += receipt.get('bytes', 0)
        for path in (Path(root) / "days").glob("*/receipts/*.json"):
            for attempt in read(path).get("attempts", []):
                stamp = attempt.get("started_at", attempt.get("received_at"))
                identity = (stamp, attempt.get("url"), attempt.get("response_sha256"))
                if (
                    stamp
                    and identity not in seen
                    and datetime.fromisoformat(stamp)
                    .astimezone(ZoneInfo("Asia/Shanghai"))
                    .date()
                    .isoformat()
                    == day
                ):
                    seen.add(identity)
                    requests += 1
                    size += attempt.get("response_bytes", 0)
    return {"requests": requests, "response_bytes": size}


def source_resources(item):
    """The same upstream and observation writer cannot be launched twice."""
    if item["kind"] == "cached_macro":
        return set()
    result = set()
    for host in item["sources"]:
        if host == "sina" or host.endswith(("sina.cn", "sina.com.cn")):
            host = "sina"
        elif host in {"tdx", "eltdx", "tdx-f10"} or "native_sdk_manifest" in item:
            host = "tdx"
        elif host.endswith("soozhu.com"):
            host = "soozhu.com"
        result.add(host)
    return result


def in_quiet_window(config, now, seconds=0, item=None):
    end = now + timedelta(seconds=seconds)
    for bounds in config.get("quiet_local_windows", []):
        if item is not None and (config.get("protected_sources") or config.get("protected_observation_roots")):
            shared_source = source_block(item, {host: {} for host in config.get("protected_sources", [])})
            shared_store = str(Path(item["observation_root"]).resolve()) in {
                str(Path(root).resolve()) for root in config.get("protected_observation_roots", [])
            }
            if not shared_source and not shared_store:
                continue
        start, stop = [
            datetime.combine(now.date(), datetime.strptime(v, "%H:%M").time(), now.tzinfo)
            for v in bounds
        ]
        if now < stop and end >= start:
            return True
    return False


def child(item, day, jobs):
    """Execute under the original pinned interpreter, never editable source."""
    if sha(item["profile"]) != item["profile_sha256"]:
        raise ValueError("child profile changed after parent check")
    profile = read(item["profile"])
    jobs = min(jobs, profile.get("max_jobs", jobs))
    if item["kind"] == "financial":
        if sha(item["financial_entry"]) != item["financial_entry_sha256"]:
            raise ValueError("financial recipe changed after parent check")
        spec = importlib.util.spec_from_file_location(
            "frozen_financial_recipe", item["financial_entry"]
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from ashare_data.providers.free_update_budget import financial_fetcher
        result = module.run_profile(item["profile"], execute=True, today=day, max_jobs=jobs,
                                    fetcher=financial_fetcher(module.source))
        audit = result["audit"]
        complete = audit["all_tasks_have_source_results"]
        pending = any(audit["task_states"].get(k, 0) for k in ("pending", "running", "captured"))
        return dict(complete=complete, settled=complete or not pending, result=result)
    if item["kind"] == "cached_macro":
        from ashare_data.providers.free_update_derived import run_cached_macro
        result = run_cached_macro(item["profile"], execute=True, as_of=day)
    elif item["kind"] == "billboard_queue":
        from ashare_data.providers.free_update_billboard_queue import run_billboard_queue
        attempts = 0
        for _ in range(jobs):
            result = run_billboard_queue(item["profile"], execute=True, as_of=day)
            attempts += result["attempted"]
            if result["all_requested_jobs_received"] or result["attempted"] == 0 or result.get("deferred"):
                break
        result = dict(result, attempted=attempts)
    elif item["kind"] == "manual":
        from ashare_data.providers.free_update_manual import run_manual

        result = run_manual(item["profile"], execute=True, as_of=day)
    else:
        from ashare_data.providers.free_update_batch import run

        result = run(item["profile"], execute=True, as_of=day, max_jobs=jobs)
    complete = result["all_requested_jobs_received"]
    deferred = result.get("deferred", False)
    return dict(complete=complete, settled=not deferred and (complete or result["attempted"] == 0),
                deferred=deferred, result=result)


def invoke(item, day, jobs, output):
    frozen = read(item["runtime_manifest"])
    command = [
        frozen["python"],
        "-I",
        str(Path(__file__).resolve()),
        "--child-item",
        str(output / "item.json"),
        "--day",
        day,
        "--jobs",
        str(jobs),
    ]
    save(output / "item.json", item)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    from ashare_data.providers.free_update_budget import ENV
    if item.get("request_budget"):
        if sha(item["request_budget"]) != item["request_budget_sha256"]:
            raise ValueError("request budget changed before child launch")
        env[ENV] = item["request_budget"]
        env[ENV + "_SHA256"] = item["request_budget_sha256"]
    else:
        env.pop(ENV, None)
        env.pop(ENV + "_SHA256", None)
    for key in ("ASHARE_DEPLOYMENT", "ASHARE_DATA_ROOT", "ASHARE_QUANT_DB"):
        env.pop(key, None)
    with (output / "stdout.json").open("wb") as out, (output / "stderr.log").open("wb") as err:
        process = subprocess.Popen(command, stdout=out, stderr=err, env=env, start_new_session=True)
        try:
            code = process.wait(timeout=item["child_timeout_seconds"])
        except subprocess.TimeoutExpired:
            # Kill only this newly-created process group, never existing collectors.
            import signal

            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise TimeoutError(
                "owned child deadline; retain receipts and resume after cooldown"
            ) from None
    if code or (output / "stdout.json").stat().st_size > 64 * 1024 * 1024:
        raise RuntimeError("child failed; inspect " + str(output))
    return read(output / "stdout.json")


def budget_totals(budget, day):
    path = budget.root / "request-budget-state.json"
    state = read(path) if path.exists() else {}
    return state.get("days", {}).get(day, {"requests": 0, "response_bytes": 0})


def run(profile, *, execute=False, clock=None, runner=invoke, usage_reader=usage):
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    config = checked(profile)
    parallel = config["schema"].endswith(".v2")
    workers = config.get("workers", 1) if parallel else 1
    clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
    root = Path(config["root"])
    network_roots = sorted({i["observation_root"] for i in config["items"]
                            if i["kind"] != "cached_macro"})
    summary = dict(
        dry_run=not execute, profile=str(Path(profile).resolve()),
        menu_rows=sorted({r for i in config["items"] for r in i["rows"]}),
        configured_profiles=len(config["items"]), workers=workers,
        serial_workers=1 if workers == 1 else None,
        budget_scope="observation_store" if parallel else "registry",
        budget_count=len(network_roots) if parallel else 1,
        timer_installed=False, formal_published=False,
    )
    if not execute:
        return summary
    root.mkdir(parents=True, exist_ok=True)
    with (root / "registry.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("daily registry already active") from None
        # A new dispatcher gets its own immutable controller checkpoint, while
        # using the same outer lock, STOP file and original observation stores.
        # Old checkpoints and deployment evidence are never rewritten.
        identity = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        state_root = root / "dispatches" / identity if parallel else root
        frozen = state_root / "profile.json"
        if frozen.exists() and read(frozen) != config:
            raise ValueError("existing registry configuration is immutable")
        save(frozen, config)
        from ashare_data.providers.free_update_budget import RequestBudget
        budgets = {}
        for scope in network_roots if parallel else ["registry"]:
            directory = (root / "request-budgets" / hashlib.sha256(scope.encode()).hexdigest()
                         if parallel else root)
            path = directory / "request-budget-config.json"
            value = dict(schema="ad.public-request-budget.v1",
                         requests_per_day=config["http_requests_per_day"],
                         response_bytes_per_day=config["response_bytes_per_day"])
            if path.exists() and read(path) != value:
                raise ValueError("existing request budget is immutable")
            save(path, value)
            budgets[scope] = RequestBudget(path)
        state_path = state_root / "state.json"
        state = read(state_path) if state_path.exists() else dict(cursor=0, items={}, source_defer={})
        started = time.monotonic()
        work, reason, skipped = [], "chunk_budget", set()
        active, held_sources, held_stores = {}, set(), set()
        launched = peak = 0
        reconciled_days = set()
        usage_scan_seconds = 0.0
        pacing_until = started

        def reconcile(day):
            nonlocal usage_scan_seconds
            if day in reconciled_days:
                return
            begin = time.monotonic()
            for scope, budget in budgets.items():
                selected = (dict(config, items=[i for i in config["items"]
                                                if i["observation_root"] == scope])
                            if parallel else config)
                budget.reconcile(day, usage_reader(selected, day))
            usage_scan_seconds += time.monotonic() - begin
            reconciled_days.add(day)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                now = clock()
                day = now.date().isoformat()
                reconcile(day)
                stop = (root / "STOP").exists()
                if stop:
                    reason = "stop_requested"
                elif shutil.disk_usage(root).free < config["minimum_free_bytes"]:
                    stop, reason = True, "low_free_disk"
                elif time.monotonic() - started >= config["seconds_per_run"]:
                    stop, reason = True, "wall_time_budget"
                blocked = blocked_sources(config, now, active_roots=held_stores)
                for host, info in state.get("source_defer", {}).items():
                    if info["until_epoch"] > now.timestamp():
                        blocked[host] = info
                pending_reason = "no_due_unblocked_work"
                while (not stop and len(active) < workers
                       and launched < config["chunks_per_run"]
                       and time.monotonic() >= pacing_until):
                    selected = None
                    for offset in range(len(config["items"])):
                        index = (state["cursor"] + offset) % len(config["items"])
                        item = config["items"][index]
                        if item["id"] in skipped or any(v[1]["id"] == item["id"] for v in active.values()):
                            continue
                        if now.strftime("%H:%M") < item.get("not_before_local", "00:00"):
                            continue
                        current = state["items"].get(item["id"])
                        if current and current["day"] > day:
                            raise ValueError("collection clock moved backwards")
                        due = (day if item["frequency"] == "daily" else
                               now.date().replace(day=1).isoformat() if item["frequency"] == "monthly" else
                               (now.date() - timedelta(days=now.weekday())).isoformat())
                        if current and current["settled"] and current["due"] == due:
                            continue
                        offline = item["kind"] == "cached_macro"
                        scope = item["observation_root"] if parallel else "registry"
                        budget = budgets.get(scope)
                        totals = budget_totals(budget, day) if budget else {}
                        if not offline and (totals.get("requests", 0) >= config["http_requests_per_day"]
                                or totals.get("response_bytes", 0) >= config["response_bytes_per_day"]):
                            pending_reason = "daily_resource_budget"
                            continue
                        block = {} if offline else source_block(item, blocked)
                        if block:
                            work.append(dict(id=item["id"], state="cooldown_skipped", evidence=block))
                            skipped.add(item["id"])
                            continue
                        if in_quiet_window(config, now, item["child_timeout_seconds"], item=item):
                            pending_reason = "protected_schedule_window"
                            continue
                        if time.monotonic() - started + item["child_timeout_seconds"] > config["seconds_per_run"]:
                            pending_reason = "wall_time_budget"
                            continue
                        resources = source_resources(item)
                        store = str(Path(item["observation_root"]).resolve())
                        if resources & held_sources or store in held_stores:
                            continue
                        if not current or current["settled"] or (item["kind"] == "billboard_queue" and current["due"] != due):
                            current = dict(day=day, due=due, settled=False, complete=False, attempts=0)
                            state["items"][item["id"]] = current
                        selected = index, item, current, resources, store, budget
                        break
                    if selected is None:
                        break
                    index, item, current, resources, store, budget = selected
                    state["cursor"] = (index + 1) % len(config["items"])
                    current["attempts"] += 1
                    save(state_path, state)
                    folder = state_root / "runs" / (now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                                                     + "-" + item["id"] + "-" + uuid4().hex)
                    folder.mkdir(parents=True)
                    bounded_item = dict(item)
                    if budget and item["kind"] != "cached_macro":
                        bounded_item.update(request_budget=str(budget.path), request_budget_sha256=sha(budget.path))
                    future = pool.submit(runner, bounded_item, current["day"], config["child_jobs"], folder)
                    active[future] = (index, item, current, resources, store, folder, time.monotonic())
                    held_sources.update(resources)
                    held_stores.add(store)
                    launched += 1
                    peak = max(peak, len(active))
                    pacing_until = time.monotonic() + (config["between_chunks_seconds"] if parallel else 0)
                if not active:
                    if stop or launched >= config["chunks_per_run"]:
                        break
                    if time.monotonic() < pacing_until:
                        time.sleep(min(0.05, pacing_until - time.monotonic()))
                        continue
                    reason = pending_reason
                    break
                done, _ = wait(active, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    _, item, current, resources, store, folder, began = active.pop(future)
                    held_sources.difference_update(resources)
                    held_stores.remove(store)
                    try:
                        result = future.result()
                        current.update(complete=result["complete"], settled=result["settled"],
                                       result=str(folder / "stdout.json"), error=None)
                        save(folder / "result.json", result)
                        if result.get("deferred"):
                            skipped.add(item["id"])
                    except (OSError, ValueError, RuntimeError, KeyError, TimeoutError) as error:
                        current["error"] = type(error).__name__ + ": " + str(error)
                        skipped.add(item["id"])
                        # Only an uncertain, owned-child timeout delays the source.
                        # Local parser/config/resource errors are not source refusal.
                        if isinstance(error, TimeoutError):
                            for host in item["sources"]:
                                state.setdefault("source_defer", {})[host] = dict(
                                    until_epoch=clock().timestamp() + 300,
                                    reason="owned child timeout: " + current["error"], store=str(root))
                    work.append(dict(id=item["id"], elapsed_seconds=time.monotonic()-began, **current))
                    save(state_path, state)
                    if not parallel:
                        time.sleep(config["between_chunks_seconds"])
        last_day = clock().date().isoformat()
        totals = {scope: budget_totals(budget, last_day) for scope, budget in budgets.items()}
        summary.update(actual_run_at=clock().isoformat(), reason=reason, work=work, items=state["items"],
                       usage={key: sum(v.get(key, 0) for v in totals.values()) for key in ("requests", "response_bytes")},
                       usage_by_budget=totals, state_path=str(state_path), peak_active_children=peak,
                       elapsed_seconds=time.monotonic()-started, usage_scan_seconds=usage_scan_seconds,
                       launched_chunks=launched, natural_schedule_observed=False)
        save(root / "latest.json", summary)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--child-item")
    parser.add_argument("--day")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    started = time.monotonic()
    result = (
        child(read(args.child_item), args.day, args.jobs)
        if args.child_item
        else run(args.profile, execute=args.execute)
    )
    if args.child_item:
        import resource

        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        scale = 1 if os.uname().sysname == "Darwin" else 1024
        result["execution_resources"] = dict(
            elapsed_seconds=time.monotonic()-started,
            cpu_seconds=own.ru_utime+own.ru_stime+children.ru_utime+children.ru_stime,
            max_process_rss_bytes=max(own.ru_maxrss, children.ru_maxrss)*scale,
            rss_scope="largest individual process or descendant; not sum of simultaneous RSS",
            dispatcher_sha256=sha(__file__),
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
