"""Finite owner/codec acceptance. Real sealed source replay, no RQ/native trading run."""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from ashare_data import (
    DataError,
    M2AD08Call,
    M2ConsumerBinding,
    Store,
)
from ashare_data.m2 import MODE
from ashare_data.m2_codec import (
    native_ad08_call,
    normalize_native_previous,
)
from ashare_data.m2_types import canonical_bytes, canonical_hash, exact_int, json_loads


def rejects(code, fn):
    with pytest.raises(DataError) as error:
        fn()
    assert error.value.code == code


@pytest.fixture(scope="session")
def bundle_path():
    path = Path(
        os.environ.get(
            "ASHARE_M2_BUNDLE", Path(__file__).parents[2] / "m2-product-evidence/input-bundle"
        )
    )
    if not path.is_dir():
        pytest.skip(
            "M2 sealed real fixture absent; set ASHARE_M2_BUNDLE to delivered offline bundle"
        )
    return path


@pytest.fixture(scope="session")
def real_objects(bundle_path):
    manifest = json.loads((bundle_path / "manifest.json").read_text())
    return {n: (bundle_path / n).read_bytes() for n in manifest["files"]}


@pytest.fixture(scope="session")
def prepared_m2(bundle_path, tmp_path_factory):
    s = Store.init(tmp_path_factory.mktemp("m2") / "store")
    sid = s.import_m2(bundle_path)
    profiles = {p.to_dict()["version"]: p.to_dict()["profile_sha256"] for p in s.m2_profiles(sid)}
    return s, sid, profiles


def use_for(prepared, window="w1", *, run="run-1", request=None):
    s, sid, profiles = prepared
    version = "m2.daily.600000.m2a.v1" if window == "m2a" else "m2.daily.600000.multiday.v1"
    view = s.m2(sid, profile_sha256=profiles[version], mode=MODE)
    plan = next(
        p.to_dict() for p in view.windows() if p.to_dict()["geometry"]["window_id"] == window
    )
    h = canonical_hash({"kind": "synthetic_consumer_declaration_not_execution"})
    b = M2ConsumerBinding(
        view.descriptor().sha256,
        window,
        plan["window_plan_sha256"],
        h,
        run,
        request or h,
        "previous-close-band.v1",
        h,
    )
    ack = [d.to_dict() for d in view.assumptions(window)]
    return view, view.use(window, assumption_ack=ack, consumer_binding=b), b


def context(
    use,
    day="2020-01-03",
    previous="2020-01-02",
    phase="DECIDE",
    generation=0,
    epoch=0,
    consumer=None,
):
    clock = "09:00:00" if phase == "DECIDE" else "08:00:00" if phase == "INITIALIZE" else "15:00:00"
    cutoff = (
        previous + "T23:59:59.999999+08:00"
        if phase in ("DECIDE", "INITIALIZE")
        else day + "T15:00:00+08:00"
    )
    return use.context(
        current_date=day,
        logical_at=day + "T" + clock + "+08:00",
        phase=phase,
        query_end=cutoff,
        visibility="assumed",
        consumer=consumer or ("strategy" if phase == "DECIDE" else "engine"),
        event_cursor=0,
        restore_generation=generation,
        epoch=epoch,
    )


def call(day="2020-01-03", previous="2020-01-02"):
    return M2AD08Call("600000.XSHG", day, previous, day, "1d", "close", 1, False, False, "pre")


@pytest.mark.parametrize("value", [100.0, True, "100", Decimal(100), np.int64(100), -1])
def test_strict_integer(value):
    rejects("M2_INTEGER", lambda: exact_int(value))


def test_generic_encoder_does_not_guess_typed_string_fields():
    assert (
        canonical_bytes(
            {"v": Decimal("12.4700"), "at": datetime(2020, 1, 3, 1, tzinfo=timezone.utc)}
        )
        == b'{"at":"2020-01-03T01:00:00.000000Z","v":"12.47"}'
    )
    rejects("M2_ENCODING", lambda: canonical_bytes({"n": 100.0}))
    rejects("M2_ENCODING", lambda: json_loads('{"n":1,"n":2}'))
    rejects("M2_ENCODING", lambda: json_loads('{"n":NaN}'))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"bar_count": True}, "M2_INTEGER"),
        ({"bar_count": 1.0}, "M2_INTEGER"),
        ({"bar_count": 2}, "M2_INTEGER"),
        ({"include_now": 0}, "M2_UNREVIEWED_AD08"),
        ({"skip_suspended": True}, "M2_UNREVIEWED_AD08"),
        ({"field": "open"}, "M2_UNREVIEWED_AD08"),
        ({"frequency": "5m"}, "M2_UNREVIEWED_AD08"),
        ({"adjustment_requested": "none"}, "M2_UNREVIEWED_AD08"),
        ({"trade_date": datetime(2020, 1, 3)}, "M2_DATE"),
        ({"adjust_orig": "2020-01-06"}, "M2_UNREVIEWED_AD08"),
        ({"security": "000001.XSHE"}, "M2_UNREVIEWED_AD08"),
    ],
)
def test_exact_ad08_shape(change, code):
    rejects(code, lambda: M2AD08Call.from_dict({**call().to_dict(), **change}))




























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
    rejects("M2_NATIVE_VALUE", lambda: normalize_native_previous(bad))




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
    rejects("M2_NATIVE_TIME", lambda: native_ad08_call(**kwargs))
