"""Portable owner codec boundaries using synthetic values only."""

from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from ashare_data import DataError, AdjustedHistoryCall
from ashare_data.research.portable_codec import native_ad08_call, normalize_native_previous
from ashare_data.research.portable_types import canonical_bytes, exact_int, json_loads


def rejects(code, fn):
    with pytest.raises(DataError) as error:
        fn()
    assert error.value.code == code


def call(day="2020-01-03", previous="2020-01-02"):
    return AdjustedHistoryCall(
        "600000.XSHG", day, previous, day, "1d", "close", 1, False, False, "pre"
    )


@pytest.mark.parametrize("value", [100.0, True, "100", Decimal(100), np.int64(100), -1])
def test_strict_integer(value):
    rejects("PORTABLE_INTEGER", lambda: exact_int(value))


def test_generic_encoder_does_not_guess_typed_string_fields():
    assert (
        canonical_bytes(
            {"v": Decimal("12.4700"), "at": datetime(2020, 1, 3, 1, tzinfo=timezone.utc)}
        )
        == b'{"at":"2020-01-03T01:00:00.000000Z","v":"12.47"}'
    )
    rejects("PORTABLE_ENCODING", lambda: canonical_bytes({"n": 100.0}))
    rejects("PORTABLE_ENCODING", lambda: json_loads('{"n":1,"n":2}'))
    rejects("PORTABLE_ENCODING", lambda: json_loads('{"n":NaN}'))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"bar_count": True}, "PORTABLE_INTEGER"),
        ({"bar_count": 1.0}, "PORTABLE_INTEGER"),
        ({"bar_count": 2}, "PORTABLE_INTEGER"),
        ({"include_now": 0}, "PORTABLE_UNREVIEWED_AD08"),
        ({"skip_suspended": True}, "PORTABLE_UNREVIEWED_AD08"),
        ({"field": "open"}, "PORTABLE_UNREVIEWED_AD08"),
        ({"frequency": "5m"}, "PORTABLE_UNREVIEWED_AD08"),
        ({"adjustment_requested": "none"}, "PORTABLE_UNREVIEWED_AD08"),
        ({"trade_date": datetime(2020, 1, 3)}, "PORTABLE_DATE"),
        ({"adjust_orig": "2020-01-06"}, "PORTABLE_UNREVIEWED_AD08"),
        ({"security": "000001.XSHE"}, "PORTABLE_UNREVIEWED_AD08"),
    ],
)
def test_exact_ad08_shape(change, code):
    rejects(code, lambda: AdjustedHistoryCall.from_dict({**call().to_dict(), **change}))


@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        12,
        Decimal("12.47"),
        np.float32(12.47),
        float("nan"),
        float("inf"),
        0.0,
        -1.0,
        np.array([], dtype="float64"),
        np.array([12.47, 12.47]),
        np.array([[12.47]]),
        np.array([12.47], dtype="float32"),
    ],
)
def test_native_codec_rejects_shapes_and_types(bad):
    rejects("PORTABLE_NATIVE_VALUE", lambda: normalize_native_previous(bad))


def test_native_time_codec_does_not_truncate_intraday():
    kwargs = dict(
        security="600000.XSHG",
        trade_date="2020-01-03",
        dt=datetime(2020, 1, 2, 12),
        adjust_orig=datetime(2020, 1, 3),
        frequency="1d",
        fields="close",
        bar_count=1,
        include_now=False,
        skip_suspended=False,
        adjust_type="pre",
        time_policy="rq641_shanghai_midnight",
    )
    rejects("PORTABLE_NATIVE_TIME", lambda: native_ad08_call(**kwargs))
