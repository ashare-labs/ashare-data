"""Synthetic filter boundary controls, separate from fixed real source evidence."""

from copy import deepcopy

import pytest

from test_historical_market import assert_code, dataset, fact, load

SYMBOL = "300750.XSHE"
DAY = "2020-08-24"
FILTERS = {"is_st": "exclude_st", "suspended": "exclude_suspended"}


def pool_dataset(field="is_st", pool_value=False, state_value=False, *, state_present=True):
    data = dataset()
    state = data["states"][0]
    state[field] = fact(state_value)
    state["rule"] = dict(kind="unknown", ratio=None, depends_on_st=False, source_refs=["raw.txt"])
    for name in ("reference_price", "upper_limit", "lower_limit"):
        state[name] = fact(None)
    if not state_present:
        data["states"].remove(state)
    member = dict(security=SYMBOL, is_st=fact(False), suspended=fact(False))
    member[field] = fact(pool_value)
    data["universes"].append(
        dict(
            date=DAY,
            source_row_count=1,
            source_query_complete=True,
            market_complete=False,
            source_refs=["raw.txt"],
            rows=[member],
        )
    )
    return data


def event(field, value, *, effective_date=DAY, end=None):
    result = dict(
        event_id="dated-flag",
        security=SYMBOL,
        effective_date=effective_date,
        publication_date="2020-08-20",
        historical_available_at=None,
        event_type="st_designation",
        source_refs=["raw.txt"],
    )
    if field == "is_st":
        result["is_st_from_effective_date"] = value
        if end:
            result["effective_end_date"] = end
    else:
        result.update(
            event_type="full_day_suspension",
            suspension_start_date=effective_date,
            suspension_end_date_inclusive=end or effective_date,
            resume_date="2020-08-25",
        )
    return result


@pytest.mark.parametrize("field", FILTERS)
@pytest.mark.parametrize("pool_value", [False, True])
@pytest.mark.parametrize("candidates", [None, [SYMBOL]])
def test_active_filter_conflict_is_unknown_in_both_directions(
    tmp_path, field, pool_value, candidates
):
    view = load(tmp_path, pool_dataset(field, pool_value, not pool_value))
    result = view.universe(DAY, candidates=candidates, **{FILTERS[field]: True})
    assert result["rows"] == result["excluded"] == []
    assert len(result["unknown"]) == 1
    item = result["unknown"][0]
    assert item["security"] == SYMBOL and item["reason"] == "filter_dependency_conflict"
    assert item["fields"] == [field] and item["source_row"][field]["value"] is pool_value
    claims = item["conflicts"][0]["claims"]
    assert claims == [
        dict(origin="dated_population", value=pool_value, source_refs=["raw.txt"]),
        dict(origin="daily_state", value=not pool_value, source_refs=["raw.txt"]),
    ]
    assert result["dependency_conflicts"] == [dict(security=SYMBOL, conflicts=item["conflicts"])]
    assert result["filter_fields"] == [field]


@pytest.mark.parametrize("field", FILTERS)
@pytest.mark.parametrize("value,expected", [(False, "rows"), (True, "excluded"), (None, "unknown")])
@pytest.mark.parametrize("state_present", [False, True])
def test_known_pool_and_missing_claims_are_distinct(
    tmp_path, field, value, expected, state_present
):
    # No daily fact is not an opposite daily fact; a known bounded pool flag suffices.
    view = load(tmp_path, pool_dataset(field, value, None, state_present=state_present))
    result = view.universe(DAY, **{FILTERS[field]: True})
    assert len(result[expected]) == 1
    assert all(not result[k] for k in {"rows", "excluded", "unknown"} - {expected})
    assert not result["dependency_conflicts"]
    if value is None:
        assert result["unknown"][0]["reason"] == "filter_field_unknown"
        assert result["unknown"][0]["fields"] == [field]


@pytest.mark.parametrize("field", FILTERS)
def test_unknown_pool_is_not_filled_from_known_daily_state(tmp_path, field):
    view = load(tmp_path, pool_dataset(field, None, False))
    result = view.universe(DAY, **{FILTERS[field]: True})
    assert not result["rows"] and result["unknown"][0]["reason"] == "filter_field_unknown"
    assert result["unknown"][0]["source_row"][field]["value"] is None


@pytest.mark.parametrize("field", FILTERS)
@pytest.mark.parametrize("state_present", [False, True])
def test_population_event_conflict_survives_missing_daily_fact(tmp_path, field, state_present):
    data = pool_dataset(field, False, None, state_present=state_present)
    data["security_events"] = [event(field, True)]
    view = load(tmp_path, data)
    result = view.universe(DAY, **{FILTERS[field]: True})
    assert not result["rows"] and not result["excluded"]
    item = result["unknown"][0]
    assert item["reason"] == "filter_dependency_conflict"
    assert {c["origin"] for c in item["conflicts"][0]["claims"]} == {
        "dated_population",
        "dated-flag",
    }


def test_unknown_daily_st_cannot_support_a_known_st_dependent_rule(tmp_path):
    data = pool_dataset("is_st", False, None)
    data["states"][0]["rule"].update(kind="ordinary", ratio="0.05", depends_on_st=True)
    # This package is already invalid at the stricter state/rule boundary.
    assert_code("HISTORICAL_LIMIT_MODEL", lambda: load(tmp_path, data))


