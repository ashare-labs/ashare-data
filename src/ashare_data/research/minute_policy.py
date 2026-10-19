"""Source-bound denials, independent of a consumer's research permission.

Only hard facts deny a use. Conditional amount/volume models never do so.
The binding prevents accidentally attaching a review to another observation;
it does not authenticate the provider or certify an unflagged observation.
"""

from decimal import Decimal

from ashare_data.core.model import digest, require

FIELDS = ("open", "high", "low", "close", "volume", "amount")
HARD_ACTIVITY = {
    "nonfinite_volume",
    "negative_volume",
    "noninteger_volume",
    "nonfinite_amount",
    "negative_amount",
    "null_volume",
    "null_amount",
}


def native_denials(observation):
    price = observation["price_status"]
    require(
        price in {"unverified", "invalid", "conflict"}, "MINUTE_USE_POLICY", "Unknown price status"
    )
    reasons = sorted(set(observation.get("diagnostics", [])) & HARD_ACTIVITY)
    price_denied = price in {"invalid", "conflict"}
    if price_denied:
        reasons.append("price_" + price)
    return {
        "execution": "deny" if price_denied or reasons else "not_granted",
        "fresh_valuation": "deny" if price_denied else "not_granted",
        "reasons": reasons,
    }


def row_use_policy(raw, source):
    """Project a stored observation, retaining the snapshot row's exact binding.

    Legacy native observations also enforce their explicit price status. The
    old constant *_admitted=False fields express absence of a grant, not denial.
    """
    obs = raw["source_fields"].get("native_observation")
    require(
        obs is not None or "audit_manifest_sha256" not in source,
        "MINUTE_USE_SOURCE_BINDING",
        "An audited native source must retain each native observation and its denials",
    )
    decision = {"execution": "not_granted", "fresh_valuation": "not_granted", "reasons": []}
    if obs is not None:
        require(
            obs["security"] == raw["symbol"]
            and obs["source_label"] == raw["source_label"]
            and obs["source_file_sha256"] == source.get("source_sha256")
            and type(obs["source_row"]) is int
            and obs["source_row"] >= 0
            and isinstance(obs["source_path"], str)
            and bool(obs["source_path"])
            and all(Decimal(str(obs[k])) == Decimal(str(raw[k])) for k in FIELDS),
            "MINUTE_USE_SOURCE_BINDING",
            "Native restriction belongs to another observation",
        )
        decision = native_denials(obs)
        if "use_decision" in obs:
            require(
                obs["use_decision"] == decision, "MINUTE_USE_POLICY", "Contradictory row decision"
            )
    return {
        "schema": "minute-row-use.v1",
        **decision,
        "binding": {
            "source_id": raw["source_id"],
            "source_contract_sha256": digest(source),
            "row_sha256": digest(raw),
        },
    }
