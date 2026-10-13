"""Discover factor libraries by source namespace without changing computation APIs.

Library naming identifies formula provenance, not JoinQuant equivalence. Existing
F1/F2 input contracts and historical comparison reports remain authoritative for
those definitions. This module neither loads market data nor dispatches calls
between the different input formats.
"""

from collections import defaultdict

import pandas as pd

from . import f1_batch, f2
from .alpha191 import VERSION as ALPHA191_VERSION, VERSIONS as ALPHA191_VERSIONS
from .alpha191 import coverage as alpha191_coverage

__all__ = ["libraries", "catalog"]
CATALOG_VERSION = "ashare-data.factor-libraries.v1"
JOINQUANT_LABEL = "聚宽主因子库（部分实现）"
_GOAL = "explicit definitions and reproducible calculation; exact JQ equality is not required"


def libraries():
    """List the two source libraries, counts, and unchanged calculation entrypoints."""
    return pd.DataFrame(
        [
            {
                "library": "joinquant",
                "namespace": "joinquant",
                "label": JOINQUANT_LABEL,
                "catalog_version": CATALOG_VERSION,
                "entry_count": 18,
                "api_field_count": 19,
                "entrypoints": (
                    "ashare_data.f1.compute_panel",
                    "ashare_data.FactorResearchView.query",
                ),
                "goal": _GOAL,
                "note": "12 original slots plus 6 F2 entries; ATR6/ATR14 share PV03. Partial catalog, with existing input/scope limits and historical comparison reports retained.",
            },
            {
                "library": "alpha191",
                "namespace": "alpha191",
                "label": "Alpha191",
                "catalog_version": CATALOG_VERSION,
                "entry_count": 191,
                "api_field_count": 191,
                "entrypoints": ("ashare_data.alpha191.Alpha191.compute",),
                "goal": _GOAL,
                "definition_versions": ALPHA191_VERSIONS,
                "note": "public.v1 retains 183 definitions and 8 ambiguities; opt-in explicit.v1 defines all 191 with documented interpretations, external dependencies and explicit 143/146 inputs. No JQ category mapping.",
            },
        ]
    ).set_index("library")


def _joinquant_catalog():
    slots = defaultdict(list)
    for field in f1_batch.FIELDS:
        slots[f1_batch.SLOTS[field]].append(field)
    rows = []
    for slot, fields in slots.items():
        rows.append(
            {
                "factor": slot,
                "api_fields": tuple(fields),
                "definition": f1_batch.DEFINITION,
                "mode": f1_batch.MODE,
                "entrypoint": "ashare_data.f1.compute_panel",
                "availability": "IMPLEMENTED_WITH_SCOPE_LIMITS",
                "documentation": "docs/f1-research.md; docs/f1-batch-usability.md",
                "historical_comparison": "docs/f1-batch-reference-validation.md",
                "note": "Existing fixed research definition and input assumptions; comparison failures and PIT limits remain in the linked reports.",
            }
        )
    for field in f2.FIELDS:
        rows.append(
            {
                "factor": field,
                "api_fields": (field,),
                "definition": f2.DEFINITION,
                "mode": f2.MODE,
                "entrypoint": "ashare_data.FactorResearchView.query",
                "availability": "IMPLEMENTED_WITH_SCOPE_LIMITS",
                "documentation": "docs/f2-research.md",
                "historical_comparison": "docs/f2-reference-gate-r4.md",
                "note": "Existing six-factor definition, missing/PIT handling and historical residual reports retained.",
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
    """One discovery entrypoint; select 'joinquant', 'alpha191', or both with None.

    The (library, factor) key is a catalog identity, not a new compute alias.
    ``api_fields`` and ``entrypoint`` tell callers which existing API/field to use.
    Original slots are retained: 18 JoinQuant entries expose 19 fields because
    PV03 contains ATR6 and ATR14. Alpha IDs have no inferred JoinQuant taxonomy.
    """
    if library not in {None, "joinquant", "alpha191"}:
        raise ValueError("library must be 'joinquant' or 'alpha191'")
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
        alpha["entrypoint"] = "ashare_data.alpha191.Alpha191.compute"
        alpha["documentation"] = "docs/alpha191.md"
        alpha["historical_comparison"] = (
            "No JQ equality claim; see formula provenance and validation"
        )
        frames.append(alpha)
    return pd.concat(frames, ignore_index=True).set_index(["library", "factor"])
