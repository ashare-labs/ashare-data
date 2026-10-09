"""Public immutable M2 wire types and explicit canonical encoding (no Pydantic)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from .model import DataError, require


def exact_int(value, *, minimum=0, maximum=None):
    require(
        type(value) is int and value >= minimum and (maximum is None or value <= maximum),
        "M2_INTEGER",
        "须为范围内Python/JSON整数，拒绝bool、float和字符串",
    )
    return value


def exact_day(value):
    if type(value) is date:
        return value
    if type(value) is str:
        try:
            d = date.fromisoformat(value)
            if d.isoformat() == value:
                return d
        except ValueError:
            pass
    raise DataError("M2_DATE", "须为date或YYYY-MM-DD，不隐式截datetime")


def aware_time(value):
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DataError("M2_CLOCK_INVALID", "时间须为aware ISO") from exc
    require(
        type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None,
        "M2_CLOCK_INVALID",
        "不接受naive或Unix数值时间",
    )
    return value.astimezone(timezone.utc)


def decimal_text(value):
    require(type(value) in (str, Decimal), "M2_DECIMAL", "经济价格须为Decimal或十进制字符串")
    if type(value) is str:
        require(
            len(value) <= 48 and re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", value),
            "M2_DECIMAL",
            "十进制字符串禁止指数/空白/NaN",
        )
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise DataError("M2_DECIMAL", "无效十进制") from exc
    require(number.is_finite() and abs(number) < Decimal("1e24"), "M2_DECIMAL", "数值须有限且有界")
    result = format(number, "f")
    result = result.rstrip("0").rstrip(".") if "." in result else result
    require(len(result.partition(".")[2]) <= 8, "M2_DECIMAL", "经济数值最多8位小数")
    return "0" if number == 0 else result


def sha(value):
    require(
        type(value) is str and re.fullmatch("[0-9a-f]{64}", value),
        "M2_IDENTITY_MISMATCH",
        "须为SHA256",
    )
    return value


def _plain(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is Decimal:
        return decimal_text(value)
    if type(value) is datetime:
        return aware_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if type(value) is date:
        return value.isoformat()
    if type(value) in (tuple, list):
        return [_plain(v) for v in value]
    if type(value) is dict:
        require(all(type(k) is str for k in value), "M2_ENCODING", "JSON键须为字符串")
        return {k: _plain(v) for k, v in value.items()}
    raise DataError("M2_ENCODING", "规范JSON不接收float或隐式自定义类型")


def canonical_bytes(value):
    """Generic canonical JSON. Typed payload codecs normalize numeric/time strings first."""
    return json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def canonical_hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def json_loads(blob):
    def pairs(items):
        result = {}
        for k, v in items:
            require(k not in result, "M2_ENCODING", "拒绝重复JSON键")
            result[k] = v
        return result

    try:
        return json.loads(
            blob,
            object_pairs_hook=pairs,
            parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
        )
    except (ValueError, UnicodeError) as exc:
        raise DataError("M2_ENCODING", "无效JSON/非有限数值") from exc


def keys(value, expected):
    require(
        type(value) is dict and set(value) == set(expected), "M2_SCHEMA", "字段集合不符公开schema"
    )
    return value


class Wire:
    def to_dict(self):
        return _plain({f.name: getattr(self, f.name) for f in fields(self)})

    @classmethod
    def from_dict(cls, value):
        keys(value, [f.name for f in fields(cls)])
        return cls(**value)


@dataclass(frozen=True)
class M2Document:
    """Immutable canonical bytes; to_dict always returns a detached copy."""

    data: bytes

    def __post_init__(self):
        require(type(self.data) is bytes, "M2_SCHEMA", "文档须为bytes")
        require(
            canonical_bytes(json_loads(self.data)) == self.data, "M2_ENCODING", "文档不是规范编码"
        )

    @classmethod
    def of(cls, value):
        return cls(canonical_bytes(value))

    def to_dict(self):
        return json_loads(self.data)

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class M2AD08Call(Wire):
    security: str
    trade_date: date
    history_dt: date
    adjust_orig: date
    frequency: str
    field: str
    bar_count: int
    include_now: bool
    skip_suspended: bool
    adjustment_requested: str

    def __post_init__(self):
        for name in ("trade_date", "history_dt", "adjust_orig"):
            object.__setattr__(self, name, exact_day(getattr(self, name)))
        exact_int(self.bar_count, minimum=1, maximum=1)
        require(
            type(self.include_now) is bool
            and self.include_now is False
            and type(self.skip_suspended) is bool
            and self.skip_suspended is False,
            "M2_UNREVIEWED_AD08",
            "两个flag须为明确False",
        )
        require(
            (self.security, self.frequency, self.field, self.adjustment_requested)
            == ("600000.XSHG", "1d", "close", "pre")
            and self.history_dt < self.trade_date == self.adjust_orig,
            "M2_UNREVIEWED_AD08",
            "只支持固定证券P/T一根pre日收盘",
        )


@dataclass(frozen=True)
class M2ConsumerBinding(Wire):
    owner_pin_sha256: str
    window_id: str
    window_plan_sha256: str
    run_binding_sha256: str
    run_id: str
    consumer_request_sha256: str
    strategy_version_id: str
    assumption_ack_bundle_sha256: str

    def __post_init__(self):
        for name in (
            "owner_pin_sha256",
            "window_plan_sha256",
            "run_binding_sha256",
            "consumer_request_sha256",
            "assumption_ack_bundle_sha256",
        ):
            sha(getattr(self, name))
        for name in ("window_id", "run_id", "strategy_version_id"):
            value = getattr(self, name)
            require(
                type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value),
                "M2_IDENTITY_MISMATCH",
                "无效消费方标识",
            )


@dataclass(frozen=True)
class M2ReadContext(M2ConsumerBinding):
    current_date: date
    logical_at: datetime
    phase: str
    query_end: datetime | None
    visibility: str
    consumer: str
    event_cursor: int
    restore_generation: int
    epoch: int

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, "current_date", exact_day(self.current_date))
        object.__setattr__(self, "logical_at", aware_time(self.logical_at))
        if self.query_end is not None:
            object.__setattr__(self, "query_end", aware_time(self.query_end))
        for name in ("event_cursor", "restore_generation", "epoch"):
            exact_int(getattr(self, name))
        require(
            self.consumer in ("strategy", "engine") and self.visibility == "assumed",
            "M2_VISIBILITY",
            "仅显式assumed；consumer须strategy或engine",
        )
        require(
            self.phase in ("INITIALIZE", "DECIDE", "SUBMIT_MATCH", "AFTER_TRADING", "SETTLEMENT"),
            "M2_CLOCK_INVALID",
            "未知phase",
        )

    @property
    def context_sha256(self):
        return canonical_hash(super().to_dict())

    def to_dict(self):
        return {**super().to_dict(), "context_sha256": self.context_sha256}

    @classmethod
    def from_dict(cls, value):
        expected = [f.name for f in fields(cls)]
        keys(value, [*expected, "context_sha256"])
        obj = cls(**{k: value[k] for k in expected})
        require(
            obj.context_sha256 == value["context_sha256"], "M2_IDENTITY_MISMATCH", "context摘要不符"
        )
        return obj


@dataclass(frozen=True)
class M2Read:
    query: M2Document
    result: M2Document
    sources: M2Document
    evidence: M2Document
    authorization: M2Document

    def to_dict(self):
        return {f.name: getattr(self, f.name).to_dict() for f in fields(self)}

    @classmethod
    def from_dict(cls, value):
        keys(value, [f.name for f in fields(cls)])
        return cls(**{k: M2Document.of(v) for k, v in value.items()})

    @property
    def value(self):
        result = self.result.to_dict()
        require(result["operation"] == "decision_prev_close", "M2_SCHEMA", "该读不是前收")
        return Decimal(result["value"])

    def with_authorization(self, authorization):
        require(type(authorization) is M2Document, "M2_SCHEMA", "须为规范授权文档")
        return M2Read(self.query, self.result, self.sources, self.evidence, authorization)
