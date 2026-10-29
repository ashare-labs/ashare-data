"""Serial daily dispatch of frozen collectors; preview never installs a timer.

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
    if config.get("schema") != "ad.daily-update-registry.v1":
        raise ValueError("unsupported daily registry")
    scope = Path(config["isolated_workspace"]).resolve()
    root = Path(config["root"]).resolve()
    if not root.is_relative_to(scope) or any(
        (root / n).exists() for n in ("CURRENT", "CURRENT.json", "releases")
    ):
        raise ValueError("registry must remain in its isolated workspace")
    if sha(__file__) != config["dispatcher_sha256"]:
        raise ValueError("dispatcher differs from reviewed version")
    if not 1 <= config["chunks_per_run"] <= 500 or not 60 <= config["seconds_per_run"] <= 3600:
        raise ValueError("invalid daily run budget")
    if not 1 <= config["http_requests_per_day"] <= 1000 or not 1 <= config["child_jobs"] <= 5:
        raise ValueError("invalid request/job budget")
    if not 1 <= config["between_chunks_seconds"] <= 30:
        raise ValueError("shared serial pacing must be 1..30 seconds")
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
        if item["kind"] == "manual" and profile["max_jobs"] != 1:
            raise ValueError("manual child must be a single bounded attempt")
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
        if not 30 <= item["child_timeout_seconds"] <= 420:
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


def blocked_sources(config, now):
    """Read every store, including protected existing collectors, before dispatch.

    A cooldown is propagated by skipping a source family, never by moving a
    request to another hostname. Financial persistent refusals remain blocked.
    """
    blocked = {}
    roots = {i["observation_root"] for i in config["items"]} | set(
        config.get("cooldown_observation_roots", [])
    )
    for root in roots:
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


def in_quiet_window(config, now, seconds=0):
    end = now + timedelta(seconds=seconds)
    for bounds in config.get("quiet_local_windows", []):
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
        result = run_billboard_queue(item["profile"], execute=True, as_of=day)
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


def run(profile, *, execute=False, clock=None, runner=invoke, usage_reader=usage):
    config = checked(profile)
    clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
    root = Path(config["root"])
    summary = dict(
        dry_run=not execute,
        profile=str(Path(profile).resolve()),
        menu_rows=sorted({r for i in config["items"] for r in i["rows"]}),
        configured_profiles=len(config["items"]),
        serial_workers=1,
        timer_installed=False,
        formal_published=False,
    )
    if not execute:
        return summary
    root.mkdir(parents=True, exist_ok=True)
    with (root / "registry.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("daily registry already active") from None
        frozen = root / "profile.json"
        if frozen.exists() and read(frozen) != config:
            raise ValueError("existing registry configuration is immutable")
        save(frozen, config)
        from ashare_data.providers.free_update_budget import RequestBudget
        budget_path = root / "request-budget-config.json"
        budget_config = dict(schema="ad.public-request-budget.v1",
            requests_per_day=config["http_requests_per_day"],
            response_bytes_per_day=config["response_bytes_per_day"])
        if budget_path.exists() and read(budget_path) != budget_config:
            raise ValueError("existing request budget is immutable")
        save(budget_path, budget_config)
        admission = RequestBudget(budget_path)
        state_path = root / "state.json"
        state = (
            read(state_path) if state_path.exists() else dict(cursor=0, items={}, source_defer={})
        )
        started = time.monotonic()
        work, reason = [], "chunk_budget"
        skipped = set()
        for _ in range(config["chunks_per_run"]):
            now = clock()
            day = now.date().isoformat()
            consumed = usage_reader(config, day)
            consumed = admission.reconcile(day, consumed)
            if (root / "STOP").exists():
                reason = "stop_requested"
                break
            exhausted = (
                consumed["requests"] >= config["http_requests_per_day"]
                or consumed["response_bytes"] >= config["response_bytes_per_day"]
            )
            if shutil.disk_usage(root).free < config["minimum_free_bytes"]:
                reason = "low_free_disk"
                break
            blocked = blocked_sources(config, now)
            for host, info in state.get("source_defer", {}).items():
                if info["until_epoch"] > now.timestamp():
                    blocked[host] = info
            selected = None
            for offset in range(len(config["items"])):
                index = (state["cursor"] + offset) % len(config["items"])
                item = config["items"][index]
                if item["id"] in skipped:
                    continue
                if now.strftime("%H:%M") < item.get("not_before_local", "00:00"):
                    continue
                offline = item["kind"] == "cached_macro"
                if exhausted and not offline:
                    continue
                current = state["items"].get(item["id"])
                if current and current["day"] > day:
                    raise ValueError("collection clock moved backwards")
                due = (day if item["frequency"] == "daily" else
                       now.date().replace(day=1).isoformat() if item["frequency"] == "monthly" else
                       (now.date() - timedelta(days=now.weekday())).isoformat())
                if current and current["settled"] and current["due"] == due:
                    continue
                block = {} if offline else source_block(item, blocked)
                if block:
                    work.append(dict(id=item["id"], state="cooldown_skipped", evidence=block))
                    skipped.add(item["id"])
                    continue
                if not current or current["settled"] or (item["kind"] == "billboard_queue" and current["due"] != due):
                    current = dict(day=day, due=due, settled=False, complete=False, attempts=0)
                    state["items"][item["id"]] = current
                selected = (index, item, current)
                break
            if selected is None:
                reason = "daily_resource_budget" if exhausted else "no_due_unblocked_work"
                break
            index, item, current = selected
            if in_quiet_window(config, now, item["child_timeout_seconds"]):
                reason = "protected_schedule_window"
                break
            if (
                time.monotonic() - started + item["child_timeout_seconds"]
                > config["seconds_per_run"]
            ):
                reason = "wall_time_budget"
                break
            state["cursor"] = (index + 1) % len(config["items"])
            current["attempts"] += 1
            save(state_path, state)
            folder = (
                root
                / "runs"
                / (
                    now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                    + "-"
                    + item["id"]
                    + "-"
                    + uuid4().hex
                )
            )
            folder.mkdir(parents=True)
            try:
                bounded_item = dict(item, request_budget=str(budget_path),
                    request_budget_sha256=sha(budget_path))
                result = runner(bounded_item, current["day"], config["child_jobs"], folder)
                current.update(
                    complete=result["complete"],
                    settled=result["settled"],
                    result=str(folder / "stdout.json"),
                    error=None,
                )
                save(folder / "result.json", result)
                if result.get("deferred"):
                    skipped.add(item["id"])
            except (OSError, ValueError, RuntimeError, KeyError, TimeoutError) as error:
                current["error"] = type(error).__name__ + ": " + str(error)
                # Do not repeatedly launch a failed child in the same invocation.
                skipped.add(item["id"])
                for host in item["sources"]:
                    state.setdefault("source_defer", {})[host] = dict(
                        until_epoch=clock().timestamp() + 300,
                        reason=current["error"],
                        store=str(root),
                    )
            work.append(dict(id=item["id"], **current))
            save(state_path, state)
            time.sleep(config["between_chunks_seconds"])
        summary.update(
            actual_run_at=clock().isoformat(),
            reason=reason,
            work=work,
            items=state["items"],
            usage=admission.reconcile(clock().date().isoformat(),
                                      usage_reader(config, clock().date().isoformat())),
            natural_schedule_observed=False,
        )
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
    result = (
        child(read(args.child_item), args.day, args.jobs)
        if args.child_item
        else run(args.profile, execute=args.execute)
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
