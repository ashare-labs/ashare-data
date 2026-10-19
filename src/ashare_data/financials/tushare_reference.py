"""Account-authorized Tushare reference collection, with in-memory credentials only.

Public request identities exclude the credential. A user-supplied runtime reference
or local hidden prompt supplies it; this module never searches credential stores.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import gzip
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ashare_data.financials import expansion as e

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
        from ashare_data.financials.tushare_fundamentals import validate_params
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
    return {r["symbol"][2:] + (".SH" if r["symbol"].startswith("sh") else ".SZ")
            for r in universe["instruments"] if r["ipo"] <= day and (not r["out"] or day < r["out"])}


def limit_rows(raw, task, universe):
    _, params = request_identity(task["url"])
    day = e.day(params["trade_date"])
    expected = active_codes(universe, day)
    if params.get("ts_code"):
        expected &= {params["ts_code"]}
    source, seen, rows = table(raw), set(), []
    for i, r in enumerate(source):
        code = r["ts_code"]
        if e.day(r["trade_date"]) != day or code in seen or (params.get("ts_code") and code != params["ts_code"]):
            raise ValueError("Limit date/code/duplicate mismatch")
        seen.add(code)
        if code not in expected:
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
            from ashare_data.financials import tushare_dividends
            rows, coverage = tushare_dividends.normalize_reused(raw, task, receipt)
            receipt["coalescing_normalizer_sha256"] = e.digest(Path(tushare_dividends.__file__).read_bytes())
            receipt["fundamentals_normalizer_sha256"] = e.digest(Path(tushare_dividends.f.__file__).read_bytes())
            partial = False
        else:
            from ashare_data.financials import tushare_fundamentals
            rows, coverage = tushare_fundamentals.observations(raw, task)
            partial = coverage["row_cap_reached"]
            receipt["fundamentals_normalizer_sha256"] = e.digest(Path(tushare_fundamentals.__file__).read_bytes())
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


def _prepare(root, baseline, tasks, extra):
    root, baseline = Path(root).resolve(), Path(baseline).resolve()
    _, original, _ = e.load_batch(baseline)
    if root == baseline:
        raise ValueError("Independent source partition required")
    raw = (baseline / "universe.json").read_bytes()
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


def prepare_limits(root, baseline, calendar_root):
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
                    "row_cap": ROW_CAP, "cap_policy": "explicit_partial_then_missing_target_code_requests"})


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
