"""Source-library discovery is additive; the existing field contracts stay intact."""

import pandas as pd
import pytest

from ashare_data import f1, f2
from ashare_data.f1_batch import FIELDS, SLOTS
from ashare_data.factors import catalog, libraries
from ashare_data.alpha191 import Alpha191, coverage


def test_two_source_libraries_and_original_slot_accounting():
    lib = libraries()
    assert list(lib.index) == ["joinquant", "alpha191"]
    assert lib.loc["joinquant", "label"] == "聚宽主因子库（部分实现）"
    assert lib.loc["alpha191", "label"] == "Alpha191"
    assert lib.loc["joinquant", "entry_count"] == 18
    assert lib.loc["joinquant", "api_field_count"] == 19
    jq = catalog("joinquant")
    assert len(jq) == 18
    assert jq.loc[("joinquant", "PV03"), "api_fields"] == ("ATR6", "ATR14")
    assert set().union(*map(set, jq.api_fields)) == set(FIELDS) | set(f2.FIELDS)
    for field in FIELDS:
        row = jq.loc[("joinquant", SLOTS[field])]
        assert field in row.api_fields
        assert row.definition == f1.BATCH_RESEARCH_DEFINITION
    for field in f2.FIELDS:
        assert jq.loc[("joinquant", field), "definition"] == f2.DEFINITION


def test_combined_catalog_namespace_and_no_invented_taxonomy():
    all_factors = catalog()
    assert len(all_factors) == 18 + 191
    assert all_factors.index.is_unique and all_factors.qualified_name.is_unique
    assert set(all_factors.namespace) == {"joinquant", "alpha191"}
    assert not {"basic", "fundamental", "quality", "growth", "momentum"} & set(
        all_factors.namespace
    )
    alpha = catalog("alpha191")
    assert set(alpha.namespace) == {"alpha191"}
    assert alpha.loc[("alpha191", "alpha030"), "availability"] == "REQUIRES_EXTERNAL_FACTORS"
    assert (alpha.availability == "AMBIGUOUS_FORMULA").sum() == 8
    assert not any("category" in c or "tag" in c for c in alpha.columns)
    assert len(coverage()) == 191
    assert set(coverage().library) == {"alpha191"}
    assert set(coverage().qualified_name) == set(alpha.qualified_name)
    with pytest.raises(ValueError, match="library"):
        catalog("fundamental")


def test_result_identifies_alpha_library_without_changing_factor_columns():
    ix = pd.date_range("2024-01-01", periods=2)
    close = pd.DataFrame({"A": [10.0, 11.0]}, index=ix)
    result = Alpha191({"close": close}, universe=close.notna(), adjustment="raw").compute([14])
    assert result.metadata["library"] == result.metadata["namespace"] == "alpha191"
    assert result.values.columns.tolist() == ["alpha014"]
    assert result.report.loc["alpha014", "qualified_name"] == "alpha191:alpha014"
