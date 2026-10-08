"""Direct public-source data. Native defaults are explicit; this is not jqdatasdk."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import pandas as pd

from .model import DataError, TZ, canonical, digest, require, timestamp, validate_ohlc
from .contracts import CoverageContract, data_age, label_time, unknown_coverage
from .transport import Transport
from .visibility import LEVELS, select_visible
from .reconciliation import reconcile_turnover

KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
CALENDAR_URL = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
DEFAULT_FIELDS = ["open", "close", "high", "low", "volume"]
FREQUENCIES = {"daily": 240, "1d": 240, "minute": 1, "1m": 1, "5m": 5}
MAX_BARS = 1023


def _security(value):
    require(isinstance(value, str) and re.fullmatch(r"(?:60\d{4}|68\d{4})\.XSHG|(?:00\d{4}|30\d{4})\.XSHE", value),
            "INVALID_SECURITY", "使用沪深 A 股聚宽格式代码，例如 600000.XSHG、000001.XSHE；暂不支持指数/基金/北交所")
    return ("sh" if value.endswith("XSHG") else "sz") + value[:6]


def _dt(value, *, end=False):
    try:
        if isinstance(value, str):
            value = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
        if isinstance(value, datetime):
            return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
        if isinstance(value, date):
            return datetime.combine(value, time.max if end else time.min, TZ)
    except (ValueError, TypeError):
        pass
    raise DataError("INVALID_DATE", "日期/时间须为 ISO 格式；不带时区按 Asia/Shanghai 解释")


def _decimal(value):
    try:
        n = Decimal(str(value))
        require(not isinstance(value, bool) and n.is_finite() and 0 <= n < Decimal("1e17"),
                "SOURCE_SCHEMA_ERROR", "行情包含非法数值")
        return n
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DataError("SOURCE_SCHEMA_ERROR", "行情数值无法解析") from exc


def _bars(body, scale):
    try:
        data = json.loads(body)
        require(data is not None and data != [], "SOURCE_NO_DATA",
                "源返回 null/空数据；代码可能不存在或源不支持此周期，不代表网络故障")
        require(isinstance(data, list), "SOURCE_SCHEMA_ERROR",
                "源返回错误对象或非 K 线数组响应，不是行情空集合",
                {"response_type": type(data).__name__,
                 "response_kind": "service_error_object" if isinstance(data, dict) and "error" in data
                 else "unexpected_json_type"})
        require(len(data) <= MAX_BARS, "SOURCE_SCHEMA_ERROR", "源返回记录超过上限")
        previous = None
        for row in data:
            require(isinstance(row, dict), "SOURCE_SCHEMA_ERROR", "K线不是对象")
            label = row["day"]
            require(isinstance(label, str) and len(label) == (10 if scale == 240 else 19),
                    "SOURCE_SCHEMA_ERROR", "源时间标签格式改变")
            stamp = _dt(label)
            require(previous is None or stamp > previous, "SOURCE_SCHEMA_ERROR", "K线重复或未按时间递增")
            previous = stamp
            values = {f: _decimal(row[f]) for f in DEFAULT_FIELDS}
            validate_ohlc(*(values[k] for k in ("open", "high", "low", "close")), code="SOURCE_SCHEMA_ERROR")
            require(values["volume"] == values["volume"].to_integral_value(),
                    "SOURCE_SCHEMA_ERROR", "股数必须为整数")
            if "amount" in row:
                _decimal(row["amount"])
        return data
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise DataError("SOURCE_SCHEMA_ERROR", "公开源 K 线格式改变") from exc


def _calendar(body):
    try:
        html = body.decode("utf-8")
        text = re.sub(r"<[^>]+>", " ", html)
        year = int(re.search(r"(20\d{2})年休市安排", text).group(1))
        ranges = re.findall(r"(\d{1,2})月(\d{1,2})日（星期.）至(\d{1,2})月(\d{1,2})日（星期.）休市", text)
        require(len(ranges) == 7, "SOURCE_SCHEMA_ERROR", "年度休市页面格式改变；不猜测日历")
        closed = set()
        for am, ad, bm, bd in ranges:
            a, b = date(year, int(am), int(ad)), date(year, int(bm), int(bd))
            require(a <= b and (b-a).days <= 15, "SOURCE_SCHEMA_ERROR", "休市区间无效")
            closed.update(a + timedelta(days=i) for i in range((b-a).days+1))
        return year, closed
    except (AttributeError, ValueError, UnicodeError) as exc:
        raise DataError("SOURCE_SCHEMA_ERROR", "无法解析交易所年度休市页面") from exc


def _completed_rows(raw, meta, wall_now, scale):
    observed = _observation_time(meta["observed_at"])
    started = _observation_time(meta.get("request_started_at", meta["observed_at"]))
    require(started <= observed, "CACHE_CORRUPT", "缓存请求开始时间晚于观测时间")
    cutoff = min(wall_now, started, observed)
    rows = [r for r in raw if (_dt(r["day"]) if scale != 240 else
            datetime.combine(_dt(r["day"]).date(), time(15, 5), TZ)) <= cutoff]
    return rows, cutoff


class Client:
    """No init/import/snapshot required. A cache directory is optional."""

    def __init__(self, *, cache=None, cache_mode="prefer", cache_ttl=300, timeout=15, coverage_contract=None):
        self.transport = Transport(cache=cache, cache_mode=cache_mode, cache_ttl=cache_ttl, timeout=timeout)
        require(coverage_contract is None or isinstance(coverage_contract, CoverageContract),
                "INVALID_CONTRACT", "coverage_contract 必须是 A数达 CoverageContract")
        self.coverage_contract = coverage_contract
        self._clock, self._visibility = None, None
        self._requirements = {}

    def at(self, as_of, *, visibility="verified", require_complete=False, require_fresh=False, require_final=False,
           require_tradable=False):
        """A pinned, offline context. Strategies supply a clock and desired guarantees."""
        clock = timestamp(as_of)
        require(clock <= datetime.now(TZ), "INVALID_TIME", "策略时钟不能在未来")
        require(visibility in LEVELS, "INVALID_VISIBILITY", "visibility 支持 assumed/received/verified")
        require(visibility != "verified", "VISIBILITY_UNKNOWN",
                "当前公开源尚无证据级历史 PIT；可显式选择 assumed 研究模型或 received 本机观测等级",
                {"as_of": clock.isoformat(), "point_in_time_verified": False, "source": "sina_public"})
        require(all(type(v) is bool for v in (require_complete, require_fresh, require_final, require_tradable)),
                "INVALID_REQUEST", "准入要求须为布尔值")
        context = Client(cache=self.transport.root, cache_mode="only", coverage_contract=self.coverage_contract)
        context.transport = self.transport.freeze(received_by=clock if visibility == "received" else None)
        context._clock, context._visibility = clock, visibility
        context._requirements = {"require_complete": require_complete, "require_fresh": require_fresh,
                                 "require_final": require_final, "require_tradable": require_tradable}
        return context

    def trading_status(self, security, *, as_of=None):
        """No HTTP/cache dependency: calendar and interval-state admission only."""
        _security(security)
        require(as_of is None or self._clock is None, "INVALID_REQUEST", "固定上下文不能替换策略时钟")
        clock = timestamp(as_of) if as_of is not None else self._clock or datetime.now(TZ)
        require(clock <= datetime.now(TZ), "INVALID_TIME", "策略时钟不能在未来")
        if self.coverage_contract is None:
            return {"state": "unknown", "tradable": False, "as_of": clock.isoformat(),
                    "reasons": ["CALENDAR_UNKNOWN", "TRADING_STATUS_UNKNOWN"], "contract_id": None}
        require(self.coverage_contract._data["provider"] == "sina_public", "CONTRACT_SCOPE_MISMATCH", "契约来源不符")
        return self.coverage_contract.trading_status(security, clock)

    def _bar_observations(self, url, scale, wall_now, *, allow_empty=False):
        """Materialize actual pinned observations, never synthesize missing labels."""
        windows = self.transport.read_windows(url, lambda body: _bars(body, scale))
        require(sum(len(rows) for rows, _ in windows) <= 100000, "BOUNDED_QUERY", "固定观测原始记录总数超过100000")
        raw_by_label, by_label, observations, conflicts = {}, {}, {}, set()
        any_completed = False
        for raw, meta in windows:
            completed, cutoff = _completed_rows(raw, meta, wall_now, scale)
            any_completed = any_completed or bool(completed)
            visibility = {"level": "unrestricted_research", "as_of": None, "point_in_time_verified": False,
                          "available_at": None, "revision_history": "unknown", "finality": "CLOSED_PROVISIONAL",
                          "source_version_observed_at": meta["observed_at"], "raw_hash": meta["sha256"],
                          "assumptions": ["原接口软件时间门槛不证明最终发布"]}
            if self._clock is not None:
                completed, visibility = select_visible(completed, meta, as_of=self._clock, level=self._visibility,
                                                       daily=scale == 240, contract=self.coverage_contract)
            identity = meta.get("observation_id") or digest({k: meta.get(k) for k in
                        ("url", "observed_at", "request_started_at", "sha256")})
            observations[identity] = {**meta, "completion_cutoff": cutoff.isoformat(), "visibility": visibility}
            for row in raw:
                raw_by_label[row["day"]] = row
            for row in completed:
                label = row["day"]
                if label in by_label:
                    old, old_id = by_label[label]
                    before = timestamp(observations[old_id]["observed_at"])
                    current = timestamp(meta["observed_at"])
                    if before == current and canonical(old) != canonical(row):
                        conflicts.add(label)
                    elif current > before:
                        conflicts.discard(label)
                by_label[label] = (row, identity)
        require(allow_empty or any_completed, "NO_COMPLETED_BARS", "固定观测抓取时没有通过软件闭合阈值的记录")
        require(allow_empty or by_label, "NO_VISIBLE_BARS", "此策略时钟下没有满足所选可见性模型的记录")
        return ([raw_by_label[k] for k in sorted(raw_by_label)],
                [by_label[k][0] for k in sorted(by_label)], by_label, observations, conflicts)

    @staticmethod
    def _observation_metadata(chosen, by_label, observations):
        ids = {by_label[row["day"]][1] for row in chosen}
        parts = sorted((observations[i] for i in ids), key=lambda p: (timestamp(p["observed_at"]), p["sha256"]))
        meta = {k: v for k, v in parts[-1].items() if k != "visibility"}
        visible = dict(parts[-1]["visibility"])
        if len(parts) > 1:
            # No single raw response/hash contains a composite query result.
            meta.update(url=None, sha256=None, observation_id=None, request_started_at=None,
                        completion_cutoff=None, cache_window_reused=any(p.get("cache_window_reused", False) for p in parts))
            visible["raw_hash"] = None
        meta["response_observations"] = [{k: v for k, v in part.items() if k != "visibility"} for part in parts]
        meta["row_observations"] = [{"source_label": row["day"], "observation_id": by_label[row["day"]][1],
            **{key: observations[by_label[row["day"]][1]].get(key) for key in
               ("url", "sha256", "observed_at", "request_started_at", "completion_cutoff")}} for row in chosen]
        meta["observation_selection"] = "latest eligible received observation per source label; absence is not a tombstone"
        meta["observation_time_meaning"] = "latest contributing receipt; per-row times retained"
        visible["raw_hashes"] = sorted({p["sha256"] for p in parts})
        visible["component_visibility"] = [p["visibility"] for p in parts]
        return meta, visible

    def get_price(self, security, start_date=None, end_date=None, frequency="daily", fields=None,
                  skip_paused=False, fq=None, count=None, panel=False, fill_paused=False,
                  round=False, *, strict=False, as_of=None, visibility="verified",
                  require_complete=False, require_fresh=False, require_final=False, require_tradable=False):
        """Inclusive source labels; count counts returned source bars, never fabricated grid rows."""
        require(all(type(v) is bool for v in (require_complete, require_fresh, require_final, require_tradable)),
                "INVALID_REQUEST", "准入要求须为布尔值")
        require(visibility in LEVELS, "INVALID_VISIBILITY", "visibility 支持 assumed/received/verified")
        require(self._clock is None or visibility == "verified", "INVALID_REQUEST", "上下文的可见性等级已固定，不能在查询中替换")
        if as_of is not None:
            require(self._clock is None, "INVALID_REQUEST", "固定上下文不能替换策略时钟")
            return self.at(as_of, visibility=visibility, require_complete=require_complete,
                           require_fresh=require_fresh, require_final=require_final,
                           require_tradable=require_tradable).get_price(
                security, start_date, end_date, frequency, fields, skip_paused, fq, count,
                panel, fill_paused, round, strict=strict)
        require_complete = require_complete or self._requirements.get("require_complete", False)
        require(self._clock is not None or visibility == "verified", "INVALID_REQUEST", "visibility 须与 as_of 或固定上下文一起使用")
        require_fresh = require_fresh or self._requirements.get("require_fresh", False)
        require_final = require_final or self._requirements.get("require_final", False)
        require_tradable = require_tradable or self._requirements.get("require_tradable", False)
        require(all(type(v) is bool for v in (require_complete, require_fresh, require_final, require_tradable)), "INVALID_REQUEST", "准入要求须为布尔值")
        require(not require_final, "BAR_NOT_FINAL", "当前源没有最终发布/修订水位证据；时间门槛不能证明 FINALIZED")
        require(not require_complete or self.coverage_contract is not None, "COVERAGE_UNKNOWN",
                "缺来源标签、日历和证券状态契约，不能保证完整覆盖", unknown_coverage())
        require(not require_fresh or self.coverage_contract is not None, "FRESHNESS_UNKNOWN",
                "缺日历/session/证券状态证据，不能仅以 HTTP 或 cache TTL 判断行情新鲜")
        require(isinstance(frequency, str) and frequency in FREQUENCIES,
                "UNSUPPORTED_FREQUENCY", "支持 daily/1d、1m/minute、5m")
        require(fq is None, "UNSUPPORTED_ADJUSTMENT", "首版仅支持原始价格；请显式使用 fq=None，不能用它替代前复权策略")
        require(skip_paused is False and fill_paused is False, "UNSUPPORTED_PAUSED",
                "没有停牌证据，不能过滤或填充；使用 skip_paused=False, fill_paused=False 取得源实际记录")
        require(panel is False, "UNSUPPORTED_PANEL", "使用 panel=False 返回现代 DataFrame")
        require(round is False, "UNSUPPORTED_ROUND", "使用 round=False 保留源报价精度")
        require(isinstance(strict, bool), "INVALID_REQUEST", "strict 须为布尔值")
        scalar = isinstance(security, str)
        securities = [security] if scalar else security
        require(isinstance(securities, (list, tuple)) and 1 <= len(securities) <= 10,
                "REQUEST_TOO_LARGE", "一次允许 1..10 个显式证券，按顺序低频请求")
        symbols = [_security(s) for s in securities]
        require(len(set(securities)) == len(securities), "INVALID_REQUEST", "证券不能重复")
        wanted = DEFAULT_FIELDS.copy() if fields is None else fields
        require(isinstance(wanted, (list, tuple)) and wanted and all(isinstance(x, str) for x in wanted)
                and len(set(wanted)) == len(wanted), "INVALID_REQUEST", "fields 须为非空、不重复的字段列表")
        require(set(wanted) <= set(DEFAULT_FIELDS + ["money"]), "UNSUPPORTED_FIELD",
                "支持 open/close/high/low/volume 和分钟 money；其他字段尚未核验")
        scale = FREQUENCIES[frequency]
        canonical_frequency = "daily" if scale == 240 else f"{scale}m"
        if self.coverage_contract:
            require(self.coverage_contract.frequency == canonical_frequency
                    and self.coverage_contract._data["provider"] == "sina_public",
                    "CONTRACT_SCOPE_MISMATCH", "覆盖契约不适用于当前来源或频率")
        require(not (scale == 240 and "money" in wanted), "UNSUPPORTED_FIELD",
                "当前源日线不提供成交额；日线使用 OHLCV，money 可用于 1m/5m")
        require(start_date is None or count is None, "INVALID_REQUEST", "start_date 和 count 不能同时传入")
        if start_date is None and count is None:
            count = 20
        require(count is None or (isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 1000),
                "INVALID_COUNT", "count 须为 1..1000")
        wall_now = datetime.now(TZ)
        now = self._clock or wall_now
        end = _dt(end_date, end=True) if end_date is not None else now
        require(end.date() <= now.date(), "INVALID_DATE", "不接受未来日期")
        start = _dt(start_date) if start_date is not None else None
        require(start is None or start <= end, "INVALID_RANGE", "start_date 不得晚于 end_date")
        # These endpoints expose only a bounded recent window, not arbitrary historical paging.
        # Fixed size for explicit dates keeps the same request cacheable across days.
        requested = min(MAX_BARS, count + 1) if count and end_date is None and self._clock is None else MAX_BARS
        all_records, provenance = [], []
        for code, source_symbol in zip(securities, symbols):
            decision = self.trading_status(code)
            require(not require_tradable or decision["tradable"],
                    {"closed_market": "MARKET_CLOSED", "session_break": "SESSION_BREAK", "suspended": "SUSPENDED"}.get(decision["state"], "TRADING_STATUS_UNKNOWN"),
                    "A数达声明时段/证券状态准入未满足", decision)
            url = KLINE_URL + "?" + urlencode({"symbol": source_symbol, "scale": scale, "ma": "no", "datalen": requested})
            raw, complete, by_label, observations, conflicts = self._bar_observations(url, scale, wall_now)
            labels = [_dt(r["day"]) for r in complete]
            chosen = [r for r, t in zip(complete, labels) if (start is None or t >= start) and t <= end]
            bounds = {"security": code, "source_first": complete[0]["day"], "source_last": complete[-1]["day"],
                      "returned_window_only": True, "full_market_history": False}
            if start is not None and self.coverage_contract is None:
                # A date-only start asks for the day's source window, not for a midnight bar.
                lower_ok = start.date() >= labels[0].date() if scale == 240 or start.time() == time.min else start >= labels[0]
                if scale != 240 and start.time() == time.min and start.date() == labels[0].date():
                    lower_ok = labels[0].time() <= (time(9, 31) if scale == 1 else time(9, 35))
                require(lower_ok, "COVERAGE_INCOMPLETE", "起点早于源近期窗口；不能声称覆盖指定历史范围", bounds)
            coverage = unknown_coverage()
            if self.coverage_contract:
                grid_start = start or label_time(min(self.coverage_contract._days))
                coverage = self.coverage_contract.assess(code, [r["day"] for r in complete], grid_start,
                                                         end, count=count, as_of=now)
            require(not require_complete or coverage["complete"],
                    "COVERAGE_UNKNOWN" if coverage["status"] == "unknown" else "COVERAGE_INCOMPLETE",
                    "A数达覆盖准入未满足；不补价、补量或把缺数据解释为停牌", coverage)
            require(chosen and (count is None or len(chosen) >= count), "COVERAGE_INCOMPLETE",
                    "源近期窗口内记录不足；缩短日期/count 或等待有数据的交易时段", bounds)
            if count:
                chosen = chosen[-count:]
            require(not (conflicts & {row["day"] for row in chosen}), "OBSERVATION_CONFLICT",
                    "请求中的同一标签在同一观测时刻有冲突值，不能按窗口大小择一",
                    {"source_labels": sorted(conflicts & {row["day"] for row in chosen})})
            meta, visible = self._observation_metadata(chosen, by_label, observations)
            freshness = data_age(raw, chosen, meta, now, daily=scale == 240)
            if self.coverage_contract:
                freshness.update(self.coverage_contract.freshness(code, [r["day"] for r in complete], now))
                selected = self.coverage_contract.freshness(code, [r["day"] for r in chosen], now)
                freshness["selected_window_status"] = selected["source_watermark_status"]
                freshness["selected_latest_label"] = selected["last_eligible_label"]
                if freshness["status"] == "fresh" and selected["source_watermark_status"] != "fresh":
                    freshness["status"] = "stale_selected_window" if selected["source_watermark_status"] == "stale" else "unknown"
                freshness["admissible"] = freshness["status"] == "fresh"
            else:
                freshness.update(source_watermark_status="unknown", selected_window_status="unknown",
                                 trading_status=decision, admissible=False)
            require(not require_fresh or freshness["admissible"],
                    {"stale": "STALE_SOURCE", "stale_selected_window": "STALE_SELECTED_WINDOW",
                     "closed_market": "MARKET_CLOSED", "session_break": "SESSION_BREAK",
                     "suspended": "SUSPENDED"}.get(freshness["status"], "FRESHNESS_UNKNOWN"),
                    "数据准入须同时满足源及所选窗口水位、声明时段与证券状态；HTTP 成功不等于可用于 paper", freshness)
            gaps = []
            if scale != 240:
                for left, right in zip(chosen, chosen[1:]):
                    a, b = _dt(left["day"]), _dt(right["day"])
                    same_session = a.date() == b.date() and ((a.time() <= time(11, 30) and b.time() <= time(11, 30)) or a.time() >= time(13))
                    if same_session and b-a != timedelta(minutes=scale):
                        gaps.append({"left": left["day"], "right": right["day"],
                                     "classification": "closing_auction_source_interval" if scale == 1 and a.time() == time(14, 57) and b.time() == time(15) else "unverified_interval"})
            require(not strict or not gaps, "IRREGULAR_SOURCE_GRID", "源标签不连续；strict=False 可取原样数据并检查 attrs 的间隔记录", {"intervals": gaps})
            for row in chosen:
                require("money" not in wanted or "amount" in row, "SOURCE_FIELD_MISSING", "当前分钟源记录缺少成交额；可仅请求 OHLCV")
                record = {"time": _dt(row["day"]).replace(tzinfo=None), "code": code}
                record.update({field: int(_decimal(row[field])) if field == "volume" else
                               float(_decimal(row["amount" if field == "money" else field])) for field in wanted})
                all_records.append(record)
            provenance.append({**meta, **bounds,
                               "completion_basis": "per_observation" if len(meta["response_observations"]) > 1 else
                               "request_started_at" if meta.get("request_started_at") else "legacy_observed_at", "irregular_intervals": gaps, "source_rows": chosen,
                               "coverage_report": coverage, "freshness": freshness, "trading_status": decision,
                               "completion_model": "legacy_software_15_05" if scale == 240 else "source_label_as_end",
                               "closure_verified": False, "visibility": visible})
        frame = pd.DataFrame(all_records).sort_values(["time", "code"], ignore_index=True)
        if scalar:
            frame = frame.drop(columns="code").set_index("time")
        frame.attrs = {"source": "sina_public", "frequency": frequency, "adjustment": None,
                       "timezone": "Asia/Shanghai", "price_unit": "CNY/share", "volume_unit": "share", "money_unit": "CNY",
                       "time_label": "source day field; inclusive filter; intraday interval boundaries unverified",
                       "available_at": None, "point_in_time_verified": False,
                       "as_of": now.isoformat() if self._clock else None,
                       "visibility_level": self._visibility or "unrestricted_research",
                       "query_snapshot_id": self.transport.snapshot_id, "finality": "CLOSED_PROVISIONAL",
                       "trading_scope": "unknown" if self.coverage_contract is None else self.coverage_contract._data["trading_scope"],
                       "trade_totals_verified": False,
                       "coverage": "bounded source observations; no paused fill, grid synthesis or completeness guarantee",
                       "provenance": provenance}
        return frame

    def coverage(self, security, start_date, end_date, *, frequency="1m"):
        """Read a bounded source window, then assess facts; no network calendar inference."""
        _security(security)
        require(frequency in FREQUENCIES, "UNSUPPORTED_FREQUENCY", "未知频率")
        scale = FREQUENCIES[frequency]
        if self.coverage_contract is None:
            return unknown_coverage()  # Missing facts cannot be repaired by fetching another bar window.
        require(self.coverage_contract.frequency == ("daily" if scale == 240 else f"{scale}m")
                and self.coverage_contract._data["provider"] == "sina_public", "CONTRACT_SCOPE_MISMATCH", "契约频率/来源不符")
        clock = self._clock or datetime.now(TZ)
        empty = self.coverage_contract.assess(security, [], _dt(start_date), _dt(end_date, end=True), as_of=clock)
        if empty["complete"]:
            return empty  # Evidenced closed/suspended slots do not require a source call or filled bars.
        url = KLINE_URL + "?" + urlencode({"symbol": _security(security), "scale": scale, "ma": "no", "datalen": MAX_BARS})
        _, rows, _, _, _ = self._bar_observations(url, scale, datetime.now(TZ), allow_empty=True)
        return self.coverage_contract.assess(security, [r["day"] for r in rows], _dt(start_date),
                                             _dt(end_date, end=True), as_of=clock)

    def acquire_price(self, security, start_date, end_date, *, frequency="1m"):
        """Explicit one-window replenishment. Unsupported history stays a failed requirement."""
        require(self._clock is None and self.transport.mode != "only", "OFFLINE_QUERY", "固定/离线上下文禁止联网补齐")
        client = Client(cache=self.transport.root, cache_mode="refresh", timeout=self.transport.timeout,
                        coverage_contract=self.coverage_contract)
        try:
            frame = client.get_price(security, start_date=start_date, end_date=end_date, frequency=frequency)
        except DataError as exc:
            if exc.code == "COVERAGE_INCOMPLETE":
                exc.details["acquisition"] = {"attempted_windows": 1, "historical_paging_supported": False,
                                              "missing_capability": "arbitrary_history_or_missing_source_records",
                                              "automatic_fallback": False, "fabricated_rows": 0}
            raise
        return {"rows": frame.reset_index().to_dict("records"), "metadata": frame.attrs,
                "requirements_met": all(p["coverage_report"]["complete"] for p in frame.attrs["provenance"]),
                "trade_totals_verified": False, "attempted_windows": len(frame.attrs["provenance"]),
                "historical_paging_supported": False, "fabricated_rows": 0}

    def reconcile_day(self, security, trade_date):
        """Explicit small diagnostic: 1m + 5m + current quote, never a silent fallback."""
        require(self._clock is None, "OFFLINE_QUERY", "策略上下文不执行外部对账采集")
        source_symbol = _security(security)
        d = _dt(trade_date).date().isoformat()
        frames = {f: self.get_price(security, start_date=d, end_date=d, frequency=f,
                                   fields=DEFAULT_FIELDS + ["money"]) for f in ("1m", "5m")}
        def decode(body):
            try:
                match = re.fullmatch(r'var hq_str_' + source_symbol + r'="([^"\r\n]*)";\s*', body.decode("gb18030"))
                require(match is not None, "SOURCE_SCHEMA_ERROR", "日行情快照格式改变")
                parts = match.group(1).split(",")
                require(len(parts) >= 32, "SOURCE_NO_DATA", "日快照字段不足")
                require(parts[30] == d, "REFERENCE_DATE_MISMATCH", "当前 quote 不能充当其他日期的日累计参考")
                o, h, low, c = (_decimal(parts[i]) for i in (1, 4, 5, 3))
                validate_ohlc(o, h, low, c, code="SOURCE_SCHEMA_ERROR")
                _decimal(parts[8])
                _decimal(parts[9])
                return {"security": security, "trade_date": d, "quote_time": _dt(parts[30]+" "+parts[31]).isoformat(),
                        "volume": parts[8], "amount": parts[9], "volume_unit": "share", "amount_unit": "CNY",
                        "trading_scope": "unknown", "scope_evidence": None,
                        "raw_fields": parts, "uninterpreted_tail_field_33": parts[33] if len(parts) > 33 else None}
            except (UnicodeError, IndexError, ValueError, TypeError) as exc:
                raise DataError("SOURCE_SCHEMA_ERROR", "日快照无法解析") from exc
        quote, meta = self.transport.read("https://hq.sinajs.cn/list=" + source_symbol, decode)
        comparisons = {}
        for frequency, frame in frames.items():
            provenance = frame.attrs["provenance"][0]
            comparisons[frequency] = {**reconcile_turnover(provenance["source_rows"], quote),
                                      "first_label": provenance["source_rows"][0]["day"],
                                      "last_label": provenance["source_rows"][-1]["day"],
                                      "raw_hash": provenance["sha256"]}
        return {"security": security, "trade_date": d, "accepted": False,
                "same_vendor_comparison": True,
                "status": "failed" if any(v["status"] == "failed" for v in comparisons.values()) else "unknown",
                "reference": quote, "reference_provenance": meta, "comparisons": comparisons,
                "ownership": "A数达负责定位和补齐；不得由上层清洗或补造",
                "limitation": "日/分钟统计范围与最终性未证；238根不意味着缺两根，不解释尾字段为已验证盘后成交"}

    def get_security_info(self, security):
        require(self._clock is None, "VISIBILITY_UNKNOWN", "当前名称/证券身份接口不提供历史时点证明")
        source_symbol = _security(security)
        def decode(body):
            try:
                text = body.decode("gb18030")
                match = re.fullmatch(r'var hq_str_' + source_symbol + r'="([^"\r\n]*)";\s*', text)
                require(match is not None, "SOURCE_SCHEMA_ERROR", "证券行情响应格式改变")
                fields = match.group(1).split(",")
                require(len(fields) >= 32 and fields[0], "SOURCE_NO_DATA", "源没有此证券信息")
                as_of = _dt(fields[30] + " " + fields[31]).isoformat()
                return {"code": security, "display_name": fields[0], "exchange": security[7:],
                        "quote_time": as_of, "start_date": None, "end_date": None,
                        "historical_membership_supported": False}
            except (UnicodeError, ValueError) as exc:
                raise DataError("SOURCE_SCHEMA_ERROR", "无法解析证券信息") from exc
        result, meta = self.transport.read("https://hq.sinajs.cn/list=" + source_symbol, decode)
        return {**result, "provenance": meta}

    def calendar(self):
        require(self._clock is None, "CALENDAR_VISIBILITY_UNKNOWN", "当前年度网页没有历史公告时点证据；策略覆盖须使用版本化契约")
        (year, closed), meta = self.transport.read(CALENDAR_URL, _calendar)
        start = date(year, 1, 1)
        days = [start + timedelta(days=i) for i in range((date(year+1, 1, 1)-start).days)]
        return {"year": year, "exchange": "XSHG", "days": [d for d in days if d.weekday() < 5 and d not in closed],
                "scope": "SSE published annual schedule; not a historical publication-time service", "provenance": meta}

    def get_trade_days(self, start_date=None, end_date=None, count=None):
        require(start_date is None or count is None, "INVALID_REQUEST", "start_date 和 count 不能同时传入")
        require(count is None or isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 366,
                "INVALID_COUNT", "count 须为 1..366")
        info = self.calendar()
        end = _dt(end_date, end=True).date() if end_date is not None else datetime.now(TZ).date()
        start = _dt(start_date).date() if start_date is not None else date(info["year"], 1, 1)
        require(start <= end and start.year == end.year == info["year"], "CALENDAR_COVERAGE",
                "公开页面仅提供当前发布年度；不推测其他年份", {"supported_year": info["year"]})
        days = [d for d in info["days"] if start <= d <= end]
        require(count is None or len(days) >= count, "CALENDAR_COVERAGE", "该年度可用交易日不足 count")
        return days[-count:] if count else days


def get_price(security, start_date=None, end_date=None, frequency="daily", fields=None,
              skip_paused=False, fq=None, count=None, panel=False, fill_paused=False,
              round=False, *, cache=None, cache_mode="prefer", cache_ttl=300, timeout=15, strict=False,
              coverage_contract=None, as_of=None, visibility="verified", require_complete=False,
              require_fresh=False, require_final=False, require_tradable=False):
    return Client(cache=cache, cache_mode=cache_mode, cache_ttl=cache_ttl, timeout=timeout,
                  coverage_contract=coverage_contract).get_price(
        security, start_date, end_date, frequency, fields, skip_paused, fq, count, panel, fill_paused, round,
        strict=strict, as_of=as_of, visibility=visibility, require_complete=require_complete,
        require_fresh=require_fresh, require_final=require_final, require_tradable=require_tradable)


def get_security_info(security, **client_options):
    return Client(**client_options).get_security_info(security)


def get_trade_days(start_date=None, end_date=None, count=None, **client_options):
    return Client(**client_options).get_trade_days(start_date, end_date, count)


def get_all_securities(types=None, date=None):
    raise DataError("UNSUPPORTED_UNIVERSE", "尚无完整历史股票池证据；请对显式证券调用 get_security_info，不把当日列表当历史股票池")


def _observation_time(value):
    try:
        value = datetime.fromisoformat(value)
        require(value.tzinfo is not None and value.utcoffset() is not None,
                "CACHE_CORRUPT", "缓存观测时间必须含时区")
        return value.astimezone(TZ)
    except (TypeError, ValueError) as exc:
        raise DataError("CACHE_CORRUPT", "缓存观测时间无效") from exc


def capabilities():
    return {"version": "0.3.0.dev3", "mode": "direct_public_source", "source": "sina_public",
            "frequency": ["daily", "1m", "5m"], "adjustment": [None],
            "default_fields": DEFAULT_FIELDS.copy(), "minute_extra_fields": ["money"],
            "count": [1, 1000], "max_securities": 10, "max_source_window": MAX_BARS,
            "automatic_source_fallback": False, "cache": ["disabled", "prefer", "only", "refresh"],
            "get_trade_days": "SSE current published annual schedule",
            "get_security_info": "current identity for explicit Shanghai/Shenzhen A-share code",
            "get_all_securities": "unsupported: no full historical membership evidence",
            "visibility_levels": ["assumed", "received"], "verified_live_pit": False,
            "coverage_contract": "explicit calendar/session/status facts; no real Sina contract bundled",
            "coverage_contract_versions": [1, 2],
            "trading_status": "offline calendar/session/half-open interval status decision; supplied evidence only",
            "require_fresh": "source watermark + selected window + tradable; not finality or verified PIT",
            "corporate_actions": "unavailable", "historical_trading_status": "unavailable without evidence contract",
            "finalized_bars": False, "turnover_completeness": "unverified; reconciliation discrepancies block acceptance",
            "joinquant_equivalent": False, "historical_minute_coverage_guaranteed": False,
            "offline_snapshot_api": "Store; explicit advanced API retained"}
