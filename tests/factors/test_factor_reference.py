"""Budget/transport failures and candidate loss that paired ranks alone can hide."""

from copy import deepcopy
import struct

import pytest

from validate_factor_reference import candidate_selection_impact, validate_captures


def fixture():
    scope = {
        "securities": [{"security": s} for s in ("a", "b")],
        "reference_dates": ["2024-04-19"],
        "fields": ["ROC120"],
        "reference_budget": {"already_spent": 3, "authorized_total": 10},
        "rank_rule": {"top_k": 1},
    }
    rows = [
        {
            "security": s,
            "date": "2024-04-19",
            "field": "ROC120",
            "provider": "JoinQuant",
            "value": value,
            "missing_kind": "NaN" if value is None else "none",
            "float64_bits_be": None if value is None else struct.pack(">d", float(value)).hex(),
        }
        for s, value in (("a", "1.0"), ("b", None))
    ]
    captures = [{"date": "2024-04-19", "returned_values": 2, "rows": rows}]
    ledger = {
        "account_precheck": {"points": 45},
        "attempts": [
            {
                "date": "2024-04-19",
                "returned_values": 2,
                "status": "captured_and_balance_checked",
                "points_before": 45,
                "points_after": 45,
            }
        ],
        "prior_actual_spend": 3,
        "actual_new_returned_values": 2,
        "actual_total_spend": 5,
        "unspent_values": 5,
    }
    return scope, captures, ledger


def test_nan_is_counted_in_budget_not_dropped():
    scope, captures, ledger = fixture()
    assert len(validate_captures(captures, ledger, scope)) == 2
    ledger["actual_new_returned_values"] = 1
    with pytest.raises(ValueError, match="budget"):
        validate_captures(captures, ledger, scope)


@pytest.mark.parametrize("change", ["duplicate", "omit", "bits", "points", "extra_returned"])
def test_uncertain_or_corrupt_capture_is_rejected(change):
    scope, captures, ledger = fixture()
    if change == "duplicate":
        captures[0]["rows"][1] = deepcopy(captures[0]["rows"][0])
    elif change == "omit":
        captures[0]["rows"].pop()
    elif change == "bits":
        captures[0]["rows"][0]["float64_bits_be"] = "0" * 16
    elif change == "points":
        ledger["attempts"][0]["points_after"] = 44
    else:
        captures[0]["returned_values"] = 3
    with pytest.raises(ValueError):
        validate_captures(captures, ledger, scope)


def test_missing_high_candidate_changes_selection_without_imputing_a_rank():
    scope, captures, _ = fixture()
    reference = captures[0]["rows"]
    reference[1]["value"] = "3"
    local = deepcopy(reference)
    local[1]["value"] = None
    result = candidate_selection_impact(local, reference, scope)[0]
    assert result["missing_local_only"] == ["b"]
    assert result["top"] == {
        "local": ["a"],
        "reference": ["b"],
        "replacement_count": 1,
        "enough_for_selection": True,
    }
    assert result["bottom"]["replacement_count"] == 0
    assert local[1]["value"] is None
