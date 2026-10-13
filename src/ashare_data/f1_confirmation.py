"""Replay a preregistered numeric experiment without inventing reviewer attestation.

The older compare_confirmation gate continues to require an independent review
before exposure. This observation path implements the parent's later authorization
to acquire official inputs, freeze predictions and then read at most four values.
It cannot issue that older certificate or imply that independent review occurred.
"""

import math
from pathlib import Path
import struct

from .f1_acceptance import instant
from .f1_financial import FinancialLedger, verify_pack_files
from .f1_followup import RULE, TOLERANCE, confirmation_prediction, digest, regime
from .f1_precision import require


def replay_inputs(pack, source_root: Path, lock, sample_plan, exposure):
    """Bind raw artifacts, original windows and complete chains to the sealed lock."""
    verification = verify_pack_files(pack, source_root)
    payload = lock["payload"]
    require(lock["sha256"] == digest(payload), "changed confirmation lock")
    require(payload["input_digest"] == digest(pack), "changed input pack")
    require(payload["sample_plan_digest"] == digest(sample_plan), "changed sample plan")
    require(payload["exposure_digest"] == digest(exposure), "changed exposure ledger")
    rows = [
        confirmation_prediction(
            FinancialLedger(pack),
            {k: row[k] for k in ("security", "date", "slot")},
            exposure["regimes"],
        )
        for row in payload["rows"]
    ]
    require(rows == payload["rows"], "quarter chain or annual bridge changed")
    return {**verification, "predictions_reproduced": len(rows)}


def observe_frozen_confirmation(lock, records, authorization):
    """Exact native float64 comparison; numerical success is not independent review."""
    payload = lock["payload"]
    require(lock["sha256"] == digest(payload), "changed confirmation lock")
    require(
        payload["schema"] == "ad.f1-final-confirmation-lock.v1"
        and payload["selected_rule"] == RULE
        and payload["tolerance"] == TOLERANCE,
        "changed rule or tolerance",
    )
    expected = payload["rows"]
    require(
        2 <= len(expected) <= 4
        and {r["slot"] for r in expected} == {"FI01", "FI04"}
        and len({regime(r) for r in expected}) == len(expected),
        "incomplete or repeated confirmation regime",
    )
    require(
        authorization["lock_sha256"] == lock["sha256"]
        and authorization["reference_read_authorized"] is True
        and authorization["max_values"] == len(expected)
        and authorization["independent_review_completed"] is False,
        "explicit bounded observation authorization required; no review may be invented",
    )
    freeze_time = instant(payload["frozen_at"])
    authorization_time = instant(authorization["recorded_at"])
    require(freeze_time <= authorization_time, "authorization record must bind completed lock")
    require(len(records) == len(expected), "missing or additional observation")
    rows = []
    for prediction, actual in zip(expected, records, strict=True):
        require(
            (prediction["security"], prediction["date"], prediction["factor_id"])
            == (actual["security"], actual["date"], actual["factor_id"]),
            "changed reference key or order",
        )
        require(
            authorization_time < instant(actual["first_seen_at"]) <= instant(actual["observed_at"]),
            "label exposure must follow sealed predictions and authorization record",
        )
        require(actual["provider"] == "JoinQuant", "unexpected provider")
        require(actual["dtype"] == "float64", "native float64 required")
        require(actual["missing_kind"] in {"none", "NaN", "absent"}, "unknown missing mask")
        match = False
        if actual["missing_kind"] == "none":
            value = float(actual["value_17g"])
            require(
                math.isfinite(value)
                and format(value, ".17g") == actual["value_17g"]
                and struct.pack(">d", value).hex() == actual["float64_bits_be"],
                "inconsistent raw float64 representation",
            )
            match = actual["float64_bits_be"] == prediction["prediction"]["float64_bits_be"]
        else:
            require(
                actual["value_17g"] is None and actual["float64_bits_be"] is None,
                "inconsistent missing representation",
            )
        rows.append({**actual, "expected": prediction["prediction"], "zero_ULP_pass": match})
    passed = all(row["zero_ULP_pass"] for row in rows)
    return {
        "schema": "ad.f1-confirmation-observation.v1",
        "lock_sha256": lock["sha256"],
        "selected_rule": RULE,
        "tolerance": TOLERANCE,
        "status": "fixed_sample_numeric_pass_independent_review_pending"
        if passed
        else "fixed_sample_numeric_failure",
        "rows": rows,
        "cells": len(rows),
        "passed_cells": sum(r["zero_ULP_pass"] for r in rows),
        "finite_numeric_experiment_complete": True,
        "another_holdout_required_for_this_exact_numeric_scope": False,
        "independent_review_completed": False,
        "legacy_pre_read_review_certificate_issued": False,
        "global_unexposed_proven": False,
        "strict_PIT_certified": False,
        "all_twelve_slots_passed": False,
        "on_failure": payload["on_failure"],
        "exclusions": payload["exclusions"],
    }
