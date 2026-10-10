"""Immutable, explicit projections for the finite D1 owner contract."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal


def _plain(value):
    if is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


class Projection:
    def to_dict(self) -> dict:
        return _plain(self)


class SourceRecord(Projection):
    @property
    def source_fields(self) -> dict:
        return json.loads(self.raw_json)


@dataclass(frozen=True)
class Evidence(SourceRecord):
    path: str
    sha256: str
    url: str | None
    locator: str | None
    retrieved_at: datetime | None
    historical_available_at: datetime | None
    raw_json: str


@dataclass(frozen=True)
class FactRecord(SourceRecord):
    fact_id: str
    value_json: str
    evidence_status: str
    evidence: tuple[Evidence, ...]
    raw_json: str

    @property
    def value(self):
        """Detached JSON value, shared by existing and new fact consumers."""
        return json.loads(self.value_json)


@dataclass(frozen=True)
class D1Descriptor(Projection):
    dataset_id: str
    manifest_sha256: str
    kind: Literal["d1_facts", "d1_composite"]
    facts_component_id: str
    source_manifest_sha256: str
    price_dataset_id: str | None
    owner_version: str
    policy_version: str
    plan_sha256: str
    security: str
    scope_start: date
    scope_end: date
    complete: bool = False
    execution_permission: bool = False
    historical_pit: bool = False
    network_required: bool = False
    purpose: str = "fixed_snapshot_posthoc_research"


@dataclass(frozen=True)
class D1Instrument(Projection):
    security: str
    name: str
    exchange: str
    kind: str
    initial_listing_date: date
    board_candidate: str
    board_evidence_status: str
    historical_eligible: bool | None
    evidence: tuple[Evidence, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class D1Rules(Projection):
    security: str
    buy_round_lot: int
    price_tick: Decimal
    same_day_resale: bool
    trading_cycle: str
    effective_from: date
    scope_start: date
    scope_end: date
    normal_daily_limit_ratio: Decimal
    evidence: tuple[Evidence, ...]
    limitations: tuple[str, ...]
    price_unit: str = "CNY/share"
    quantity_unit: str = "share"
    odd_lot_remaining_balance_sell_once: bool = True
    general_sell_round_lot_required: bool = False


@dataclass(frozen=True)
class D1Bar(SourceRecord):
    security: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal
    object_sha256: str
    raw_record_sha256: str
    raw_json: str
    price_basis: str = "raw_unadjusted"
    price_unit: str = "CNY/share"
    volume_unit: str = "share"
    amount_unit: str = "CNY"
    time_semantics: str = "source_daily_label; interval boundaries unverified"
    historical_available_at: datetime | None = None
    finality: str = "UNVERIFIED"


@dataclass(frozen=True)
class D1Status(SourceRecord):
    security: str
    trade_date: date
    suspended: bool | None
    is_st: bool | None
    object_sha256: str
    raw_record_sha256: str
    raw_json: str
    evidence_status: str = "source_observed"
    intraday_halts_verified: bool = False


@dataclass(frozen=True)
class D1CalendarDay(Projection):
    trade_date: date
    is_trading_day: bool
    previous_open: date | None
    next_open: date | None
    object_sha256: str
    evidence_status: str = "source_observed"


@dataclass(frozen=True)
class D1SourcePrevClose(SourceRecord):
    security: str
    trade_date: date
    value: Decimal
    object_sha256: str
    raw_record_sha256: str
    raw_json: str
    evidence_status: str = "source_observed"
    unit: str = "CNY/share"
    adjustment_equivalence_verified: bool = False


@dataclass(frozen=True)
class D1Event(SourceRecord):
    event_id: str
    event_type: str
    entitled_security: str
    resulting_security: str | None
    issuer_announcement_date: date | None
    exchange_disclosure_date: date | None
    record_date: date | None
    ex_date: date | None
    pay_date: date | None
    effective_date: date | None
    subscription_date: date | None
    bond_listing_date: date | None
    conversion_start: date | None
    cash_per_share_before_tax: Decimal | None
    coupon_before: Decimal | None
    coupon_after: Decimal | None
    d1_relation: str
    evidence_status: str
    evidence: tuple[Evidence, ...]
    raw_json: str
    historical_available_at: datetime | None = None
    cash_unit: str = "CNY/share, before tax; only when cash_per_share_before_tax is present"
    coupon_unit: str = "ratio; only when coupon fields are present"


@dataclass(frozen=True)
class D1EventCoverage(Projection):
    eligibility_start: date
    eligibility_end: date
    price_dependency_start: date
    price_dependency_end: date
    announcement_start: date
    announcement_end: date
    metadata_rows: int
    known_event_count: int
    conclusion: str
    evidence_status: str
    limitations: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    returned_metadata_pages_complete: bool = True
    verified_absent: bool = False
    complete_economic_event_set: bool = False


@dataclass(frozen=True)
class D1LimitCandidate(Projection):
    security: str
    trade_date: date
    reference: Decimal
    lower: Decimal
    upper: Decimal
    ratio: Decimal
    tick: Decimal
    rule_fact_ids: tuple[str, ...]
    object_sha256: str
    raw_record_sha256: str
    conditions: tuple[str, ...]
    status: str = "conditional_candidate"
    rounding: str = "ROUND_HALF_UP"
    calculation_version: str = "d1-normal-limits-1"
    unit: str = "CNY/share"
    engine_value: None = None
    eligible_for_engine: bool = False


@dataclass(frozen=True)
class D1PrevCloseCall(Projection):
    security: str
    trade_date: date
    history_dt: date
    adjust_orig: date
    frequency: str
    field: str
    bar_count: int
    include_now: bool
    skip_suspended: bool
    adjustment_requested: str


@dataclass(frozen=True)
class D1PrevCloseCandidate(Projection):
    call: D1PrevCloseCall
    candidate_value: Decimal
    source_preclose: Decimal
    previous_raw_close: Decimal
    observed_equality: bool
    object_sha256: str
    source_record_sha256: str
    history_record_sha256: str
    conditions: tuple[str, ...]
    status: str = "conditional_candidate"
    engine_value: None = None
    eligible_for_engine: bool = False
    absolute_adjustment_factor: None = None
    unit: str = "CNY/share"


@dataclass(frozen=True)
class D1FactClaim(Projection):
    key: str
    conclusion: Literal[
        "supported", "no_related_events", "events_present", "unknown", "unsupported"
    ]
    evidence_status: str
    evidence_references: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class D1Requirement(Projection):
    classification: Literal["required_fact", "declared_model_assumption", "not_used"]
    claim: D1FactClaim


@dataclass(frozen=True)
class D1Admission(Projection):
    dataset_id: str
    plan_sha256: str
    requirements: tuple[D1Requirement, ...]
    blockers: tuple[str, ...]
    status: str = "BLOCKED"
    execution_permission: bool = False


@dataclass(frozen=True)
class D1OwnerReceipt(Projection):
    owner: str
    owner_version: str
    request_id: str
    dataset_id: str
    manifest_sha256: str
    security: str
    scope_start: date
    scope_end: date
    facts: tuple[D1FactClaim, ...]


@dataclass(frozen=True)
class D1Coverage(Projection):
    security: str
    scope_start: date
    scope_end: date
    trade_days: tuple[date, ...]
    source_grid_complete: bool
    gaps: tuple[str, ...]
    complete: bool = False
    note: str = "source grid only; execution facts and finality incomplete"


@dataclass(frozen=True)
class D1Quality(Projection):
    price_classification: str = "source_claim_unverified"
    status_classification: str = "source_observed"
    official_facts: str = "bounded_document_review"
    receipt_time: str = "unknown_legacy_backfill"
    finality: str = "UNVERIFIED"
    historical_pit: bool = False
    complete: bool = False
    execution_permission: bool = False
    network_used: bool = False


@dataclass(frozen=True)
class D1Lineage(Projection):
    descriptor: D1Descriptor
    price_object_hashes: tuple[str, ...]
    official_file_hashes: tuple[tuple[str, str], ...]
    fact_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    manifest_json: str
