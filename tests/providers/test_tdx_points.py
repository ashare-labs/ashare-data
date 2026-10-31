"""Synthetic wire records only; real TDX responses stay outside the package."""

from dataclasses import replace
import hashlib
import json
import struct

import pytest

from ashare_data import DataError, Store, TDXPointInput


def encode(n):
    value = abs(n)
    out = [(value & 63) | (64 if n < 0 else 0)]
    value >>= 6
    while value:
        out[-1] |= 128
        out.append(value & 127)
        value >>= 7
    return bytes(out)


def source(tmp_path, count=240):
    points, records, prior = [], [], 0
    for index in range(count):
        price = 2500 + index % 3
        delta, aux, volume = price - prior, -1, 0 if index == 239 else 120 + index
        record = encode(delta) + encode(aux) + encode(volume)
        minute = 571 + index if index < 120 else 661 + index
        label = f"{minute // 60:02}:{minute % 60:02}"
        points.append(
            dict(
                index=index,
                time_label=label,
                time=f"2024-03-04T{label}:00+08:00",
                price=price / 100,
                price_milli=price * 10,
                volume=volume,
                price_field=None,
                avg_field=None,
                avg_price=None,
                price_raw=price,
                avg_raw=None,
                price_delta_raw=delta,
                aux_delta_raw=aux,
                record_hex=record.hex(),
            )
        )
        records.append(record)
        prior = price
    packet = struct.pack("<Hf", count, 24.0) + b"".join(records)
    response = dict(
        exchange="sz",
        market_id=0,
        code="000001",
        trading_date="2024-03-04",
        points=points,
        reserved_zero=None,
        prev_close=24.0,
        open_price=None,
        date_selector_raw=None,
        raw_payload=dict(
            encoding="hex", data=packet.hex(), sha256=hashlib.sha256(packet).hexdigest()
        ),
    )
    path = tmp_path / "synthetic-response.json"
    path.write_text(json.dumps(response))
    return TDXPointInput(
        "000001.XSHE",
        "2024-03-04",
        str(path),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        "synthetic.invalid:7709",
        "2026-10-10T15:00:00+00:00",
        "2026-10-10T15:00:01+00:00",
        "test-1",
        "a" * 40,
        "b" * 64,
    )


def publish(store, source):
    return store.tdx_points(store.import_tdx_points(source, source_kind="synthetic_fixture"))


def read(view, clock="13:01", **kwargs):
    return view.read(
        "000001.XSHE",
        at=f"2024-03-04T{clock}:00+08:00",
        role="retrospective_point_research",
        **kwargs,
    )


def mutate(src, change):
    from pathlib import Path

    p = Path(src.response_path)
    obj = json.loads(p.read_text())
    change(obj)
    p.write_text(json.dumps(obj))
    return replace(src, response_sha256=hashlib.sha256(p.read_bytes()).hexdigest())


def test_exact_points_with_separate_source_clocks_and_units(store, tmp_path):
    s = source(tmp_path)
    view = publish(store, s)
    result = read(view)
    assert result["rows"][0]["source_index"] == 120
    assert result["rows"][0]["values"] == {
        "price": "25",
        "volume_lots": "240",
        "volume_shares": "24000",
    }
    assert result["rows"][0]["observed_at"] == "2026-10-10T23:00:01+08:00"
    assert result["rows"][0]["source_label_at"] == "2024-03-04T13:01:00+08:00"
    assert result["rows"][0]["executable"] is None
    assert result["strategy_visible"] is False and result["historical_pit"] is False
    desc = view.describe()
    assert desc["coverage"] == dict(
        point_count=240, full_240_point_grid=True, complete_minute_ohlcv=False
    )
    assert "point_interval_boundaries" in desc["unknown"]
    assert "source lot" in desc["volume_precision"]
    assert Store(store.root).tdx_points(view.dataset_id).describe() == desc
    assert publish(store, s).dataset_id == view.dataset_id


def test_no_fill_no_ohlcv_synthesis_no_auction_no_zero_volume_execution(store, tmp_path):
    view = publish(store, source(tmp_path))
    for at in ("09:25", "09:30", "12:00", "12:59", "13:00", "15:01"):
        result = read(view, at)
        assert result["status"] == "unavailable" and result["rows"] == []
        assert result["reason"] == "NO_EXACT_SOURCE_POINT"
    row = read(view, "15:00")["rows"][0]
    assert row["executable"] is False and row["quality"] == "source_zero_volume"
    assert not {"open", "high", "low", "close", "amount"}.intersection(row["values"])


