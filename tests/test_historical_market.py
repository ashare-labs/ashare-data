"""Synthetic contract tests; real-source checks live in the bounded verification script."""

from copy import deepcopy
import hashlib

import pytest

from ashare_data import DataError, HistoricalMarketView
from ashare_data.historical_market import DATA_SCHEMA, SCHEMA, ordinary_limits
from ashare_data.model import canonical


def fact(value, status="source_observed"):
    return dict(
        value=value,
        status=status if value is not None else "unavailable",
        source_refs=["raw.txt"],
        historical_available_at=None,
        reason="synthetic test",
    )


def dataset():
    state = dict(
        security="300750.XSHE",
        date="2020-08-24",
        suspended=fact(False),
        is_st=fact(False),
        reference_price=fact("192.23"),
        upper_limit=fact("230.68", "derived"),
        lower_limit=fact("153.78", "derived"),
        rule=dict(kind="ordinary", ratio="0.20", depends_on_st=False, source_refs=["raw.txt"]),
        source_claimed_limits=dict(
            raw_fields={"up_limit": "211.45", "down_limit": "173.01"},
            source_refs=["raw.txt"],
            execution_usable=False,
            validation="conflict_with_rule_model",
        ),
        intraday_halts_verified=False,
    )
    missing = deepcopy(state)
    missing.update(
        date="2020-08-25",
        suspended=fact(None),
        is_st=fact(None),
        reference_price=fact(None),
        upper_limit=fact(None),
        lower_limit=fact(None),
    )
    action = dict(
        event_id="cash-bonus",
        security="300750.XSHE",
        action_type="distribution",
        record_date="2023-04-25",
        ex_date="2023-04-26",
        publication_date="2023-04-18",
        payment_date="2023-04-26",
        new_share_trading_date="2023-04-26",
        share_credit_date="2023-04-26",
        cash_per_share="2.52",
        bonus_per_share="0",
        transfer_per_share="0.8",
        ex_price_cash_deduction=None,
        currency="CNY",
        unit="per_old_eligible_share",
        historical_available_at=None,
        cash_available_at=None,
        share_credit_at=None,
        payment_date_scope="depository only",
        share_credit_scope="capitalization shares",
        new_share_trading_scope="unrestricted only",
        source_refs=["raw.txt"],
    )
    instruments = [
        dict(
            security=s,
            initial_listing_date=fact(start),
            delisting_date=fact(end),
            source_refs=["raw.txt"],
        )
        for s, start, end in [
            ("300750.XSHE", "2018-06-11", None),
            ("600087.XSHG", "1997-06-12", "2014-06-05"),
        ]
    ]
    universe = dict(
        date="2014-06-04",
        source_row_count=2,
        source_query_complete=True,
        market_complete=False,
        source_refs=["raw.txt"],
        rows=[
            dict(security="600087.XSHG", suspended=fact(False), is_st=fact(False)),
            dict(security="600518.XSHG", suspended=fact(False), is_st=fact(None)),
        ],
    )
    empty = dict(
        date="2024-01-02",
        source_row_count=0,
        source_query_complete=True,
        market_complete=False,
        source_refs=["raw.txt"],
        rows=[],
    )
    return dict(
        schema=DATA_SCHEMA,
        scope={"synthetic": True},
        states=[state, missing],
        instruments=instruments,
        actions=[action],
        universes=[universe, empty],
    )


def package(tmp_path, data=None, *, binding_edit=None):
    data = dataset() if data is None else data
    files = {
        "data.json": canonical(data),
        "raw.txt": b"synthetic evidence, not a market observation",
    }
    files["raw.txt.receipt.json"] = canonical(
        dict(
            status=200,
            bytes=len(files["raw.txt"]),
            sha256=hashlib.sha256(files["raw.txt"]).hexdigest(),
            captured_start="2026-10-13T02:00:00+00:00",
            captured_end="2026-10-13T02:00:01+00:00",
        )
    )
    binding = dict(
        schema=SCHEMA,
        data="data.json",
        historical_pit=False,
        execution_permission=False,
        received_at="2026-10-13T02:00:01+00:00",
        files=[
            dict(path=k, bytes=len(v), sha256=hashlib.sha256(v).hexdigest())
            for k, v in files.items()
        ],
    )
    if binding_edit:
        binding_edit(binding)
    for name, raw in files.items():
        (tmp_path / name).write_bytes(raw)
    raw = canonical(binding)
    (tmp_path / "binding.json").write_bytes(raw)
    return tmp_path / "binding.json", hashlib.sha256(raw).hexdigest()


