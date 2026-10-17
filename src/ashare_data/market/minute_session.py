"""Offline, opt-in TDX session research. Source clocks never become replay clocks."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, localcontext
import copy

from ashare_data.core.model import canonical, day, digest, require, symbol, timestamp
from ashare_data.providers.tdx_bars import TDXBarView

SCHEMA = "ad-tdx-minute-session.v1"
OBSERVATIONS_SCHEMA = "ad-minute-session-observations.v1"
MODEL = "tdx-full-session-retrospective.v1"
LIMITATIONS = [
    "Source labels are assumed interval ends; original interval and historical availability remain unknown.",
    "First 09:31 open is a single-value posthoc 09:30 opening proxy; no other first-bar fields are exposed early.",
    "Session phases are research classifications, not certified upstream auction aggregation rules.",
    "15:00 close valuation is separate from explicitly requested closing-auction research matching.",
    "Closed-day volume is the sum of 240 native source rows, including zeros, without precision repair.",
    "Early daily-volume capacity access is engine-only, explicitly posthoc and non-PIT; strategy access waits until 15:00.",
    "This export does not certify trading states, price limits, corporate actions or strict execution admission.",
]


@dataclass(frozen=True)
class TDXSessionSource:
    security: str
    directory: str
    manifest_sha256: str


def _sessions(date):
    return [
        [f"{date}T{a}:00+08:00", f"{date}T{b}:00+08:00"]
        for a, b in (("09:30", "11:30"), ("13:00", "15:00"))
    ]


def _labels(date):
    return [
        (timestamp(a) + timedelta(minutes=i)).isoformat()
        for a, _ in _sessions(date)
        for i in range(1, 121)
    ]


def _dates(values, name, *, empty=False):
    require(
        type(values) in (list, tuple) and (0 if empty else 1) <= len(values) <= 31,
        "MINUTE_SESSION_SCOPE",
        f"bounded explicit {name} required",
    )
    result = [day(value) for value in values]
    require(
        result == sorted(set(result)), "MINUTE_SESSION_SCOPE", f"{name} must be sorted unique dates"
    )
    return result


def _phase(label):
    clock = timestamp(label).strftime("%H:%M")
    if clock == "09:31":
        return "opening_first_bar"
    if clock == "14:57":
        return "closing_boundary"
    if clock > "14:57":
        return "closing_auction_candidate"
    return "continuous_candidate"


class TDXMinuteSessionView:
    """Owner export and clock-filtered research reads of explicitly pinned captures.

    Every export/read revalidates captures. No acquisition, JQ input, source mixing,
    state admission or replacement of the original strict export is performed.
    """

    def __init__(self, sources, *, execution_days, history_days=(), model):
        require(model == MODEL, "MINUTE_SESSION_MODEL", f"explicit model={MODEL!r} required")
        require(
            type(sources) in (list, tuple)
            and 1 <= len(sources) <= 10
            and all(type(s) is TDXSessionSource for s in sources),
            "MINUTE_SESSION_SCOPE",
            "provide 1..10 pinned TDXSessionSource values",
        )
        self._sources = tuple(sources)
        self._ids = [symbol(s.security) for s in sources]
        require(len(set(self._ids)) == len(self._ids), "MINUTE_SESSION_SCOPE", "duplicate security")
        self._days = _dates(execution_days, "execution_days")
        self._history = _dates(history_days, "history_days", empty=True)
        require(
            not self._history or self._history[-1] < self._days[0],
            "MINUTE_SESSION_SCOPE",
            "history must precede execution; it never emits execution events",
        )
        all_days = self._history + self._days
        require(
            len(all_days) <= 31
            and (
                timestamp(all_days[-1] + "T00:00:00+08:00")
                - timestamp(all_days[0] + "T00:00:00+08:00")
            ).days
            <= 62,
            "MINUTE_SESSION_SCOPE",
            "at most 31 selected dates within 63 calendar days",
        )

    def _minute_rows(self, source, dates):
        with localcontext() as context:
            context.prec = 50
            owner = TDXBarView(source.directory, expected_sha256=source.manifest_sha256)
            # Internal AD projection; the public TDXBarView output is unchanged.
            config, journal, raw_rows = owner._load()
        require(
            config["security"] == source.security and config["frequency"] == "1m",
            "MINUTE_SESSION_SOURCE_SCOPE",
            "capture must match the declared stock and native 1m",
        )
        report = owner._report(config, journal, raw_rows)
        report.pop("capture_directory")  # Portable bytes do not depend on machine-local paths.
        moments = [timestamp(r["time"], minute=True) for r in raw_rows]
        lookup = {at: (index, row) for index, (at, row) in enumerate(zip(moments, raw_rows))}
        wanted = [label for date in dates for label in _labels(date)]
        missing = [label for label in wanted if timestamp(label) not in lookup]
        require(
            not missing,
            "MINUTE_SESSION_MISSING_LABELS",
            "complete source-label grid required",
            {"security": source.security, "missing_labels": missing, "source_report": report},
        )
        pages = [
            (timestamp(p["first"]), timestamp(p["last"]), p)
            for p in journal["pages"]
            if p["accepted"]
        ]
        result = []
        with localcontext() as context:
            context.prec = 50
            for label in wanted:
                at = timestamp(label)
                position, raw = lookup[at]
                matches = [(first, p) for first, last, p in pages if first <= at <= last]
                require(len(matches) == 1, "MINUTE_SESSION_SOURCE_SCOPE", "ambiguous source page")
                first, page = matches[0]
                volume, amount = Decimal(str(raw["volume_wire_value"])), Decimal(str(raw["amount"]))
                require(
                    volume == volume.to_integral_value(),
                    "MINUTE_SESSION_VOLUME",
                    "source volume must be integral shares; no rounding or lot repair",
                )
                evidence = dict(
                    source="tdx_native_0x052d",
                    manifest_sha256=source.manifest_sha256,
                    payload_file=page["payload_file"],
                    payload_sha256=page["payload_sha256"],
                    rows_file=page["rows_file"],
                    rows_sha256=page["rows_sha256"],
                    row_index=position - bisect_left(moments, first),
                    row_sha256=digest(raw),
                )
                phase = _phase(label)
                result.append(
                    dict(
                        security=source.security,
                        date=at.date().isoformat(),
                        source_label=label,
                        **{
                            f: format(Decimal(raw[f + "_price_milli"]) / 1000, "f")
                            for f in ("open", "high", "low", "close")
                        },
                        volume=format(volume, "f"),
                        amount=format(amount, "f"),
                        volume_unit="share",
                        amount_unit="CNY",
                        price_basis="raw",
                        observed_at=page["response_completed_at"],
                        evidence=evidence,
                        bar_start=None,
                        bar_end=None,
                        available_at=None,
                        historical_available_at=None,
                        replay_bar_start=(at - timedelta(minutes=1)).isoformat(),
                        replay_bar_end=label,
                        replay_available_at=label,
                        interval_verified=False,
                        phase=phase,
                        phase_verified=False,
                        phase_basis="explicit_source_label_research_model",
                        opening_auction_inclusion="unknown"
                        if phase == "opening_first_bar"
                        else "not_certified",
                        zero_volume=volume == 0,
                        execution_day=at.date().isoformat() in self._days,
                        purpose="minute_research",
                        historical_pit=False,
                        strict_execution_admitted=False,
                    )
                )
        return result, report

    def export(self):
        """Owner-level data export, not a strategy-visible historical snapshot."""
        dates = self._history + self._days
        rows, reports = [], {}
        for source in self._sources:
            part, report = self._minute_rows(source, dates)
            rows.extend(part)
            reports[source.security] = report
        source_refs = {
            "tdx_manifest_sha256": {s.security: s.manifest_sha256 for s in self._sources}
        }
        observations = dict(
            schema=OBSERVATIONS_SCHEMA,
            source_refs=source_refs,
            limitations=list(LIMITATIONS),
            opening_prices=[],
            closing_marks=[],
            daily=[],
            history_minutes=[r for r in rows if not r["execution_day"]],
            history_sessions={d: _sessions(d) for d in self._history},
        )
        for sid in self._ids:
            for date in dates:
                series = [r for r in rows if r["security"] == sid and r["date"] == date]
                first, last = series[0], series[-1]
                require(
                    first["source_label"] == f"{date}T09:31:00+08:00"
                    and last["source_label"] == f"{date}T15:00:00+08:00"
                    and len(series) == 240,
                    "MINUTE_SESSION_MISSING_LABELS",
                    "opening/closing source labels required",
                )
                if date in self._days:
                    for field, purpose, destination, clock, derivation in (
                        (
                            "open",
                            "opening_price",
                            "opening_prices",
                            "09:30",
                            "first_09_31_open_as_posthoc_opening_proxy",
                        ),
                        (
                            "close",
                            "valuation_only",
                            "closing_marks",
                            "15:00",
                            "exact_15_00_source_close",
                        ),
                    ):
                        raw = first if field == "open" else last
                        observations[destination].append(
                            dict(
                                security=sid,
                                date=date,
                                purpose=purpose,
                                price=raw[field],
                                price_basis="raw",
                                source_label=raw["source_label"],
                                observed_at=raw["observed_at"],
                                evidence=copy.deepcopy(raw["evidence"]),
                                available_at=None,
                                historical_available_at=None,
                                replay_available_at=f"{date}T{clock}:00+08:00",
                                historical_pit=False,
                                matching_allowed=False,
                                derivation={"model": derivation, "source_field": field},
                            )
                        )
                with localcontext() as context:
                    context.prec = 50
                    volume = sum((Decimal(r["volume"]) for r in series), Decimal(0))
                observations["daily"].append(
                    dict(
                        security=sid,
                        date=date,
                        purpose="closed_daily",
                        close=last["close"],
                        volume=format(volume, "f"),
                        volume_unit="share",
                        price_basis="raw",
                        source_label=last["source_label"],
                        observed_at=max((r["observed_at"] for r in series), key=timestamp),
                        available_at=None,
                        historical_available_at=None,
                        replay_available_at=last["source_label"],
                        historical_pit=False,
                        aggregation="sum_of_240_native_1m_source_volumes",
                        evidence=dict(
                            source="tdx_native_0x052d",
                            manifest_sha256=last["evidence"]["manifest_sha256"],
                            rows=[
                                {
                                    "source_label": r["source_label"],
                                    "row_sha256": r["evidence"]["row_sha256"],
                                }
                                for r in series
                            ],
                        ),
                        coverage=dict(
                            expected_labels=_labels(date),
                            observed_rows=240,
                            labels_complete=True,
                            interval_verified=False,
                            exchange_daily_total_verified=False,
                        ),
                    )
                )
        minutes = [r for r in rows if r["execution_day"]]
        closing = [
            dict(
                copy.deepcopy(r),
                purpose="closing_auction_matching",
                matching_model="explicit_jq_research_only",
                exchange_matching_verified=False,
            )
            for r in minutes
            if r["phase"] in {"closing_boundary", "closing_auction_candidate"}
        ]
        return dict(
            schema=SCHEMA,
            model=MODEL,
            producer={"name": "ashare-data", "interface": "TDXMinuteSessionView"},
            securities=list(self._ids),
            days=list(self._days),
            history_days=list(self._history),
            sessions={d: _sessions(d) for d in self._days},
            source_refs=source_refs,
            source_reports=reports,
            limitations=list(LIMITATIONS),
            minute=minutes,
            session_observations=observations,
            closing_auction_inputs=closing,
            coverage=dict(
                execution_rows=len(minutes),
                history_rows=len(rows) - len(minutes),
                source_labels_complete=True,
                interval_verified=False,
            ),
            historical_pit=False,
            strict_execution_admitted=False,
            state_and_actions="not_modified_or_certified_by_this_export",
        )

    def export_bytes(self):
        return canonical(self.export())

    def read(self, security, *, trade_date, purpose, as_of):
        """Role-specific research read; the model clock is required on every call."""
        require(
            security in self._ids, "MINUTE_SESSION_SCOPE", "security outside declared capture set"
        )
        date, at = day(trade_date), timestamp(as_of)
        require(
            date in self._days + self._history and date <= at.date().isoformat(),
            "MINUTE_SESSION_SCOPE",
            "date outside selection or in the future",
        )
        require(
            purpose
            in {
                "opening_price",
                "valuation_only",
                "closed_daily",
                "minute_history",
                "closing_auction_matching",
                "jq_daily_initial_capacity",
            },
            "MINUTE_SESSION_PURPOSE",
            "explicit supported purpose required",
        )
        doc = self.export()
        obs = doc["session_observations"]
        if purpose == "jq_daily_initial_capacity":
            require(
                date in self._days,
                "MINUTE_SESSION_SCOPE",
                "capacity is execution-day engine research only",
            )
            row = next(r for r in obs["daily"] if r["security"] == security and r["date"] == date)
            # Deliberately only the denominator; no close or other future-day fields.
            return dict(
                security=security,
                date=date,
                purpose=purpose,
                volume=row["volume"],
                volume_unit="share",
                source_label=row["source_label"],
                observed_at=row["observed_at"],
                evidence=row["evidence"],
                available_at=None,
                historical_available_at=None,
                replay_available_at=row["replay_available_at"],
                model_access_at=at.isoformat(),
                policy="explicit_jq_daily_initial_posthoc_volume",
                strategy_visible=False,
                historical_pit=False,
                non_pit=True,
            )
        if purpose in {"minute_history", "closing_auction_matching"}:
            pool = (
                doc["minute"] + obs["history_minutes"]
                if purpose == "minute_history"
                else doc["closing_auction_inputs"]
            )
        else:
            pool = obs[
                {
                    "opening_price": "opening_prices",
                    "valuation_only": "closing_marks",
                    "closed_daily": "daily",
                }[purpose]
            ]
        scoped = [r for r in pool if r["security"] == security and r["date"] == date]
        require(scoped, "MINUTE_SESSION_SCOPE", "purpose unavailable for this date")
        visible = [r for r in scoped if timestamp(r["replay_available_at"]) <= at]
        require(
            visible,
            "MINUTE_SESSION_NOT_YET_AVAILABLE",
            "record is not yet visible under the declared research clock",
        )
        return copy.deepcopy(
            visible if purpose in {"minute_history", "closing_auction_matching"} else visible[0]
        )
