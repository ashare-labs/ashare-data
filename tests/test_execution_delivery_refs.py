"""Regression coverage for semantic pointers and package-relative gap references."""

from copy import deepcopy
import json
from pathlib import Path
import runpy

import pytest


@pytest.fixture(scope="module")
def refs():
    return runpy.run_path(str(Path(__file__).resolve().parent / "support/execution_delivery_refs.py"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def event():
    return dict(
        security="000651.XSHE",
        ex_date="2022-04-08",
        type="cash_dividend",
        announcement_date="2022-03-31",
        record_date="2022-04-07",
        payment_date="2022-04-08",
        cash_per_share="1",
        bonus_share_ratio="0",
        capitalization_share_ratio="0",
        ex_price_cash_deduction="0.936124",
    )


def test_raw_identity_search_does_not_reuse_filtered_index(tmp_path, refs):
    expected = event()
    foreign = dict(expected, security="600000.XSHG", ex_date="2023-07-21", cash_per_share="0.32")
    previous = dict(expected, ex_date="2023-08-09")
    path = tmp_path / "raw.json"
    save(path, dict(actions=[foreign, previous, foreign, expected]))
    assert refs["action_reference"](path, expected) == str(path) + "#/actions/3"
    _, wrong = refs["resolve_reference"](str(path) + "#/actions/0", tmp_path)
    with pytest.raises(ValueError, match="Event field mismatch"):
        refs["verify_event"](wrong, expected)
    save(path, dict(actions=[expected, foreign]))
    assert refs["action_reference"](path, expected).endswith("#/actions/0")


@pytest.mark.parametrize(
    "field,value",
    [
        ("cash_per_share", "0.32"),
        ("record_date", "2022-04-06"),
        ("ex_price_cash_deduction", "1"),
        ("payment_date", None),
    ],
)
def test_matching_identity_but_wrong_cash_or_date_is_rejected(tmp_path, refs, field, value):
    expected = event()
    path = tmp_path / "raw.json"
    save(path, dict(actions=[dict(expected, **{field: value})]))
    with pytest.raises(ValueError, match="Event field mismatch: " + field):
        refs["action_reference"](path, expected)


@pytest.mark.parametrize("actions", [[], [event(), event()]])
def test_missing_and_duplicate_identity_are_not_arbitrarily_selected(tmp_path, refs, actions):
    path = tmp_path / "raw.json"
    save(path, dict(actions=actions))
    with pytest.raises(ValueError, match="Expected one raw action"):
        refs["action_reference"](path, event())


def test_gap_path_must_exist_and_details_must_match_declared_scope(tmp_path, refs):
    issues = [dict(security="000651.XSHE", date="2024-01-02", field="is_st")]
    contract = dict(
        unresolved=dict(
            details="reports/exact-gap-scope.json",
            field_count=1,
            security_date_cells=1,
            securities=["000651.XSHE"],
            fields_each=["is_st"],
        )
    )
    with pytest.raises(ValueError, match="Missing reference file"):
        refs["verify_gap_contract"](contract, tmp_path)
    save(
        tmp_path / contract["unresolved"]["details"],
        dict(cells=1, fields_count=1, exact_issues=issues),
    )
    assert refs["verify_gap_contract"](contract, tmp_path)["fields_count"] == 1
    bad = deepcopy(contract)
    bad["unresolved"]["details"] = "reports/unresolved-fields.json"
    with pytest.raises(ValueError, match="Missing reference file"):
        refs["verify_gap_contract"](bad, tmp_path)
    bad = deepcopy(contract)
    bad["unresolved"]["field_count"] = 2
    with pytest.raises(ValueError, match="Gap field count mismatch"):
        refs["verify_gap_contract"](bad, tmp_path)
    save(
        tmp_path / contract["unresolved"]["details"],
        dict(cells=1, fields_count=1, exact_issues=issues * 2),
    )
    with pytest.raises(ValueError, match="Gap issue detail count mismatch"):
        refs["verify_gap_contract"](contract, tmp_path)


def test_json_pointer_escapes_and_missing_target(tmp_path, refs):
    path = tmp_path / "pointer.json"
    save(path, {"a/b": {"~key": [7]}})
    assert refs["resolve_reference"]("pointer.json#/a~1b/~0key/0", tmp_path)[1] == 7
    for fragment in ("/a~1b/~0key/-", "/a~1b/~0key/01", "/a~1b/~0key/1", "/a~2b", "/missing"):
        with pytest.raises(ValueError):
            refs["resolve_reference"]("pointer.json#" + fragment, tmp_path)
