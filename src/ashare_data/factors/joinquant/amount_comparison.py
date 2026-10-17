"""TVMA6 source intervals, rounded-output comparisons and comparison summaries.

A serialized rounded output is compared with the image of the latent-mean
interval under the rounding/serialization operations. Its direct distance from
the unrounded interval remains a diagnostic, not an additional output gate.
This adapter does not certify the source-precision or output-rounding hypotheses.
"""

import math
from fractions import Fraction

from ashare_data.financials.validation import AcceptanceError
from ashare_data.factors.joinquant.amount import (
    DEFINITION,
    decimal_text,
    exact_number,
    round_milliyuan_even,
)

COMPARISON_SCHEMA = "ad.factor-panel-tvma6-composite-comparison.v2"
SUMMARY_SCHEMA = "ad.factor-panel-tvma6-composite-summary.v2"
COMPOSITE_CRITERION = "target in binary64(Q_0.001_half_even([native_mean-0.5, native_mean+0.5]))"


def compare_composite_reference(prediction: dict, *, reference_float_hex: str) -> dict:
    """Retain every v1 diagnostic and separately evaluate the composite model.

    The v1 output comparison already checks membership in the serialized fixed
    lattice and its rounded endpoint range. For this fixed, continuous closed
    source interval and monotone rounding map that is the composite image.
    Do not AND the unrounded-target diagnostic into this criterion again.
    """
    original = compare_reference(prediction, reference_float_hex=reference_float_hex)
    raw_interval = original["input_precision_comparison"][
        "inside_conditional_source_mean_interval_up_to_representation"
    ]
    output = original["output_rounding_comparison"]
    return {
        "schema": COMPARISON_SCHEMA,
        "security": original["security"],
        "date": original["date"],
        "original_v1_comparison": original,
        "original_preregistered_bounded_checks_all_satisfied": (
            raw_interval
            and output["fixed_milliyuan_lattice_compatible"]
            and output["conditional_rounded_output_interval_compatible"]
        ),
        "composite_model_compatible": output["conditional_rounded_output_interval_compatible"],
        "composite_criterion": COMPOSITE_CRITERION,
        "unrounded_target_interval_check_is_diagnostic_only": True,
        "source_error_bound_certified": False,
        "rounding_rule_identified": False,
        "jq_equivalent_certified": False,
        "production_eligible": False,
    }


def summarize_composite_comparisons(comparisons: list[dict]) -> dict:
    """Keep historical bounded-check semantics separate from model compatibility."""
    if not comparisons:
        raise AcceptanceError("nonempty composite comparisons required")
    if any(row.get("schema") != COMPARISON_SCHEMA for row in comparisons):
        raise AcceptanceError("versioned composite comparisons required")
    keys = [(row["security"], row["date"]) for row in comparisons]
    if len(set(keys)) != len(keys):
        raise AcceptanceError("duplicate composite comparison target")
    originals = [row["original_v1_comparison"] for row in comparisons]
    counts = {
        "original_strict": sum(
            row["original_strict_comparison"]["within_original_arithmetic_bound"]
            for row in originals
        ),
        "conditional_input_interval": sum(
            row["input_precision_comparison"][
                "inside_conditional_source_mean_interval_up_to_representation"
            ]
            for row in originals
        ),
        "fixed_output_lattice": sum(
            row["output_rounding_comparison"]["fixed_milliyuan_lattice_compatible"]
            for row in originals
        ),
        "conditional_output_interval": sum(
            row["output_rounding_comparison"]["conditional_rounded_output_interval_compatible"]
            for row in originals
        ),
        "frozen_point_binary64": sum(
            row["output_rounding_comparison"]["frozen_candidate_point_matches_binary64"]
            for row in originals
        ),
    }
    compatible_count = sum(row["composite_model_compatible"] for row in comparisons)
    return {
        "schema": SUMMARY_SCHEMA,
        "sample_count": len(comparisons),
        "original_v1_checks": {
            "pass_counts": counts,
            "strict_acceptance_met": counts["original_strict"] == len(comparisons),
            "bounded_consistency_met": all(
                row["original_preregistered_bounded_checks_all_satisfied"] for row in comparisons
            ),
            "bounded_consistency_meaning": "all original preregistered bounded checks satisfied",
            "point_replication_met": counts["frozen_point_binary64"] == len(comparisons),
            "original_observations_and_criteria_modified": False,
        },
        "composite_model": {
            "criterion": COMPOSITE_CRITERION,
            "compatible_count": compatible_count,
            "all_compatible": compatible_count == len(comparisons),
            "unrounded_target_interval_check_is_diagnostic_only": True,
        },
        "source_error_bound_certified": False,
        "rounding_rule_identified": False,
        "JQ_equivalence_or_production_acceptance": False,
    }


