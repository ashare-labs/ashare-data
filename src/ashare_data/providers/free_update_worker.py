"""One isolated AKShare call with bounded HTTP evidence; no publishing side effects."""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.response import addinfourl
from urllib.error import HTTPError, URLError

from ashare_data.providers.free_updates import catalog, encoded, assess_records, implementation_identity
from ashare_data.storage.immutable import immutable_write

DOMAINS = ("eastmoney.com", "sina.com.cn", "sina.cn", "10jqka.com.cn", "cninfo.com.cn",
           "sse.com.cn", "szse.cn", "csindex.com.cn", "legulegu.com", "swsresearch.com",
           "jin10.com", "soozhu.com", "sge.com.cn", "bjets.com.cn", "hbets.cn",
           "safe.gov.cn", "chinabond.com.cn", "treasury.gov", "99qh.com", "cnbs.ac.cn", "gtimg.cn", "qq.com",
           "cls.cn", "cctv.com", "cntv.cn", "mofcom.gov.cn", "fx168api.com", "stats.gov.cn",
           "pbc.gov.cn", "ndrc.gov.cn", "fgw.sh.gov.cn")
# The CASS Institute of Finance and Banking official database directory links
# this public CNBS origin: http://ifb.cass.cn/newpc/sjk/ (reviewed 2026-10-28).
# Restrict the exception to the SDK's reviewed data download, not arbitrary IPs.
REVIEWED_PUBLIC_URLS = {"http://114.115.232.154:8080/handler/download.ashx"}


def now():
    return datetime.now(timezone.utc).isoformat()


class SourceBlocked(RuntimeError):
    pass


def bounded_timeout(value):
    if isinstance(value, tuple):
        return tuple(min(float(v or 12), 12) for v in value)
    return min(float(value or 12), 12)