@pytest.mark.parametrize("field", FILTERS)
@pytest.mark.parametrize("value", [None, False])
def test_disabled_flag_unknown_or_conflict_does_not_drive_other_filter(tmp_path, field, value):
    view = load(tmp_path, pool_dataset(field, value, True))
    other = "suspended" if field == "is_st" else "is_st"
    result = view.universe(DAY, **{FILTERS[other]: True})
    assert len(result["rows"]) == 1 and not result["unknown"] and not result["excluded"]
    assert result["filter_fields"] == [other]
    assert bool(result["dependency_conflicts"]) is (value is False)
    raw = view.universe(DAY)
    assert len(raw["rows"]) == 1 and raw["filter_fields"] == []


@pytest.mark.parametrize("field", FILTERS)
def test_two_filters_do_not_silently_exclude_a_conflict_or_unknown(tmp_path, field):
    other = "suspended" if field == "is_st" else "is_st"
    data = pool_dataset(field, False, True)
    data["universes"][-1]["rows"][0][other] = fact(True)
    data["states"][0][other] = fact(True)
    view = load(tmp_path, data)
    result = view.universe(DAY, exclude_st=True, exclude_suspended=True)
    assert result["rows"] == result["excluded"] == []
    assert result["unknown"][0]["reason"] == "filter_dependency_conflict"
    view._universes[DAY]["rows"][0][field] = fact(None)
    result = view.universe(DAY, exclude_st=True, exclude_suspended=True)
    assert result["rows"] == result["excluded"] == []
    assert result["unknown"][0]["reason"] == "filter_field_unknown"


@pytest.mark.parametrize("field", FILTERS)
@pytest.mark.parametrize("status", ["not_applicable", "unknown"])
def test_invalid_flag_states_are_rejected_at_load_even_without_filters(tmp_path, field, status):
    data = pool_dataset(field, None, None)
    data["universes"][-1]["rows"][0][field]["status"] = status
    assert_code("HISTORICAL_SCHEMA", lambda: load(tmp_path, data))


@pytest.mark.parametrize("field", FILTERS)
@pytest.mark.parametrize(
    "value,status",
    [
        (None, "not_applicable"),
        (False, "not_applicable"),
        (False, "unavailable"),
        ("0", "source_observed"),
    ],
)
def test_filter_defends_invalid_values_if_internal_data_is_corrupted(
    tmp_path, field, value, status
):
    view = load(tmp_path, pool_dataset(field, False, None))
    view._universes[DAY]["rows"][0][field].update(value=value, status=status)
    result = view.universe(DAY, **{FILTERS[field]: True})
    assert not result["rows"] and not result["excluded"]
    assert result["unknown"][0]["reason"] == "filter_field_unknown"


@pytest.mark.parametrize("field", FILTERS)
def test_conflicts_are_same_key_not_future_or_other_security(tmp_path, field):
    data = pool_dataset(field, False, True)
    data["states"][0]["date"] = "2020-08-23"
    future = event(field, True, effective_date="2020-08-26")
    foreign = event(field, True)
    foreign.update(event_id="other-security", security="600087.XSHG")
    data["security_events"] = [future, foreign]
    result = load(tmp_path, data).universe(DAY, **{FILTERS[field]: True})
    assert len(result["rows"]) == 1 and not result["dependency_conflicts"]


@pytest.mark.parametrize(
    "end,unknown", [(None, False), ("2020-08-24", True), ("2020-08-23", False)]
)
def test_st_event_does_not_extend_beyond_observed_bound(tmp_path, end, unknown):
    data = pool_dataset("is_st", False, None, state_present=False)
    data["security_events"] = [event("is_st", True, effective_date="2020-08-23", end=end)]
    result = load(tmp_path, data).universe(DAY, exclude_st=True)
    assert bool(result["unknown"]) is unknown and bool(result["rows"]) is not unknown


def test_known_resume_date_is_not_extended_after_halt(tmp_path):
    data = pool_dataset("suspended", True, None, state_present=False)
    data["security_events"] = [
        event("suspended", True, effective_date="2020-08-22", end="2020-08-23")
    ]
    data["security_events"][0]["resume_date"] = DAY
    result = load(tmp_path, data).universe(DAY, exclude_suspended=True)
    assert result["unknown"][0]["reason"] == "filter_dependency_conflict"
    assert {c["value"] for c in result["unknown"][0]["conflicts"][0]["claims"]} == {True, False}


def test_unlimited_price_na_does_not_block_known_boolean_filters(tmp_path):
    data = pool_dataset()
    state = data["states"][0]
    state["reference_price"] = fact("192.23")
    state["rule"].update(kind="unlimited", reason="synthetic IPO control")
    for field in ("upper_limit", "lower_limit"):
        state[field].update(status="not_applicable")
    result = load(tmp_path, data).universe(DAY, exclude_st=True, exclude_suspended=True)
    assert len(result["rows"]) == 1 and not result["unknown"]


def test_diagnostics_are_copied_and_do_not_contaminate_later_calls(tmp_path):
    view = load(tmp_path, pool_dataset("is_st", False, True))
    result = view.universe(DAY, exclude_st=True)
    original = deepcopy(result)
    result["unknown"][0]["source_row"]["is_st"]["value"] = True
    result["dependency_conflicts"][0]["conflicts"][0]["claims"].clear()
    assert view.universe(DAY, exclude_st=True) == original