def load(tmp_path, data=None, **kwargs):
    path, sha = package(tmp_path, data, **kwargs)
    return HistoricalMarketView(path, expected_binding_sha256=sha)


def assert_code(code, fn):
    with pytest.raises(DataError) as err:
        fn()
    assert err.value.code == code


def test_realistic_bad_source_band_is_preserved_but_only_explicit_model_can_be_used(tmp_path):
    view = load(tmp_path)
    row = view.state("300750.XSHE", "2020-08-24")
    assert row["source_claimed_limits"]["raw_fields"]["up_limit"] == "211.45"
    assert row["upper_limit"]["value"] == "230.68"
    assert not row["source_claimed_limits"]["execution_usable"]
    args = dict(as_of="2020-08-24T09:31:00+08:00", side="BUY")
    assert not view.order_state("300750.XSHE", **args)["ready"]
    order = view.order_state(
        "300750.XSHE", **args, accept_daily_replay=True, accept_limit_model=True
    )
    assert order["ready"] and not order["intraday_halts_verified"]
    assert not order["account_actions_checked"] and not order["execution_permission"]


@pytest.mark.parametrize("purpose", ["historical_pit", "live_execution"])
def test_no_execution_promotion(tmp_path, purpose):
    view = load(tmp_path)
    assert_code(
        "PIT_UNAVAILABLE",
        lambda: view.order_state(
            "300750.XSHE",
            purpose=purpose,
            as_of="2020-08-24T09:31:00+08:00",
            side="BUY",
            accept_daily_replay=True,
            accept_limit_model=True,
        ),
    )


def test_knowledge_clocks_are_not_historical_publication(tmp_path):
    view = load(tmp_path)
    assert_code(
        "PIT_UNAVAILABLE", lambda: view.state("300750.XSHE", "2020-08-24", visibility="verified")
    )
    assert_code(
        "RECEIPT_NOT_VISIBLE",
        lambda: view.state(
            "300750.XSHE",
            "2020-08-24",
            visibility="received",
            knowledge_at="2020-08-24T16:00:00+08:00",
        ),
    )
    assert (
        view.state(
            "300750.XSHE",
            "2020-08-24",
            visibility="received",
            knowledge_at="2026-10-13T10:00:01+08:00",
        )["is_st"]["value"]
        is False
    )
    assert_code(
        "HISTORICAL_CLOCK",
        lambda: view.state("300750.XSHE", "2020-08-24", knowledge_at="2026-10-13T02:00:01+00:00"),
    )


def test_unknown_state_cannot_be_waived_and_model_does_not_infer_a_trading_day(tmp_path):
    view = load(tmp_path)
    row = view.state("300750.XSHE", "2020-08-25")
    assert row["suspended"]["value"] is None and row["is_st"]["value"] is None
    order = view.order_state(
        "300750.XSHE",
        as_of="2020-08-25T09:31:00+08:00",
        side="SELL",
        accept_daily_replay=True,
        accept_limit_model=True,
    )
    assert not order["ready"] and "UNKNOWN_SUSPENDED" in order["issues"]
    assert_code(
        "HISTORICAL_UNKNOWN",
        lambda: view.state("300750.XSHE", "2020-08-25", require_fields=["is_st"]),
    )


def test_earlier_day_clock_and_full_day_halt_block(tmp_path):
    data = dataset()
    data["states"][0]["suspended"] = fact(True)
    view = load(tmp_path, data)
    order = view.order_state(
        "300750.XSHE",
        as_of="2020-08-24T08:59:59+08:00",
        side="SELL",
        accept_daily_replay=True,
        accept_limit_model=True,
    )
    assert set(order["issues"]) == {"BEFORE_REPLAY_CLOCK", "DAILY_SUSPENDED"}


def test_unlimited_is_neither_zero_nor_unknown(tmp_path):
    data = dataset()
    row = data["states"][0]
    row["rule"]["kind"] = "unlimited"
    row["rule"]["ratio"] = None
    for f in ("upper_limit", "lower_limit"):
        row[f] = dict(fact(None), status="not_applicable")
    row["source_claimed_limits"]["validation"] = "conflict_with_unlimited_rule"
    view = load(tmp_path, data)
    assert (
        view.state("300750.XSHE", "2020-08-24", require_fields=["upper_limit"])["upper_limit"][
            "value"
        ]
        is None
    )
    assert view.order_state(
        "300750.XSHE",
        as_of="2020-08-24T09:31:00+08:00",
        side="BUY",
        accept_daily_replay=True,
        accept_limit_model=True,
    )["ready"]


