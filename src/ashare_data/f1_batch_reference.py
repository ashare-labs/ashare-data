"""Audit one frozen reference batch and expose missing-candidate selection effects.

This module does not calculate factors, modify tolerances, or repair missing inputs.
The complete returned-cell count includes NaN and is distinct from paired coverage.
"""

import math
import struct


def validate_captures(captures, ledger, scope):
    dates = scope["reference_dates"]
    securities = [item["security"] for item in scope["securities"]]
    fields = scope["fields"]
    count = len(securities) * len(fields)
    if [item["date"] for item in captures] != dates:
        raise ValueError("reference date order/completeness mismatch")
    attempts = ledger["attempts"]
    if [item["date"] for item in attempts] != dates:
        raise ValueError("ledger date order/completeness mismatch")
    rows, previous_points = [], ledger["account_precheck"]["points"]
    for capture, attempt in zip(captures, attempts, strict=True):
        if (
            capture["returned_values"] != count
            or attempt["returned_values"] != count
            or attempt["status"] != "captured_and_balance_checked"
        ):
            raise ValueError("unexpected returned count or unfinished attempt")
        if (
            attempt["points_before"] != previous_points
            or attempt["points_after"] != previous_points
        ):
            raise ValueError("points changed or checks are discontinuous")
        expected = {(s, capture["date"], f) for s in securities for f in fields}
        seen = set()
        for row in capture["rows"]:
            key = row["security"], row["date"], row["field"]
            if key not in expected or key in seen or row.get("provider") != "JoinQuant":
                raise ValueError("unexpected or duplicate reference identity")
            seen.add(key)
            if row["value"] is None:
                if row.get("missing_kind") != "NaN" or row.get("float64_bits_be") is not None:
                    raise ValueError("invalid reference missing mask")
            elif (
                row.get("missing_kind") != "none"
                or not math.isfinite(float(row["value"]))
                or struct.pack(">d", float(row["value"])).hex() != row.get("float64_bits_be")
            ):
                raise ValueError("reference float64 transport mismatch")
        if seen != expected or len(capture["rows"]) != count:
            raise ValueError("incomplete reference grid")
        rows.extend(capture["rows"])
    actual = sum(item["returned_values"] for item in captures)
    prior = scope["reference_budget"]["already_spent"]
    authorized = scope["reference_budget"]["authorized_total"]
    if (
        ledger["prior_actual_spend"] != prior
        or ledger["actual_new_returned_values"] != actual
        or ledger["actual_total_spend"] != prior + actual
        or ledger["unspent_values"] != authorized - prior - actual
        or prior + actual > authorized
    ):
        raise ValueError("persistent returned-value budget mismatch")
    return rows


def candidate_selection_impact(local, reference, scope):
    """Compare each side's available pool, in addition to paired-only rank metrics.

    Missing securities are never assigned a value/rank. This supplementary diagnostic
    cannot change the frozen comparison pass/fail or certify the uncomputed position.
    """
    allowed = {item["security"] for item in scope["securities"]}
    output, k = [], scope["rank_rule"]["top_k"]
    for field in scope["fields"]:
        for day in scope["reference_dates"]:
            pools = []
            for rows in (local, reference):
                matching = [
                    row
                    for row in rows
                    if row["field"] == field and row["date"] == day and row["security"] in allowed
                ]
                if len({row["security"] for row in matching}) != len(matching):
                    raise ValueError("duplicate candidate identity")
                pool = {
                    row["security"]: float(row["value"])
                    for row in matching
                    if row["value"] is not None
                }
                if not all(math.isfinite(value) for value in pool.values()):
                    raise ValueError("nonfinite candidate")
                pools.append(pool)
            a, b = pools
            result = {
                "field": field,
                "date": day,
                "requested": len(allowed),
                "local_available": len(a),
                "reference_available": len(b),
                "missing_local_only": sorted(b.keys() - a.keys()),
                "missing_reference_only": sorted(a.keys() - b.keys()),
                "missing_both": sorted(allowed - a.keys() - b.keys()),
                "same_available_pool": a.keys() == b.keys(),
                "supplementary_only": True,
            }
            for side, sign in (("top", -1), ("bottom", 1)):
                chosen = [
                    sorted(sorted(pool, key=lambda s: (sign * pool[s], s))[:k]) for pool in pools
                ]
                enough = min(len(a), len(b)) >= k
                result[side] = {
                    "local": chosen[0],
                    "reference": chosen[1],
                    "replacement_count": k - len(set(chosen[0]) & set(chosen[1]))
                    if enough
                    else None,
                    "enough_for_selection": enough,
                }
            output.append(result)
    return output
