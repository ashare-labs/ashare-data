"""Artificial source markers; no B26 or reference-answer prices."""

import hashlib
import json

import pytest

from ashare_data import DataError, PremarketReferenceView
from ashare_data.reference.premarket import POLICY, ROLE


def write(root, name, value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    (root / name).write_bytes(raw)
    return {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}


@pytest.fixture
def delivery(tmp_path):
    raw = write(tmp_path, "raw.js", b'var KLC_K2_sz000002="synthetic";')
    decoder = write(tmp_path, "decoder.js", b"synthetic fixture")
    decoded = {
        "raw_sha256": raw["sha256"],
        "decoder_sha256": decoder["sha256"],
        "rows": [
            {"date": "2024-03-01T00:00:00.000Z", "close": 13},
            {"date": "2024-03-04T00:00:00.000Z", "prevclose": 12.65, "close": "FORBIDDEN_SAME_DAY"},
            {"date": "2025-03-04T00:00:00.000Z", "prevclose": 999, "close": 999},
        ],
    }
    source = {
        **write(tmp_path, "decoded.json", decoded),
        "raw": raw,
        "decoder": decoder,
        "kind": "sina_daily_klc_decoded",
        "security": "000002.XSHE",
        "adjustment": "none",
        "source_publication_time": None,
        "historical_received_at": None,
        "captured_start": "2026-01-01T01:00:00+00:00",
        "captured_end": "2026-01-01T01:00:01+00:00",
    }
    fact = dict(
        schema="ad.document-cash-dividend-fact.v1",
        security="000002.XSHE",
        event_type="cash_dividend",
        announcement_date="2024-02-20",
        record_date="2024-03-01",
        ex_date="2024-03-04",
        payment_date="2024-03-04",
        currency="CNY",
        gross_cash_per_share="0.347",
        bonus_share_ratio="0",
        capitalization_share_ratio="0",
    )
    b = dict(
        schema="ad.premarket-reference-binding.v1",
        policy=POLICY,
        role=ROLE,
        security="000002.XSHE",
        historical_pit=False,
        strategy_visible=False,
        matching_eligible=False,
        ex_date="2024-03-04",
        previous_session="2024-03-01",
        model_available_at="2024-03-04T09:00:00+08:00",
        source_price_unit="0.01",
        calendar=write(tmp_path, "calendar.json", {"dates": ["2024-03-01", "2024-03-04"]}),
        cash_fact=write(tmp_path, "fact.json", fact),
        primary_document={
            **write(tmp_path, "issuer.pdf", b"synthetic issuer"),
            "document_date": "2024-02-20",
        },
        source=source,
    )
    return tmp_path, b


def read(delivery, **kwargs):
    root, b = delivery
    ref = write(root, "binding.json", b)
    view = PremarketReferenceView(root / ref["path"], expected_binding_sha256=ref["sha256"])
    args = dict(security=b["security"], as_of=b["model_available_at"], role=ROLE)
    return view.read(**(args | kwargs))


def test_source_marker_is_preserved_and_future_fields_are_excluded(delivery):
    result = read(delivery)
    assert result["row"]["reference_price"] == "12.65"
    assert result["consistency"]["raw_close_minus_gross"] == "12.653"
    assert result["historical_available_at"] is None
    assert result["historical_pit"] is False
    assert "FORBIDDEN_SAME_DAY" not in json.dumps(result)
    root, b = delivery
    d = json.loads((root / "decoded.json").read_text())
    d["rows"][1].update(open=-999, close=9999, high=None, amount="FUTURE")
    d["rows"][2]["prevclose"] = -1
    b["source"].update(write(root, "decoded.json", d))
    assert read(delivery)["row"] == result["row"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": "strategy"},
        {"role": "historical_pit"},
        {"security": "000001.XSHE"},
        {"as_of": "2024-03-01T09:00:00+08:00"},
        {"as_of": "2024-03-04T08:59:59+08:00"},
        {"as_of": "2024-03-04T09:01:00+08:00"},
    ],
)
def test_unsupported_roles_scope_and_clocks_refused(delivery, kwargs):
    with pytest.raises(DataError):
        read(delivery, **kwargs)


@pytest.mark.parametrize("value", [None, 12, 12.653, 0, -1, True, "NaN"])
def test_missing_or_inconsistent_source_never_falls_back(delivery, value):
    root, b = delivery
    d = json.loads((root / "decoded.json").read_text())
    if value is None:
        del d["rows"][1]["prevclose"]
    else:
        d["rows"][1]["prevclose"] = value
    b["source"].update(write(root, "decoded.json", d))
    with pytest.raises(DataError):
        read(delivery)


@pytest.mark.parametrize(
    "field,value",
    [
        ("announcement_date", "2024-03-04"),
        ("gross_cash_per_share", "0.1"),
        ("bonus_share_ratio", "0.2"),
        ("record_date", "2024-02-29"),
    ],
)
def test_action_conflicts_refused(delivery, field, value):
    root, b = delivery
    f = json.loads((root / "fact.json").read_text())
    f[field] = value
    b["cash_fact"] = write(root, "fact.json", f)
    with pytest.raises(DataError):
        read(delivery)


def test_evidence_tampering_refused(delivery):
    (delivery[0] / "raw.js").write_text("tampered")
    with pytest.raises(DataError, match="digest"):
        read(delivery)


def test_nonadjacent_calendar_refused(delivery):
    root, b = delivery
    b["calendar"] = write(
        root, "calendar.json", {"dates": ["2024-03-01", "2024-03-02", "2024-03-04"]}
    )
    with pytest.raises(DataError, match="adjacent"):
        read(delivery)


@pytest.fixture
def capitalization_delivery(delivery):
    from ashare_data.reference.premarket import CAPITALIZATION_POLICY

    root, b = delivery
    fact = json.loads((root / "fact.json").read_text())
    fact.update(
        event_type="cash_dividend_capitalization",
        capitalization_share_ratio="0.3",
        new_shares_tradable_date="2024-03-04",
        shares_credit_date="2024-03-04",
    )
    b["cash_fact"] = write(root, "fact.json", fact)
    b["policy"] = CAPITALIZATION_POLICY
    decoded = json.loads((root / "decoded.json").read_text())
    decoded["rows"][1]["prevclose"] = "9.73"  # Source marker, theoretical 9.7330769...
    b["source"].update(write(root, "decoded.json", decoded))
    return delivery


def test_capitalization_source_marker_does_not_round_theory(capitalization_delivery):
    r = read(capitalization_delivery)
    assert r["row"]["reference_price"] == "9.73"
    assert r["row"]["capitalization_share_ratio"] == "0.3"
    assert r["historical_pit"] is False
    assert "FORBIDDEN_SAME_DAY" not in json.dumps(r)


@pytest.mark.parametrize(
    "field,value",
    [
        ("capitalization_share_ratio", "0"),
        ("capitalization_share_ratio", "-0.3"),
        ("capitalization_share_ratio", "NaN"),
        ("capitalization_share_ratio", "0.5"),
        ("shares_credit_date", "2024-03-05"),
        ("new_shares_tradable_date", "2024-03-05"),
        ("payment_date", "2024-03-05"),
        ("bonus_share_ratio", "0.1"),
        ("event_type", "cash_dividend"),
    ],
)
def test_capitalization_conflicts_refuse(capitalization_delivery, field, value):
    root, b = capitalization_delivery
    fact = json.loads((root / "fact.json").read_text())
    fact[field] = value
    b["cash_fact"] = write(root, "fact.json", fact)
    with pytest.raises(DataError):
        read(capitalization_delivery)