@pytest.mark.parametrize("field", ["suspended", "is_st", "reference_price", "upper_limit"])
def test_not_applicable_is_field_and_regime_specific(tmp_path, field):
    data = dataset()
    data["states"][0][field].update(value=None, status="not_applicable")
    assert_code("HISTORICAL_SCHEMA", lambda: load(tmp_path, data))


@pytest.mark.parametrize("field", ["suspended", "reference_price"])
def test_consumers_defend_required_values_even_if_internal_state_is_mislabeled(tmp_path, field):
    view = load(tmp_path)
    # Deliberate post-load internal corruption exercises both consumer checks,
    # independently of the loader's stricter field-state validation.
    view._states[("300750.XSHE", "2020-08-24")][field].update(value=None, status="not_applicable")
    assert_code(
        "HISTORICAL_UNKNOWN",
        lambda: view.state("300750.XSHE", "2020-08-24", require_fields=[field]),
    )
    result = view.order_state(
        "300750.XSHE",
        as_of="2020-08-24T09:31:00+08:00",
        side="BUY",
        accept_daily_replay=True,
        accept_limit_model=True,
    )
    assert not result["ready"] and "UNKNOWN_" + field.upper() in result["issues"]


def conflicting_dataset():
    data = dataset()
    data["universes"].append(
        dict(
            date="2020-08-24",
            source_row_count=1,
            source_query_complete=True,
            market_complete=False,
            source_refs=["raw.txt"],
            rows=[dict(security="300750.XSHE", suspended=fact(False), is_st=fact(True))],
        )
    )
    return data


def test_cross_source_conflict_cannot_keep_a_usable_band(tmp_path):
    assert_code("HISTORICAL_DEPENDENCY_CONFLICT", lambda: load(tmp_path, conflicting_dataset()))


def test_quarantined_conflict_retains_both_sources_and_blocks_consumers(tmp_path):
    data = conflicting_dataset()
    for field in ("upper_limit", "lower_limit"):
        data["states"][0][field] = fact(None)
    view = load(tmp_path, data)
    row = view.state("300750.XSHE", "2020-08-24")
    assert row["is_st"]["value"] is False
    assert view.universe("2020-08-24")["rows"][0]["is_st"]["value"] is True
    assert row["dependency_conflicts"][0]["claims"] == [
        dict(origin="daily_state", value=False, source_refs=["raw.txt"]),
        dict(origin="dated_population", value=True, source_refs=["raw.txt"]),
    ]
    assert_code(
        "HISTORICAL_DEPENDENCY_CONFLICT",
        lambda: view.state("300750.XSHE", "2020-08-24", require_fields=["suspended"]),
    )
    result = view.order_state(
        "300750.XSHE",
        as_of="2020-08-24T09:31:00+08:00",
        side="SELL",
        accept_daily_replay=True,
        accept_limit_model=True,
    )
    assert not result["ready"] and "STATE_DEPENDENCY_CONFLICT" in result["issues"]


def test_repinning_cannot_erase_computed_dependency_conflicts(tmp_path):
    data = conflicting_dataset()
    for field in ("upper_limit", "lower_limit"):
        data["states"][0][field] = fact(None)
    data["states"][0]["dependency_conflicts"] = []
    assert_code("HISTORICAL_DEPENDENCY_CONFLICT", lambda: load(tmp_path, data))


def test_historical_population_keeps_delisted_and_absence_is_unknown(tmp_path):
    view = load(tmp_path)
    pop = view.universe(
        "2014-06-04", candidates=["600087.XSHG", "300750.XSHE", "600518.XSHG"], exclude_st=True
    )
    assert [r["security"] for r in pop["rows"]] == ["600087.XSHG"]
    assert {r["reason"] for r in pop["unknown"]} == {
        "absent_from_captured_population",
        "filter_field_unknown",
    }
    assert not pop["survivor_filter_applied"]
    assert view.universe("2024-01-02")["coverage_state"] == "unavailable"
    assert_code(
        "HISTORICAL_COVERAGE", lambda: view.universe("2014-06-04", require_complete_market=True)
    )
    assert_code("HISTORICAL_SCOPE", lambda: view.universe("2026-10-13"))


def test_lifecycle_does_not_make_continuous_listing_claim(tmp_path):
    view = load(tmp_path)
    assert (
        view.instruments(["600087.XSHG"], on_date="2014-06-04")[0]["lifecycle_state"] == "unknown"
    )
    assert (
        view.instruments(["600087.XSHG"], on_date="2014-06-05")[0]["lifecycle_state"] == "delisted"
    )
    assert (
        view.instruments(["300750.XSHE"], on_date="2017-01-01")[0]["lifecycle_state"]
        == "not_yet_listed"
    )
    assert (
        view.instruments(["300750.XSHE"], on_date="2026-01-01")[0]["delisting_date"]["value"]
        is None
    )


