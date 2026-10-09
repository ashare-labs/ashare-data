"""JoinQuant-inspired names, NOT a drop-in JQData or strategy API implementation."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .model import DataError, TZ, require, timestamp


class JQStyle:
    def __init__(self, snapshot, *, quality="observed"):
        self.snapshot = snapshot
        self.quality_policy = quality

    def get_price(self, security, start_date=None, end_date=None, frequency="1m", fields=None,
                  skip_paused=False, fq=None, count=None, panel=False, fill_paused=False,
                  round=False):
        require(frequency in {"1m", "minute"}, "UNSUPPORTED", "仅支持 1m/minute")
        require(fq is None, "UNSUPPORTED", "fq='pre'/'post' 暂无因子证据")
        require(count is None, "UNSUPPORTED", "count 暂不支持，须显式起止范围")
        require(skip_paused is False and fill_paused is False, "UNSUPPORTED",
                "不支持停牌过滤/填充，缺口按原生质量门禁报错")
        require(panel is False, "UNSUPPORTED", "不提供 pandas Panel/DataFrame；返回带元数据的 records")
        require(round is False, "UNSUPPORTED", "不提供 SDK 的 round 行为，保持源数值精度")
        require(start_date is not None and end_date is not None, "INVALID_REQUEST", "须显式 start_date/end_date")
        # Datetimes only. A bare date is too ambiguous for unverified vendor minute semantics.
        require(not isinstance(start_date, date) or isinstance(start_date, datetime),
                "INVALID_TIME", "分钟查询须为带时区时间，不接受裸日期")
        a, b = timestamp(start_date, minute=True), timestamp(end_date, minute=True)
        require(a <= b, "INVALID_RANGE", "start_date 不得晚于 end_date")
        wanted = ["open", "close", "high", "low", "volume", "money"] if fields is None else fields
        require(isinstance(wanted, (list, tuple)) and len(wanted) > 0 and len(set(wanted)) == len(wanted),
                "INVALID_REQUEST", "fields 须为不重复的非空列表")
        allowed = {"open", "close", "high", "low", "volume", "money"}
        require(set(wanted) <= allowed, "UNSUPPORTED", "不支持 factor/paused/limits/avg/pre_close 等字段")
        syms = [security] if isinstance(security, str) else security
        # Our public adapter contract: inclusive bar-END labels. At 14:55 return [14:54,14:55).
        result = self.snapshot.bars(syms, a-timedelta(minutes=1), b, quality=self.quality_policy,
                                    strict=True, as_of=b)
        rows = []
        for bar in result["rows"]:
            row = {"code": bar["symbol"], "time": bar["bar_end"], "quality": bar["quality"],
                   "bar_start": bar["bar_start"], "bar_end": bar["bar_end"],
                   "source_id": bar["source_id"], "available_at": bar["available_at"]}
            row.update({k: bar["amount" if k == "money" else k] for k in wanted})
            rows.append(row)
        return {**result, "rows": rows,
                "compatibility": "JQ-style subset; inclusive end labels; upstream 14:55 semantics unverified"}

    def get_all_securities(self, types=None, date=None):
        require(types is None or types == [] or types == ["stock"], "UNSUPPORTED", "仅支持 stock")
        require(date is not None, "INVALID_REQUEST", "须显式 date；不读取 today 或策略时钟")
        d = _date(date)
        # Documented local choice, not an upstream-equivalence claim.
        cutoff = datetime.combine(d, datetime.max.time(), TZ)
        result = self.snapshot.instruments(as_of=cutoff)
        require(result["scope"] == "all_a_shares", "UNIVERSE_INCOMPLETE", "样本股票集合不能冒充全 A 股")
        return result

    def get_trade_days(self, start_date=None, end_date=None, count=None):
        require(count is None, "UNSUPPORTED", "count 暂不支持")
        require(start_date is not None and end_date is not None, "INVALID_REQUEST", "须显式起止日期")
        a, b = _date(start_date), _date(end_date)
        result = self.snapshot.calendar(a.isoformat(), b.isoformat())
        return {"snapshot_id": result["snapshot_id"],
                "days": [r["date"] for r in result["rows"] if r["is_open"]]}


def _date(value):
    if isinstance(value, datetime):
        raise DataError("INVALID_DATE", "请显式传入日期，避免静默丢弃日内时间")
    if isinstance(value, date):
        return value
    try:
        require(isinstance(value, str) and len(value) == 10, "INVALID_DATE", "须为 YYYY-MM-DD")
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DataError("INVALID_DATE", "无效日期") from exc
