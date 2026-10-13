"""Synthetic arithmetic and source-boundary tests; no private labels or network."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json

import pytest

from ashare_data import f1
from ashare_data import f1_remaining as candidates
from ashare_data.f1_vroc6_snapshot import FLAGS, InputError, factor_rows, load_json
from test_f1_scoped import financial_pack as _financial_pack


def seal(path, value):
    data = json.dumps(value).encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def roc_pack(tmp_path):
    days = [(date(2000, 1, 1) + timedelta(days=i)).isoformat() for i in range(124)]
    item = {
        "security": "000001.XSHE",
        "sessions": days,
        "rows": [
            {
                "date": day,
                "close": str(Decimal(100) + Decimal(i) / 1000),
                "trading_state": "observed_trade",
            }
            for i, day in enumerate(days)
        ],
        "source_family": "Sina",
        "currency": "CNY",
        "raw_price_adjustment": "none",
        "price_unit": "CNY_per_share",
        "price_policy": "raw_times_native_hfq_no_rounding",
        "factors": [{"date": days[0], "value": "3.7"}, {"date": days[60], "value": "7.3"}],
    }
    source = tmp_path / "synthetic-source.json"
    source.write_text('{"synthetic":true}')
    pack = {
        "schema": "ad.f1-pv-native-inputs.v1",
        "input_contains_JQ_values": False,
        "items": [item],
        "sources": [
            {
                "path": source.name,
                "bytes": source.stat().st_size,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ],
    }
    return tmp_path, pack


def roc(fixture, **kwargs):
    root, pack = fixture
    parameters = dict(
        expected_inputs_sha256=seal(root / "inputs.json", pack),
        definition=f1.ROC120_RESEARCH_DEFINITION,
        role=f1.RESEARCH_ROLE,
    )
    dates = kwargs.pop("output_dates", pack["items"][0]["sessions"][-3:])
    parameters.update(kwargs)
    return f1.roc120_native_candidate(root, "000001.XSHE", dates, **parameters)


def test_roc_matches_fraction_endpoints_without_price_or_output_quantization(roc_pack):
    result = roc(roc_pack)
    item = roc_pack[1]["items"][0]
    for row in result["rows"]:
        end = item["sessions"].index(row["date"])
        first, last = item["rows"][end - 120], item["rows"][end]
        exact = 100 * (
            Fraction(last["close"]) * Fraction("7.3") / (Fraction(first["close"]) * Fraction("3.7"))
            - 1
        )
        with localcontext() as context:
            context.prec = 50
            expected = Decimal(exact.numerator) / Decimal(exact.denominator)
        assert row["state"] == "valid" and Decimal(row["value"]) == expected
        assert len(row["value"].split(".")[1]) > 6
    assert result["alignment_status"] == "not_passed"
    assert result["integration_independent_review_completed"] is False
    assert result["source_issue"] == "ADJ-INPUT-001"
    assert result["jq_equivalent"] is False and result["formal_id"] is None


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("missing_row", "SOURCE_SESSION_MISSING"),
        ("missing_close", "RAW_CLOSE_MISSING"),
        ("no_factor", "NATIVE_FACTOR_UNAVAILABLE"),
        ("future_factor", "NO_PRIOR_NATIVE_FACTOR"),
        ("zero_close", "NONPOSITIVE_CLOSE"),
        ("pause", "PAUSE_OR_ZERO_TRADE_SEMANTICS_UNVERIFIED"),
        ("zero_trade", "NORMAL_TRADING_ONLY"),
    ],
)
def test_roc_missing_or_unsupported_inputs_remain_failed_rows(roc_pack, fault, reason):
    item = roc_pack[1]["items"][0]
    if fault == "missing_row":
        item["rows"].pop(50)
    elif fault == "missing_close":
        item["rows"][50].pop("close")
    elif fault == "no_factor":
        item["factors"] = []
    elif fault == "future_factor":
        item["factors"] = [{"date": item["sessions"][-1], "value": "1"}]
    elif fault == "zero_close":
        item["rows"][50]["close"] = "0"
    elif fault == "pause":
        item["rows"][50]["trading_state"] = "paused"
    elif fault == "zero_trade":
        item["rows"][50]["trading_state"] = "verified_zero_trade"
    result = roc(roc_pack)
    assert all(row["value"] is None and row["reason"] == reason for row in result["rows"])


def test_roc_warmup_and_future_factors_cannot_shift_window(roc_pack):
    item = roc_pack[1]["items"][0]
    assert (
        roc(roc_pack, output_dates=[item["sessions"][119]])["rows"][0]["reason"]
        == "WARMUP_INSUFFICIENT"
    )
    before = roc(roc_pack)["rows"]
    item["factors"].append({"date": "2001-01-01", "value": "999"})
    assert roc(roc_pack)["rows"] == before


@pytest.mark.parametrize(
    "fault", ["unit", "precision", "duplicate_factor", "binary_float", "source_tamper"]
)
def test_roc_incompatible_contract_or_source_is_rejected(roc_pack, fault):
    item = roc_pack[1]["items"][0]
    if fault == "unit":
        item["price_unit"] = "CNY_per_lot"
    elif fault == "precision":
        item["price_policy"] = "Q2"
    elif fault == "duplicate_factor":
        item["factors"].append(deepcopy(item["factors"][-1]))
    elif fault == "binary_float":
        item["rows"][50]["close"] = 100.5
    else:
        (roc_pack[0] / "synthetic-source.json").write_text("changed")
    # JSON numbers parse as exact source strings by the existing pack loader;
    # a direct float object passed to the primitive still must be rejected.
    if fault == "binary_float":
        with pytest.raises(f1.AcceptanceError, match="exact source"):
            candidates._ROCSeries(item).roc120(item["sessions"][-1])
    else:
        with pytest.raises(f1.AcceptanceError):
            roc(roc_pack)


@pytest.fixture
def vroc_archive(tmp_path, monkeypatch):
    sessions = [f"2012-06-{day}" for day in [19, 20, 21, 25, 26, 27]]
    volumes = [101, 103, 107, 109, 113, 127]
    calendar = {
        "venue": "SSE",
        "days": [
            {"date": f"2012-06-{day}", "open": f"2012-06-{day}" in sessions}
            for day in range(19, 28)
        ],
    }
    payloads = {
        "sina_decoded": json.dumps([{"date": d, "volume": v} for d, v in zip(sessions, volumes)]),
        "sina_hfq": "var sh601012hfq="
        + json.dumps(
            {"total": 2, "data": [{"d": "2012-06-26", "f": "2"}, {"d": "2012-04-11", "f": "1"}]}
        ),
        "tencent_raw": "kline_day2012="
        + json.dumps(
            {
                "code": 0,
                "data": {
                    "sh601012": {
                        "day": [
                            [d, "0", "0", "0", "0", str(Decimal(v) / 100)]
                            for d, v in zip(sessions, volumes)
                        ]
                    }
                },
            }
        ),
        "calendar": json.dumps(calendar),
        "calendar_notice": "synthetic calendar evidence",
        "listing": "synthetic listing evidence",
    }
    refs = {}
    for name, text in payloads.items():
        path = tmp_path / (name + ".txt")
        path.write_text(text)
        refs[name] = {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    binding = {
        "schema": "vroc6.native-research-binding.v1",
        "definition": f1.VROC6_RESEARCH_DEFINITION,
        "role": f1.VROC6_RESEARCH_ROLE,
        **FLAGS,
        "security": "601012.XSHG",
        "input_start": "2012-06-19",
        "input_end": "2012-06-27",
        "ipo_date": "2012-04-11",
        "volume_unit": "share",
        "raw_adjustment": "none",
        "factor_convention": "sina_native_hfq",
        "anchor_policy": "source_ipo_row_no_rescaling",
        "factor_origin": "independent_provider_snapshot",
        "evidence": refs,
    }
    sha = seal(tmp_path / "binding.json", binding)
    monkeypatch.setattr(candidates, "_VROC6_BINDING_SHA256", sha)
    return tmp_path, sha


def vroc(archive, **kwargs):
    root, sha = archive
    parameters = dict(
        expected_binding_sha256=sha,
        definition=f1.VROC6_RESEARCH_DEFINITION,
        role=f1.VROC6_RESEARCH_ROLE,
    )
    parameters.update(kwargs)
    return f1.vroc6_fixed_snapshot_candidate(root, **parameters)


def test_vroc_keeps_declared_half_even_exact_result_and_failed_alignment(vroc_archive):
    result = vroc(vroc_archive)
    assert [row["local_research_quantity"] for row in result["rows"]] == [
        101,
        103,
        107,
        109,
        56,
        64,
    ]
    assert result["value_exact"] == {"numerator": "-3700", "denominator": "101"}
    assert result["raw_cross_source_matches"] == 6
    assert result["alignment_status"] == "failed_keep_blocked"
    assert result["fixed_demo_JQ_target_compared"] is False
    assert result["source_implementation_independent_review_completed"] is False
    assert result["integration_independent_review_completed"] is False
    assert all(result[key] is False for key in FLAGS)


@pytest.mark.parametrize(
    "fault", ["tamper", "missing", "alias", "production", "replacement_binding"]
)
def test_vroc_fixed_source_and_role_gates(vroc_archive, fault):
    kwargs = {}
    if fault == "tamper":
        (vroc_archive[0] / "sina_hfq.txt").write_text("changed")
    elif fault == "missing":
        (vroc_archive[0] / "calendar.txt").unlink()
    elif fault == "alias":
        kwargs["definition"] = "VROC6"
    elif fault == "production":
        kwargs["role"] = "production"
    else:
        kwargs["expected_binding_sha256"] = "a" * 64
    with pytest.raises((InputError, f1.AcceptanceError)):
        vroc(vroc_archive, **kwargs)


def test_vroc_json_and_factor_boundaries_remain_fail_closed():
    for text in ['{"a":1,"a":2}', '{"a":NaN}']:
        with pytest.raises(InputError):
            load_json(text)
    with pytest.raises(InputError, match="IPO=1"):
        factor_rows({"total": 1, "data": [{"d": "2012-04-11", "f": "2"}]}, "2012-04-11")


@pytest.fixture
def revenue_pack():
    pack = _financial_pack.__wrapped__()
    for row in list(pack["observations"]):
        if row["field"] != "total_operating_revenue":
            continue
        period = row["period_end"]
        base = Decimal("3000000.17" if period.startswith("2024") else "2500000.13")
        pack["observations"].append(
            {
                **row,
                "field": "operating_revenue",
                "record_id": period + ":operating_revenue",
                "value": str(base * (int(period[5:7]) // 3)),
            }
        )
    return pack


def revenue(pack, **kwargs):
    params = dict(definition=f1.FI03_RESEARCH_DEFINITION, mode=f1.RESEARCH_MODE)
    params.update(kwargs)
    return f1.reported_operating_revenue_ttm(
        f1.FinancialLedger(pack), "000001.XSHE", "2024-04-29T15:00:00+08:00", **params
    )


def test_reported_revenue_preserves_cents_four_quarters_bridge_and_sources(revenue_pack):
    result = revenue(revenue_pack)
    component = result["components"]["operating_revenue"]
    assert result["value"] == "10500000.56" and result["unit"] == "CNY"
    assert sum(Fraction(q["value"]) for q in component["quarters"]) == Fraction(result["value"])
    assert len(component["quarters"]) == 4 and component["quarter_sum_equals_bridge"]
    assert all(
        term["source_sha256"] and term["report_id"]
        for q in component["quarters"]
        for term in q["terms"]
    )
    assert result["alignment_status"] == "not_passed" and result["formal_id"] is None
    assert (
        result["jq_equivalent"] is False
        and result["integration_independent_review_completed"] is False
    )


def test_missing_quarter_cannot_pass_on_annual_bridge_alone(revenue_pack):
    revenue_pack["observations"] = [
        r
        for r in revenue_pack["observations"]
        if (r["field"], r["period_end"]) != ("operating_revenue", "2023-06-30")
    ]
    with pytest.raises(f1.FinancialInputUnavailable):
        revenue(revenue_pack)


def test_unlinked_report_versions_refused(revenue_pack):
    row = next(
        r for r in revenue_pack["observations"] if r["record_id"] == "2023-03-31:operating_revenue"
    )
    revenue_pack["reports"]["unlinked"] = {"published_date": "2024-04-27", "sha256": "b" * 64}
    revenue_pack["observations"].append(
        {**row, "report_id": "unlinked", "record_id": "unlinked-row"}
    )
    with pytest.raises(f1.AcceptanceError, match="complete replacement chain"):
        revenue(revenue_pack)


@pytest.mark.parametrize("kwargs", [{"definition": "operating_revenue_ttm"}, {"mode": "strict"}])
def test_fi03_requires_the_explicit_unquantized_research_definition(revenue_pack, kwargs):
    with pytest.raises(f1.AcceptanceError, match="explicit reported-revenue"):
        revenue(revenue_pack, **kwargs)


def test_no_official_factor_aliases_or_new_alignment_certificates():
    assert not any(hasattr(f1, name) for name in ["ROC120", "VROC6", "operating_revenue_ttm"])
