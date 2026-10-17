"""Missing selected members must not disappear behind perfect common-pool ranks."""

import pytest
from compare_factor_reference import compare_rows
from compute_factor_research import compare

FIELDS = ["BIAS5", "CCI20", "PSY"]
SECURITIES = [f"{i:06}.XSHE" for i in range(1, 13)]
DATES = ["2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11"]


def fixture(missing):
    scope = {
        "fields": FIELDS,
        "securities": [{"security": s} for s in SECURITIES],
        "queries": [{"date": d, "fields": FIELDS, "securities": SECURITIES} for d in DATES],
        "thresholds": {f: {"atol": 1e-6, "rtol": 0} for f in FIELDS},
        "coverage_min": 0.95,
        "numeric_pass_min": 0.95,
        "minimum_panel_size": 10,
        "rank_rule": {
            "spearman_min": 0.98,
            "pair_concordance_min": 0.97,
            "top_k": 3,
            "top_overlap_min": 1.0,
            "bottom_overlap_min": 1.0,
        },
    }
    local = [
        {"security": s, "date": d, "field": f, "value": str(i)}
        for d in DATES
        for i, s in enumerate(SECURITIES, 1)
        for f in FIELDS
    ]
    other = [
        dict(row, value=None)
        if row["field"] == "PSY" and (row["date"], row["security"]) in missing
        else dict(row)
        for row in local
    ]
    return local, other, scope


@pytest.mark.parametrize(
    "missing,accepted",
    [
        ([], True),
        ([(DATES[0], SECURITIES[-1])], False),
        ([(DATES[0], SECURITIES[0])], False),
        ([(DATES[0], SECURITIES[5])], True),
        ([(d, SECURITIES[5]) for d in DATES[:3]], False),
    ],
    ids=["complete", "missing-top", "missing-bottom", "missing-middle", "low-coverage"],
)
@pytest.mark.parametrize("comparison", ["reference", "source"])
def test_available_pool_selection(missing, accepted, comparison):
    local, other, scope = fixture(set(missing))
    output = (
        compare_rows(local, other, scope)
        if comparison == "reference"
        else compare(local, other, scope, DATES)
    )
    result = output["summary"]["PSY"]
    assert result["requested"] == 48
    assert result["paired"] == 48 - len(missing)
    key = "practical_acceptance" if comparison == "reference" else "source_control_pass"
    assert result[key] is accepted
    if len(missing) == 1 and missing[0][1] in (SECURITIES[0], SECURITIES[-1]):
        panel = next(p for p in output["panels"] if p["field"] == "PSY" and p["date"] == DATES[0])
        assert panel["n"] == 11 and panel["spearman"] == 1
        tail = "bottom" if missing[0][1] == SECURITIES[0] else "top"
        assert panel[tail]["overlap"] == 1
        replacements = (
            panel["available_pools"][tail]["replacement_count"]
            if comparison == "reference"
            else panel["available_pools"][tail + "_replacement_count"]
        )
        assert replacements == 1


def test_reference_rejects_unrecorded_missing_duplicate_and_wrong_bits():
    local, other, scope = fixture(set())
    for bad in (
        other[:-1],
        other + [other[0]],
        [dict(other[0], float64_bits_be="0" * 16)] + other[1:],
    ):
        with pytest.raises(ValueError):
            compare_rows(local, bad, scope)
