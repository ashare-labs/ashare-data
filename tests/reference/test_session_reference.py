"""Self-authored minimal quote shapes; no actual market observations."""

import pytest

from ashare_data.core.model import DataError
from ashare_data.providers.sse_snapshot import snapshot

from synthetic_pilot import quote


@pytest.mark.parametrize(
    "code,ref,upper,lower",
    [
        ("600000", "10", "11.0", "9.0"),
        ("600036", "20", "22.0", "18.0"),
        ("601318", "30", "33.0", "27.0"),
    ],
)
def test_synthetic_reference_parses_values_and_clock(code, ref, upper, lower):
    result = snapshot(quote(code, int(ref)), code, "2026-10-14", "2026-10-14T09:33:34+08:00")
    assert (result["reference_close"], result["upper_limit"], result["lower_limit"]) == (
        ref,
        upper,
        lower,
    )
    assert result["source_as_of"] == "2026-10-14T09:33:33+08:00"
    assert result["trade_phase"] == "T111"


@pytest.mark.parametrize(
    "change",
    [
        {"date": 20261013},
        {"time": 240000},
        {"time": 93333.0},
        {"date": 2026101},
        {"total": 2},
        {"end": 2},
        {"list": []},
    ],
)
def test_incomplete_or_non_current_official_snapshots_are_not_session_parameters(change):
    value = quote()
    value.update(change)
    with pytest.raises((DataError, ValueError)):
        snapshot(value, "600000", "2026-10-14", "2026-10-14T09:33:34+08:00")
