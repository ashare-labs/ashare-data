"""Versioned TVMA6 comparison semantics; the frozen v1 formula is unchanged.

A serialized rounded output is compared with the image of the latent-mean
interval under the rounding/serialization operations. Its direct distance from
the unrounded interval remains a diagnostic, not an additional output gate.
This adapter does not certify the source-precision or output-rounding hypotheses.
"""

from .f1_acceptance import AcceptanceError
from .f1_tvma6_bounded import compare_reference

COMPARISON_SCHEMA = "ad.f1-tvma6-composite-comparison.v2"
SUMMARY_SCHEMA = "ad.f1-tvma6-composite-summary.v2"
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
