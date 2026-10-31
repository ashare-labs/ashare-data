"""Account-authorized Tushare reference collection, with in-memory credentials only.

Public request identities exclude the credential. A user-supplied runtime reference
or local hidden prompt supplies it; this module never searches credential stores.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys as _sys
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ashare_data.financials import expansion as e

ts = f = _sys.modules[__name__]

ENDPOINT = "https://api.tushare.pro"
FIELDS = {
    "trade_cal": "exchange,cal_date,is_open,pretrade_date",
    "stk_limit": "trade_date,ts_code,pre_close,up_limit,down_limit,asset_type,exchange",
}
FINANCIAL_ID = "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,end_type,update_flag"
FIELDS.update({
    "income": FINANCIAL_ID + ",total_revenue,revenue,n_income,n_income_attr_p,basic_eps,diluted_eps",
    "balancesheet": FINANCIAL_ID + ",total_assets,total_liab,total_hldr_eqy_exc_min_int,total_share",
    "cashflow": FINANCIAL_ID + ",n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,c_cash_equ_end_period",
    "fina_indicator": "ts_code,ann_date,end_date,update_flag,eps,roe,roa,current_ratio,quick_ratio,netprofit_margin,debt_to_assets",
    "dividend": "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,imp_ann_date,base_date,base_share",
    "adj_factor": "ts_code,trade_date,adj_factor",
})
ROW_CAP = 5800
MAX_BODY = 16 * 1024 * 1024


def request_url(api, params, fields=None):
    if api not in FIELDS:
        raise ValueError("Unapproved Tushare API")
    query = {"api_name": api, **params}
    if fields is not None:
        query["fields"] = fields if isinstance(fields, str) else ",".join(fields)
    url = ENDPOINT + "/?" + urlencode(query)
    request_identity(url)
    return url


def request_fields(url):
    values = parse_qs(urlparse(url).query, keep_blank_values=True)
    api = values.get("api_name", [None])[0]
    selected = values.get("fields", [FIELDS.get(api, "")])[0].split(",")
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= set(FIELDS.get(api, "").split(",")):
        raise ValueError("Unapproved or duplicate fields")
    return ",".join(selected)


def request_identity(url):
    p = urlparse(url)
    if p.scheme != "https" or p.netloc != "api.tushare.pro" or p.path != "/" or p.fragment:
        raise ValueError("Only the official TLS endpoint is allowed")
    values = parse_qs(p.query, keep_blank_values=True)
    if any(len(v) != 1 for v in values.values()):
        raise ValueError("Duplicate request parameter")
    params = {k: v[0] for k, v in values.items()}
    api = params.pop("api_name", None)
    params.pop("fields", None)
    request_fields(url)
    if api == "trade_cal":
        if set(params) != {"exchange", "start_date", "end_date"} or params["exchange"] not in {"SSE", "SZSE"}:
            raise ValueError("Explicit exchange and bounded dates required")
        a, b = e.day(params["start_date"]), e.day(params["end_date"])
        if not 0 <= (date.fromisoformat(b) - date.fromisoformat(a)).days <= 365:
            raise ValueError("Calendar window must fit one year")
    elif api == "stk_limit":
        if set(params) not in ({"trade_date"}, {"trade_date", "ts_code"}):
            raise ValueError("Limit queries must bind one trading day")
        e.day(params["trade_date"])
        if "ts_code" in params and not re.fullmatch(r"\d{6}\.(SH|SZ)", params["ts_code"]):
            raise ValueError("Invalid target code")
    elif api in {"income", "balancesheet", "cashflow", "fina_indicator", "dividend", "adj_factor"}:
        validate_params(api, params)
    else:
        raise ValueError("Unapproved Tushare API")
    return api, params


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Transport:
    """The secret is private runtime memory, never a request URL or log field."""

    def __init__(self, token, opener=None):
        if not isinstance(token, str) or len(token.strip()) < 16:
            raise ValueError("Tushare credential is absent or invalid; configure locally")
        self.__token = token.strip()
        self._opener = opener or build_opener(NoRedirect())

    def __repr__(self):
        return "TushareTransport(credential=[redacted])"

    def __call__(self, url):
        api, params = request_identity(url)
        body = e.encoded(dict(api_name=api, params=params, fields=request_fields(url), token=self.__token))
        req = Request(ENDPOINT, body, {"Content-Type": "application/json", "Accept": "application/json",
                                     "User-Agent": "ashare-local-research/1.0"}, method="POST")
        try:
            with self._opener.open(req, timeout=45) as response:
                status, headers, raw = response.status, dict(response.headers), response.read(MAX_BODY + 1)
        except HTTPError as error:
            status, headers, raw = error.code, dict(error.headers), error.read(4096)
        except Exception:
            # Do not allow transport-library exceptions to echo authenticated bodies.
            raise RuntimeError("Tushare transport failed; request body omitted") from None
        if len(raw) > MAX_BODY:
            raise ValueError("Tushare response exceeds bounded body size")
        if self.__token.encode() in raw:
            raise ValueError("Credential echo detected; response not retained")
        if status == 200:
            payload = json.loads(raw)
            code = payload.get("code")
            if code != 0:
                message = str(payload.get("msg", ""))
                if re.search(r"每分钟|每小时|每天|频次|访问.*次数|rate.?limit|frequency", message, re.I):
                    effective = 429
                elif code == 2002 or re.search(r"权限|积分|token|认证|账号", message, re.I):
                    effective = 403
                else:
                    effective = 502
                return effective, raw, headers | {"_transport_status": status, "_provider_status": code}
        return status, raw, headers


def table(raw):
    payload = json.loads(raw, parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise ValueError("Unsuccessful Tushare envelope")
    fields, items = payload["data"].get("fields"), payload["data"].get("items")
    if (not isinstance(fields, list) or len(fields) != len(set(fields))
            or not all(isinstance(x, str) for x in fields) or not isinstance(items, list)):
        raise ValueError("Malformed Tushare table")
    if any(not isinstance(r, list) or len(r) != len(fields) for r in items):
        raise ValueError("Tushare row width mismatch")
    return [dict(zip(fields, r, strict=True)) for r in items]


def calendar_rows(raw, task):
    api, params = request_identity(task["url"])
    if api != "trade_cal":
        raise ValueError("Calendar task required")
    seen, rows = set(), []
    for i, r in enumerate(table(raw)):
        day = e.day(r["cal_date"])
        if r["exchange"] != params["exchange"] or not task["start"] <= day <= task["end"] or day in seen:
            raise ValueError("Calendar identity, range or duplicate mismatch")
        if str(r["is_open"]) not in {"0", "1"}:
            raise ValueError("Unknown calendar status")
        seen.add(day)
        rows.append(dict(schema="ad.tushare-calendar-observation.v1", exchange=r["exchange"],
                         date=day, is_open=str(r["is_open"]) == "1", pretrade_date=r.get("pretrade_date"),
                         source_locator=f"data/items/{i}", source="tushare", absence_proved=False))
    a, b = date.fromisoformat(task["start"]), date.fromisoformat(task["end"])
    expected = {(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)}
    return rows, {"missing_calendar_dates": sorted(expected - seen), "source_rows": len(rows)}


def active_codes(universe, day):
    """Use listing range AND separately frozen trading-code validity, if known."""
    # New collection plans also honor the already frozen source code-change evidence.
    starts, ends = {}, {}
    for event in universe.get('code_events', []):
        if not event.get('receipt') or not event.get('body'):
            continue
        body = json.loads(event['body'])
        if any(body.get(k) != event.get(k) for k in ('old_symbol','new_symbol','effective_date','receipt')):
            raise ValueError('Frozen code-event evidence differs')
        starts[event['new_symbol']] = event['effective_date']
        ends[event['old_symbol']] = event['effective_date']
    return {r['symbol'][2:] + ('.SH' if r['symbol'].startswith('sh') else '.SZ')
            for r in universe['instruments']
            if r['ipo'] <= day and (not r['out'] or day < r['out'])
            and (not (r.get('code_valid_from') or starts.get(r['symbol'])) or (r.get('code_valid_from') or starts[r['symbol']]) <= day)
            and (not (r.get('code_valid_to') or ends.get(r['symbol'])) or day < (r.get('code_valid_to') or ends[r['symbol']]))}


def with_identifier_periods(universe, release):
    """Freeze exchange-code intervals without confusing them with source aliases."""
    from copy import deepcopy
    value = deepcopy(universe)
    with release.metadata() as db:
        intervals = {row[0]: row[1:] for row in db.execute(
            'SELECT security,valid_from,valid_to,validity_precision FROM release_identifiers')}
    for item in value['instruments']:
        security = e.security(item['symbol'])
        if security in intervals:
            start, end, precision = intervals[security]
            item.update(code_valid_from=start, code_valid_to=end, code_validity_precision=precision)
    value['identifier_release'] = release.receipt()
    return value


def limit_rows(raw, task, universe):
    _, params = request_identity(task["url"])
    day = e.day(params["trade_date"])
    expected = active_codes(universe, day)
    # Historical observations may be backfilled under a newer source file/code.
    # Keep those source observations while using trading-code periods as denominator.
    source_targets = {r['symbol'][2:] + ('.SH' if r['symbol'].startswith('sh') else '.SZ')
                      for r in universe['instruments'] if r['ipo'] <= day and (not r['out'] or day < r['out'])}
    if params.get("ts_code"):
        expected &= {params["ts_code"]}
    source, seen, rows = table(raw), set(), []
    for i, r in enumerate(source):
        code = r["ts_code"]
        if e.day(r["trade_date"]) != day or code in seen or (params.get("ts_code") and code != params["ts_code"]):
            raise ValueError("Limit date/code/duplicate mismatch")
        seen.add(code)
        if code not in source_targets:
            continue
        if r.get("asset_type") not in {None, "", "STK"}:
            raise ValueError("Target security returned non-stock asset type")
        exchange = "SSE" if code.endswith(".SH") else "SZSE"
        if r.get("exchange") not in {None, "", exchange}:
            raise ValueError("Target exchange mismatch")
        fields = {}
        for name in ("pre_close", "up_limit", "down_limit"):
            value = r.get(name)
            if value is None:
                state, normalized = "source_null_or_missing", None
            else:
                number = Decimal(str(value))
                if not number.is_finite() or number < 0:
                    raise ValueError("Invalid limit value")
                state = "source_positive_price" if number > 0 else "zero_semantics_unverified"
                normalized = format(number, "f")
            fields[name] = dict(value=normalized, state=state, unit="CNY/share")
        hi, lo = fields["up_limit"]["value"], fields["down_limit"]["value"]
        if hi and lo and Decimal(hi) > 0 and Decimal(lo) > Decimal(hi):
            raise ValueError("Inverted limit pair")
        rows.append(dict(schema="ad.tushare-limit-observation.v1", security=code[:6] + (".XSHG" if code.endswith("SH") else ".XSHE"),
                         trade_date=day, fields=fields, source="tushare", source_locator=f"data/items/{i}",
                         source_revision_id=e.digest(e.encoded({k: str(v) if isinstance(v, Decimal) else v for k, v in r.items()})),
                         classification="source_claimed", official_limit_certified=False,
                         no_price_limit_inferred=False, historical_available_at=None))
    return rows, dict(source_rows=len(source), target_rows=len(rows), expected_active_codes=len(expected),
                      missing_target_codes=sorted(expected - seen), row_cap_reached=len(source) >= ROW_CAP,
                      target_all_codes_returned=expected <= seen, full_market_complete=False,
                      absence_proved=False, missing_is_not_no_limit_or_suspension=True)


def normalize(root, manifest, task, receipt, raw=None):
    if raw is None:
        e.verify_receipt(root, receipt)
        raw = gzip.decompress((root / receipt["raw"]["path"]).read_bytes())
    try:
        if task["kind"] == "tushare_trade_cal":
            rows, coverage = calendar_rows(raw, task)
            partial = bool(coverage["missing_calendar_dates"])
        elif task["kind"] == "tushare_stk_limit":
            rows, coverage = limit_rows(raw, task, json.loads((root / "universe.json").read_text()))
            partial = coverage["row_cap_reached"]
        elif receipt.get("source_reuse"):
            rows, coverage = ts.normalize_reused(raw, task, receipt)
            receipt["coalescing_normalizer_sha256"] = e.digest(Path(ts.__file__).read_bytes())
            receipt["fundamentals_normalizer_sha256"] = e.digest(Path(ts.f.__file__).read_bytes())
            partial = False
        else:
            rows, coverage = f.observations(raw, task)
            partial = coverage["row_cap_reached"]
            receipt["fundamentals_normalizer_sha256"] = e.digest(Path(f.__file__).read_bytes())
        for row in rows:
            row.update(observed_at=receipt["observed_at"], response_sha256=e.digest(raw),
                       task_id=task["id"], historical_pit_certified=False)
        receipt.update(normalized=e.artifact(root, "normalized/" + task["id"] + ".jsonl.gz",
                       gzip.compress(b"".join(e.encoded(r) for r in rows), mtime=0)),
                       row_count=len(rows), coverage=coverage,
                       state="partial_source" if partial else "complete" if rows else "empty_unknown",
                       normalizer_sha256=e.digest(Path(__file__).read_bytes()))
    except Exception as error:
        receipt.update(state="parse_failed", parse_error=f"{type(error).__name__}: {error}"[:300])


def _prepare(root, baseline, tasks, extra, *, market_release=None):
    root, baseline = Path(root).resolve(), Path(baseline).resolve()
    _, original, _ = e.load_batch(baseline)
    if root == baseline:
        raise ValueError("Independent source partition required")
    raw = (baseline / "universe.json").read_bytes()
    if market_release is not None:
        raw = e.encoded(with_identifier_periods(json.loads(raw), market_release))
    credential_policy = "request_identity_and_source_files_never_contain_credentials; storage_policy_is_explicit_in_parent_worker"
    if (root / "manifest.json").exists():
        previous = json.loads((root / "manifest.json").read_text())
        if previous.get("credential_policy") == "runtime_reference_or_local_hidden_prompt; never_stored":
            # Preserve the immutable earlier request plan. An explicit parent
            # credential-policy.json records any later user-approved storage change.
            credential_policy = previous["credential_policy"]
    manifest = dict(schema=e.SCHEMA, baseline_batch=str(baseline), target_start=original["target_start"],
                    target_end=original["target_end"], baseline_codes=original["baseline_codes"],
                    target_codes=original["target_codes"], source="tushare", source_families=sorted({t["kind"] for t in tasks}),
                    universe_sha256=e.digest(raw), tasks_sha256=e.digest(e.encoded(tasks)), planned_tasks=len(tasks),
                    credential_policy=credential_policy, endpoint=ENDPOINT,
                    local_request_interval_seconds=1.0, documented_general_requests_per_minute=200,
                    documented_daily_per_api_ceiling=100000, required_points=2000,
                    auth_is_existing_user_permission=True, data_license="local_research; no_raw_redistribution",
                    unified_database_imported=False, full_history_complete=False, **extra)
    root.mkdir(parents=True, exist_ok=True)
    e.immutable(root / "universe.json", raw)
    e.immutable(root / "tasks.json", e.encoded(tasks))
    e.immutable(root / "manifest.json", e.encoded(manifest))
    return manifest


def prepare_calendar(root, baseline):
    _, base, _ = e.load_batch(baseline)
    tasks = []
    for year in range(int(base["target_start"][:4]), int(base["target_end"][:4]) + 1):
        a, b = max(base["target_start"], f"{year}-01-01"), min(base["target_end"], f"{year}-12-31")
        for exchange in ("SSE", "SZSE"):
            tasks.append(dict(id=f"tushare_trade_cal-{exchange}-{year}", kind="tushare_trade_cal", symbol="",
                              start=a, end=b, url=request_url("trade_cal", dict(exchange=exchange,
                              start_date=a.replace("-", ""), end_date=b.replace("-", "")))))
    return _prepare(root, baseline, tasks, {"calendar_inference": False})


def prepare_limits(root, baseline, calendar_root, *, market_release=None):
    calendar_root, _, tasks = e.load_batch(calendar_root)
    dates, sources, missing = set(), [], []
    for task in tasks:
        path = calendar_root / "receipts" / (task["id"] + ".json")
        if not path.exists():
            raise ValueError("Calendar source pass must finish before limit task creation")
        receipt = json.loads(path.read_text())
        if receipt["state"] not in {"complete", "partial_source", "empty_unknown"}:
            raise ValueError("Calendar has unresolved source errors")
        e.verify_receipt(calendar_root, receipt)
        rows, cov = calendar_rows(gzip.decompress((calendar_root / receipt["raw"]["path"]).read_bytes()), task)
        dates.update(r["date"] for r in rows if r["is_open"])
        sources.append({"task_id": task["id"], "receipt_sha256": e.digest(path.read_bytes())})
        missing.extend(dict(exchange=request_identity(task["url"])[1]["exchange"], date=d) for d in cov["missing_calendar_dates"])
    if not dates:
        raise ValueError("No evidenced trading days; no guessed work schedule")
    # The latest ordinary task gives immediate evidence for current replacement;
    # the same uninterrupted run then walks all historical dates chronologically.
    ordered = [max(dates)] + sorted(dates - {max(dates)})
    planned = [dict(id="tushare_stk_limit-" + d, kind="tushare_stk_limit", symbol="", start=d, end=d,
                    url=request_url("stk_limit", {"trade_date": d.replace("-", "")})) for d in ordered]
    return _prepare(root, baseline, planned, {"calendar_batch": str(calendar_root),
                    "calendar_receipts": sources, "unknown_calendar_dates": missing,
                    "row_cap": ROW_CAP, "cap_policy": "explicit_partial_then_missing_target_code_requests"},
                    market_release=market_release)


def prepare_capped_targets(root, baseline, limit_root):
    """Use documented single-code queries only for target omissions on capped days."""
    limit_root, _, source_tasks = e.load_batch(limit_root)
    tasks, sources = [], []
    for task in source_tasks:
        path = limit_root / "receipts" / (task["id"] + ".json")
        if not path.exists():
            raise ValueError("Limit source pass has not finished")
        receipt = json.loads(path.read_text())
        if receipt["state"] in {"pending", "running", "captured", "failed_retryable"}:
            raise ValueError("Limit source pass has pending requests")
        if receipt["state"] not in {"complete", "partial_source", "empty_unknown"}:
            continue  # Remain explicit upstream gaps, never fabricate supplements.
        e.verify_receipt(limit_root, receipt)
        cov = receipt["coverage"]
        if not cov["row_cap_reached"]:
            continue
        sources.append({"task_id": task["id"], "receipt_sha256": e.digest(path.read_bytes())})
        for code in cov["missing_target_codes"]:
            symbol = ("sh" if code.endswith("SH") else "sz") + code[:6]
            tasks.append(dict(id="tushare_stk_limit-" + task["start"] + "-" + symbol,
                              kind="tushare_stk_limit", symbol=symbol, start=task["start"], end=task["end"],
                              url=request_url("stk_limit", {"trade_date": task["start"].replace("-", ""), "ts_code": code})))
    return _prepare(root, baseline, tasks, {"capped_source_batch": str(limit_root),
                    "source_receipts": sources, "cap_repair": "missing_target_codes_only; no_offset_assumption"})


def run(root, transport, max_jobs=0, *, stop_paths=(), retain_temporaries=False):
    return e.run(root, max_jobs, 1.0, fetcher=transport, normalizer=normalize,
                 authenticated=True, minimum_interval=1.0, stop_paths=stop_paths,
                 retain_temporaries=retain_temporaries)


SERVICE = "ashare-data.tushare"
ACCOUNT = "tushare"
MAX_BYTES = 4096


def verify_helper(helper, policy_path):
    policy = json.loads(Path(policy_path).read_text())
    helper = Path(helper).resolve()
    if (policy.get("schema") != "ad.tushare-credential-policy.v1"
            or policy.get("service") != SERVICE or policy.get("account") != ACCOUNT
            or Path(policy.get("helper", "")).resolve() != helper
            or hashlib.sha256(helper.read_bytes()).hexdigest() != policy.get("helper_sha256")):
        raise ValueError("Keychain helper or dedicated service policy changed; credential not read")


def _environment():
    return {"PATH": os.defpath}


def _failure(code, stderr=b""):
    match = re.search(rb"keychain_osstatus=(-?\d+)", stderr)
    status = int(match[1]) if match else None
    reason = {-25300: "item_not_configured", -25308: "keychain_access_requires_user_unlock_or_permission",
              -128: "user_canceled_keychain_access", -25293: "keychain_access_denied"}.get(status, "native_keychain_operation_failed")
    return RuntimeError(f"{reason}; exit={code}; osstatus={status}; credential omitted")


def store_token(helper, token):
    if not isinstance(token, str) or len(token.strip()) < 16:
        raise ValueError("Tushare credential is absent or invalid; configure locally")
    value = token.strip().encode("utf-8")
    if len(value) > MAX_BYTES:
        raise ValueError("Credential exceeds the bounded storage size")
    result = subprocess.run([str(Path(helper).resolve()), "store"], input=value, capture_output=True,
                            env=_environment(), check=False)
    if result.returncode:
        raise _failure(result.returncode, result.stderr)
    metadata = json.loads(result.stdout)
    if metadata != dict(stored_in_keychain=True, service=SERVICE, account=ACCOUNT):
        raise RuntimeError("Unexpected keychain storage acknowledgment; credential omitted")
    return metadata


def status(helper):
    result = subprocess.run([str(Path(helper).resolve()), "status"], capture_output=True,
                            env=_environment(), timeout=20, check=False)
    if result.returncode:
        raise _failure(result.returncode, result.stderr)
    value = json.loads(result.stdout)
    if value not in ({"configured": True}, {"configured": False}):
        raise RuntimeError("Unexpected keychain metadata response")
    return value


def read_token(helper):
    read_fd, write_fd = os.pipe()
    try:
        child = subprocess.Popen([str(Path(helper).resolve()), "read", str(write_fd)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            pass_fds=(write_fd,), close_fds=True, env=_environment())
        os.close(write_fd)
        write_fd = None
        try:
            _, errors = child.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()  # Only this credential helper, before any source request.
            child.communicate()
            raise RuntimeError("Keychain read timed out; collector not started; credential omitted") from None
        if child.returncode:
            raise _failure(child.returncode, errors)
        value = os.read(read_fd, MAX_BYTES + 1)
        if not 16 <= len(value) <= MAX_BYTES:
            raise RuntimeError("Invalid bounded keychain value; credential omitted")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError("Invalid keychain value encoding; credential omitted") from None
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


STATEMENTS = {"income": "lrb", "balancesheet": "fzb", "cashflow": "llb"}
METRICS = {
    "income": {"total_revenue": "total_operating_revenue", "revenue": "operating_revenue",
               "n_income": "net_profit", "n_income_attr_p": "parent_net_profit"},
    "balancesheet": {"total_assets": "total_assets", "total_liab": "total_liabilities"},
    "cashflow": {"n_cashflow_act": "net_operate_cash_flow", "c_cash_equ_end_period": "cash_equivalents_at_end"},
}
VERSION_TYPES = {"1": "latest_consolidated", "4": "adjusted_prior_comparable_consolidated",
                 "5": "pre_adjustment_consolidated"}
DOCS = {"income": 33, "balancesheet": 36, "cashflow": 44, "fina_indicator": 79, "dividend": 103, "adj_factor": 28}
# Only fina_indicator (100) and dividend (2000) document these source limits.
# The statement limit is a conservative local truncation guard, not a source claim.
ROW_GUARDS = {"income": 100, "balancesheet": 100, "cashflow": 100, "fina_indicator": 100, "dividend": 2000}
FINANCIAL_DATES = {"ann_date", "f_ann_date", "end_date"}
DIVIDEND_DATES = {"end_date", "ann_date", "record_date", "ex_date", "pay_date", "div_listdate", "imp_ann_date", "base_date"}


def required_day(value):
    result = e.day(value)
    if result is None:
        raise ValueError("Explicit valid date required")
    return result


def validate_params(api, params):
    if not re.fullmatch(r"\d{6}\.(SH|SZ)", params.get("ts_code", "")):
        raise ValueError("Ordinary endpoints require one explicit mainboard target code")
    allowed = {"ts_code", "ann_date", "start_date", "end_date", "period"}
    if api in STATEMENTS:
        allowed |= {"report_type", "comp_type"}
        if api != "balancesheet":
            allowed.add("f_ann_date")
        if params.get("report_type") not in VERSION_TYPES:
            raise ValueError("Explicit consolidated report type 1, 4 or 5 required")
    elif api == "dividend":
        allowed = {"ts_code", "ann_date", "record_date", "ex_date", "imp_ann_date"}
    elif api == "adj_factor":
        allowed = {"ts_code", "trade_date", "start_date", "end_date"}
    elif api != "fina_indicator":
        raise ValueError("Unapproved fundamentals endpoint")
    if not set(params) <= allowed:
        raise ValueError("Undocumented or unapproved request parameter")
    for key, value in params.items():
        if key.endswith("date") or key == "period":
            required_day(value)
    if ("start_date" in params) != ("end_date" in params):
        raise ValueError("Both range boundaries are required")
    if "start_date" in params:
        start, end = (date.fromisoformat(required_day(params[k])) for k in ("start_date", "end_date"))
        if end < start or (api == "adj_factor" and (end - start).days > 365):
            raise ValueError("Invalid or oversized date window")
    elif api != "dividend" and not set(params) & {"period", "ann_date", "f_ann_date", "trade_date"}:
        raise ValueError("Explicit bounded window or one date required")


def decimal_value(value):
    if value in (None, "", "--"):
        return None
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Nonfinite source number")
    return format(number, "f")


def plain(row):
    return {k: format(v, "f") if isinstance(v, Decimal) else v for k, v in row.items()}


def field(value, unit, **extra):
    value = decimal_value(value)
    return dict(value=value, state="source_observed" if value is not None else "source_null_or_missing", unit=unit, **extra)


def financial_row(row, api, task):
    _, params = ts.request_identity(task["url"])
    period = required_day(row.get("end_date"))
    dates = {k: e.day(row.get(k)) for k in FINANCIAL_DATES}
    if params.get("period") and period != required_day(params["period"]):
        raise ValueError("Financial period mismatch")
    for key in ("ann_date", "f_ann_date"):
        if params.get(key) and dates[key] != required_day(params[key]):
            raise ValueError("Announcement filter mismatch")
    if "start_date" in params:
        # The indicator API uses report periods; statement APIs use announcement dates.
        selected = period if api == "fina_indicator" else dates["ann_date"]
        if selected is None or not required_day(params["start_date"]) <= selected <= required_day(params["end_date"]):
            raise ValueError("Financial date-window semantics mismatch")
    if api in STATEMENTS and str(row.get("report_type")) != params["report_type"]:
        raise ValueError("Consolidated report type mismatch")
    if str(row.get("update_flag")) not in {"0", "1", "None", ""}:
        raise ValueError("Unknown update_flag")
    selected = ts.request_fields(task["url"]).split(",")
    identity = set(ts.FINANCIAL_ID.split(","))
    values = {}
    for name in selected:
        if name in identity:
            continue
        unit = ("shares" if name == "total_share" else "ratio" if api == "fina_indicator"
                else "provider_native_amount_unit_unverified")
        if name in {"roe", "roa", "netprofit_margin", "debt_to_assets"}:
            unit = "percent"
        if name in {"basic_eps", "diluted_eps", "eps"}:
            unit = "CNY/share_source_documentation"
        values[name] = field(row.get(name), unit, canonical_field=METRICS.get(api, {}).get(name),
                             currency=None, scale_verified=False)
    return dict(schema="ad.tushare-financial-version.v1", statement=STATEMENTS.get(api, "indicators"),
                period_end=period, period_start=None if api == "balancesheet" else period[:4] + "-01-01",
                scope="consolidated" if api in STATEMENTS else "provider_indicator_scope",
                basis="instant" if api == "balancesheet" else "YTD" if api in STATEMENTS else "per_metric_definition",
                announcement_date=dates["ann_date"], actual_announcement_date=dates["f_ann_date"],
                announcement_time_precision="date" if dates["ann_date"] or dates["f_ann_date"] else "unknown",
                published_at=None, historical_available_at=None,
                report_type=str(row.get("report_type")) if api in STATEMENTS else None,
                version_kind=VERSION_TYPES.get(str(row.get("report_type")), "provider_indicator_snapshot"),
                source_update_flag=row.get("update_flag"), company_type=row.get("comp_type"), end_type=row.get("end_type"),
                revision_history_complete=False, supersedes=None, fields=values,
                period_after_code_exit=bool(task.get("out") and period >= required_day(task["out"])),
                cross_provider_publication_binding=False)


def dividend_row(row, task):
    _, params = ts.request_identity(task["url"])
    dates = {k: e.day(row.get(k)) for k in DIVIDEND_DATES}
    for key in set(params) - {"ts_code"}:
        if dates[key] != required_day(params[key]):
            raise ValueError("Dividend event filter mismatch")
    values = {k: field(row.get(k), "CNY/eligible_share" if k.startswith("cash_") else "new_shares/eligible_share")
              for k in ("stk_div", "stk_bo_rate", "stk_co_rate", "cash_div", "cash_div_tax")}
    values["cash_div"]["tax_basis"] = "after_tax_source_claimed_not_investor_specific"
    values["cash_div_tax"]["tax_basis"] = "gross_pre_tax"
    base = decimal_value(row.get("base_share"))
    values["base_share"] = dict(native_value=base, native_unit="10000_shares",
        value=format(Decimal(base) * 10000, "f") if base is not None else None, unit="shares",
        state="source_observed" if base is not None else "source_null_or_missing")
    if any(Decimal(v["value"]) < 0 for v in values.values() if v["value"] is not None):
        raise ValueError("Negative dividend quantity")
    for name, value in dates.items():
        values[name] = dict(value=value, unit="date", state="source_observed" if value else "source_null_or_missing")
    values["div_proc"] = dict(value=row.get("div_proc"), unit="source_status",
                               state="source_observed" if row.get("div_proc") else "source_null_or_missing")
    return dict(schema="ad.tushare-dividend-version.v1", fields=values, source_status=row.get("div_proc"),
                implemented=row.get("div_proc") == "实施", absence_proved=False,
                period_after_code_exit=bool(task.get("out") and dates.get("ex_date") and dates["ex_date"] >= required_day(task["out"])),
                historical_available_at=None, revision_history_complete=False,
                announcement_semantics="ann_date_preplan; imp_ann_date_implementation", date_precision="date")


def factor_row(row, task):
    _, params = ts.request_identity(task["url"])
    day = required_day(row.get("trade_date"))
    start, end = (required_day(params[k]) for k in ("start_date", "end_date")) if "start_date" in params else (required_day(params["trade_date"]),) * 2
    if not start <= day <= end:
        raise ValueError("Factor date mismatch")
    value = decimal_value(row.get("adj_factor"))
    if value is None or Decimal(value) <= 0:
        raise ValueError("Factor must be positive and finite")
    return dict(schema="ad.tushare-factor-observation.v1", trade_date=day,
                fields={"adj_factor": dict(value=value, unit="dimensionless", state="source_observed")},
                basis="tushare_dividend_reinvestment_provider_factor", compatible_with_sina_hfq=False,
                carry_forward_authorized=False, historical_available_at=None)


def observations(raw, task):
    api, params = ts.request_identity(task["url"])
    source, rows, periods, seen_days = ts.table(raw), [], [], set()
    for index, row in enumerate(source):
        if row.get("ts_code") != params["ts_code"]:
            raise ValueError("Response target code mismatch")
        if api in STATEMENTS or api == "fina_indicator":
            result = financial_row(row, api, task)
            period = result["period_end"]
            periods.append(period)
            if not task["target_start"] <= period <= task["target_end"]:
                continue
        elif api == "dividend":
            result = dividend_row(row, task)
            event_day = result["fields"]["ex_date"]["value"] or result["fields"]["ann_date"]["value"]
            if event_day:
                periods.append(event_day)
                if not task["target_start"] <= event_day <= task["target_end"]:
                    continue
        elif api == "adj_factor":
            result = factor_row(row, task)
            if result["trade_date"] in seen_days:
                raise ValueError("Duplicate daily factor")
            seen_days.add(result["trade_date"])
        else:
            raise ValueError("Fundamentals task required")
        result.update(source="tushare", api=api, security=e.security(task["symbol"]),
                      identity_status="request_bound_not_independently_verified",
                      source_locator=f"data/items/{index}", source_ordinal=index,
                      source_revision_id=e.digest(e.encoded(plain(row))),
                      revision_identity_basis="requested_field_projection_hash_and_source_locator",
                      source_documentation=f"https://tushare.pro/document/2?doc_id={DOCS[api]}")
        rows.append(result)
    cap = ROW_GUARDS.get(api)
    has_more = json.loads(raw).get("data", {}).get("has_more") is True
    coverage = dict(source_rows=len(source), target_rows=len(rows), source_earliest_period=min(periods) if periods else None,
                    source_latest_period=max(periods) if periods else None,
                    row_cap_reached=bool((cap and len(source) >= cap) or has_more), row_guard=cap,
                    guard_is_documented_source_limit=api in {"fina_indicator", "dividend"},
                    date_window_semantics="report_period" if api == "fina_indicator" else "announcement_date" if api in STATEMENTS else "event_or_trading_date",
                    absence_proved=False, revision_history_complete=False, full_history_complete=False)
    if api == "adj_factor":
        coverage.update(missing_evidenced_sessions=sorted(set(task.get("expected_sessions", [])) - seen_days),
                        expected_sessions_bound="expected_sessions" in task,
                        source_earliest_date=min(seen_days) if seen_days else None,
                        source_latest_date=max(seen_days) if seen_days else None)
    return rows, coverage


def split_task(task):
    """Bisect only the documented date dimension; never assume offset pagination."""
    api, params = ts.request_identity(task["url"])
    if api not in {*STATEMENTS, "fina_indicator"} or "start_date" not in params:
        return []
    a, b = (date.fromisoformat(required_day(params[k])) for k in ("start_date", "end_date"))
    if a == b:
        return []
    mid = a + (b - a) // 2
    children = []
    for start, end in ((a, mid), (mid + timedelta(days=1), b)):
        new = params | dict(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        url = ts.request_url(api, new, ts.request_fields(task["url"]))
        children.append(task | dict(id="tushare_" + api + "-" + e.digest(url.encode())[:24], url=url,
                                    start=start.isoformat(), end=end.isoformat(), split_parent=task["id"],
                                    gap_group=task.get("gap_group", task["id"])))
    return children


def pending(receipt):
    # A captured response, even after parse failure, must be reused in place.
    return receipt.get("state") not in e.TERMINAL and not receipt.get("raw")


def link_artifact(source, target, relative, artifact):
    """Share bytes using a hard link, with both immutable checksum identities."""
    destination = target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        os.link(source / artifact["path"], destination)
    result = artifact | {"path": relative}
    e.verify_receipt(target, {"raw": result})
    return result


def prepare(root, source):
    from ashare_data.financials import tushare_gaps as g

    root, source = Path(root).resolve(), Path(source).resolve()
    with (source / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source, manifest, tasks = e.load_batch(source)
        if (root / "manifest.json").exists():
            _, prior, captures = e.load_batch(root)
            if prior.get("original_source") != str(source) or prior.get("original_tasks_sha256") != manifest["tasks_sha256"]:
                raise ValueError("Dividend coalescing source identity changed")
            snapshot = (root / "original-receipts.json.gz").read_bytes()
            if e.digest(snapshot) != prior["original_receipts_sha256"]:
                raise ValueError("Dividend checkpoint receipt snapshot changed")
            adopt_existing(root, source, captures, json.loads(gzip.decompress(snapshot)))
            return prior
        groups, receipts, originals = defaultdict(list), {}, {}
        for task in tasks:
            if task["kind"] != "tushare_dividend":
                continue
            _, params = ts.request_identity(task["url"])
            groups[params["ts_code"]].append(task)
            path = source / "receipts" / (task["id"] + ".json")
            if path.exists():
                blob = path.read_bytes()
                receipt = json.loads(blob)
                e.verify_receipt(source, receipt)
                receipts[task["id"]] = receipt
                originals[task["id"]] = dict(sha256=e.digest(blob), receipt=receipt)
        planned = []
        for owned in groups.values():
            remaining = [t for t in owned if pending(receipts.get(t["id"], {}))]
            if len(remaining) < 2:
                continue
            fields = sorted({name for t in remaining for name in ts.request_fields(t["url"]).split(",")})
            # Existing successful whole-code responses take priority over a new call.
            candidates = [t for t in owned if set(ts.request_identity(t["url"])[1]) == {"ts_code"}
                and receipts.get(t["id"], {}).get("state") == "complete"
                and not receipts[t["id"]].get("coverage", {}).get("row_cap_reached")
                and set(fields) <= set(ts.request_fields(t["url"]).split(","))]
            capture = (candidates[0].copy() if candidates else
                g.make_task("dividend", remaining[0], {}, fields, manifest,
                            reason="coalesced_pending_dividend_dates", gaps=[], matching_events=[]))
            capture["coalesces"] = [t["id"] for t in remaining]
            planned.append(capture)
            if candidates:
                capture["existing_response_task_id"] = capture["id"]
        snapshot = gzip.compress(e.encoded(originals), mtime=0)
        result = ts._prepare(root, source, planned, dict(original_source=str(source),
            original_tasks_sha256=manifest["tasks_sha256"], original_receipts_sha256=e.digest(snapshot),
            original_pending_dates=sum(len(t["coalesces"]) for t in planned),
            retained_existing_broad_responses=sum("existing_response_task_id" in t for t in planned), source_only=True,
            coverage_policy="all_versions; cap_or_unmatched_event_uses_original_date_fallback"))
        e.immutable(root / "original-receipts.json.gz", snapshot)
        adopt_existing(root, source, planned, originals)
        return result


def adopt_existing(root, source, planned, originals):
    from ashare_data.financials import tushare_gaps as g

    for task in planned:
        task_id = task.get("existing_response_task_id")
        if task_id:
            path = root / "receipts" / (task_id + ".json")
            if path.exists():
                e.verify_receipt(root, g.read(path))
                continue
            receipt = originals[task_id]["receipt"]
            e.verify_receipt(source, receipt)
            copied = receipt | {"reused_from": dict(batch=str(source), task_id=task_id,
                                                     receipt_sha256=originals[task_id]["sha256"])}
            for key in ("raw", "normalized"):
                if receipt.get(key):
                    copied[key] = link_artifact(source, root, receipt[key]["path"], receipt[key])
            e.immutable(root / "receipts" / (task_id + ".json"), e.encoded(copied))


def source_rows(raw, task, receipt):
    fields = set(json.loads(raw).get("data", {}).get("fields", []))
    if fields != set(ts.request_fields(task["url"]).split(",")):
        raise ValueError("Coalesced response field projection mismatch")
    if set(ts.request_identity(task["url"])[1]) != {"ts_code"}:
        raise ValueError("Coalesced response must bind exactly one code")
    rows, coverage = f.observations(raw, task)
    for row in rows:
        row.update(observed_at=receipt["observed_at"], response_sha256=e.digest(raw),
                   source_task_id=task["id"], historical_pit_certified=False)
    return rows, coverage


def select_rows(task, source_task, rows, coverage):
    if coverage["row_cap_reached"]:
        return [], "source_truncated"
    api, params = ts.request_identity(task["url"])
    _, origin = ts.request_identity(source_task["url"])
    if (api != "dividend" or params.get("ts_code") != origin["ts_code"]
            or not set(params) <= {"ts_code", "ex_date"}
            or any(task.get(k) != source_task.get(k) for k in ("symbol", "out", "target_start", "target_end"))
            or not set(ts.request_fields(task["url"]).split(",")) <= set(ts.request_fields(source_task["url"]).split(","))):
        return [], "incompatible_scope_or_fields"
    selected = [row for row in rows if not params.get("ex_date") or row["fields"]["ex_date"]["value"] == e.day(params["ex_date"])]
    if not selected or any(not any(query_event_compatible(row, event) for row in selected) for event in task["matching_events"]):
        return [], "event_not_matched"
    # Keep every returned version on the date, including ambiguous matches.
    return selected, None


def query_event_compatible(row, event):
    """Reuse a date query without pretending missing provider anchors are zero.

    Date, registration and gross cash must match; other present anchors cannot
    conflict. Missing source anchors stay missing and the stricter cross-source
    reconciliation still reports an unresolved binding. A date network request
    would return the same provider nulls, not establish the missing relationship.
    """
    if (event.get("cash_basis") != "gross_pre_tax_per_old_eligible_share" or not row["implemented"]
            or row["fields"]["ex_date"]["value"] != event["date"]):
        return False
    for key, expected in event["anchors"].items():
        actual = row["fields"].get(key, {}).get("value")
        if expected is None or actual is None:
            if key in {"record_date", "cash_div_tax"}:
                return False
            continue
        if (expected != actual if key == "record_date" else Decimal(expected) != Decimal(actual)):
            return False
    return True


def normalize_reused(raw, task, receipt):
    from ashare_data.financials import tushare_gaps as g

    proof = receipt["source_reuse"]
    if e.digest(raw) != proof["response_sha256"]:
        raise ValueError("Reused dividend response checksum changed")
    rows, coverage = source_rows(raw, proof["source_task"], receipt)
    selected, failure = select_rows(task, proof["source_task"], rows, coverage)
    if failure:
        raise ValueError("Reused dividend coverage failed: " + failure)
    for row in selected:
        row.update(source_batch=proof["batch"], original_task_id=task["id"],
                   local_projection=True, source_receipt_sha256=proof["receipt_sha256"])
    matched = sum(any(g.dividend_matches(row, event) for row in selected) for event in task["matching_events"])
    return selected, coverage | dict(target_rows=len(selected), reused_from_whole_code=True,
        original_query_executed=False, original_task_id=task["id"], source_task_id=proof["source_task"]["id"],
        source_batch=proof["batch"], matched_event_count=matched,
        unresolved_event_bindings=len(task["matching_events"]) - matched, missing_anchors_imputed=False)


def apply(root):
    from ashare_data.financials import tushare_gaps as g

    root, manifest, captures = e.load_batch(root)
    source = Path(manifest["original_source"])
    counts, ledger, network = Counter(), [], 0
    with (source / "writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, original, tasks = e.load_batch(source)
        if original["tasks_sha256"] != manifest["original_tasks_sha256"]:
            raise ValueError("Original dividend queue changed")
        tasks = {t["id"]: t for t in tasks}
        for capture in captures:
            receipt_path = root / "receipts" / (capture["id"] + ".json")
            blob = receipt_path.read_bytes() if receipt_path.exists() else b"{}"
            receipt = json.loads(blob)
            network += len(receipt.get("attempts", [])) if not receipt.get("reused_from") else 0
            rows, coverage, raw = None, None, None
            for identity in capture["coalesces"]:
                task = tasks[identity]
                path = source / "receipts" / (identity + ".json")
                prior = g.read(path, {})
                item = dict(original_task_id=identity, source_task_id=capture["id"])
                if not pending(prior):
                    e.verify_receipt(source, prior)
                    item["state"] = "reused" if prior.get("source_reuse", {}).get("batch") == str(root) else "original_response_preserved"
                elif receipt.get("state") not in {"complete", "empty_unknown", "partial_source"}:
                    item["state"] = "source_pending" if receipt.get("state") not in e.TERMINAL else "fallback_source_failed"
                else:
                    if rows is None:
                        e.verify_receipt(root, receipt)
                        raw = gzip.decompress((root / receipt["raw"]["path"]).read_bytes())
                        try:
                            rows, coverage = source_rows(raw, capture, receipt)
                        except ValueError:
                            rows, coverage = [], {"row_cap_reached": False}
                            item["invalid_source_projection"] = True
                    selected, failure = select_rows(task, capture, rows, coverage)
                    if failure:
                        item["state"] = "fallback_" + failure
                    else:
                        origin = {k: capture[k] for k in ("id", "kind", "url", "symbol", "out", "target_start", "target_end")}
                        result = prior | dict(task_id=identity, attempts=prior.get("attempts", []),
                            observed_at=receipt["observed_at"], state="captured", source_reuse=dict(
                                schema="ad.tushare-dividend-reuse.v1", batch=str(root), source_task=origin,
                                receipt_sha256=e.digest(blob), response_sha256=e.digest(raw),
                                original_tasks_sha256=original["tasks_sha256"], network_request_saved=True))
                        result["raw"] = link_artifact(root, source, "raw/shared-" + capture["id"] + ".json.gz", receipt["raw"])
                        ts.normalize(source, original, task, result, raw)
                        if result["state"] != "complete":
                            raise ValueError("Validated dividend projection did not normalize")
                        e.atomic(path, e.encoded(result))
                        item["state"] = "reused"
                counts[item["state"]] += 1
                ledger.append(item)
        result = dict(schema="ad.tushare-dividend-coalescing.v1", at=e.now(), source=str(source),
            planned_capture_requests=len(captures), original_pending_dates=manifest["original_pending_dates"],
            states=dict(counts), capture_network_attempts=network,
            original_network_requests_avoided=counts["reused"],
            net_network_requests_saved_so_far=counts["reused"] - network,
            ledger=e.artifact(root, "reuse-ledger.jsonl.gz", gzip.compress(b"".join(e.encoded(row) for row in ledger), mtime=0)),
            originals_preserved=True, full_history_complete=False, database_imported=False)
        e.atomic(root / "reuse-summary.json", e.encoded(result))
        e.audit(source, verify=False, write=True, _writer_owned=True)
    return result
