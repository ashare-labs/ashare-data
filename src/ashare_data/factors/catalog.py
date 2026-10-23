"""Discover named factor fields and their computation contracts by source library."""

from collections import defaultdict

import pandas as pd

from ashare_data.factors.joinquant import panel as joinquant_batch
from ashare_data.factors.joinquant import research_view as factor_research
from ashare_data.factors.alpha191 import VERSION as ALPHA191_VERSION, VERSIONS as ALPHA191_VERSIONS
from ashare_data.factors.alpha191 import coverage as alpha191_coverage
from ashare_data.factors.qlib_alpha158 import coverage as alpha158_coverage

__all__ = ["libraries", "catalog"]
CATALOG_VERSION = "ashare-data.factor-libraries.v1"
JOINQUANT_LABEL = "聚宽主因子库（部分实现）"
_GOAL = "explicit definitions and reproducible calculation; exact JQ equality is not required"


def libraries():
    """List source libraries, feature counts, and calculation entrypoints."""
    return pd.DataFrame(
        [
            {
                "library": "joinquant",
                "namespace": "joinquant",
                "label": JOINQUANT_LABEL,
                "catalog_version": CATALOG_VERSION,
                "entry_count": 19,
                "api_field_count": 19,
                "entrypoints": (
                    "ashare_data.factors.joinquant.compute_panel",
                    "ashare_data.FactorResearchView.query",
                ),
                "goal": _GOAL,
                "note": "19 named fields across explicit price, share and financial research contracts.",
            },
            {
                "library": "alpha191",
                "namespace": "alpha191",
                "label": "Alpha191",
                "catalog_version": CATALOG_VERSION,
                "entry_count": 191,
                "api_field_count": 191,
                "entrypoints": ("ashare_data.factors.alpha191.Alpha191.compute",),
                "goal": _GOAL,
                "definition_versions": ALPHA191_VERSIONS,
                "note": "public.v1 retains 183 definitions and 8 ambiguities; opt-in explicit.v1 defines all 191 with documented interpretations, external dependencies and explicit 143/146 inputs. No JQ category mapping.",
            },
            {
                "library": "qlib_alpha158",
                "namespace": "qlib_alpha158",
                "label": "Qlib Alpha158",
                "catalog_version": CATALOG_VERSION,
                "entry_count": 158,
                "api_field_count": 158,
                "entrypoints": ("ashare_data.factors.qlib_alpha158.Alpha158.compute",),
                "goal": "Pinned official default feature semantics over explicit inputs",
                "note": "158 engineered features, not effective strategy counts; no training.",
            },
        ]
    ).set_index("library")


def _joinquant_catalog():
    slots = defaultdict(list)
    for field in joinquant_batch.FIELDS:
        slots[joinquant_batch.SLOTS[field]].append(field)
    rows = []
    for slot, fields in slots.items():
        rows.append(
            {
                "factor": slot,
                "api_fields": tuple(fields),
                "definition": joinquant_batch.DEFINITION,
                "mode": joinquant_batch.MODE,
                "entrypoint": "ashare_data.factors.joinquant.compute_panel",
                "availability": "IMPLEMENTED_WITH_SCOPE_LIMITS",
                "documentation": "docs/factors.md",
                "historical_comparison": "No external equality certification",
                "note": "Explicit input assumptions; historical PIT and provider equality are not certified.",
            }
        )
    for field in factor_research.FIELDS:
        rows.append(
            {
                "factor": field,
                "api_fields": (field,),
                "definition": factor_research.DEFINITION,
                "mode": factor_research.MODE,
                "entrypoint": "ashare_data.FactorResearchView.query",
                "availability": "IMPLEMENTED_WITH_SCOPE_LIMITS",
                "documentation": "docs/factors.md",
                "historical_comparison": "No external equality certification",
                "note": "Named definitions with explicit missing-data and financial visibility handling.",
            }
        )
    for row in rows:
        row.update(
            library="joinquant",
            namespace="joinquant",
            library_label=JOINQUANT_LABEL,
            qualified_name="joinquant:" + row["factor"],
        )
    return pd.DataFrame(rows)


def catalog(library=None, *, alpha191_version=ALPHA191_VERSION):
    """One discovery entrypoint; select a namespace, or all with None.

    The (library, factor) key is a catalog identity, not a new compute alias.
    ``api_fields`` and ``entrypoint`` tell callers which existing API/field to use.
    JoinQuant entries correspond to named fields. Alpha IDs have no inferred taxonomy.
    """
    if library not in {None, "joinquant", "alpha191", "qlib_alpha158"}:
        raise ValueError("library must be 'joinquant', 'alpha191' or 'qlib_alpha158'")
    frames = []
    if library in {None, "joinquant"}:
        frames.append(_joinquant_catalog())
    if library in {None, "alpha191"}:
        alpha = (
            alpha191_coverage(version=alpha191_version)
            .reset_index()
            .rename(columns={"version": "definition"})
        )
        alpha["api_fields"] = alpha["factor"].map(lambda name: (name,))
        alpha["entrypoint"] = "ashare_data.factors.alpha191.Alpha191.compute"
        alpha["documentation"] = "docs/alpha191.md"
        alpha["historical_comparison"] = (
            "No JQ equality claim; see formula provenance and validation"
        )
        frames.append(alpha)
    if library in {None, "qlib_alpha158"}:
        frames.append(alpha158_coverage().reset_index())
    return pd.concat(frames, ignore_index=True).set_index(["library", "factor"])
