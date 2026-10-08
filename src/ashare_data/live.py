"""Direct public-source data. Native defaults are explicit; this is not jqdatasdk."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import pandas as pd

from .model import DataError, TZ, require
from .transport import Transport

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
            require(values["low"] <= min(values["open"], values["close"]) <=
                    max(values["open"], values["close"]) <= values["high"],
                    "SOURCE_SCHEMA_ERROR", "OHLC 顺序矛盾")
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


class Client:
    """No init/import/snapshot required. A cache directory is optional."""

    def __init__(self, *, cache=None, cache_mode="prefer", cache_ttl=300, timeout=15):
        self.transport = Transport(cache=cache, cache_mode=cache_mode, cache_ttl=cache_ttl, timeout=timeout)

    def get_price(self, security, start_date=None, end_date=None, frequency="daily", fields=None,
                  skip_paused=False, fq=None, count=None, panel=False, fill_paused=False,
                  round=False, *, strict=False):
        """Inclusive source labels; count counts returned source bars, never fabricated grid rows."""
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
        require(not (scale == 240 and "money" in wanted), "UNSUPPORTED_FIELD",
                "当前源日线不提供成交额；日线使用 OHLCV，money 可用于 1m/5m")
        require(start_date is None or count is None, "INVALID_REQUEST", "start_date 和 count 不能同时传入")
        if start_date is None and count is None:
            count = 20
        require(count is None or (isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 1000),
                "INVALID_COUNT", "count 须为 1..1000")
        now = datetime.now(TZ)
        end = _dt(end_date, end=True) if end_date is not None else now
        require(end.date() <= now.date(), "INVALID_DATE", "不接受未来日期")
        start = _dt(start_date) if start_date is not None else None
        require(start is None or start <= end, "INVALID_RANGE", "start_date 不得晚于 end_date")
        # These endpoints expose only a bounded recent window, not arbitrary historical paging.
        # Fixed size for explicit dates keeps the same request cacheable across days.
        requested = min(MAX_BARS, count + 1) if count and end_date is None else MAX_BARS
        all_records, provenance = [], []
        for code, source_symbol in zip(securities, symbols):
            url = KLINE_URL + "?" + urlencode({"symbol": source_symbol, "scale": scale, "ma": "no", "datalen": requested})
            raw, meta = self.transport.read(url, lambda b: _bars(b, scale))
            # A later query must never upgrade a partial observation in an old response.
            # New responses also retain request start, so crossing a boundary in flight
            # cannot promote a cached row later. Legacy caches use their observed_at.
            observed = _observation_time(meta["observed_at"])
            started = _observation_time(meta.get("request_started_at", meta["observed_at"]))
            require(started <= observed, "CACHE_CORRUPT", "缓存请求开始时间晚于观测时间")
            cutoff = min(now, started, observed)
            complete = [r for r in raw if
                        (_dt(r["day"]) if scale != 240 else
                         datetime.combine(_dt(r["day"]).date(), time(15, 5), TZ)) <= cutoff]
            require(complete, "NO_COMPLETED_BARS",
                    "该响应抓取时没有可确认结束的记录；等待闭合后可显式 cache_mode='refresh' 重新取数")
            labels = [_dt(r["day"]) for r in complete]
            chosen = [r for r, t in zip(complete, labels) if (start is None or t >= start) and t <= end]
            bounds = {"security": code, "source_first": complete[0]["day"], "source_last": complete[-1]["day"],
                      "returned_window_only": True, "full_market_history": False}
            if start is not None:
                # A date-only start asks for the day's source window, not for a midnight bar.
                lower_ok = start.date() >= labels[0].date() if scale == 240 or start.time() == time.min else start >= labels[0]
                if scale != 240 and start.time() == time.min and start.date() == labels[0].date():
                    lower_ok = labels[0].time() <= (time(9, 31) if scale == 1 else time(9, 35))
                require(lower_ok, "COVERAGE_INCOMPLETE", "起点早于源近期窗口；不能声称覆盖指定历史范围", bounds)
            require(chosen and (count is None or len(chosen) >= count), "COVERAGE_INCOMPLETE",
                    "源近期窗口内记录不足；缩短日期/count 或等待有数据的交易时段", bounds)
            if count:
                chosen = chosen[-count:]
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
            provenance.append({**meta, **bounds, "completion_cutoff": cutoff.isoformat(),
                               "completion_basis": "request_started_at" if "request_started_at" in meta
                               else "legacy_observed_at", "irregular_intervals": gaps, "source_rows": chosen})
        frame = pd.DataFrame(all_records).sort_values(["time", "code"], ignore_index=True)
        if scalar:
            frame = frame.drop(columns="code").set_index("time")
        frame.attrs = {"source": "sina_public", "frequency": frequency, "adjustment": None,
                       "timezone": "Asia/Shanghai", "price_unit": "CNY/share", "volume_unit": "share", "money_unit": "CNY",
                       "time_label": "source day field; inclusive filter; intraday interval boundaries unverified",
                       "available_at": None, "point_in_time_verified": False,
                       "coverage": "bounded source observations; no paused fill, grid synthesis or completeness guarantee",
                       "provenance": provenance}
        return frame

    def get_security_info(self, security):
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
              round=False, *, cache=None, cache_mode="prefer", cache_ttl=300, timeout=15, strict=False):
    return Client(cache=cache, cache_mode=cache_mode, cache_ttl=cache_ttl, timeout=timeout).get_price(
        security, start_date, end_date, frequency, fields, skip_paused, fq, count, panel, fill_paused, round, strict=strict)


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
    return {"version": "0.2.0rc2", "mode": "direct_public_source", "source": "sina_public",
            "frequency": ["daily", "1m", "5m"], "adjustment": [None],
            "default_fields": DEFAULT_FIELDS.copy(), "minute_extra_fields": ["money"],
            "count": [1, 1000], "max_securities": 10, "max_source_window": MAX_BARS,
            "automatic_source_fallback": False, "cache": ["disabled", "prefer", "only", "refresh"],
            "get_trade_days": "SSE current published annual schedule",
            "get_security_info": "current identity for explicit Shanghai/Shenzhen A-share code",
            "get_all_securities": "unsupported: no full historical membership evidence",
            "joinquant_equivalent": False, "historical_minute_coverage_guaranteed": False,
            "offline_snapshot_api": "Store; explicit advanced API retained"}
