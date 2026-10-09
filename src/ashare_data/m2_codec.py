"""Public finite RQ 6.4.1 call/value codec. No RQ import or native execution claim."""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .m2_types import M2AD08Call, M2Document, exact_day
from .model import require

TZ = ZoneInfo("Asia/Shanghai")
CODEC_VERSION = "m2.rq641.codec.v1"


def _native_day(value):
    # RQ get_previous_trading_date returns pandas.Timestamp; reject sub-microsecond loss.
    if type(value) is pd.Timestamp:
        require(value.nanosecond == 0 and not pd.isna(value), "M2_NATIVE_TIME", "拒绝NaT/亚微秒")
        value = value.to_pydatetime()
    if type(value) is datetime:
        if value.tzinfo is not None:
            value = value.astimezone(TZ)
        require(value.time() == time(0), "M2_NATIVE_TIME", "窄codec仅接受RQ已归一化午夜日期")
        return value.date()
    return exact_day(value)


def native_ad08_call(
    *,
    security,
    trade_date,
    dt,
    adjust_orig,
    frequency,
    fields,
    bar_count,
    include_now,
    skip_suspended,
    adjust_type,
    time_policy,
):
    require(
        time_policy == "rq641_shanghai_midnight", "M2_NATIVE_TIME", "须明确naive上海午夜codec策略"
    )
    return M2AD08Call(
        security,
        exact_day(trade_date),
        _native_day(dt),
        _native_day(adjust_orig),
        frequency,
        fields,
        bar_count,
        include_now,
        skip_suspended,
        adjust_type,
    )


def normalize_native_previous(value):
    """Only fixed Float64 scalar or 1D singleton Float64 array; never epsilon/rounding."""
    original = value
    if type(value) is np.ndarray:
        require(
            value.shape == (1,) and value.dtype == np.dtype("float64"),
            "M2_NATIVE_VALUE",
            "须为一维length1 float64数组",
        )
        value = value[0]
    require(
        type(value) in (float, np.float64),
        "M2_NATIVE_VALUE",
        "须为Python float或NumPy float64，拒绝float32/bool/Decimal替代原生值",
    )
    number = float(value)
    require(np.isfinite(number) and number > 0, "M2_NATIVE_VALUE", "原生前收须为正有限数")
    decimal = Decimal(str(number))
    observation = {
        "codec_version": CODEC_VERSION,
        "native_type": type(original).__name__,
        "native_shape": list(original.shape) if type(original) is np.ndarray else [],
        "native_dtype": str(original.dtype)
        if isinstance(original, np.generic) or type(original) is np.ndarray
        else "python.float64",
        "native_repr": repr(original),
        "normalized_decimal": format(decimal, "f"),
        "native_call_observed": False,
    }
    return decimal, observation


def history_array(read, *, use, context):
    """Data-source return conversion, not evidence that RQ invoked history_bars."""
    read = use.verify(read, context=context)
    array = np.array([float(read.value)], dtype="float64")
    normalized, _ = normalize_native_previous(array)
    require(normalized == read.value, "M2_NATIVE_MISMATCH", "Float64往返改变owner值")
    return array


@dataclass(frozen=True)
class M2ConsumedPrevious:
    value: Decimal
    history_date: date
    owner_evidence_sha256: str
    owner_authorization_sha256: str
    codec_observation: M2Document


def consume_previous(native_value, *, read, use, context):
    """Value comes from native_value, then exact-compared with the revalidated owner read."""
    read = use.verify(read, context=context)
    value, observation = normalize_native_previous(native_value)
    require(
        value == read.value, "M2_NATIVE_MISMATCH", "原生返回与owner十进制不等；不得用owner预读替换"
    )
    return M2ConsumedPrevious(
        value,
        exact_day(read.query.to_dict()["target_date"]),
        read.evidence.to_dict()["evidence_sha256"],
        read.authorization.to_dict()["authorization_sha256"],
        M2Document.of(observation),
    )
