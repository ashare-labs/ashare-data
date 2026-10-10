"""Immutable research projections, preserving service number tokens separately."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .d1_types import Projection


@dataclass(frozen=True)
class ZzshareValue(Projection):
    source_field: str
    raw_token: str | None
    value: Decimal | int | None
    state: str


@dataclass(frozen=True)
class SourceClaimedLimits(Projection):
    high: ZzshareValue
    low: ZzshareValue
    classification: str = "source_claimed"
    server_generation: str = "UNKNOWN"
    exchange_certified: bool = False


@dataclass(frozen=True)
class ZzshareDailyRow(Projection):
    security: str
    trade_date: date
    open: ZzshareValue
    high: ZzshareValue
    low: ZzshareValue
    close: ZzshareValue
    prev_close: ZzshareValue
    volume: ZzshareValue
    amount: ZzshareValue
    is_st: ZzshareValue
    is_paused: ZzshareValue
    source_claimed_limits: SourceClaimedLimits
    capture_id: str
    raw_response_sha256: str
    response_json_pointer: str
    request_started_at: str
    response_received_at: str
    raw_json: str
    provider: str = "zzshare"
    quality: str = "service_reported_unverified"
    upstream_provider: str = "UNKNOWN"
    request_mode: int = 0
    price_basis: str = "source_claimed_mode0"
    price_unit: str = "CNY/share"
    volume_unit: str = "shares"
    amount_unit: str = "CNY"
    unit_evidence: str = "provider_documentation; not upstream certification"
    rule_derived_limits: None = None
    historical_available_at: None = None
    historical_pit: bool = False
    events_complete: bool = False
    execution_permission: bool = False

    @property
    def source_fields(self):
        return json.loads(self.raw_json, parse_float=Decimal, parse_int=Decimal)


@dataclass(frozen=True)
class ZzshareDailyResult:
    capture_id: str
    rows: tuple[ZzshareDailyRow, ...]
    report_json: str

    @property
    def report(self):
        return json.loads(self.report_json)

    def to_dict(self):
        return {"rows": [row.to_dict() for row in self.rows], "report": self.report}
