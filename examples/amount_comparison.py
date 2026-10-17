"""Compare one synthetic TVMA6 value without network or historical archives."""

import json

from ashare_data.factors.joinquant import (
    RESEARCH_ROLE,
    bounded_prediction,
    compare_reference,
    compare_composite_reference,
    summarize_composite_comparisons,
)


def comparison_example():
    days = ["2024-01-15", "2024-01-16", "2024-01-17", "2024-01-18", "2024-01-19", "2024-01-22"]
    amounts = [700000001, 700000002, 700000003, 700000004, 700000005, 700000007]
    item = {
        "security": "600000.XSHG",
        "sessions": days,
        "source_family": "Sina",
        "currency": "CNY",
        "raw_price_adjustment": "none",
        "amount_unit": "CNY",
        "amount_policy": "native",
        "rows": [
            {"date": day, "amount": str(amount), "trading_state": "observed_trade"}
            for day, amount in zip(days, amounts, strict=True)
        ],
    }
    prediction = bounded_prediction(item, days[-1], role=RESEARCH_ROLE)
    reference = float("700000004.167").hex()
    direct = compare_reference(prediction, reference_float_hex=reference)
    composite = compare_composite_reference(prediction, reference_float_hex=reference)
    summary = summarize_composite_comparisons([composite])
    return {
        "input_kind": "synthetic",
        "security": item["security"],
        "date": days[-1],
        "strict_arithmetic_compatible": direct["original_strict_comparison"][
            "within_original_arithmetic_bound"
        ],
        "direct_source_interval_compatible": direct["input_precision_comparison"][
            "inside_conditional_source_mean_interval_up_to_representation"
        ],
        "composite_model_compatible": composite["composite_model_compatible"],
        "summary_all_compatible": summary["composite_model"]["all_compatible"],
        "summary_sample_count": summary["sample_count"],
        "source_error_bound_certified": summary["source_error_bound_certified"],
        "production_eligible": composite["production_eligible"],
    }


if __name__ == "__main__":
    print(json.dumps(comparison_example(), indent=2))