def compare_reference(prediction: dict, *, reference_float_hex: str) -> dict:
    """Consume a revealed target separately; never revise the input prediction."""
    if prediction.get("definition") != DEFINITION:
        raise AcceptanceError("locked definition required")
    try:
        observed_float = float.fromhex(reference_float_hex)
    except (TypeError, ValueError) as exc:
        raise AcceptanceError("binary64 reference required") from exc
    if not math.isfinite(observed_float) or observed_float.hex() != reference_float_hex:
        raise AcceptanceError("canonical finite binary64 required")
    observed = Fraction.from_float(observed_float)
    mean = Fraction(*prediction["unrounded_mean_exact_ratio"])
    source_low, source_high = [
        Fraction(*r) for r in prediction["source_precision"]["mean_interval_exact_ratios"]
    ]
    half_ulp = Fraction.from_float(math.ulp(observed_float)) / 2
    bound = 50 * Fraction(1, 2**52) * abs(mean)
    rounded = prediction["output_rounding_candidate"]
    rounded_low, rounded_high = [
        exact_number(s) for s in rounded["conditional_output_interval_CNY"]
    ]
    nearest_lattice_value = round_milliyuan_even(observed)
    lattice_compatible = float(nearest_lattice_value).hex() == reference_float_hex
    # The source interval and candidate output interval are distinct checks.
    economic_compatible = observed + half_ulp >= source_low and observed - half_ulp <= source_high
    # Serialize interval endpoints too: at large supported amounts, several
    # milliyuan grid points can share one binary64 value. Comparing the observed
    # float to unconverted decimal endpoints would incorrectly reject that edge.
    output_compatible = lattice_compatible and float(rounded_low) <= observed_float <= float(
        rounded_high
    )
    return {
        "security": prediction["security"],
        "date": prediction["date"],
        "original_strict_comparison": {
            "target_minus_native_mean_CNY": decimal_text(observed - mean),
            "arithmetic_bound_CNY": decimal_text(bound),
            "within_original_arithmetic_bound": abs(observed - mean) <= bound,
            "old_observations_or_tolerance_modified": False,
        },
        "input_precision_comparison": {
            "target_representation_half_ULP_CNY": decimal_text(half_ulp),
            "inside_conditional_source_mean_interval_up_to_representation": economic_compatible,
            "absolute_relative_error": decimal_text(abs(observed - mean) / abs(mean)),
            "meaning": "limited numerical consistency conditional on source precision assumption",
            "supplier_error_guaranteed": False,
        },
        "output_rounding_comparison": {
            "fixed_milliyuan_lattice_compatible": lattice_compatible,
            "conditional_rounded_output_interval_compatible": output_compatible,
            "frozen_candidate_point_matches_binary64": rounded["point_binary64_hex"]
            == reference_float_hex,
            "target_minus_candidate_point_CNY": decimal_text(
                observed - exact_number(rounded["point_CNY"])
            ),
            "rounding_rule_identified": False,
            "limitation": "integer-yuan inputs cannot identify exact output rounding from interval containment alone",
        },
        "jq_equivalent_certified": False,
        "production_eligible": False,
    }
