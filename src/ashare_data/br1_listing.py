"""Versioned, bounded A-NORMAL predicates; never an absolute lifecycle date."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from importlib.resources import files

from . import br1, d1
from .d1_types import Projection, SourceRecord
from .model import DataError, canonical, digest, require

POLICY_SHA256 = "4e66bfe529a8e82178b48ad85c00840c73c4b5ee9df4e74e3227de6bea7747eb"


@dataclass(frozen=True)
class BR1ListingContract(SourceRecord):
    contract_sha256: str
    dataset_id: str
    strict_dataset_id: str
    profile_sha256: str
    policy_sha256: str
    query_dates: tuple[date, ...]
    event_dates: tuple[date, ...]
    allowed_requests: tuple[tuple[str, date, date], ...]
    raw_json: str
    owner_version: str = "0.6.0.dev3"
    execution_permission: bool = False


@dataclass(frozen=True)
class BR1ListingProjection(Projection):
    contract_sha256: str
    dataset_id: str
    profile_sha256: str
    security: str
    current_date: date
    target_date: date
    role: str
    context_sha256: str
    evidence_references: tuple[str, ...]
    model_listed: bool = True
    model_delisted: bool = False
    absolute_delisting_date: None = None
    absolute_date_status: str = "unknown"
    assumption_ids: tuple[str, ...] = ("A-NORMAL",)
    evidence_status: str = "derived_under_declared_assumptions"
    source_evidence_status: str = "bounded_corroborated_review"
    execution_permission: bool = False
    historical_pit: bool = False


def _policy():
    data = files("ashare_data").joinpath("br1-listing-policy.json").read_bytes()
    require(d1._sha(data) == POLICY_SHA256, "BR1_LISTING_POLICY_INTEGRITY", "上市状态策略hash不符")
    return json.loads(data)


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _day(value):
    if type(value) is date:
        return value
    if type(value) is str:
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
    raise DataError("BR1_LISTING_DATE", "只接受精确date或YYYY-MM-DD；不隐式截断datetime/时区")


def contract(view) -> BR1ListingContract:
    """A separate projection identity, not a mutation of the old BR1 manifest."""
    policy = _policy()
    descriptor = view.descriptor()
    body = {
        "kind": "br1_bounded_listing_contract",
        "dataset_id": descriptor.dataset_id,
        "strict_dataset_id": descriptor.strict_dataset_id,
        "profile_sha256": descriptor.profile_sha256,
        "price_dataset_id": descriptor.price_dataset_id,
        "facts_component_id": descriptor.facts_component_id,
        "facts_manifest_sha256": descriptor.facts_manifest_sha256,
        "policy_sha256": POLICY_SHA256,
        "policy": policy,
    }
    return BR1ListingContract(
        digest(body),
        descriptor.dataset_id,
        descriptor.strict_dataset_id,
        descriptor.profile_sha256,
        POLICY_SHA256,
        tuple(_day(x) for x in policy["query_dates"]),
        tuple(_day(x) for x in policy["event_dates"]),
        tuple((r, _day(c), _day(t)) for r, c, t in policy["requests"]),
        canonical(body).decode(),
    )


class BR1ListingView:
    """Stateless owner queries. Caller context hashes do not prove runtime authority."""

    def __init__(self, store, dataset_id, *, contract_sha256):
        require(_hash(contract_sha256), "BR1_LISTING_BINDING", "须固定上市状态契约SHA256")
        self._store, self._id, self._pin = store, dataset_id, contract_sha256
        self._fresh()

    def _fresh(self):
        # Reopen even for a previously bound view: verify nested objects and all
        # BR1 contradiction gates before producing or revalidating cached data.
        view = self._store.br1(self._id, mode=br1.MODE)
        c = contract(view)
        require(c.contract_sha256 == self._pin, "BR1_LISTING_BINDING", "上市状态契约身份已改变")
        instrument = view.instrument()
        require(
            instrument.model_eligible is True
            and instrument.assumption_ids == ("A-NORMAL",)
            and instrument.evidence_status == "assumed_normal_applicability"
            and instrument.fact.historical_eligible is None
            and instrument.fact.initial_listing_date == date(1999, 11, 10),
            "BR1_LISTING_CONFLICT",
            "未知或冲突的新资格结论须另审；不能覆盖固定原事实",
        )
        return view, c

    def descriptor(self) -> BR1ListingContract:
        return self._fresh()[1]

    def status(self, *, security, current_date, target_date, role, context_sha256):
        """Use warmup/current/settlement_successor only for the five fixed tuples."""
        view, c = self._fresh()
        require(
            type(security) is str and security == d1.SECURITY,
            "BR1_LISTING_SECURITY",
            "仅固定证券600000.XSHG",
        )
        require(_hash(context_sha256), "BR1_LISTING_CONTEXT", "须提供消费方当前上下文摘要")
        current, target = _day(current_date), _day(target_date)
        require(
            current in c.query_dates and target in c.query_dates,
            "BR1_LISTING_SCOPE",
            "当前日和目标日均须在四日覆盖内",
        )
        require(
            type(role) is str and role in {"warmup", "current", "settlement_successor"},
            "BR1_LISTING_ROLE",
            "未知查询用途",
        )
        calendar = {x.trade_date: x for x in view.calendar()}
        require(
            all(d in calendar and calendar[d].is_trading_day is True for d in c.query_dates),
            "BR1_LISTING_CALENDAR",
            "四日源交易日状态冲突",
        )
        if role == "settlement_successor":
            following = calendar[current].next_open
            require(
                following is not None and following > current and target == following,
                "BR1_LISTING_SUCCESSOR",
                "后继缺失、同日、倒退或不等于源next_open",
            )
        require(
            (role, current, target) in c.allowed_requests,
            "BR1_LISTING_ROLE",
            "暖启动/事件/结算后继用途不能互换或扩大执行日",
        )
        refs = (
            "manifest:" + c.dataset_id,
            "manifest:" + c.strict_dataset_id,
            "profile:" + c.profile_sha256,
            "policy:" + c.policy_sha256,
            "fact:security_identity",
            "fact:initial_listing_date",
            "fact:historical_board_candidate",
            "fact:daily_status",
            "object:" + calendar[current].object_sha256,
        )
        return BR1ListingProjection(
            c.contract_sha256,
            c.dataset_id,
            c.profile_sha256,
            security,
            current,
            target,
            role,
            context_sha256,
            refs,
        )

    def successor(self, *, security, current_date, candidate_date, context_sha256, n=1):
        """Validate the actual engine candidate; never silently repair tail clamping."""
        require(type(n) is int and n == 1, "BR1_LISTING_SUCCESSOR", "仅审阅n=1的真实后继")
        return self.status(
            security=security,
            current_date=current_date,
            target_date=candidate_date,
            role="settlement_successor",
            context_sha256=context_sha256,
        )

    def revalidate(self, projection, *, security, current_date, target_date, role, context_sha256):
        """Call at final consumption with FRESH caller context, not cached arguments."""
        fresh = self.status(
            security=security,
            current_date=current_date,
            target_date=target_date,
            role=role,
            context_sha256=context_sha256,
        )
        require(
            type(projection) is BR1ListingProjection
            and canonical(projection.to_dict()) == canonical(fresh.to_dict()),
            "BR1_LISTING_CACHE_MISMATCH",
            "缓存身份/请求/上下文/状态不符；恢复须重新查询",
        )
        return fresh

    def require_execution(self):
        self._fresh()
        raise DataError(
            "BR1_LISTING_REVIEW_REQUIRED", "新上市状态契约及后端适配须独审；未授予执行许可"
        )
