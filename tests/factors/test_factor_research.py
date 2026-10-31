"""Contract/edge tests; synthetic fixtures are not counted as broad real evidence."""

from copy import deepcopy
from fractions import Fraction as F
import hashlib
import json

import pytest

from ashare_data import FactorResearchView
from ashare_data.factors.joinquant.research_view import DEFINITION, FIELDS, MODE


def inputs():
    days = [f"2024-05-{i:02d}" for i in range(1, 21)]
    bars = {
        d: {
            "open": str(i),
            "high": str(i),
            "low": str(i),
            "close": str(i),
            "volume": "100",
            "amount": str(100 * i),
        }
        for i, d in enumerate(days, 1)
    }
    reports = []
    for period, notice, cash, profit in (
        ("2023-09-30", "2023-10-20", "60", "30"),
        ("2023-12-31", "2024-03-20", "100", "40"),
        ("2024-03-31", "2024-04-20", "12", "6"),
    ):
        for kind, values in (
            ("cash", {"NETCASH_OPERATE": cash}),
            ("income", {"PARENT_NETPROFIT": profit}),
            (
                "balance",
                {
                    "TOTAL_CURRENT_ASSETS": "30",
                    "INVENTORY": "10",
                    "TOTAL_CURRENT_LIAB": "10",
                    "TOTAL_LIABILITIES": "40",
                    "TOTAL_ASSETS": "100",
                },
            ),
        ):
            reports.append(
                {
                    "kind": kind,
                    "period": period,
                    "notice_date": notice,
                    "update_date": notice,
                    "values": values,
                    "source_path": "raw/test",
                }
            )
    return {
        "schema": "ad.factor-research-independent-inputs.v1",
        "input_contains_JQ_values": False,
        "raw_price_adjustment": "none",
        "units": {"price": "CNY/share", "volume": "shares", "financial": "consolidated_CNY"},
        "sessions": days,
        "input_windows": [[days[0], days[-1]]],
        "items": [
            {
                "security": "600519.XSHG",
                "sina": bars,
                "tencent": deepcopy(bars),
                "factors": [{"date": "2023-01-01", "value": "7.123456789"}],
                "reports": reports,
            }
        ],
    }


def query(data=None, fields=FIELDS, dates="2024-05-20", **kwargs):
    return FactorResearchView(inputs() if data is None else data, **kwargs).query(
        "600519.XSHG", fields, dates=dates, definition=DEFINITION, mode=MODE
    )


def current(data, kind):
    return next(
        r for r in data["items"][0]["reports"] if r["kind"] == kind and r["period"] == "2024-03-31"
    )


def close_enough(value, expected):
    assert abs(F(value) - expected) <= F(1, 10**47)


def test_hand_calculations_and_explicit_research_flags():
    result = query()
    expected = [F(100, 9), F(380, 3), F(100), F(2), F(2, 5), F(2)]
    assert len(result["rows"]) == 6 and not result["source_files_verified"]
    for row, value in zip(result["rows"], expected, strict=True):
        assert row["status"] == "ok"
        close_enough(row["value"], value)
        assert not any(
            row[k] for k in ("production_eligible", "historical_pit_certified", "jq_equivalent")
        )


def test_common_positive_adjustment_scale_does_not_change_price_ratios():
    data = inputs()
    before = query(data)["rows"][:3]
    data["items"][0]["factors"][0]["value"] = "0.00000001345678987654321"
    assert [r["value"] for r in query(data)["rows"][:3]] == [r["value"] for r in before]


def test_real_factor_event_is_effective_on_event_date_without_quantization():
    data = inputs()
    data["items"][0]["factors"] = [
        {"date": "2023-01-01", "value": "1"},
        {"date": "2024-05-20", "value": "2"},
    ]
    row = query(data, "BIAS5")["rows"][0]
    # 16,17,18,19,40, with factor applied before computing the mean.
    close_enough(row["value"], F(900, 11))
    assert row["within_window_factor_changes"] == 1


def test_psy_ties_are_not_up_and_uses_thirteen_closes():
    data = inputs()
    for row in data["items"][0]["sina"].values():
        row.update(open="10", high="10", low="10", close="10")
    data["items"][0]["sina"]["2024-05-20"].update(open="11", high="11", low="11", close="11")
    row = query(data, "PSY")["rows"][0]
    close_enough(row["value"], F(100, 12))
    assert row["observed_sessions"] == 13 and row["window_start"] == "2024-05-08"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("gap", "SOURCE_SESSION_MISSING:2024-05-17"),
        ("halt", "NORMAL_TRADING_INPUT_REQUIRED:2024-05-17"),
        ("factor", "EFFECTIVE_FACTOR_MISSING:2024-05-16"),
        ("warmup", "WARMUP_OR_CONTIGUOUS_WINDOW_MISSING"),
        ("split_grid", "WARMUP_OR_CONTIGUOUS_WINDOW_MISSING"),
    ],
)
def test_missing_windows_are_not_compressed_filled_or_bridged(change, reason):
    data = inputs()
    if change == "gap":
        del data["items"][0]["sina"]["2024-05-17"]
    elif change == "halt":
        data["items"][0]["sina"]["2024-05-17"]["volume"] = "0"
    elif change == "factor":
        data["items"][0]["factors"] = []
    elif change == "warmup":
        data["sessions"] = data["sessions"][-4:]
    else:
        data["input_windows"] = [["2024-05-01", "2024-05-17"], ["2024-05-18", "2024-05-20"]]
    row = query(data, "BIAS5")["rows"][0]
    assert row["status"] == "missing" and row["value"] is None and row["reason"] == reason


