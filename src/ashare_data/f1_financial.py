"""FI01/FI04 ledger arithmetic for explicit research inputs.

Only these two slots are integrated. Outputs remain research candidates; finite
external confirmation is recorded separately in docs/f1-integration.json.
Strict visibility fails closed and never obtains certification from a calculation.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
from pathlib import Path

from .f1_acceptance import AcceptanceError, instant, number


class InputUnavailable(AcceptanceError):
    """Required input, period, or publication evidence is unavailable."""


class VisibilityUnproven(InputUnavailable):
    """A relevant report lacks evidence needed for strict historical visibility."""


RESEARCH_MODE = "reported_date_next_day_incomplete_archive"


FIELDS = {
    "operating_revenue",
    "total_operating_revenue",
    "net_profit",
    "parent_net_profit",
    "net_operate_cash_flow",
}


SHANGHAI = timezone(timedelta(hours=8))


def verify_pack_files(pack: dict, root: Path) -> dict:
    """Check all captured bytes; no network and no trusting a PDF text filename alone."""
    root = root.resolve()
    seen = set()
    for source in pack["sources"]:
        path = (root / source["path"]).resolve()
        if not path.is_relative_to(root) or path in seen:
            raise AcceptanceError("invalid or duplicate evidence path")
        seen.add(path)
        raw = path.read_bytes()
        if len(raw) != source["bytes"] or hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise AcceptanceError(f"source hash/size mismatch: {source['path']}")
    declared = {s["path"]: s["sha256"] for s in pack["sources"]}
    for report in pack["reports"].values():
        for path_key, hash_key in (
            ("path", "sha256"),
            ("text_path", "text_sha256"),
            ("index_path", "index_sha256"),
        ):
            if declared.get(report[path_key]) != report[hash_key]:
                raise AcceptanceError("report evidence is absent from verified sources")
    return {
        "source_files_verified": len(seen),
        "jq_equivalent": False,
        "historical_pit_certified": False,
    }


class FinancialLedger:
    """Append-only report versions, ordered by release and explicit replacement.

    Availability gates eligibility, including out-of-order ingestion. Capture
    times describe evidence collection and never promote a version or gate a
    reconstructed historical selection. Re-capture is not a new publication.
    """

    def __init__(self, pack: dict):
        if pack.get("schema") != "ad.f1-small-pack.v1":
            raise AcceptanceError("unsupported input pack")
        self.reports = pack["reports"]
        self.observations = pack["observations"]
        self.by_id = {}
        for r in self.observations:
            if r["record_id"] in self.by_id:
                raise AcceptanceError("duplicate record ID")
            if r["report_id"] not in self.reports or r["field"] not in FIELDS:
                raise AcceptanceError("unknown source report or raw field")
            if (r["basis"], r["scope"], r["currency"], r["unit"]) != (
                "YTD",
                "consolidated",
                "CNY",
                "CNY",
            ):
                raise AcceptanceError("explicit consolidated YTD CNY inputs required")
            end = date.fromisoformat(r["period_end"])
            if r["period_start"] != f"{end.year}-01-01" or end.strftime("%m-%d") not in {
                "03-31",
                "06-30",
                "09-30",
                "12-31",
            }:
                raise AcceptanceError("invalid calendar-year YTD period")
            report = self.reports[r["report_id"]]
            if date.fromisoformat(report["published_date"]) < end:
                raise AcceptanceError("report publication precedes its statement period")
            self._publication_time(report)
            if not isinstance(r["page"], int) or r["page"] < 1:
                raise AcceptanceError("source page required")
            if "replaces_in_this_pack" not in r:
                raise AcceptanceError("explicit original/replacement relationship required")
            number(r["value"])
            self.by_id[r["record_id"]] = r
        for r in self.observations:
            parent = r.get("replaces_in_this_pack")
            if parent is not None:
                if parent not in self.by_id:
                    raise AcceptanceError("missing predecessor")
                old = self.by_id[parent]
                if any(
                    r[k] != old[k] for k in ("security", "field", "period_end", "scope", "unit")
                ):
                    raise AcceptanceError("replacement identity mismatch")
                new_report = self.reports[r["report_id"]]
                old_report = self.reports[old["report_id"]]
                if new_report["published_date"] == old_report["published_date"] and any(
                    report.get("publication_precision") != "timestamp"
                    for report in (new_report, old_report)
                ):
                    raise AcceptanceError("same-day replacement requires exact publication order")
                if self._publication_time(new_report) <= self._publication_time(old_report):
                    raise AcceptanceError("replacement chronology mismatch")
        self.ancestors = {}
        for r in self.observations:
            ancestors, current = set(), r
            while current["replaces_in_this_pack"] is not None:
                parent = current["replaces_in_this_pack"]
                if parent in ancestors or parent == r["record_id"]:
                    raise AcceptanceError("cyclic replacement chain")
                ancestors.add(parent)
                current = self.by_id[parent]
            self.ancestors[r["record_id"]] = ancestors

    @staticmethod
    def _publication_time(report: dict) -> datetime:
        """Release precedence; a date-only midnight is an ordering key, not visibility."""
        day = date.fromisoformat(report["published_date"])
        if report.get("publication_precision") == "timestamp":
            released = instant(report["published_at"])
            if released.astimezone(SHANGHAI).date() != day:
                raise AcceptanceError("publication timestamp/date mismatch")
            return released
        return datetime.combine(day, time.min, SHANGHAI)

    def _available(self, report: dict, mode: str) -> datetime:
        if mode == "strict":
            if (
                report.get("publication_precision") != "timestamp"
                or report.get("historical_first_publication_proven") is not True
                or report.get("complete_revision_history") is not True
                or report.get("available_at") is None
            ):
                raise VisibilityUnproven(
                    "strict PIT requires first-release clock and complete version evidence"
                )
            return max(instant(report["published_at"]), instant(report["available_at"]))
        if mode != RESEARCH_MODE:
            raise AcceptanceError("choose strict or explicitly named research visibility mode")
        # Conservative date convention, NOT an observed release/availability clock.
        day = date.fromisoformat(report["published_date"]) + timedelta(days=1)
        return datetime.combine(day, time.min, SHANGHAI)

    def select(self, security: str, field: str, period: str, *, as_of: str, mode: str) -> dict:
        cutoff = instant(as_of)
        matches = []
        for r in self.observations:
            if (r["security"], r["field"], r["period_end"]) != (security, field, period):
                continue
            report = self.reports[r["report_id"]]
            # A known future date is excluded before demanding its exact timestamp.
            if date.fromisoformat(report["published_date"]) > cutoff.astimezone(SHANGHAI).date():
                continue
            released = self._publication_time(report)
            if released > cutoff:
                continue
            # An explicitly future intake is ineligible even if its proof is incomplete.
            # Do not let knowledge of a later correction block a valid earlier version.
            if mode == "strict" and report.get("available_at") is not None:
                if instant(report["available_at"]) > cutoff:
                    continue
            available = self._available(report, mode)
            if available <= cutoff:
                matches.append((released, r))
        if not matches:
            raise InputUnavailable(f"missing visible observation: {security} {field} {period}")
        matches.sort(key=lambda item: item[0])
        if len(matches) > 1 and matches[-1][0] == matches[-2][0]:
            raise AcceptanceError("ambiguous same-time versions")
        r = matches[-1][1]
        # Validate precedence among eligible versions only. A future or un-ingested
        # descendant cannot supersede the original at an earlier cutoff. An ancestor
        # may arrive after a correction without disqualifying that correction.
        if any(
            other["record_id"] not in self.ancestors[r["record_id"]] for _, other in matches[:-1]
        ):
            raise AcceptanceError("eligible versions lack one complete replacement chain")
        return {
            **r,
            "visibility_mode": mode,
            "source_sha256": self.reports[r["report_id"]]["sha256"],
            "historical_pit_certified": False,
        }

    def latest_period(self, security: str, field: str, *, as_of: str, mode: str) -> str:
        periods = sorted(
            {
                r["period_end"]
                for r in self.observations
                if (r["security"], r["field"]) == (security, field)
            },
            reverse=True,
        )
        for period in periods:
            try:
                self.select(security, field, period, as_of=as_of, mode=mode)
                return period
            except VisibilityUnproven:
                raise
            except InputUnavailable:
                continue
        raise InputUnavailable("no visible report period")

    def ttm(self, security: str, field: str, period: str, *, as_of: str, mode: str) -> dict:
        end = date.fromisoformat(period)
        current = self.select(security, field, period, as_of=as_of, mode=mode)
        inputs = [current]
        with localcontext() as ctx:
            ctx.prec = 50
            value = number(current["value"])
            if end.month != 12:
                annual = self.select(
                    security, field, f"{end.year - 1}-12-31", as_of=as_of, mode=mode
                )
                prior = self.select(
                    security, field, f"{end.year - 1}{period[4:]}", as_of=as_of, mode=mode
                )
                value += number(annual["value"]) - number(prior["value"])
                inputs += [annual, prior]
        return {
            "value": str(value),
            "unit": "CNY",
            "field": field,
            "period_end": period,
            "formula": "current_ytd + prior_fy - prior_same_ytd; FY uses itself",
            "inputs": [r["record_id"] for r in inputs],
            "visibility_mode": mode,
            "historical_pit_certified": False,
            "jq_equivalent": False,
        }

    def quarter(self, security: str, field: str, period: str, *, as_of: str, mode: str) -> dict:
        """Difference latest visible YTD versions, retaining both signed source terms."""
        end = date.fromisoformat(period)
        quarter_ends(period, 1)
        current = self.select(security, field, period, as_of=as_of, mode=mode)
        observations = [(1, current)]
        if end.month != 3:
            previous = quarter_ends(period, 2)[0]
            observations.append(
                (-1, self.select(security, field, previous, as_of=as_of, mode=mode))
            )
        with localcontext() as ctx:
            ctx.prec = 50
            value = sum((sign * number(r["value"]) for sign, r in observations), Decimal(0))
        return {
            "period_end": period,
            "value": str(value),
            "unit": "CNY",
            "terms": [
                {
                    "coefficient": sign,
                    "record_id": r["record_id"],
                    "report_id": r["report_id"],
                    "value": r["value"],
                    "source_sha256": r["source_sha256"],
                }
                for sign, r in observations
            ],
            "version_policy": "latest_visible_YTD_at_same_cutoff",
            "historical_pit_certified": False,
        }

    def quarter_ttm(self, security: str, field: str, period: str, *, as_of: str, mode: str) -> dict:
        """Require four complete quarters and reconcile to the YTD/FY bridge."""
        quarters = [
            self.quarter(security, field, p, as_of=as_of, mode=mode)
            for p in quarter_ends(period, 4)
        ]
        bridge = self.ttm(security, field, period, as_of=as_of, mode=mode)
        with localcontext() as ctx:
            ctx.prec = 50
            value = sum((number(q["value"]) for q in quarters), Decimal(0))
        if value != number(bridge["value"]):
            raise AcceptanceError("quarter chain differs from YTD/FY bridge")
        return {**bridge, "quarters": quarters, "quarter_sum_equals_bridge": True}


def quarter_ends(period: str, count: int) -> list[str]:
    end = date.fromisoformat(period)
    days = {3: 31, 6: 30, 9: 30, 12: 31}
    if end.month not in days or end.day != days[end.month]:
        raise AcceptanceError("calendar quarter end required")
    if type(count) is not int or not 1 <= count <= 8:
        raise AcceptanceError("one to eight quarters required")
    ordinal = end.year * 4 + end.month // 3 - 1
    result = []
    for q in range(ordinal - count + 1, ordinal + 1):
        year, offset = divmod(q, 4)
        month = (offset + 1) * 3
        result.append(f"{year:04d}-{month:02d}-{days[month]:02d}")
    return result


FINANCIAL_IDS = {
    "FI01": "net_profit_to_total_operate_revenue_ttm",
    "FI04": "net_operate_cashflow_growth_rate",
}


def financial_factor_candidate(
    ledger: FinancialLedger, slot: str, security: str, as_of: str, *, mode: str
) -> dict:
    """Apply documented core formulas without certifying unresolved vendor policies."""
    fields = {
        "FI01": ("net_profit", "total_operating_revenue"),
        "FI04": ("net_operate_cash_flow",),
    }
    if slot not in fields:
        raise AcceptanceError(
            "only FI01/FI04 research candidates are integrated; other slots unsupported"
        )
    periods = {
        ledger.latest_period(security, field, as_of=as_of, mode=mode) for field in fields[slot]
    }
    if len(periods) != 1:
        raise InputUnavailable("financial fields have different latest report periods")
    period = periods.pop()
    components = {
        field: ledger.quarter_ttm(security, field, period, as_of=as_of, mode=mode)
        for field in fields[slot]
    }
    prior = None
    with localcontext() as ctx:
        ctx.prec = 50
        if slot == "FI01":
            denominator = number(components["total_operating_revenue"]["value"])
            if denominator == 0:
                raise InputUnavailable("zero total operating revenue")
            value = number(components["net_profit"]["value"]) / denominator
        else:
            field = fields[slot][0]
            prior_period = quarter_ends(period, 5)[0]
            prior = ledger.quarter_ttm(security, field, prior_period, as_of=as_of, mode=mode)
            value = growth(components[field]["value"], prior["value"], denominator="signed")
    return {
        "slot": slot,
        "factor_id": FINANCIAL_IDS[slot],
        "security": security,
        "as_of": as_of,
        "period_end": period,
        "value": str(value),
        "unit": "ratio",
        "components": components,
        "prior_ttm": prior,
        "core_formula_status": "official_catalog_confirmed",
        "version_policy": "latest_visible_YTD_at_same_cutoff",
        "precision_status": "unquantized_decimal50_candidate",
        "historical_pit_certified": False,
        "jq_equivalent": False,
    }


def growth(current, prior, *, denominator: str) -> Decimal:
    """No inferred vendor policy: callers must name the treatment of a negative base."""
    current, prior = number(current), number(prior)
    if denominator not in {"signed", "absolute", "positive_only"}:
        raise AcceptanceError("explicit growth denominator policy required")
    if prior == 0 or (denominator == "positive_only" and prior < 0):
        raise InputUnavailable("undefined growth under selected base policy")
    with localcontext() as ctx:
        ctx.prec = 50
        return (current - prior) / (abs(prior) if denominator == "absolute" else prior)