class Capture:
    def __init__(self, root, blocked_hosts=(), interval=1.0, request_limit=40, journal_path=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blocked = set(blocked_hosts)
        self.receipts = []
        self.dataset_metadata = {}
        self.next_time = {}
        self.interval = interval
        self.request_limit = request_limit
        self.attempts = 0
        self.journal_path=Path(journal_path) if journal_path else self.root/"receipts.jsonl"
        from ashare_data.providers.free_update_budget import from_environment
        self.budget = from_environment()
        self.budget_tokens = {}

    def network_before(self, url, maximum_body=16 * 1024 * 1024 + 1):
        if self.attempts >= self.request_limit:
            raise SourceBlocked("request budget exceeded; split the scope")
        if self.budget:
            token = self.budget.reserve(url, maximum_body)
            self.budget_tokens.setdefault(url, []).append(token)
        self.attempts += 1

    def before(self, url):
        from ashare_data.providers.free_update_legacy_reference import reviewed_url
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not (url in REVIEWED_PUBLIC_URLS or any(
            host == d or host.endswith("." + d) for d in DOMAINS
        ) or reviewed_url(url)):
            raise SourceBlocked("source host requires explicit review: " + host)
        if host in self.blocked:
            raise SourceBlocked("source host is cooling down: " + host)
        delay = max(0, self.next_time.get(host, 0) - time.monotonic())
        time.sleep(min(delay, 5))
        self.network_before(url)
        self.next_time[host] = time.monotonic() + self.interval
        return host

    def record(self, url, host, status, body, headers, *, protocol="http", request_method=None, request_body_sha256=None):
        if len(body) > 16 * 1024 * 1024:
            raise SourceBlocked("response exceeds 16 MiB")
        if protocol != "transport" and self.budget and self.budget_tokens.get(url):
            self.budget.settle(self.budget_tokens[url].pop(), len(body))
        sha = hashlib.sha256(body).hexdigest()
        path = self.root / (sha + ".gz")
        if not path.exists():
            immutable_write(path,gzip.compress(body, mtime=0))
        receipt = dict(url=url, host=host, status=status, protocol=protocol, sha256=sha, bytes=len(body),
                       received_at=now(), retry_after=headers.get("Retry-After"))
        if request_method is not None:
            receipt.update(request_method=request_method, request_body_sha256=request_body_sha256)
        self.receipts.append(receipt)
        with self.journal_path.open("ab") as stream:
            stream.write(encoded(receipt) + b"\n")
        if protocol != "http" or status == "transport_error":
            return
        if status in {401, 403, 429}:
            self.blocked.add(host)
            raise SourceBlocked(f"HTTP {status}; stop this host, no authentication workaround")
        if 500 <= status < 600:
            self.blocked.add(host)
            raise SourceBlocked(f"HTTP {status}; transient source failure, retry after cooldown")
        if not 200 <= status < 300:
            raise RuntimeError(f"HTTP {status}")
        lower = body[:5000].lower()
        if b"captcha" in lower and b"<html" in lower:
            self.blocked.add(host)
            raise SourceBlocked("captcha response; manual source decision required")

    def transport_failure(self, url, host, exc):
        self.record(url, host, "transport_error", str(type(exc).__name__).encode(), {}, protocol="transport")
        self.blocked.add(host)

    def install(self):
        import requests
        import urllib.request
        from curl_cffi import requests as curl_requests
        capture = self
        original_send = requests.Session.send
        original_curl = curl_requests.Session.request

        def send(session, request, **kwargs):
            request_body = request.body or b""
            if isinstance(request_body, str):
                request_body = request_body.encode()
            if not isinstance(request_body, bytes):
                raise SourceBlocked("streaming request body has no reviewed replay contract")
            host = capture.before(request.url)
            kwargs["timeout"] = bounded_timeout(kwargs.get("timeout"))
            kwargs["verify"] = True
            if capture.budget:
                kwargs["stream"] = True
                # Avoid requests consuming intermediate redirect bodies before
                # our bound. Reviewed scheduled endpoints are direct URLs.
                kwargs["allow_redirects"] = False
            try:
                response = original_send(session, request, **kwargs)
                if capture.budget:
                    try:
                        body = response.raw.read(16 * 1024 * 1024 + 1, decode_content=True)
                        response._content = body
                        response._content_consumed = True
                    finally:
                        response.close()
            except requests.RequestException as exc:
                capture.transport_failure(request.url, host, exc)
                raise
            capture.record(response.url, host, response.status_code, response.content, response.headers,
                           request_method=request.method, request_body_sha256=hashlib.sha256(request_body).hexdigest())
            return response

        def curl(session, method, url, **kwargs):
            host = capture.before(url)
            kwargs["timeout"] = 12
            kwargs["verify"] = True
            # Redirect targets must receive a separate host review.
            kwargs["allow_redirects"] = False
            # Do not impersonate a browser to get around a challenge.
            kwargs.pop("impersonate", None)
            chunks, size = [], 0
            if capture.budget:
                if kwargs.get("content_callback") or kwargs.get("stream"):
                    raise SourceBlocked("unreviewed streaming callback in scheduled collector")
                def bounded_content(body):
                    nonlocal size
                    size += len(body)
                    if size > 16 * 1024 * 1024:
                        raise SourceBlocked("response exceeds 16 MiB")
                    chunks.append(body)
                    return len(body)
                kwargs["content_callback"] = bounded_content
            try:
                response = original_curl(session, method, url, **kwargs)
                if capture.budget:
                    response.content = b"".join(chunks)
            except Exception as exc:
                capture.transport_failure(url, host, exc)
                raise
            capture.record(response.url, host, response.status_code, response.content, response.headers)
            return response

        requests.Session.send = send
        curl_requests.Session.request = curl

        def urlopen(url, data=None, timeout=None, *, context=None):
            # pandas.read_excel(URL) uses urllib, not requests. It must obey
            # the same host review, TLS validation, budgets and raw evidence.
            if context is not None:
                raise SourceBlocked("custom TLS context is not permitted")
            request = url if isinstance(url, Request) else Request(url, data=data)
            if data is not None:
                request.data = data
            return capture.open(request, timeout=bounded_timeout(timeout))

        urllib.request.urlopen = urlopen

    def get(self, url, headers=None):
        request = Request(url, headers={"User-Agent":"ashare-data public-source research", **(headers or {})})
        with self.open(request) as response:
            return response.read()

    def post(self, url, parameters):
        request = Request(url, data=urlencode(parameters).encode(),
                          headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with self.open(request) as response:
            return response.read()

    def post_json(self, url, parameters):
        request = Request(url, data=json.dumps(parameters, ensure_ascii=False).encode(),
                          headers={"User-Agent": "eltdx/1.0", "Content-Type": "application/json"}, method="POST")
        with self.open(request) as response:
            return response.read()

    def open(self, request, *, timeout=12):
        url = request.full_url
        host = self.before(url)
        capture = self
        class ReviewedRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                if req.get_method() != "GET":
                    raise SourceBlocked("unexpected POST redirect; no method/body rewrite")
                capture.before(newurl)
                return super().redirect_request(req, fp, code, msg, hdrs, newurl)
        try:
            with build_opener(ReviewedRedirect).open(request,timeout=timeout) as response:
                body=response.read(16*1024*1024+1)
                self.record(response.url,urlsplit(response.url).hostname,response.status,body,response.headers,
                            request_method=request.get_method(), request_body_sha256=hashlib.sha256(request.data or b"").hexdigest())
                return addinfourl(io.BytesIO(body), response.headers, response.url, response.status)
        except HTTPError as exc:
            self.record(url,host,exc.code,exc.read(16*1024*1024+1),exc.headers)
            raise
        except (URLError, TimeoutError, OSError) as exc:
            self.transport_failure(url, host, exc)
            raise


def direct_records(capture, name, params):
    from ashare_data.providers.free_update_board_bars import FUNCTIONS as BOARD_BARS, collect as board_bars
    if name in BOARD_BARS:
        return board_bars(capture, name, params)
    from ashare_data.providers.free_update_policy_releases import FUNCTIONS as POLICY, collect as policy
    if name in POLICY:
        return policy(capture, name, params)
    if name == "macro_china_au_report":
        from ashare_data.providers.free_update_commodity_reports import sge
        return sge(capture, params)
    if name == "spot_price_qh":
        # Offline byte replay only; live transport uses the reviewed SDK and
        # then binds its response to the product page's explicit units.
        from ashare_data.providers.free_update_commodity_reports import qh_replay
        return qh_replay(capture, params)
    if name == "stock_fund_flow_industry":
        from ashare_data.providers.free_update_industry_flow import collect
        return collect(capture, params)
    if name == "safe_rmb_window":
        from ashare_data.providers.free_update_fx import collect
        return collect(capture, params)
    from ashare_data.providers.free_update_billboard import FUNCTIONS as BILLBOARD, collect as billboard
    if name in BILLBOARD:
        return billboard(capture, name, params)
    if name in {"macro_china_market_margin_sh", "china_government_yield"}:
        from ashare_data.providers.free_update_market_windows import collect
        return collect(capture, name, params)
    if name in {"fund_etf_only_szse", "sw_index_first_cons", "sw_index_second_cons", "sw_index_third_cons"}:
        from ashare_data.providers.free_update_current_members import szse_etfs, sw_members
        if name == "fund_etf_only_szse":
            return szse_etfs(capture, params)
        return sw_members(capture, params, level={"sw_index_first_cons":1,"sw_index_second_cons":2,"sw_index_third_cons":3}[name])
    from ashare_data.providers.free_update_tdx import FUNCTIONS as NATIVE, collect as native
    if name in NATIVE:
        return native(capture, name, params)
    from ashare_data.providers.free_update_legacy_reference import FUNCTIONS, collect as legacy_reference
    if name in FUNCTIONS:
        return legacy_reference(capture, name, params)
    if name == "sina_native_minutes":
        from ashare_data.providers.free_update_sina_minutes import collect
        return collect(capture, params)
    from ashare_data.providers.free_update_industry_directory import LEVELS, collect as industry_directory
    if name in LEVELS:
        return industry_directory(capture, name, params)
    if name == "soozhu_province_rank":
        from ashare_data.providers.free_update_province_prices import collect
        return collect(capture, params)
    if name == "szse_margin_summary":
        from ashare_data.providers.free_update_market_reports import collect
        return collect(capture, params)
    if name == "szse_margin_latest":
        from ashare_data.providers.free_update_market_reports import collect_latest
        return collect_latest(capture, params)
    if name == "futures_zh_minute_sina":
        from ashare_data.providers.free_update_futures_minutes import collect
        return collect(capture, params)
    from ashare_data.providers.free_update_limit_pools import SPECS, collect as limit_pool
    if name in SPECS:
        return limit_pool(capture, name, params)
    if name == "stock_zygc_em":
        from ashare_data.providers.free_update_business_segments import collect
        return collect(capture, params)
    if name == "stock_zh_a_st_em":
        from ashare_data.providers.free_update_reference import collect_st
        return collect_st(capture, params)
    if name == "index_stock_cons_csindex":
        from ashare_data.providers.free_update_reference import collect_index_components
        return collect_index_components(capture, params)
    if name in {"energy_carbon_hb", "macro_china_new_house_price"}:
        from ashare_data.providers.free_update_regional_prices import collect
        return collect(capture, name, params)
    if name == "stock_financial_analysis_indicator_em":
        from ashare_data.providers.free_update_financial_indicators import collect
        return collect(capture, params)
    from ashare_data.providers.free_update_futures import FUNCTIONS, collect as futures
    if name in FUNCTIONS:
        return futures(capture, name, params)
    if name in {"bond_zh_cov", "bond_zh_cov_info"}:
        from ashare_data.providers.free_update_reference import collect_bonds
        return collect_bonds(capture, name, params)
    if name == "spot_goods":
        from ashare_data.providers.free_update_spot_contracts import collect_goods
        return collect_goods(capture, params)
    if name == "stock_profit_forecast_em":
        from ashare_data.providers.free_update_research import collect
        return collect(capture, params)
    if name == "stock_zh_a_disclosure_report_cninfo":
        from ashare_data.providers.free_update_disclosures import collect
        return collect(capture, params)
    if name == "macro_china_shibor_all":
        from ashare_data.providers.free_update_rates import collect
        return collect(capture, params)
    if name in {"energy_oil_hist", "energy_oil_detail_latest"}:
        from ashare_data.providers.free_update_oil import collect
        return collect(capture, name, params)
    if name == "stock_value_em":
        from ashare_data.providers.free_update_valuations import collect
        return collect(capture, params)
    from ashare_data.providers.free_update_period_tables import TABLES, collect as tables
    if name in TABLES:
        return tables(capture, name, params)
    if name == "news_cctv":
        from ashare_data.providers.free_update_broadcast import collect
        return collect(capture, params)
    from ashare_data.providers.free_update_shareholders import REPORTS, collect as holders
    if name in REPORTS:
        return holders(capture, name, params)
    from ashare_data.providers.free_update_news import NEWS, collect as news
    if name in NEWS:
        return news(capture,name,params)
    if name == "security_events_em":
        from ashare_data.providers.free_update_security_events import collect
        return collect(capture, params)
    if name == "nbs_gdp_yoy":
        from ashare_data.providers.free_update_official import collect_gdp
        return collect_gdp(capture)
    if name == "sge_benchmark":
        from ashare_data.providers.free_update_official import collect_sge
        return collect_sge(capture, params["kind"])
    if name in {"fund_etf_scale_sse", "stock_fund_flow_individual", "energy_carbon_bj"}:
        from ashare_data.providers.free_update_public_pages import collect
        return collect(capture, name, params)
    if name == "macro_indicator_em":
        from ashare_data.providers.free_update_macro_contracts import INDICATORS
        definition = INDICATORS[params["indicator"]]
        source = direct_records(capture, definition["dataset"], {})
        capture.dataset_metadata = {**capture.dataset_metadata, **definition, "publication_date": None,
            "source_projection": "one named actual indicator; source row/page counts unchanged",
            "limitation": "只映射实际指标；统计期不冒充发布日期，未提供预测值"}
        return [{"日期": r["REPORT_DATE"], "statistical_month": r["REPORT_DATE"][:7],
                 "indicator": params["indicator"], "value": r[definition["field"]],
                 "source_field": definition["field"], "publication_date": None} for r in source]
    if name == "sina_macro_page":
        from ashare_data.providers.free_update_macro_pages import collect
        return collect(capture,params)
    if name == "cpi_rate_em":
        field={"mom":"NATIONAL_SEQUENTIAL","yoy":"NATIONAL_SAME"}[params["indicator"]]
        source=direct_records(capture,"macro_china_cpi",{})
        capture.dataset_metadata={**capture.dataset_metadata, "source":"Eastmoney RPT_ECONOMY_CPI",
            "source_projection":"one named actual indicator; source row/page counts unchanged",
            "field":field,"unit":"percent","time_basis":"statistical_month",
            "publication_date":None,"forecast":None,
            "limitation":"原金十报告的预测和发布日期未由此来源提供；只映射全国 CPI 实际同比/环比值"}
        return [{"日期":r["REPORT_DATE"],"statistical_month":r["REPORT_DATE"][:7],
                 "indicator":params["indicator"],"value":r[field],"source_field":field,
                 "publication_date":None} for r in source]
    if name in {"stock_zh_a_daily", "tencent_raw_daily", "sina_raw_daily", "sina_period_bars", "sina_trade_calendar"}:
        from ashare_data.providers.free_update_equities import collect
        return collect(capture,name,params)
    specs=json.loads(Path(__file__).with_name("public-macro-requests.json").read_text())["specs"]
    if name in specs:
        from urllib.parse import urlencode
        spec=specs[name]
        query=dict(spec["parameters"])
        if spec["kind"] == "jin10_latest":
            if params:
                raise ValueError("Jin10 latest report does not accept date-window parameters")
            headers={"x-app-id":"rU6QIu7JHe2gOUeR","x-csrf-token":"x-csrf-token","x-version":"1.0.0"}
            values=json.loads(capture.get(spec["url"]+"?"+urlencode(query),headers))["data"]["values"]
            if any(len(row)!=4 for row in values):
                raise ValueError("Jin10 indicator schema changed")
            return [dict(zip(("日期","今值","预测值","前值"),row),indicator=name) for row in values]
        from ashare_data.providers.free_update_macro_history import collect
        records = collect(capture,spec,params)
        if name == 'macro_china_hgjck':
            from ashare_data.providers.free_update_macro_contracts import customs_actuals
            records = customs_actuals(records)
            capture.dataset_metadata.update(raw_amount_unit='1000 USD',
                derived_columns={'TRADE_BALANCE_100M_USD':'(EXIT_BASE - IMPORT_BASE) / 100000'},
                unit_evidence='Eastmoney hgjck.js: raw amount fieldfmt zoom=-5 under 亿美元 heading')
        return records
    if name in {"tdx_blocks","tdx_members"}:
        from ashare_data.providers.tdx_blocks import collect
        return collect(capture,name,params)
    if name == "ths_hot":
        body=capture.get("https://eq.10jqka.com.cn/open/api/hot_list/v1/hot_stock/a/hour/data.txt")
        value=json.loads(body)
        if value.get("status_code") != 0:
            raise ValueError("THS hot-list status is not success")
        rows=value["data"]["stock_list"]
        capture.dataset_metadata={"complete_returned_window":True,"source_count":len(rows),
            "complete_history":False,"scope":"current published hourly hot-stock list; not entire market or rank history"}
        return rows
    if name == "treasury_rates":
        from urllib.parse import urlencode
        import xml.etree.ElementTree as ET
        url="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?"
        root=ET.fromstring(capture.get(url+urlencode({"data":params["kind"],"field_tdr_date_value":params["year"]})))
        rows=[]
        for properties in root.iter():
            if properties.tag.rsplit("}",1)[-1] == "link" and properties.get("rel") == "next":
                raise ValueError("Treasury feed is paginated; unreceived pages cannot pass")
            if properties.tag.rsplit("}",1)[-1] == "properties":
                row={c.tag.rsplit("}",1)[-1]:c.text for c in properties}
                day=row.get("NEW_DATE") or row.get("QUOTE_DATE") or row.get("INDEX_DATE")
                if day:
                    row["date"]=day[:10]
                rows.append(row)
        if not rows or any(r.get("date", "")[:4] != str(params["year"]) for r in rows):
            raise ValueError("Treasury feed empty or outside requested year")
        capture.dataset_metadata={"complete_returned_window":True,"source_count":len(rows),
            "complete_history":False,"source_window":"official Treasury feed for requested year " + str(params["year"]),
            "scope":{"kind":params["kind"],"year":str(params["year"])},"date_timezone":"America/New_York"}
        return rows
    raise ValueError("unknown direct adapter")


def special(ak, name, params):
    import pandas as pd
    if name == "fund_etf_only_szse":
        from collections import Counter
        frame=ak.fund_etf_scale_szse(**params)
        if "基金类别" not in frame or frame["基金类别"].isna().any():
            raise ValueError("SZSE fund classification absent")
        counts=dict(Counter(frame["基金类别"]))
        result=frame.loc[frame["基金类别"] == "ETF"].copy()
        if result.empty:
            raise ValueError("SZSE source has no explicitly classified ETFs")
        result.attrs["source_coverage"]={"complete_universe":True,"exchange":"SZSE",
            "universe_definition":"all explicitly ETF-classified rows in the complete returned official XLSX",
            "source_fund_rows":len(frame),"source_type_counts":counts,"selected_etf_rows":len(result),
            "excluded_non_etf_rows":len(frame)-len(result),"share_statistical_date":None,
            "limitation":"listing date is not the date of current fund shares; historical/delisted universe not provided"}
        return result
    if name == "china_government_yield":
        frame = ak.bond_china_yield(**params)
        # The upstream default also returns credit curves, not government debt.
        return frame.loc[frame["曲线名称"] == "中债国债收益率曲线"].copy()
    if name == "energy_oil_detail_latest":
        dates = ak.energy_oil_hist()
        if dates.empty or "调整日期" not in dates:
            raise ValueError("oil adjustment calendar missing")
        day = pd.to_datetime(dates["调整日期"]).max().strftime("%Y%m%d")
        result = ak.energy_oil_detail(date=day)
        result["调整日期"] = day
        return result
    if name == "futures_resample":
        frame = ak.futures_main_sina(**{k:v for k,v in params.items() if k != "period"})
        frame["日期"] = pd.to_datetime(frame["日期"])
        rules = {"开盘价":"first", "最高价":"max", "最低价":"min", "收盘价":"last",
                 "成交量":"sum", "持仓量":"last"}
        frame = frame.set_index("日期")
        for col in rules:
            if col in frame:
                frame[col] = pd.to_numeric(frame[col])
        groups = frame.groupby(frame.index.to_period(params["period"].replace("ME", "M")))
        result = groups.agg({k:v for k,v in rules.items() if k in frame}).dropna()
        actual_dates = groups.apply(lambda x: x.index.max())
        result["日期"] = actual_dates
        result["周期开始"] = result.index.start_time
        result["周期结束"] = result.index.end_time.normalize()
        result["周期未结束"] = result["周期结束"] >= pd.Timestamp(params["end_date"])
        return result.reset_index(drop=True).rename(columns={"开盘价":"开盘","最高价":"最高","最低价":"最低","收盘价":"收盘"})
    return getattr(ak, name)(**params)


def run(request):
    job = request["job"]
    allowed = {c["function"] for i in catalog()["items"] for c in i["calls"]}
    if job["function"] not in allowed:
        raise ValueError("function is not part of the frozen menu")
    if job.get("implementation_sha256") != implementation_identity():
        raise ValueError("implementation changed after planning; create a fresh plan")
    capture = Capture(request["raw_root"], request.get("blocked_hosts", []),
                      request_limit=request.get("request_limit", 40),journal_path=request.get("journal_path"))
    started = now()
    try:
        ak_version=None
        direct_specs=json.loads(Path(__file__).with_name("public-macro-requests.json").read_text())["specs"]
        from ashare_data.providers.free_update_news import NEWS
        from ashare_data.providers.free_update_billboard import FUNCTIONS as BILLBOARD
        direct_specs.update(dict.fromkeys(BILLBOARD | {"safe_rmb_window"}))
        direct_specs.update(dict.fromkeys(NEWS))
        from ashare_data.providers.free_update_policy_releases import FUNCTIONS as POLICY
        direct_specs.update(dict.fromkeys(POLICY))
        from ashare_data.providers.free_update_shareholders import REPORTS
        direct_specs.update(dict.fromkeys(REPORTS))
        from ashare_data.providers.free_update_period_tables import TABLES
        direct_specs.update(dict.fromkeys(TABLES | {"macro_china_au_report", "stock_fund_flow_industry", "news_cctv", "stock_value_em", "stock_profit_forecast_em",
            "stock_zh_a_disclosure_report_cninfo", "macro_china_shibor_all", "energy_oil_hist", "energy_oil_detail_latest", "spot_goods",
            "bond_zh_cov", "bond_zh_cov_info", "stock_zh_a_st_em", "index_stock_cons_csindex", "fund_etf_only_szse", "sw_index_first_cons", "sw_index_second_cons", "sw_index_third_cons", "stock_financial_analysis_indicator_em", "energy_carbon_hb", "macro_china_new_house_price"}))
        from ashare_data.providers.free_update_limit_pools import SPECS
        direct_specs.update(dict.fromkeys(set(SPECS) | {"stock_zygc_em", "szse_margin_summary", "szse_margin_latest", "futures_zh_minute_sina", "soozhu_province_rank", "sina_native_minutes", "macro_china_market_margin_sh", "china_government_yield"}))
        from ashare_data.providers.free_update_futures import FUNCTIONS as FUTURES
        direct_specs.update(dict.fromkeys(FUTURES))
        from ashare_data.providers.free_update_tdx import FUNCTIONS as NATIVE
        direct_specs.update(dict.fromkeys(NATIVE))
        from ashare_data.providers.free_update_industry_directory import LEVELS
        direct_specs.update(dict.fromkeys(LEVELS))
        from ashare_data.providers.free_update_board_bars import FUNCTIONS as BOARD_BARS
        direct_specs.update(dict.fromkeys(BOARD_BARS))
        from ashare_data.providers.free_update_legacy_reference import FUNCTIONS
        if job["function"] in {"ths_hot","treasury_rates","tdx_blocks","tdx_members","stock_zh_a_daily","tencent_raw_daily","sina_raw_daily","sina_period_bars","sina_trade_calendar","sina_macro_page","cpi_rate_em","macro_indicator_em","fund_etf_scale_sse","stock_fund_flow_individual","energy_carbon_bj","nbs_gdp_yoy","sge_benchmark","security_events_em"} or job["function"] in direct_specs or job["function"] in FUNCTIONS:
            records=direct_records(capture,job["function"],job["parameters"])
        else:
            from ashare_data.providers.akshare_loader import ReviewedAKShare
            import pandas as pd
            ak = ReviewedAKShare()
            ak_version=ak.version
            capture.install()
            if job["function"] in {"stock_profile_cninfo", "stock_new_ipo_cninfo"}:
                from ashare_data.providers.free_update_reference import collect_cninfo
                records = collect_cninfo(capture, job["function"], job["parameters"],
                    lambda: special(ak, job["function"], job["parameters"]))
            else:
                frame = special(ak, job["function"], job["parameters"])
                if not isinstance(frame, pd.DataFrame):
                    raise ValueError("adapter did not return a DataFrame")
                if frame.columns.has_duplicates:
                    raise ValueError("duplicate provider column labels")
                if "source_coverage" in frame.attrs:
                    capture.dataset_metadata=frame.attrs["source_coverage"]
                records = json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
            from ashare_data.providers.free_update_spot_contracts import PRODUCTS, YEAR_TREND, enrich
            if job["function"] in PRODUCTS or job["function"] == YEAR_TREND:
                records = enrich(capture, job["function"])
            if job["function"] == "spot_price_qh":
                from ashare_data.providers.free_update_commodity_reports import qh_enrich
                records = qh_enrich(capture)
        observed_day=request.get("observation_as_of",request["as_of"])
        if job["function"] in FUTURES and capture.dataset_metadata["source_last_date"] > observed_day:
            raise ValueError("future futures actual in native response; do not hide it with a requested date filter")
        empty_window = (not records and job["function"] == "stock_zh_a_disclosure_report_cninfo"
                        and capture.dataset_metadata.get("source_count") == 0
                        and capture.dataset_metadata.get("complete_requested_security_window") is True)
        assessment = {"freshness": "verified_empty_source_window", "live_sample_acceptance": False} if empty_window else assess_records(records, mode=job["mode"], as_of=observed_day)
        if not capture.receipts:
            raise ValueError("missing captured HTTP evidence; cannot claim a live sample")
        return {"status": "empty_source_window" if empty_window else "received", "started_at": started, "finished_at": now(),
                "records": records, "assessment": assessment, "receipts": capture.receipts,
                "akshare_version": ak_version, "scope": "explicit_context_sample",
                "coverage":capture.dataset_metadata,
                "observation_as_of":observed_day,
                "implementation_sha256": job["implementation_sha256"]}
    except Exception as exc:
        from ashare_data.providers.free_update_budget import ResourceBudgetReached
        return {"status": "blocked" if isinstance(exc, SourceBlocked) else "failed",
                "local_resource_budget": isinstance(exc, ResourceBudgetReached),
                "started_at": started, "finished_at": now(), "error": type(exc).__name__ + ": " + str(exc)[:1000],
                "records": [], "receipts": capture.receipts}


def main():
    request_path, output_path = map(Path, sys.argv[1:])
    request = json.loads(request_path.read_text())
    # A parent interruption cannot leave an unbounded provider process behind.
    import signal
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.setitimer(signal.ITIMER_REAL, float(request.get("wall_timeout",90)))
    # Suppress provider progress on stdout; the output file is the only protocol.
    from contextlib import redirect_stdout, redirect_stderr
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = run(request)
    except Exception as exc:
        result = {"status":"failed", "error":type(exc).__name__+": "+str(exc)[:1000],
                  "records":[], "receipts":[]}
    output_path.write_bytes(encoded(result))


if __name__ == "__main__":
    main()