def test_partial_sequence_does_not_claim_full_grid(store, tmp_path):
    view = publish(store, source(tmp_path, count=2))
    assert view.describe()["coverage"]["full_240_point_grid"] is False
    assert read(view)["status"] == "unavailable"


def test_detached_reads_and_sealed_integrity(store, tmp_path):
    view = publish(store, source(tmp_path))
    read(view)["rows"][0]["values"]["price"] = "999"
    view.describe()["unknown"].clear()
    assert read(view)["rows"][0]["values"]["price"] == "25"
    assert view.describe()["unknown"]
    path = store.root / "tdx_points" / f"{view.dataset_id}.json"
    path.chmod(0o644)
    path.write_text(path.read_text() + " ")
    with pytest.raises(DataError, match="hash mismatch"):
        read(view)


@pytest.mark.parametrize("role", ["strategy", "matching", "current", "engine_capacity", None])
def test_reject_implicit_historical_visibility_or_execution_roles(store, tmp_path, role):
    view = publish(store, source(tmp_path))
    with pytest.raises(DataError) as caught:
        view.read("000001.XSHE", at="2024-03-04T13:01:00+08:00", role=role)
    assert caught.value.code == "TDX_ROLE"


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda x: x.update(orders=[{"price": "REFERENCE_ANSWER"}]), "TDX_SCHEMA"),
        (lambda x: x["points"][120].update(fill_price=99), "TDX_SCHEMA"),
        (lambda x: x["points"][120].update(time_label="13:00"), "TDX_LABEL"),
        (lambda x: x["points"][120].update(price=99), "TDX_WIRE"),
        (lambda x: x["points"][120].update(price_milli=99), "TDX_WIRE"),
        (lambda x: x["points"][120].update(volume=-1), "TDX_SCHEMA"),
        (lambda x: x["points"][120].update(volume=999), "TDX_WIRE"),
        (lambda x: x["points"][120].update(volume=True), "TDX_SCHEMA"),
        (lambda x: x["points"][120].update(record_hex="8000"), "TDX_WIRE"),
        (lambda x: x["points"][120].update(record_hex="xyz"), "TDX_SCHEMA"),
        (lambda x: x["points"][120].update(index=119), "TDX_SCHEMA"),
        (lambda x: x.update(trading_date="2024-03-05"), "TDX_SCOPE"),
        (lambda x: x.update(code="000002"), "TDX_SCOPE"),
        (lambda x: x.update(prev_close=1e100), "TDX_SCHEMA"),
        (lambda x: x.update(prev_close=26), "TDX_WIRE"),
        (lambda x: x["raw_payload"].update(sha256="0" * 64), "TDX_WIRE"),
    ],
)
def test_reject_answer_fields_and_wire_disagreement_before_publication(
    store, tmp_path, change, code
):
    s = mutate(source(tmp_path), change)
    with pytest.raises(DataError) as caught:
        publish(store, s)
    assert caught.value.code == code
    assert not (store.root / "tdx_points").exists()


def test_input_hash_and_clock_scope(store, tmp_path):
    s = source(tmp_path)
    for altered, code in [
        (replace(s, response_sha256="0" * 64), "TDX_INTEGRITY"),
        (replace(s, captured_start="2024-03-04T13:01:00+08:00"), "TDX_CLOCK"),
        (replace(s, captured_end="2026-10-10T14:59:00+00:00"), "TDX_CLOCK"),
    ]:
        with pytest.raises(DataError) as caught:
            publish(store, altered)
        assert caught.value.code == code
    view = publish(store, s)
    with pytest.raises(DataError):
        view.read(
            "000002.XSHE", at="2024-03-04T13:01:00+08:00", role="retrospective_point_research"
        )
    with pytest.raises(DataError):
        view.read(
            "000001.XSHE", at="2024-03-05T13:01:00+08:00", role="retrospective_point_research"
        )
    with pytest.raises(DataError):
        view.read("000001.XSHE", at="2024-03-04 13:01:00", role="retrospective_point_research")
