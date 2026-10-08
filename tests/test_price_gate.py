"""A shared positive, finite and ordered price gate; synthetic inputs only."""
import json
from pathlib import Path

import pytest

from ashare_data import Client, DataError, transport
from ashare_data.model import normalize


@pytest.mark.parametrize("bad", ["0", "-1", "NaN", "Infinity", "1e17", True])
def test_live_and_offline_reject_invalid_prices(bad, monkeypatch, tmp_path):
    row = {"day": "2026-09-30 09:31:00", "volume": "100", "amount": "1000"}
    row.update({key: bad for key in ("open", "high", "low", "close")})
    monkeypatch.setattr(transport, "public_read", lambda *a: json.dumps([row]).encode())
    with pytest.raises(DataError) as exc:
        Client(cache=tmp_path).get_price("600000.XSHG", frequency="1m", count=1)
    assert exc.value.code == "SOURCE_SCHEMA_ERROR" and not list(tmp_path.iterdir())
    bundle = json.loads((Path(__file__).parents[1] / "examples/fixtures/golden.json").read_text())
    bundle["bars"][0].update({key: bad for key in ("open", "high", "low", "close")})
    with pytest.raises(DataError):
        normalize(bundle)


@pytest.mark.parametrize("field,value", [("low", "10.1"), ("high", "9.9"), ("close", "12"), ("open", "8")])
def test_both_paths_reject_ohlc_relation(field, value, monkeypatch, tmp_path):
    row = {"day": "2026-09-30 09:31:00", "open": "10", "high": "11", "low": "9", "close": "10", "volume": "100", "amount": "1000", field: value}
    monkeypatch.setattr(transport, "public_read", lambda *a: json.dumps([row]).encode())
    with pytest.raises(DataError):
        Client(cache=tmp_path).get_price("600000.XSHG", frequency="1m", count=1)
    bundle = json.loads((Path(__file__).parents[1] / "examples/fixtures/golden.json").read_text())
    bundle["bars"][0].update({key: row[key] for key in ("open", "high", "low", "close")})
    with pytest.raises(DataError):
        normalize(bundle)