def test_flat_cci_is_missing_instead_of_inventing_zero():
    data = inputs()
    for row in data["items"][0]["sina"].values():
        row.update(open="1", high="1", low="1", close="1")
    assert query(data, "CCI20")["rows"][0]["reason"] == "ZERO_DENOMINATOR"


@pytest.mark.parametrize("field", ["close", "volume"])
def test_missing_bar_field_is_a_typed_gap(field):
    data = inputs()
    del data["items"][0]["sina"]["2024-05-20"][field]
    assert query(data, "BIAS5")["rows"][0]["reason"] == "NUMERIC_INPUT_MISSING"


def test_same_day_notice_excluded_and_quarter_is_ytd_difference():
    rows = query(fields="net_operating_cash_flow_coverage", dates=["2024-04-20", "2024-04-21"])[
        "rows"
    ]
    assert [(r["period_end"], r["value"]) for r in rows] == [
        ("2023-12-31", "4"),
        ("2024-03-31", "2"),
    ]
    assert len(rows[0]["financial_sources"]) == 4


@pytest.mark.parametrize(
    "field,key", [("quick_ratio", "INVENTORY"), ("debt_to_asset_ratio", "TOTAL_ASSETS")]
)
def test_missing_latest_balance_field_does_not_fall_back(field, key):
    data = inputs()
    del current(data, "balance")["values"][key]
    assert query(data, field)["rows"][0]["reason"] == "NUMERIC_INPUT_MISSING"


@pytest.mark.parametrize(
    "field,kind,key",
    [
        ("quick_ratio", "balance", "TOTAL_CURRENT_LIAB"),
        ("debt_to_asset_ratio", "balance", "TOTAL_ASSETS"),
        ("net_operating_cash_flow_coverage", "income", "PARENT_NETPROFIT"),
    ],
)
def test_zero_denominator_is_missing(field, kind, key):
    data = inputs()
    current(data, kind)["values"][key] = "0"
    assert query(data, field)["rows"][0]["reason"] == "ZERO_DENOMINATOR"


def test_negative_profit_and_snapshot_vintage_are_explicit():
    data = inputs()
    current(data, "income")["values"]["PARENT_NETPROFIT"] = "-6"
    current(data, "income")["update_date"] = "2026-10-13"
    row = query(data, "net_operating_cash_flow_coverage")["rows"][0]
    assert row["value"] == "-2" and row["negative_denominator"]
    assert row["component_updates_after_cutoff"] == 1 and not row["historical_pit_certified"]


def test_missing_prior_ytd_and_mismatched_latest_periods_are_gaps():
    data = inputs()
    data["items"][0]["reports"] = [
        r
        for r in data["items"][0]["reports"]
        if not (r["kind"] == "cash" and r["period"] == "2023-09-30")
    ]
    assert query(data, "net_operating_cash_flow_coverage", "2024-04-20")["rows"][0][
        "reason"
    ].startswith("PREVIOUS_YTD_COMPONENT_MISSING")
    current(data, "income")["notice_date"] = "2024-06-01"
    assert (
        query(data, "net_operating_cash_flow_coverage")["rows"][0]["reason"]
        == "LATEST_REPORT_PERIODS_DIFFER"
    )


@pytest.mark.parametrize("value", ["-1", "NaN", True])
def test_invalid_balance_input_is_rejected(value):
    data = inputs()
    current(data, "balance")["values"]["INVENTORY"] = value
    with pytest.raises(ValueError):
        query(data, "quick_ratio")


def test_api_copies_inputs_preserves_grid_and_requires_named_mode():
    data = inputs()
    view = FactorResearchView(data)
    data["items"].clear()
    result = view.query(
        ["600519.XSHG", "000001.XSHE"],
        ["BIAS5", "PSY"],
        dates="2024-05-20",
        definition=DEFINITION,
        mode=MODE,
    )
    assert [r["status"] for r in result["rows"]] == ["ok", "ok", "missing", "missing"]
    for override in (
        {"definition": "auto"},
        {"mode": "strict"},
        {"provider": "JQ"},
        {"fields": "ROC120"},
        {"securities": "sh600519"},
        {"dates": "20240520"},
        {"fields": ["PSY", "PSY"]},
    ):
        args = dict(
            securities="600519.XSHG",
            fields="PSY",
            dates="2024-05-20",
            definition=DEFINITION,
            mode=MODE,
        )
        args.update(override)
        with pytest.raises(ValueError):
            view.query(**args)


def test_hash_pinned_sources_and_containment(tmp_path):
    data = inputs()
    raw = b"captured bytes"
    (tmp_path / "raw.txt").write_bytes(raw)
    data["sources"] = [
        {"path": "raw.txt", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    ]
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(data))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    view = FactorResearchView.from_file(path, evidence_root=tmp_path, expected_sha256=sha)
    assert view.query("600519.XSHG", "PSY", dates="2024-05-20", definition=DEFINITION, mode=MODE)[
        "source_files_verified"
    ]
    with pytest.raises(ValueError, match="projection hash"):
        FactorResearchView.from_file(path, evidence_root=tmp_path, expected_sha256="0" * 64)
    (tmp_path / "raw.txt").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash/size"):
        FactorResearchView.from_file(path, evidence_root=tmp_path)
    data["sources"][0]["path"] = "../raw.txt"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unsafe"):
        FactorResearchView.from_file(path, evidence_root=tmp_path)