def test_cash_shares_scope_and_unknown_clock_remain_distinct(tmp_path):
    view = load(tmp_path)
    args = dict(start="2023-04-01", end="2023-04-30")
    events = view.corporate_actions(["300750.XSHE"], **args)
    row = events["rows"][0]
    assert (row["cash_per_share"], row["transfer_per_share"]) == ("2.52", "0.8")
    assert row["payment_date_scope"] == "depository only"
    assert row["new_share_trading_scope"] == "unrestricted only"
    assert row["cash_available_at"] is None and row["share_credit_at"] is None
    assert not events["absence_proved"] and events["tax_model"] is None
    for kw in (dict(require_cash_time=True), dict(require_share_credit_time=True)):
        assert_code(
            "HISTORICAL_ACTION_TIME_UNKNOWN",
            lambda: view.corporate_actions(["300750.XSHE"], **args, **kw),
        )
    assert not view.corporate_actions(["300750.XSHE"], start="2024-01-01", end="2024-12-31")[
        "complete"
    ]


def test_mutation_isolation_and_no_postload_filesystem_dependency(tmp_path):
    view = load(tmp_path)
    row = view.state("300750.XSHE", "2020-08-24")
    row["is_st"]["value"] = True
    view.lineage()["files"].clear()
    view.universe("2014-06-04")["rows"].clear()
    (tmp_path / "data.json").write_text("garbage")
    assert view.state("300750.XSHE", "2020-08-24")["is_st"]["value"] is False
    assert len(view.universe("2014-06-04")["rows"]) == 2


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda d: d["states"][0]["upper_limit"].update(value="211.45"), "HISTORICAL_LIMIT_MODEL"),
        (lambda d: d["states"][0]["is_st"].update(value=0), "HISTORICAL_VALUE"),
        (
            lambda d: d["states"][0]["is_st"].update(
                historical_available_at="2020-08-24T09:00:00+08:00"
            ),
            "HISTORICAL_SCHEMA",
        ),
        (
            lambda d: d["states"][0]["source_claimed_limits"].update(execution_usable=True),
            "HISTORICAL_SCHEMA",
        ),
        (lambda d: d["universes"][0].update(source_row_count=3), "HISTORICAL_COVERAGE"),
        (lambda d: d["universes"][0].update(market_complete=True), "HISTORICAL_COVERAGE"),
        (
            lambda d: d["actions"][0].update(cash_available_at="2023-04-26T09:00:00+08:00"),
            "HISTORICAL_ACTION",
        ),
        (lambda d: d["actions"][0].update(unit="per_10_shares"), "HISTORICAL_ACTION"),
        (lambda d: d["actions"][0].update(payment_date_scope=""), "HISTORICAL_ACTION"),
    ],
)
def test_reject_semantic_promotions_even_with_rehashed_binding(tmp_path, mutation, code):
    data = dataset()
    mutation(data)
    assert_code(code, lambda: load(tmp_path, data))


def test_package_clock_cannot_precede_its_receipts(tmp_path):
    assert_code(
        "HISTORICAL_CLOCK",
        lambda: load(
            tmp_path, binding_edit=lambda b: b.update(received_at="2020-01-01T00:00:00+00:00")
        ),
    )


def test_digest_and_path_guards(tmp_path):
    path, sha = package(tmp_path)
    assert_code(
        "HISTORICAL_INTEGRITY", lambda: HistoricalMarketView(path, expected_binding_sha256="0" * 64)
    )
    (tmp_path / "raw.txt").write_text("changed")
    assert_code(
        "HISTORICAL_INTEGRITY", lambda: HistoricalMarketView(path, expected_binding_sha256=sha)
    )
    path, sha = package(tmp_path, binding_edit=lambda b: b["files"][0].update(path="../outside"))
    assert_code("HISTORICAL_FILE", lambda: HistoricalMarketView(path, expected_binding_sha256=sha))


def test_cent_rounding_and_smallest_supported_domain():
    assert ordinary_limits("6.64", "0.05") == ("6.97", "6.31")
    assert ordinary_limits("192.23", "0.20") == ("230.68", "153.78")
    assert ordinary_limits("10", "0.30") == ("13.00", "7.00")
    assert_code("HISTORICAL_LIMIT_MODEL", lambda: ordinary_limits("0.01", "0.10"))
