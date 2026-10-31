"""Offline recovery into an immutable repair batch, without editing live writers.

Source receipts and every consumed response are hash-bound. Partial index
recovery admits individual observations, never invents missing pages or proves
historical visibility. Canonical replacement requires the source writer lock.
"""

from collections import defaultdict
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import time

from ashare_data.financials import expansion as e
from ashare_data.financials import limit_expansion as limits
from ashare_data.financials import official_expansion as o
from ashare_data.financials import official_expansion as official
from ashare_data.financials import tushare_reference as ts
from ashare_data.financials.official_daily import reader, reviewed_ledger, verified_source

SCHEMA = "ad.cached-source-repair.v1"
QUERIES = ("periodic", "correction", "revision")


def read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if (path.exists() if default is not None else True) else default


@contextmanager
def writer(root):
    with (Path(root) / "writer.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class CachedOfficial:
    """No transport, retry, receipt rewrite or network fallback exists here."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.inputs = {}

    def load(self, key):
        if Path(key).name != key:
            raise ValueError("Invalid cached receipt key")
        path = self.root / "receipts" / (key + ".json")
        receipt_bytes = path.read_bytes()
        receipt = json.loads(receipt_bytes)
        source = (self.root / receipt["path"]).resolve()
        if receipt["state"] != "complete" or not source.is_relative_to(self.root):
            raise ValueError("Incomplete or escaped cached response")
        raw = gzip.decompress(source.read_bytes())
        if len(raw) != receipt["bytes"] or e.digest(raw) != receipt["sha256"]:
            raise ValueError("Cached official response checksum mismatch")
        self.inputs[key] = dict(receipt_sha256=e.digest(receipt_bytes), response_sha256=e.digest(raw))
        return json.loads(raw), receipt


def recover_index(root, symbol):
    """Recover all safe cached rows, with completeness scoped to each query."""
    e.security(symbol)
    cache = CachedOfficial(root)
    manifest = read(cache.root / "manifest.json")
    scope = read(cache.root / "scope.json")
    if e.digest((cache.root / "scope.json").read_bytes()) != manifest["scope_sha256"]:
        raise ValueError("Changed official source scope")
    instruments = {v["symbol"]: v for v in scope["instruments"]}
    if symbol not in instruments:
        raise ValueError("Requested security is outside the frozen baseline")
    identities, identity_receipt = cache.load("identity-" + symbol)
    if identity_receipt["request"] != dict(url=o.ORIGIN + "/new/information/topSearch/query",
                                          form=dict(keyWord=symbol[2:], maxNum=30), authenticated=False):
        raise ValueError("Changed cached issuer request")
    identities = [v for v in identities if v.get("code") == symbol[2:] and v.get("category") == "A股"]
    if len(identities) != 1:
        raise ValueError("Cached issuer identity missing or ambiguous")
    issuer = identities[0]
    variants, queries, rejected = defaultdict(dict), [], []
    for label in QUERIES:
        pages, ties, query_ids, duplicate_days = [], [], set(), set()
        main_seen, main_days, problems = set(), defaultdict(set), []
        pattern = re.compile(r"index-" + re.escape(symbol) + "-" + label + r"-(\d{3}|tie-\d{4}-\d{2}-\d{2})$")
        keys = sorted(p.stem for p in (cache.root / "receipts").glob("index-" + symbol + "-" + label + "-*.json"))
        for key in keys:
            match = pattern.fullmatch(key)
            if not match:
                raise ValueError("Unrecognized cached index page identity")
            if read(cache.root / "receipts" / (key + ".json"))["state"] != "complete":
                problems.append(dict(reason="uncaptured_response", receipt=key))
                continue
            payload, receipt = cache.load(key)
            suffix = match[1]
            tie_day = suffix[4:] if suffix.startswith("tie-") else None
            page = 1 if tie_day else int(suffix)
            expected = dict(pageNum=page, pageSize=30, column="sse" if symbol.startswith("sh") else "szse",
                plate="sh" if symbol.startswith("sh") else "sz", tabName="fulltext",
                stock=symbol[2:] + "," + issuer["orgId"], searchkey={"periodic": "", "correction": "更正", "revision": "修订"}[label],
                secid="", category=o.CATEGORIES if label == "periodic" else "", trade="",
                seDate=(tie_day + "~" + tie_day) if tie_day else manifest["target_start"] + "~" + manifest["target_end"],
                sortName="time", sortType="asc", isHLtitle="false")
            if receipt["request"] != dict(url=o.ORIGIN + "/new/hisAnnouncement/query", form=expected, authenticated=False):
                raise ValueError("Changed cached index request")
            count, more, rows = payload.get("totalAnnouncement"), payload.get("hasMore"), payload.get("announcements") or []
            if type(count) is not int or count < 0 or type(more) is not bool or not isinstance(rows, list):
                raise ValueError("Malformed cached pagination envelope")
            info = dict(receipt=key, page=page, total=count, has_more=more, source_rows=len(rows), ids=set(), day=tie_day)
            for ordinal, raw in enumerate(rows):
                occurrence = dict(receipt=key, ordinal=ordinal, **cache.inputs[key])
                try:
                    if not isinstance(raw, dict):
                        raise ValueError("malformed_announcement_row")
                    item = o.report_identity(raw)
                    if item["org_id"] != issuer["orgId"]:
                        raise ValueError("different_source_issuer")
                    source_symbol = symbol[:2] + item["security_code"]
                    e.security(source_symbol)
                    if tie_day and item["published_date"] != tie_day:
                        raise ValueError("row_outside_tie_date")
                    if not manifest["target_start"] <= item["published_date"] <= manifest["target_end"]:
                        raise ValueError("row_outside_requested_dates")
                    if source_symbol != symbol:
                        instrument = instruments.get(source_symbol)
                        if (not instrument or item["published_date"] < instrument["ipo"]
                                or (instrument.get("out") and item["published_date"] >= instrument["out"])):
                            raise ValueError("returned_code_lifetime_unverified")
                    item.update(source_security=e.security(source_symbol), requested_security=e.security(symbol),
                        code_binding="exact_returned_code" if source_symbol == symbol else "returned_code_in_baseline_at_publication",
                        issuer_continuity_proven=False, historical_code_intervals_merged=False)
                except (KeyError, TypeError, ValueError) as error:
                    reason = str(error)
                    rejected.append(dict(query=label, reason=reason, source=occurrence,
                        announcement_id=str(raw.get("announcementId", "")) if isinstance(raw, dict) else None))
                    problems.append(dict(reason="rejected_row", source=occurrence))
                    continue
                aid, day = item["announcement_id"], item["published_date"]
                info["ids"].add(aid)
                query_ids.add(aid)
                if not tie_day:
                    if aid in main_seen:
                        duplicate_days.add(day)
                    main_seen.add(aid)
                    main_days[day].add(aid)
                signature = e.digest(e.encoded(item))
                group = variants[aid]
                if signature not in group:
                    group[signature] = item | dict(index_receipt=key, index_observed_at=receipt["observed_at"], source_occurrences=[])
                group[signature]["source_occurrences"].append(occurrence)
            (ties if tie_day else pages).append(info)
        totals = sorted({p["total"] for p in pages})
        terminals = [p["page"] for p in pages if not p["has_more"]]
        if not pages:
            problems.append(dict(reason="query_not_captured"))
        elif len(totals) != 1:
            problems.append(dict(reason="source_count_drift", observed_totals=totals))
        if pages and (len(terminals) != 1 or sorted(p["page"] for p in pages) != list(range(1, max(terminals or [0]) + 1))):
            problems.append(dict(reason="missing_or_nonterminal_pages", captured_pages=[p["page"] for p in pages]))
        if len(totals) == 1 and sum(p["source_rows"] for p in pages) != totals[0]:
            problems.append(dict(reason="source_row_count_mismatch", observed=sum(p["source_rows"] for p in pages), declared=totals[0]))
        repairs = []
        for day in sorted(duplicate_days):
            captured = [p for p in ties if p["day"] == day]
            counts = {p["total"] for p in captured}
            union = set(main_days[day]).union(*(p["ids"] for p in captured))
            valid = len(counts) == 1 and len(union) == next(iter(counts))
            repairs.append(dict(date=day, records=len(union), observed_totals=sorted(counts),
                complete_from_cached_union=valid, receipt_keys=[p["receipt"] for p in captured]))
            if not valid:
                problems.append(dict(reason="tie_date_union_incomplete", date=day, observed=len(union), observed_totals=sorted(counts)))
        if len(totals) == 1 and len(query_ids) != totals[0]:
            problems.append(dict(reason="distinct_count_mismatch", observed=len(query_ids), declared=totals[0]))
        queries.append(dict(kind=label, pages=len(pages), records=len(query_ids), declared_totals=totals,
            tie_date_repairs=repairs, pagination_complete_for_query=not problems, gaps=problems,
            announcement_ids=sorted(query_ids), page_evidence=[{k:v for k,v in p.items() if k != "ids"} for p in pages + ties]))
    conflicts = {aid for aid, group in variants.items() if len(group) != 1}
    for query in queries:
        affected = sorted(conflicts.intersection(query["announcement_ids"]))
        if affected:
            query["gaps"].append(dict(reason="conflicting_announcement_versions", announcement_ids=affected))
            query["pagination_complete_for_query"] = False
    accepted = [next(iter(group.values())) for aid, group in sorted(variants.items()) if aid not in conflicts]
    return dict(schema="ad.recovered-official-index.v1", security=e.security(symbol), issuer=issuer,
        queries=queries, sources=cache.inputs, announcements=[r for r in accepted if r["source_security"] == e.security(symbol)],
        related_code_announcements=[r for r in accepted if r["source_security"] != e.security(symbol)],
        quarantined_rows=rejected, conflicting_versions={aid:list(variants[aid].values()) for aid in sorted(conflicts)},
        query_set_complete=all(q["pagination_complete_for_query"] for q in queries),
        captured_at=e.now(), historical_revision_inventory_complete=False, historical_pit_certified=False,
        network_requests=0, limitation="Only cached observed rows are recovered; incomplete queries and code continuity remain explicit.")


def prepare(root, financial, official, financial_tasks, official_symbols, *, retain_temporaries=False):
    """Publish an independent repair batch atomically; keep live sources untouched."""
    root, financial, official = (Path(p).resolve() for p in (root, financial, official))
    if root.exists() or root.is_relative_to(financial) or root.is_relative_to(official):
        raise ValueError("A new repair root outside source batches is required")
    stage = root.with_name(root.name + ".preparing")
    if stage.exists():
        raise ValueError("Repair staging directory already exists")
    stage.mkdir(parents=True)
    try:
        with writer(stage), e.preserve_temporaries() if retain_temporaries else nullcontext():
            _, original, source_tasks = e.load_batch(financial)
            selected = {t["id"]:t for t in source_tasks if t["id"] in financial_tasks}
            if set(selected) != set(financial_tasks) or len(set(financial_tasks)) != len(financial_tasks):
                raise ValueError("Unknown or duplicate financial repair tasks")
            for name in ("raw", "receipts", "source-receipts", "normalized", "official"):
                (stage / name).mkdir()
            os.link(financial / "universe.json", stage / "universe.json")
            tasks_raw = e.encoded(list(selected.values()))
            e.immutable(stage / "tasks.json", tasks_raw)
            manifest = original | dict(tasks_sha256=e.digest(tasks_raw), planned_tasks=len(selected),
                target_codes=len({t["symbol"] for t in selected.values()}), repair_of=str(financial),
                repair_source_manifest_sha256=e.digest((financial / "manifest.json").read_bytes()))
            e.immutable(stage / "manifest.json", e.encoded(manifest))
            repairs = []
            for tid, task in selected.items():
                if task["kind"] not in {*e.STATEMENTS, "sina_structure"}:
                    raise ValueError("Only financial statement and share-structure cache repairs are supported")
                path = financial / "receipts" / (tid + ".json")
                old = path.read_bytes()
                receipt = json.loads(old)
                if receipt["state"] != "parse_failed" or not receipt.get("raw"):
                    raise ValueError("Repair target is no longer a captured parse failure")
                e.verify_receipt(financial, receipt)
                e.immutable(stage / "source-receipts" / path.name, old)
                raw_path = (financial / receipt["raw"]["path"]).resolve()
                if not raw_path.is_relative_to(financial) or Path(receipt["raw"]["path"]).parent != Path("raw"):
                    raise ValueError("Invalid financial source path")
                os.link(raw_path, stage / receipt["raw"]["path"])
                receipt = e.normalize_saved(stage, manifest, task, receipt)
                accepted = {"complete", "empty_unknown"}
                if task["kind"] == "sina_structure":
                    accepted.add("partial_source")
                if receipt["state"] not in accepted:
                    raise ValueError("Financial cache remains unparseable: " + tid)
                e.atomic(stage / "receipts" / (tid + ".json"), e.encoded(receipt))
                repairs.append(dict(task_id=tid, source_receipt_sha256=e.digest(old),
                    source_provenance_sha256=e.digest(e.encoded({k:json.loads(old)[k] for k in ("task_id", "raw", "observed_at", "attempts")})),
                    original_error=json.loads(old).get("parse_error"),
                    original_receipt_path="source-receipts/" + path.name,
                    source_raw=receipt["raw"], normalized=receipt["normalized"], rows=receipt["row_count"], new_state=receipt["state"]))
            indexes = []
            if len(set(official_symbols)) != len(official_symbols):
                raise ValueError("Duplicate official repair symbols")
            for symbol in official_symbols:
                status_path = official / "securities" / (symbol + ".json")
                status_raw = status_path.read_bytes()
                if json.loads(status_raw).get("state") != "parse_failed":
                    raise ValueError("Official repair target is no longer a parse failure")
                index = recover_index(official, symbol)
                output = e.artifact(stage, "official/" + symbol + ".json.gz", gzip.compress(e.encoded(index), mtime=0))
                indexes.append(dict(symbol=symbol, source_status_sha256=e.digest(status_raw), index=output,
                    source_receipts=index["sources"], query_set_complete=index["query_set_complete"],
                    target_code_records=len(index["announcements"]), related_code_records=len(index["related_code_announcements"]),
                    quarantined_rows=len(index["quarantined_rows"]), conflicting_ids=len(index["conflicting_versions"]),
                    query_gaps={q["kind"]:q["gaps"] for q in index["queries"] if q["gaps"]}))
            result = dict(schema=SCHEMA, created_at=e.now(), financial_source=str(financial), official_source=str(official),
                source_manifest_sha256={"financial":e.digest((financial / "manifest.json").read_bytes()),
                                        "official":e.digest((official / "manifest.json").read_bytes())},
                financial=repairs, official=indexes, parser_sha256=e.NORMALIZER_SHA256,
                repair_code_sha256=e.digest(Path(__file__).read_bytes()), network_requests=0,
                live_sources_modified=False, source_bodies_copied=0, source_body_hardlinks=len(repairs),
                canonical_application="only_with_idle_source_writer_lock", formal_database_imported=False)
            e.immutable(stage / "repair-manifest.json", e.encoded(result))
            verify_inputs(stage)
            stage.rename(root)
        return result
    finally:
        if stage.exists() and not retain_temporaries:
            shutil.rmtree(stage)


def verify_inputs(root):
    root = Path(root).resolve()
    manifest = read(root / "repair-manifest.json")
    if manifest.get("schema") != SCHEMA:
        raise ValueError("Unknown repair manifest")
    for kind in ("financial", "official"):
        source = Path(manifest[kind + "_source"])
        if e.digest((source / "manifest.json").read_bytes()) != manifest["source_manifest_sha256"][kind]:
            raise ValueError("Repair source batch identity changed")
    financial, official = Path(manifest["financial_source"]), Path(manifest["official_source"])
    for item in manifest["financial"]:
        if item.get("original_receipt_path"):
            preserved = (root / item["original_receipt_path"]).resolve()
            if not preserved.is_relative_to(root) or e.digest(preserved.read_bytes()) != item["source_receipt_sha256"]:
                raise ValueError("Preserved source receipt changed")
        original = financial / "receipts" / (item["task_id"] + ".json")
        current = read(original)
        provenance = {k:current[k] for k in ("task_id", "raw", "observed_at", "attempts")}
        if e.digest(e.encoded(provenance)) != item["source_provenance_sha256"]:
            raise ValueError("Financial source provenance changed after recovery")
        if e.digest(original.read_bytes()) != item["source_receipt_sha256"]:
            if (current.get("normalized") or {}).get("sha256") != item["normalized"]["sha256"]:
                raise ValueError("Financial source receipt changed after recovery")
        e.verify_receipt(financial, current)
        repaired = read(root / "receipts" / (item["task_id"] + ".json"))
        if (repaired["raw"] != item["source_raw"] or repaired["normalized"] != item["normalized"]
                or {k:repaired[k] for k in provenance} != provenance):
            raise ValueError("Repaired financial provenance differs from its source")
        e.verify_receipt(root, repaired)
    for item in manifest["official"]:
        output = item["index"]
        if e.digest((root / output["path"]).read_bytes()) != output["sha256"]:
            raise ValueError("Recovered official index changed")
        cache = CachedOfficial(official)
        for key, expected in item["source_receipts"].items():
            cache.load(key)
            if cache.inputs[key] != expected:
                raise ValueError("Official source receipt changed after recovery")
    return manifest


def recovered_announcements(root, *, security=None):
    """Read recovered observations by returned code, retaining query gaps."""
    root = Path(root).resolve()
    manifest = verify_inputs(root)
    output = []
    for item in manifest["official"]:
        index = json.loads(gzip.decompress((root / item["index"]["path"]).read_bytes()))
        for row in index["announcements"] + index["related_code_announcements"]:
            if security is None or row["source_security"] == security:
                output.append(row | dict(source_queries_complete=index["query_set_complete"],
                                         query_gaps=item["query_gaps"]))
    return output


def apply_idle(root, source, *, _writer_owned=False):
    """Idempotent canonical adoption only while the caller owns the source lock."""
    root, source = Path(root).resolve(), Path(source).resolve()
    if not _writer_owned:
        try:
            with writer(source):
                return apply_idle(root, source, _writer_owned=True)
        except BlockingIOError:
            return dict(state="writer_active", source=str(source), source_modified=False)
    manifest = verify_inputs(root)
    applied, already, partial = [], [], []
    if str(source) == manifest["financial_source"]:
        for item in manifest["financial"]:
            path = source / "receipts" / (item["task_id"] + ".json")
            current = read(path)
            if (current.get("normalized") or {}).get("sha256") == item["normalized"]["sha256"]:
                already.append(item["task_id"])
                continue
            if current["state"] != "parse_failed" or e.digest(path.read_bytes()) != item["source_receipt_sha256"]:
                raise ValueError("Financial source changed before canonical repair")
            e.atomic(source / item["normalized"]["path"], (root / item["normalized"]["path"]).read_bytes())
            e.atomic(path, (root / "receipts" / path.name).read_bytes())
            applied.append(item["task_id"])
    elif str(source) == manifest["official_source"]:
        scope = read(source / "scope.json")
        for item in manifest["official"]:
            symbol = item["symbol"]
            if not item["query_set_complete"] or symbol in scope["document_symbols"] or item["related_code_records"]:
                partial.append(symbol)
                continue
            path = source / "securities" / (symbol + ".json")
            current = read(path)
            target = source / "indices" / (symbol + ".json.gz")
            index = json.loads(gzip.decompress((root / item["index"]["path"]).read_bytes()))
            index.update(schema="ad.official-report-index.v1", sources=list(index["sources"]),
                         repair_manifest_sha256=e.digest((root / "repair-manifest.json").read_bytes()))
            encoded = gzip.compress(e.encoded(index), mtime=0)
            if current.get("repair_index_sha256") == e.digest(encoded):
                if not target.exists() or e.digest(target.read_bytes()) != e.digest(encoded):
                    raise ValueError("Previously adopted repair index changed")
                already.append(symbol)
                continue
            if current["state"] != "parse_failed" or e.digest(path.read_bytes()) != item["source_status_sha256"]:
                raise ValueError("Official status changed before canonical repair")
            if target.exists() and e.digest(target.read_bytes()) != e.digest(encoded):
                raise ValueError("Refusing to overwrite an existing official index")
            e.atomic(target, encoded)
            e.atomic(path, e.encoded(dict(state="complete", symbol=symbol, announcements=len(index["announcements"]),
                selected_documents=0, updated_at=e.now(), repair_index_sha256=e.digest(encoded),
                prior_parse_error=current.get("reason"), repair_manifest_sha256=index["repair_manifest_sha256"])))
            applied.append(symbol)
    else:
        raise ValueError("Repair bundle does not belong to this source")
    return dict(state="applied_with_explicit_partial_recovery", source=str(source), applied=applied,
                already_applied=already, partial_indexes_available=partial, source_modified=bool(applied), network_requests=0)


FINISHED = "source_pass_finished_with_explicit_gaps"
HANDOFF_SCHEMA = "ad.reference-handoff-stop.v1"
TERMINAL_FOLLOWUP = {"verified_with_explicit_gaps", "integrity_failed", "followup_failed",
                     "user_stop_preserved", "source_blocked", "source_stopped", "source_exited_incomplete",
                     "dependency_ended_before_start"}


@contextmanager
def idle_writer(root):
    """Check an existing advisory lock without ever removing it."""
    with (root / "writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def lock_held(path):
    if not path.exists():
        return False
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def handoff_evidence(tushare_root, legacy_root):
    """Observe two ordinary batch results, never issue a validation request.

    Earliest and latest planned sessions establish bounded source usability,
    not complete history. The bulk legacy plan is paused; pending unique fields
    and missing dates remain explicit and resumable.
    """
    root = Path(tushare_root)
    finished = all(read(root / name / "supervisor.json", {}).get("state") == FINISHED
                   for name in ("calendar", "limits", "capped-targets"))
    if (not lock_held(root / "worker.lock") and not finished) or (root / "STOP").exists():
        return None
    target = root / "limits"
    if not (target / "manifest.json").exists():
        return None
    _, manifest, tasks = e.load_batch(target)
    if not tasks:
        return None
    _, prior, _ = e.load_batch(legacy_root)
    if prior["source_families"] != [limits.KIND] or manifest.get("source") != "tushare":
        raise ValueError("Handoff requires explicit Tushare and zzshare reference batches")
    if any(manifest[k] != prior[k] for k in ("universe_sha256", "target_start", "target_end")):
        raise ValueError("Handoff universe or historical bounds differ")
    host = read(target / "source-state.json", {}).get("api.tushare.pro", {})
    if host.get("blocked") or host.get("consecutive_errors") or host.get("cooldown_until", 0) > time.time():
        return None
    progress = read(target / "progress.json", {})
    if not finished and (not progress.get("updated_at") or
            (datetime.now(timezone.utc) - datetime.fromisoformat(progress["updated_at"])).total_seconds() > 600):
        return None
    if read(root / "calendar/supervisor.json", {}).get("state") != FINISHED:
        return None
    universe = read(target / "universe.json")
    evidence = []
    for task in {t["id"]: t for t in (min(tasks, key=lambda t: t["start"]), max(tasks, key=lambda t: t["start"]))}.values():
        path = target / "receipts" / (task["id"] + ".json")
        receipt = read(path, {})
        if receipt.get("state") not in {"complete", "partial_source"}:
            return None
        e.verify_receipt(target, receipt)
        rows, coverage = ts.limit_rows(gzip.decompress((target / receipt["raw"]["path"]).read_bytes()), task, universe)
        usable = [r for r in rows if all(Decimal(r["fields"][f]["value"] or "0") > 0 for f in ("up_limit", "down_limit"))]
        if {r["security"].split(".")[1] for r in usable} != {"XSHG", "XSHE"}:
            return None
        evidence.append(dict(task_id=task["id"], date=task["start"], usable_rows=len(usable),
                             receipt_sha256=e.digest(path.read_bytes()), coverage=coverage))
    return dict(tushare_batch=str(root), observed_at=e.now(), ordinary_request_evidence=evidence,
                complete_history_asserted=False, original_sources_preserved=True,
                unresolved_families=["ST", "suspension", "adjustment_factor", "missing_limit_dates_or_codes"],
                policy="pause duplicate bulk; reuse existing daily status/factors; derive explicit gap requests before any legacy resume")


def request_handoff(tushare_root, legacy_root, record):
    """Write an owned STOP once; never replace an existing user stop instruction."""
    legacy_root, record = Path(legacy_root), Path(record)
    stop = legacy_root / "STOP"
    if stop.exists():
        existing = read(record, {})
        return existing if owned_handoff(legacy_root, record) else {"state": "existing_stop_preserved"}
    if read(record, {}).get("state") == "stop_requested":
        return {"state": "owned_stop_removed; no_recreation"}
    if generic_ready(legacy_root):
        return {"state": "legacy_already_finished; no_stop_needed"}
    proof = handoff_evidence(tushare_root, legacy_root)
    if proof is None:
        return {"state": "waiting_for_ordinary_tushare_results"}
    payload = dict(schema=HANDOFF_SCHEMA, at=e.now(), legacy_batch=str(legacy_root), evidence=proof)
    body = e.encoded(payload)
    e.atomic(record, e.encoded(dict(state="stop_requested", stop_sha256=e.digest(body), **payload)))
    try:
        with stop.open("xb") as output:
            output.write(body)
    except FileExistsError:
        return {"state": "existing_stop_preserved"}
    return read(record)


def owned_handoff(root, record):
    proof = read(record, {})
    stop = root / "STOP"
    return (proof.get("schema") == HANDOFF_SCHEMA and proof.get("legacy_batch") == str(root)
            and stop.exists() and e.digest(stop.read_bytes()) == proof.get("stop_sha256"))


def generic_ready(root, *, handoff_record=None):
    supervisor, exited = read(root / "supervisor.json", {}), read(root / "worker-exit.json", {})
    progress = read(root / "progress.json", {})
    if not exited.get("at") or exited.get("pid") != progress.get("pid"):
        return False
    if exited["at"] < progress.get("updated_at", "") or supervisor.get("at", "") < exited["at"]:
        return False
    if (root / "STOP").exists():
        return bool(handoff_record and owned_handoff(root, handoff_record) and supervisor.get("state") in {"stopped", FINISHED})
    return supervisor.get("state") == FINISHED and not exited.get("stop_requested")


def official_ready(root):
    exited, progress = read(root / "worker-exit.json", {}), read(root / "progress.json", {})
    if ((root / "STOP").exists() or exited.get("stop_requested") or not exited.get("at")
            or exited.get("pid") != progress.get("pid") or exited["at"] < progress.get("updated_at", "")):
        return False
    scope = read(root / "scope.json")
    return all(read(root / "securities" / (symbol + ".json"), {}).get("state") in
               {"complete", "parse_failed", "source_exhausted"} for symbol in scope["symbols"])


def official_integrity(root):
    verified, errors = 0, []
    for path in (root / "receipts").glob("*.json"):
        receipt = read(path)
        if receipt.get("state") != "complete":
            continue
        try:
            verified_source(root, path.stem)
            verified += 1
        except (ValueError, OSError) as error:
            errors.append(dict(receipt=path.stem, error=str(error)))
    review_path = root / "field-reviews.json"
    if review_path.exists():
        reviewed_ledger(root, read(review_path))
    return dict(verified_originals=verified, integrity_errors=errors, coverage=official.audit(root),
                source_gaps={p.stem: read(p) for p in (root / "securities").glob("*.json")
                             if read(p).get("state") != "complete"})


def follow_batch(root, kind, *, handoff_record=None, cache_repair=None):
    """No source request, no restart, no all-captured derivative rewrite."""
    root = Path(root).resolve()
    if (root / "STOP").exists() and not (handoff_record and owned_handoff(root, handoff_record)):
        return {"state": "user_stop_preserved"}
    with idle_writer(root) as idle:
        if not idle:
            return {"state": "writer_active"}
        ready = official_ready(root) if kind == "official" else generic_ready(root, handoff_record=handoff_record)
        if not ready:
            return {"state": "waiting_for_natural_source_boundary"}
        repaired = None
        if cache_repair:
            repair_root = Path(cache_repair["root"])
            if e.digest((repair_root / "repair-manifest.json").read_bytes()) != cache_repair["manifest_sha256"]:
                raise ValueError("Cache repair manifest changed before adoption")
            repaired = apply_idle(repair_root, root, _writer_owned=True)
        if kind == "official":
            result = official_integrity(root)
            return dict(state="verified_with_explicit_gaps" if not result["integrity_errors"] else "integrity_failed",
                        cache_repair=repaired, result=result)
        before = e.audit(root)
        if before["pending_or_retryable"] and not (handoff_record and owned_handoff(root, handoff_record)):
            return {"state": "waiting_for_pending_requests"}
    # replay acquires the same non-blocking writer lock itself; an intervening
    # writer causes a safe retry, never concurrent mutation.
    try:
        result = e.replay(root, normalizer=limits.normalize_saved if kind == "limits" else None)
    except BlockingIOError:
        return {"state": "writer_active"}
    return dict(state="verified_with_explicit_gaps" if not result["integrity_errors"] else "integrity_failed",
                network_requests=0, before=before, result=result,
                cache_repair=repaired,
                legacy_pending_preserved=bool(result["pending_or_retryable"]))


def validate_daily(spec):
    root = Path(spec["official_root"])
    review = Path(spec["review"])
    if e.digest(review.read_bytes()) != spec["review_sha256"]:
        raise ValueError("Field review changed; do not inherit earlier query expectations")
    correction = read(Path(spec["correction_reviews"])) if spec.get("correction_reviews") else None
    view = reader(root, read(review), reference_root=spec["reference_root"], reference_tasks=spec["reference_tasks"],
                  correction_reviews=correction)
    results = []
    for case in spec["queries"]:
        query = view.query(**case["query"])
        rows = query["rows"]
        actual = [{k: row.get(k) for k in ("status", "value")} for row in rows]
        results.append(dict(query=case["query"], actual=actual, expected=case["expected"], matched=actual == case["expected"]))
    return dict(at=e.now(), source="real_captured_official_documents", queries=results,
                all_matched=all(r["matched"] for r in results), network_requests=0, database_imported=False)


def tick(spec, state, output):
    output = Path(output)
    result = dict(state)
    handoff = output / "handoff.json"
    if spec.get("handoff"):
        config = spec["handoff"]
        result["handoff"] = request_handoff(config["tushare_root"], config["legacy_root"], handoff)
        tsroot = Path(config["tushare_root"])
        worker = read(tsroot / "worker.json", {})
        final = read(tsroot / "completion.json", {})
        if final.get("pid") != worker.get("pid") or final.get("at", "") < worker.get("started_at", ""):
            final = {}
        result["tushare_runtime"] = dict(worker_metadata_exists=(tsroot / "worker.json").exists(),
            worker_active=lock_held(tsroot / "worker.lock"), credential_values_read=False,
            progress=read(tsroot / "progress.json", {}), final=final,
            phases={name: dict(progress=read(tsroot / name / "progress.json", {}),
                               supervisor=read(tsroot / name / "supervisor.json", {}))
                    for name in ("calendar", "limits", "capped-targets", "fundamentals")})
    jobs = result.setdefault("jobs", {})
    transitioned = False
    for job in spec["batches"]:
        prior = jobs.get(job["id"], {})
        if prior.get("state") in TERMINAL_FOLLOWUP:
            continue
        try:
            current = follow_batch(job["root"], job["kind"], handoff_record=handoff if job["kind"] == "limits" else None,
                                   cache_repair=job.get("cache_repair"))
        except Exception as error:
            current = dict(state="followup_failed", error=f"{type(error).__name__}: {error}"[:500])
        if spec.get("finish_policy") == "current_batch_only" and current["state"] in {"waiting_for_natural_source_boundary", "waiting_for_pending_requests"}:
            current = classify_inactive(job, jobs) or current
        jobs[job["id"]] = dict(at=e.now(), **current)
        transitioned |= current["state"] == "verified_with_explicit_gaps"
    if spec.get("daily_validation") and ("daily_validation" not in result or transitioned):
        try:
            validation = validate_daily(spec["daily_validation"])
            e.atomic(output / "historical-query-validation.json", e.encoded(validation))
            result["daily_validation"] = dict(at=validation["at"], all_matched=validation["all_matched"], queries=len(validation["queries"]))
        except Exception as error:
            result["daily_validation"] = dict(at=e.now(), all_matched=False, error=f"{type(error).__name__}: {error}"[:500])
    if spec.get("gap_preview") and "closed_source_gap_preview" not in result:
        config = spec["gap_preview"]
        if jobs.get(config["after"], {}).get("state") == "verified_with_explicit_gaps":
            from ashare_data.financials import tushare_gaps
            try:
                preview = tushare_gaps.preview(config["baseline"], config["root"], config.get("factor_inventory"))
                result["closed_source_gap_preview"] = dict(state="prepared_offline", at=preview["at"],
                    requests_by_api=preview["requests_by_api"], summary=str(Path(config["root"]) / "preview-summary.json"))
            except Exception as error:
                result["closed_source_gap_preview"] = dict(state="failed", error=f"{type(error).__name__}: {error}"[:300])
    result.update(at=e.now(), schema="ad.reference-followup-state.v1", no_network_requests=True)
    if spec.get("finish_policy") == "current_batch_only":
        result["completion"] = completion(spec, result)
    e.atomic(output / "progress.json", e.encoded(result))
    return result


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def classify_inactive(job, jobs):
    root = Path(job["root"])
    progress, supervisor = read(root / "progress.json", {}), read(root / "supervisor.json", {})
    if (lock_held(root / "writer.lock") or process_alive(progress.get("pid"))
            or process_alive(supervisor.get("pid")) or process_alive(job.get("owner_pid"))):
        return None  # Includes an existing collector's cooldown, not just its write section.
    dependency = jobs.get(job.get("after"), {})
    if job.get("after") and dependency.get("state") not in TERMINAL_FOLLOWUP:
        return None
    state = supervisor.get("state")
    if state == "remaining_sources_blocked_access_or_payment":
        return dict(state="source_blocked", reason=state, source_state=read(root / "source-state.json", {}))
    if (root / "STOP").exists() or read(root / "worker-exit.json", {}).get("stop_requested"):
        return dict(state="source_stopped", reason="existing_collector_stopped; no_restart")
    if job.get("after") and not progress.get("pid"):
        return dict(state="dependency_ended_before_start", reason="sequential_source_not_started; no_restart")
    if progress.get("pid"):
        return dict(state="source_exited_incomplete", reason="source_process_exited_without_completed_boundary")
    return None


def completion(spec, state):
    jobs = state.get("jobs", {})
    pending = [job["id"] for job in spec["batches"] if jobs.get(job["id"], {}).get("state") not in TERMINAL_FOLLOWUP]
    # Do not exit while a collector still holds its writer lock, including STOP drain.
    active = [job["id"] for job in spec["batches"] if lock_held(Path(job["root"]) / "writer.lock") or process_alive(job.get("owner_pid"))]
    runtime = state.get("tushare_runtime", {})
    if pending or active or runtime.get("worker_active"):
        return dict(exit_ready=False, pending_jobs=pending, active_writers=active,
                    tushare_active=runtime.get("worker_active", False))
    tsstate = (runtime.get("final", {}).get("state") or
               ("credentials_pending_not_started" if not runtime.get("worker_metadata_exists") else "worker_exited_without_final_receipt"))
    return dict(exit_ready=True, state="current_batch_closed_with_explicit_gaps", tushare_state=tsstate,
                jobs={name: job["state"] for name, job in jobs.items()}, full_history_complete=False,
                stop_condition="all_current_source_writers_closed_and_tushare_inactive",
                future_credentials_watched=False, source_collectors_restarted=False, system_service_installed=False)
