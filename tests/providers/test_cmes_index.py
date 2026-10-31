"""Index observations use the same release reader without stock or unit fiction."""

from decimal import Decimal

import pytest

from ashare_data.core.model import DataError
from ashare_data.market.client import _security
from ashare_data.product.market_release import ReleasedDailyProduct
from ashare_data.storage.cmes_release import import_cmes
from ashare_data.storage.market_release import MarketRelease
from test_cmes import MINUTE_HEADER, fixture


def test_index_price_only_publication_identity_units_replay_and_gaps(tmp_path):
    original = fixture(tmp_path / "indices.zip", {
        code + ".csv": [MINUTE_HEADER,
            ["2024-01-02", "100", "102", "99", "101", "unverified volume", "unverified amount"],
            ["2024-01-04", "101", "104", "100", "103", "20.5", "300"]]
        for code in ("SH.000300", "SZ.399001")
    })
    root = tmp_path / "release"
    receipt = import_cmes(root, daily=[original], daily_profile="daily-index-points", free_reserve_bytes=0)
    release = MarketRelease(root)
    assert {r["asset_class"] for r in release.instruments.values()} == {"index"}
    with release.metadata() as db:
        assert {r[0] for r in db.execute("SELECT namespace FROM release_identifiers")} == {"venue_index_code"}
    owner = ReleasedDailyProduct({"market_release": dict(root=str(root), release_id=1,
                                                        manifest_sha256=receipt["manifest_sha256"])})
    result = owner.prices(["000300.XSHG", "399001.XSHE"], "2024-01-02", "2024-01-04")
    assert len(result["rows"]) == 4
    assert result["price_unit"] == "index_point"
    assert result["volume_unit"] is result["amount_unit"] is None
    assert {Decimal(r["close"]) for r in result["rows"]} == {Decimal(101), Decimal(103)}
    assert all(r["volume"] is r["amount"] is None for r in result["rows"])
    assert not any(r["date"] == "2024-01-03" for r in result["rows"])
    assert result["gaps"]  # No invented listing or calendar accompanies index levels.
    assert {r["asset_class"] for r in owner.catalogue()["securities"]} == {"index"}
    assert owner.catalogue(as_of="2024-01-03")["securities"] == []
    replay = import_cmes(root, daily=[original], daily_profile="daily-index-points", free_reserve_bytes=0)
    assert replay["unchanged"] and replay["manifest_sha256"] == receipt["manifest_sha256"]
    with pytest.raises(DataError):
        owner.prices(["000001.XSHG"], "2024-01-02", "2024-01-04")
    # Publication reads do not broaden the independent live equity fetcher's scope.
    with pytest.raises((ValueError, DataError)):
        _security("000300.XSHG")


def test_index_projection_cannot_be_mixed_with_execution_profiles(tmp_path):
    with pytest.raises(DataError, match="cannot mix"):
        import_cmes(tmp_path / "bad", daily=[tmp_path / "unread.zip"], daily_profile="daily-index-points",
                    minute=[tmp_path / "unread.zip"], minute_profile="minute-shares-cny")
