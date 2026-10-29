"""Synthetic fixtures protect quote identity/time/price and scoped batches."""

from copy import deepcopy
import json

import pytest

from ashare_data.providers import free_update_tdx as native
from ashare_data.providers.free_update_quotes import parse, validate
from ashare_data.providers.free_update_batch import prepare


def quote(symbol="sh600000"):
    levels = [{"price": 10 - i / 100, "volume": 10, "price_delta_raw": -i} for i in range(5)]
    record = {
        "exchange": symbol[:2],
        "code": symbol[2:],
        "market_id": int(symbol.startswith("sh")),
        "last_price": 10,
        "last_close_price": 9.9,
        "open_price": 9.8,
        "high_price": 10.1,
        "low_price": 9.7,
        "total_hand": 100,
        "current_hand": 1,
        "inside_dish": 30,
        "outer_disc": 70,
        "buy_levels": levels,
        "sell_levels": [dict(r, price=10.01 + i / 100) for i, r in enumerate(levels)],
        "update_time_raw": 150012,
        "record_hex": "aabbcc",
    }
    return {
        "requested_codes": [symbol],
        "records": [record],
        "raw_payload": "eeaabbcc",
        "decoded_payload": "01aabbcc",
    }


def params(symbol="sh600000", kind="stock"):
    return {
        "symbol": symbol,
        "asset_type": kind,
        "host": native.HOST,
        "sdk_python": "/explicit/python",
        "sdk_sha256": "a" * 64,
    }


def test_stock_etf_empty_after_close_levels_are_retained_without_clock_inference():
    for symbol, kind in [("sh600000", "stock"), ("sz159915", "etf")]:
        value = quote(symbol)
        value["records"][0]["buy_levels"] = [
            {"price": 0, "volume": 0, "price_delta_raw": 0} for _ in range(5)
        ]
        value["records"][0]["sell_levels"] = deepcopy(value["records"][0]["buy_levels"])
        p = params(symbol, kind)
        native.parameters("tdx_native_depth", p)
        rows = parse(value, p, "2026-10-29T08:00:00+00:00")
        sem = validate(rows, p, "2026-10-29")
        assert rows[0]["exchange_timestamp"] is None and rows[0]["source_time_raw"] == 150012
        assert rows[0]["bids"][0]["volume"] == 0 and not sem["gaps"]


@pytest.mark.parametrize("mutation", ["scope", "record", "security", "price", "levels", "order"])
def test_depth_mismatches_are_rejected(mutation):
    value = quote()
    r = value["records"][0]
    if mutation == "scope":
        value["requested_codes"] = ["sh600001"]
    if mutation == "record":
        value["decoded_payload"] = "ffff"
    if mutation == "security":
        r["market_id"] = 0
    if mutation == "price":
        r["last_price"] = 1000
    if mutation == "levels":
        r["buy_levels"].pop()
    if mutation == "order":
        r["buy_levels"].reverse()
    with pytest.raises(ValueError):
        parse(value, params(), "2026-10-29T08:00:00+00:00")


def test_bond_depth_is_explicitly_excluded_but_bond_bars_are_independent():
    with pytest.raises(ValueError, match="bond price scale"):
        native.parameters("tdx_native_depth", params("sh113052", "convertible_bond"))
    assert (
        native.parameters(
            "tdx_native_bond_bars",
            {k: v for k, v in params("sh113052").items() if k != "asset_type"}
            | {"period": "1m", "count": 800},
        )["operation"]
        == "bars"
    )


@pytest.mark.parametrize("kinds", [["stock"], ["etf"], ["stock", "etf"]])
def test_same_menu_row_accepts_stock_etf_scopes_without_losing_job_mapping(tmp_path, kinds):
    cfg = {
        "schema": "ad.security-update-batch.v1",
        "root": "isolated",
        "rows": [276],
        "tdx": {k: v for k, v in params().items() if k not in {"symbol", "asset_type"}},
        "instruments": [
            {"asset_type": k, "symbol": {"stock": "sh600000", "etf": "sh510300"}[k]} for k in kinds
        ],
    }
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(cfg))
    _, _, prepared, _ = prepare(p, as_of="2026-10-29")
    assert len(prepared["jobs"]) == len(kinds) == len(prepared["items"][0]["job_ids"])
