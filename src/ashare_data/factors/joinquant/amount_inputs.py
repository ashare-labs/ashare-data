"""Native amount input validation for the scoped TVMA6 research model.

Only the explicit session-grid dependency is integrated. ROC, price candidates
and broader pause/zero-trade factor policies are unsupported.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import re

from ashare_data.financials.validation import AcceptanceError

RESEARCH_ROLE = "retrospective_native_source"


class InputUnavailable(AcceptanceError):
    def __init__(self, reason: str, state: str = "failure"):
        super().__init__(reason)
        self.reason = reason
        self.state = state


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise AcceptanceError("canonical date required")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise AcceptanceError("invalid date") from exc
    return value


def verify_pack(root: Path, expected_sha256: str) -> dict:
    root = Path(root).resolve()
    raw = (root / "inputs.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise AcceptanceError("input manifest SHA256 mismatch")
    pack = json.loads(raw, parse_float=str)
    if pack.get("schema") != "ad.factor-panel-pv-native-inputs.v1":
        raise AcceptanceError("unsupported native input pack")
    if pack.get("input_contains_JQ_values") is not False:
        raise AcceptanceError("reference values are not independent source inputs")
    seen = set()
    for ref in pack["sources"]:
        relative = Path(ref["path"])
        path = root / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or str(relative) in seen
        ):
            raise AcceptanceError("unsafe or duplicate evidence path")
        seen.add(str(relative))
        raw = path.read_bytes()
        if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise AcceptanceError(f"evidence changed: {relative}")
    return pack


class NativeSeries:
    """An explicit session grid with typed source gaps; no inferred pause fill."""

    def __init__(self, item: dict):
        self.item = item
        self.security = item["security"]
        if not re.fullmatch(r"\d{6}\.X(SHG|SHE)", self.security):
            raise AcceptanceError("invalid A-share identity")
        self.sessions = [_day(d) for d in item["sessions"]]
        if not self.sessions or self.sessions != sorted(set(self.sessions)):
            raise AcceptanceError("unique ordered explicit sessions required")
        labels = [_day(r["date"]) for r in item["rows"]]
        if labels != sorted(set(labels)) or not set(labels) <= set(self.sessions):
            raise AcceptanceError("raw rows duplicate, reordered, or outside session grid")
        self.by_day = dict(zip(labels, item["rows"], strict=True))
        if (
            item.get("raw_price_adjustment") != "none"
            or item.get("source_family") != "Sina"
            or item.get("currency") != "CNY"
        ):
            raise AcceptanceError("explicit native unadjusted CNY source required")

    def window(self, output_date: str, count: int) -> list[dict]:
        _day(output_date)
        if output_date not in self.sessions:
            raise InputUnavailable("OUTPUT_SESSION_NOT_IN_CALENDAR")
        pos = self.sessions.index(output_date)
        if pos < count - 1:
            raise InputUnavailable("WARMUP_INSUFFICIENT")
        result = []
        for day in self.sessions[pos - count + 1 : pos + 1]:
            row = self.by_day.get(day)
            if row is None:
                raise InputUnavailable("SOURCE_SESSION_MISSING", "source_missing")
            if row.get("trading_state") not in {"observed_trade", "verified_zero_trade"}:
                raise InputUnavailable("PAUSE_OR_ZERO_TRADE_SEMANTICS_UNVERIFIED")
            result.append(row)
        return result
