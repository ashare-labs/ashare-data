"""General report queries over the existing FI version ledger.

No vendor, factor ranking, or release-time inference lives here. Callers supply
an append-only ``ad.financial-ledger.v1`` archive and explicitly choose visibility.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time
import hashlib
import json
from pathlib import Path

from ashare_data.financials.validation import AcceptanceError, instant, number
from ashare_data.financials.ledger import (
    FIELDS,
    RESEARCH_MODE,
    SHANGHAI,
    FinancialLedger,
    InputUnavailable,
    VisibilityUnproven,
    quarter_ends,
    verify_pack_files,
)


def _cutoff(as_of: str, mode: str) -> str:
    if mode not in {"strict", RESEARCH_MODE}:
        raise AcceptanceError("choose strict or explicitly named research visibility mode")
    if isinstance(as_of, str) and len(as_of) == 10:
        try:
            day = date.fromisoformat(as_of)
        except ValueError as exc:
            raise AcceptanceError("canonical ISO date required") from exc
        if day.isoformat() != as_of:
            raise AcceptanceError("canonical ISO date required")
        if mode == "strict":
            raise AcceptanceError("strict queries require a timezone-aware timestamp")
        return datetime.combine(day, time.min, SHANGHAI).isoformat()
    return instant(as_of).isoformat()


def _names(values, label: str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    try:
        result = tuple(values)
    except TypeError as exc:
        raise AcceptanceError(f"{label} must be nonempty names") from exc
    if not result or any(not isinstance(v, str) or not v for v in result):
        raise AcceptanceError(f"{label} must be nonempty names")
    if len(set(result)) != len(result):
        raise AcceptanceError(f"duplicate {label}")
    return result


class FinancialPIT:
    """Multi-company YTD, quarter and reconciled TTM queries in CNY.

    ``as_of='YYYY-MM-DD'`` means the *start* of that Shanghai calendar day,
    and is accepted only in the explicit date-only research mode. The archive
    is copied on construction. ``from_file`` additionally verifies captured
    source bytes; neither path authenticates sources or certifies completeness.
    """

    def __init__(self, pack: dict):
        self._pack = deepcopy(pack)
        self._ledger = FinancialLedger(self._pack)
        self._files_verified = False
        self._pack_sha256 = None
        for row in self._ledger.observations:
            report = self._ledger.reports[row["report_id"]]
            if report.get("security", row["security"]) != row["security"]:
                raise AcceptanceError("report/observation security mismatch")
            if report.get("period_end", row["period_end"]) < row["period_end"]:
                raise AcceptanceError("observation period exceeds source report period")

    @classmethod
    def from_file(
        cls, path: str | Path, *, evidence_root: str | Path, expected_sha256: str | None = None
    ) -> FinancialPIT:
        """Load a JSON archive and require every declared PDF/text/index hash.

        An optional independently stored pack hash also pins the transcription.
        No network access or fetching is performed by this interface.
        """
        raw = Path(path).read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and sha != expected_sha256:
            raise AcceptanceError("financial pack hash mismatch")
        pack = json.loads(raw)
        result = cls(pack)
        if not pack.get("sources") or not pack.get("reports"):
            raise AcceptanceError("nonempty captured evidence required")
        verify_pack_files(pack, Path(evidence_root))
        result._files_verified = True
        result._pack_sha256 = sha
        return result

    def coverage(self) -> dict:
        """Describe captured cells, without claiming an exhaustive market archive."""
        result = {}
        for row in self._ledger.observations:
            fields = result.setdefault(row["security"], {})
            fields.setdefault(row["field"], set()).add(row["period_end"])
        return {
            "securities": {
                security: {field: sorted(periods) for field, periods in sorted(fields.items())}
                for security, fields in sorted(result.items())
            },
            "report_count": len(self._ledger.reports),
            "observation_count": len(self._ledger.observations),
            "source_files_verified": self._files_verified,
            "pack_sha256": self._pack_sha256,
            "complete_market_history": False,
        }

    def _term(self, row: dict, coefficient: int, mode: str) -> dict:
        report = self._ledger.reports[row["report_id"]]
        return {
            "coefficient": coefficient,
            "record_id": row["record_id"],
            "report_id": row["report_id"],
            "security": row["security"],
            "field": row["field"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "value": str(number(row["value"])),
            "basis": row["basis"],
            "scope": row["scope"],
            "unit": row["unit"],
            "replaces_record_id": row["replaces_in_this_pack"],
            "page": row["page"],
            "column": row.get("column"),
            "report_title": report.get("title"),
            "report_type": report.get("report_type"),
            "version": report.get("version"),
            "published_date": report["published_date"],
            "published_at": report.get("published_at"),
            "publication_precision": report.get("publication_precision"),
            "available_from": self._ledger._available(report, mode).isoformat(),
            "availability_basis": (
                "max_publication_and_recorded_availability"
                if mode == "strict"
                else "next_calendar_day_Shanghai_convention"
            ),
            "captured_at": report.get("captured_at"),
            "source_url": report.get("source_url", report.get("url")),
            "source_path": report.get("path"),
            "source_sha256": report["sha256"],
            "index_path": report.get("index_path"),
            "index_sha256": report.get("index_sha256"),
        }

    def _value(self, security, field, period, basis, cutoff, mode):
        ledger = self._ledger
        selected = ledger.select(security, field, period, as_of=cutoff, mode=mode)
        quarters = None
        if basis == "YTD":
            value = str(number(selected["value"]))
            terms = [self._term(selected, 1, mode)]
            start = selected["period_start"]
        elif basis == "quarter":
            calculated = ledger.quarter(security, field, period, as_of=cutoff, mode=mode)
            value = calculated["value"]
            terms = [
                self._term(ledger.by_id[t["record_id"]], t["coefficient"], mode)
                for t in calculated["terms"]
            ]
            end = date.fromisoformat(period)
            start = date(end.year, end.month - 2, 1).isoformat()
        else:
            calculated = ledger.quarter_ttm(security, field, period, as_of=cutoff, mode=mode)
            value = calculated["value"]
            coefficients = [1] if period.endswith("12-31") else [1, 1, -1]
            terms = [
                self._term(ledger.by_id[record_id], coefficient, mode)
                for record_id, coefficient in zip(calculated["inputs"], coefficients, strict=True)
            ]
            quarters = [
                {
                    "period_end": quarter["period_end"],
                    "value": quarter["value"],
                    "terms": [
                        self._term(ledger.by_id[t["record_id"]], t["coefficient"], mode)
                        for t in quarter["terms"]
                    ],
                }
                for quarter in calculated["quarters"]
            ]
            first = date.fromisoformat(quarter_ends(period, 4)[0])
            start = date(first.year, first.month - 2, 1).isoformat()
        return {
            "status": "ok",
            "security": security,
            "field": field,
            "period_start": start,
            "period_end": period,
            "basis": basis,
            "value": value,
            "unit": "CNY",
            "currency": "CNY",
            "scope": "consolidated",
            "terms": terms,
            "quarters": quarters,
            "quarter_sum_equals_bridge": True if basis == "TTM" else None,
        }

    def query(
        self,
        securities,
        fields,
        *,
        as_of: str,
        mode: str,
        period: str | None = None,
        basis: str = "YTD",
    ) -> dict:
        """Return a rectangular batch with explicit missing/visibility statuses.

        With no period, all requested fields for each security must share the
        latest visible period. Missing fields never cause silent fallback to an
        older common period. TTM requires four complete quarters and reconciles
        their sum with the YTD/FY bridge, using one cutoff for every source term.
        Malformed archives and ambiguous version chains raise AcceptanceError.
        """
        securities, fields = _names(securities, "securities"), _names(fields, "fields")
        if set(fields) - FIELDS:
            raise AcceptanceError(f"unsupported financial fields: {sorted(set(fields) - FIELDS)}")
        if basis not in {"YTD", "quarter", "TTM"}:
            raise AcceptanceError("basis must be YTD, quarter or TTM")
        cutoff = _cutoff(as_of, mode)
        if period is not None:
            try:
                canonical = date.fromisoformat(period).isoformat()
            except (TypeError, ValueError) as exc:
                raise AcceptanceError("canonical ISO period date required") from exc
            if canonical != period:
                raise AcceptanceError("canonical ISO period date required")
            quarter_ends(period, 1)
        rows = []
        for security in securities:
            selected_period, period_error = period, None
            if selected_period is None:
                try:
                    periods = {
                        self._ledger.latest_period(security, field, as_of=cutoff, mode=mode)
                        for field in fields
                    }
                    if len(periods) != 1:
                        raise InputUnavailable("requested fields have different latest periods")
                    selected_period = periods.pop()
                except InputUnavailable as exc:
                    period_error = exc
            for field in fields:
                try:
                    if period_error is not None:
                        raise period_error
                    rows.append(self._value(security, field, selected_period, basis, cutoff, mode))
                except InputUnavailable as exc:
                    rows.append(
                        {
                            "security": security,
                            "field": field,
                            "period_end": selected_period,
                            "basis": basis,
                            "value": None,
                            "status": (
                                "visibility_unproven"
                                if isinstance(exc, VisibilityUnproven)
                                else "missing"
                            ),
                            "reason": str(exc),
                        }
                    )
        return deepcopy(
            {
                "schema": "ad.financial-pit-query.v1",
                "as_of": cutoff,
                "visibility_mode": mode,
                "version_policy": "latest_visible_YTD_at_same_cutoff",
                "rows": rows,
                "source_files_verified": self._files_verified,
                "pack_sha256": self._pack_sha256,
                "historical_pit_certified": False,
                "jq_equivalent": False,
            }
        )
