"""Portable metadata rejects machine locations without rejecting source URLs."""

import pytest

from ashare_data.core.paths import contains_local_path
from ashare_data.research.components import _portable
from ashare_data import DataError


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/report.pdf",
        "source=/var/data/report.pdf",
        "~/data/report.pdf",
        r"C:\data\report.pdf",
        "D:/data/report.pdf",
        r"\\server\share\report.pdf",
        "file:///tmp/report.pdf",
        "FILE:/tmp/report.pdf",
        "see '/opt/data/report.pdf'",
    ],
)
def test_local_locations_rejected(value):
    assert contains_local_path(value)
    with pytest.raises(DataError) as error:
        _portable({"nested": [value]})
    assert error.value.code == "PORTABLE_LOCAL_PATH"


@pytest.mark.parametrize(
    "value",
    [
        "raw/report.pdf",
        "https://example.invalid/home/data/report.pdf",
        "https://example.invalid/Users/example/report.pdf",
        "CNY/share",
        "source: https://example.invalid/data?a=b",
        "page 1 / 2",
        "a" * 64,
    ],
)
def test_portable_locators_and_public_urls_remain_valid(value):
    assert not contains_local_path(value)
    _portable({"locator": value})
