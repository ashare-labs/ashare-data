"""R1: complete field allowlists; economic payloads never contain transport receipts."""

from .m2_types import M2AD08Call, canonical_hash, decimal_text, exact_day, exact_int, keys
from .model import require

COMMON = ("schema", "operation", "security", "current_date", "target_date", "role")
LISTING = (
    "model_listed",
    "model_delisted",
    "absolute_delisting_date",
    "absolute_date_status",
    "evidence_status",
)
BAR = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "price_basis",
    "price_unit",
    "volume_unit",
    "amount_unit",
    "bar_start",
    "bar_end",
)
RESULT_FIELDS = {
    "decision_prev_close": ("value", "unit", "value_basis", "scoped_ratio"),
    "execution_bar": BAR,
    "valuation_bar": BAR,
    "listing": LISTING,
    "successor": LISTING,
    "trading_state": (
        "suspended",
        "is_st",
        "state_evidence_status",
        "lower_limit",
        "upper_limit",
        "source_preclose",
        "price_tick",
        "buy_round_lot",
        "resale_rule",
        "formula_version",
        "rule_policy_sha256",
        "listing",
    ),
    "modeled_actions": (
        "modeled_events",
        "known_event_ids",
        "coverage_evidence_sha256",
        "verified_absent",
        "source_completeness",
        "status",
        "price_basis_domain",
        "registration_domain",
    ),
}
QUERY_FIELDS = (*COMMON, "call", "n")


def query_payload(value):
    keys(value, QUERY_FIELDS)
    out = dict(value)
    op = out["operation"]
    require(
        op in RESULT_FIELDS and out["schema"] == "m2.query.v1" and out["security"] == "600000.XSHG",
        "M2_SCHEMA",
        "查询schema/operation/security不符",
    )
    for k in ("current_date", "target_date"):
        out[k] = exact_day(out[k]).isoformat()
    require(
        out["role"] in ("previous", "current", "warmup", "settlement_successor"),
        "M2_ROLE_SCOPE",
        "未知角色",
    )
    if op == "decision_prev_close":
        out["call"] = M2AD08Call.from_dict(out["call"]).to_dict()
    else:
        require(out["call"] is None, "M2_SCHEMA", "该operation不接受call")
    if op == "successor":
        exact_int(out["n"], minimum=1, maximum=1)
    else:
        require(out["n"] is None, "M2_SCHEMA", "该operation不接受n")
    return out


def economic_payload(value):
    require(
        type(value) is dict and value.get("operation") in RESULT_FIELDS, "M2_SCHEMA", "未知经济结果"
    )
    op = value["operation"]
    keys(value, (*COMMON, *RESULT_FIELDS[op]))
    out = dict(value)
    require(
        out["schema"] == "m2.result.v1" and out["security"] == "600000.XSHG",
        "M2_SCHEMA",
        "结果schema不符",
    )
    for k in ("current_date", "target_date"):
        out[k] = exact_day(out[k]).isoformat()
    if op == "decision_prev_close":
        for k in ("value", "scoped_ratio"):
            out[k] = decimal_text(out[k])
    if op in ("execution_bar", "valuation_bar"):
        for k in ("open", "high", "low", "close"):
            out[k] = decimal_text(out[k])
        out["amount"] = decimal_text(out["amount"]) if out["amount"] is not None else None
        exact_int(out["volume"])
    if op == "trading_state":
        for k in ("lower_limit", "upper_limit", "source_preclose", "price_tick"):
            out[k] = decimal_text(out[k])
        exact_int(out["buy_round_lot"], minimum=100, maximum=100)
        out["listing"] = economic_payload(out["listing"])
        require(
            out["listing"]["operation"] == "listing", "M2_SCHEMA", "嵌套listing只能是纯listing结果"
        )
    for key in ("suspended", "is_st", "model_listed", "model_delisted", "verified_absent"):
        if key in out:
            require(type(out[key]) is bool, "M2_SCHEMA", "标志须为bool")
    # Remaining nested result shapes are also exact: no recursive receipt escape hatch.
    if op == "modeled_actions":
        require(
            out["modeled_events"] == [] and type(out["modeled_events"]) is list,
            "M2_RELATED_EVENT",
            "本profile只支持模型空事件",
        )
        require(
            type(out["known_event_ids"]) is list
            and all(type(x) is str for x in out["known_event_ids"]),
            "M2_SCHEMA",
            "事件引用须为ID列表",
        )
        for k in ("price_basis_domain", "registration_domain"):
            keys(out[k], ("start", "end"))
            out[k] = {n: exact_day(v).isoformat() for n, v in out[k].items()}
    # Enforce scalar types for every non-container field; reject nested arbitrary objects.
    for k, v in out.items():
        if k not in (
            "listing",
            "modeled_events",
            "known_event_ids",
            "price_basis_domain",
            "registration_domain",
        ):
            require(v is None or type(v) in (str, bool, int), "M2_SCHEMA", "经济字段类型不符")
    return out


def query_contract():
    return {
        "version": "m2.daily.owner.v1",
        "query_schema": "m2.query.v1",
        "result_schema": "m2.result.v1",
        "query_fields": list(QUERY_FIELDS),
        "result_fields": {op: list((*COMMON, *names)) for op, names in RESULT_FIELDS.items()},
        "encoder": "m2.canonical.v1; UTF8 sorted compact; UTC-six-microseconds-Z; decimal-no-exponent-no-trailing-zero; exact-int",
        "result_excludes": [
            "sources",
            "receipt",
            "evidence",
            "authorization",
            "context",
            "self_hash",
        ],
        "hash_order": ["query/result/sources", "evidence", "context", "authorization", "transport"],
        "window_plan_hash": "all plan body fields except window_plan_sha256",
        "execution_permission": False,
    }


QUERY_CONTRACT_SHA256 = canonical_hash(query_contract())
