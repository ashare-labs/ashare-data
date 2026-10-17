"""Offline, hash-pinned daily inputs with an explicit retrospective clock model.

The snapshot is loaded and verified once. Public results are detached copies;
source receipt times never become historical publication times. This module
contains neither a network client nor an execution engine.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from ashare_data.core.model import DataError, day, require, symbol, timestamp, validate_ohlc
from ashare_data.storage.immutable import identifier

SCHEMA = "ad.daily-research-binding.v1"
ROLE = "daily_strategy_retrospective_research"
TZ = ZoneInfo("Asia/Shanghai")
PRICE_FIELDS = ("open", "high", "low", "close")
BAR_FIELDS = PRICE_FIELDS + ("volume", "money")


def _number(value, *, positive=False):
    try:
        require(
            isinstance(value, (str, int)) and not isinstance(value, bool),
            "DAILY_RESEARCH_VALUE",
            "Values must be decimal strings or integers",
        )
        value = Decimal(value)
        require(
            value.is_finite() and (value > 0 if positive else value >= 0),
            "DAILY_RESEARCH_VALUE",
            "Invalid market value",
        )
        return value
    except (InvalidOperation, ValueError) as exc:
        raise DataError("DAILY_RESEARCH_VALUE", "Invalid decimal") from exc


def _json(raw):
    try:
        return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as exc:
        raise DataError("DAILY_RESEARCH_SCHEMA", "Invalid JSON") from exc


class DailyResearchView:
    """A portable owner export. All times below are declared research clocks.

    ``read_window`` is an engine-side dataset export, not a strategy query.
    Strategies use ``history``; current-day OHLCV is unavailable before close.
    ``opening`` and ``closing`` expose only the matching/valuation price at
    their own model times. Modeled state/limits require an explicit opt-in.
    """

    def __init__(self, binding_path, *, expected_binding_sha256):
        try:
            self._load(binding_path, expected_binding_sha256)
        except (OSError, KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            raise DataError(
                "DAILY_RESEARCH_SCHEMA", "Malformed or unreadable daily research package"
            ) from exc

    def _load(self, binding_path, expected_binding_sha256):
        self.path = Path(binding_path).resolve()
        self.binding_sha256 = identifier(expected_binding_sha256)
        raw = self.path.read_bytes()
        require(
            len(raw) <= 2 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == self.binding_sha256,
            "DAILY_RESEARCH_INTEGRITY",
            "Binding digest mismatch",
        )
        b = _json(raw)
        require(
            isinstance(b, dict)
            and b.get("schema") == SCHEMA
            and b.get("role") == ROLE
            and b.get("historical_pit") is False
            and b.get("execution_permission") is False,
            "DAILY_RESEARCH_SCHEMA",
            "Unsupported binding or admission claim",
        )
        files = b.get("files")
        require(
            isinstance(files, list) and 1 <= len(files) <= 1000,
            "DAILY_RESEARCH_SCHEMA",
            "Invalid file inventory",
        )
        contents = {}
        total_bytes = 0
        for ref in files:
            relative = Path(ref["path"])
            p = self.path.parent / relative
            require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and not p.is_symlink()
                and p.resolve().is_relative_to(self.path.parent)
                and relative.as_posix() not in contents
                and p.is_file(),
                "DAILY_RESEARCH_FILE",
                "Missing, duplicate or unsafe evidence path",
            )
            require(
                p.stat().st_size <= 16 * 1024 * 1024, "DAILY_RESEARCH_FILE", "Oversized evidence"
            )
            content = p.read_bytes()
            total_bytes += len(content)
            require(
                total_bytes <= 128 * 1024 * 1024,
                "DAILY_RESEARCH_FILE",
                "Package evidence exceeds 128 MiB",
            )
            require(
                len(content) == ref["bytes"]
                and hashlib.sha256(content).hexdigest() == identifier(ref["sha256"]),
                "DAILY_RESEARCH_INTEGRITY",
                "Evidence digest mismatch",
                {"path": ref["path"]},
            )
            contents[relative.as_posix()] = content
        require(b.get("data") in contents, "DAILY_RESEARCH_SCHEMA", "Unbound data file")
        self._binding = b
        self._file_paths = set(contents)
        self._receipts = {
            p: _json(value) for p, value in contents.items() if p.endswith(".receipt.json")
        }
        self._data = _json(contents[b["data"]])
        self._validate()

    def _validate(self):
        data = self._data
        require(
            data.get("schema") == "ad.daily-research-data.v1"
            and data.get("units") == {"price": "CNY/share", "volume": "share", "money": "CNY"},
            "DAILY_RESEARCH_SCHEMA",
            "Invalid schema or units",
        )
        securities = data.get("securities")
        require(
            isinstance(securities, list)
            and 1 <= len(securities) <= 100
            and len(set(securities)) == len(securities)
            and all(symbol(s) == s for s in securities),
            "DAILY_RESEARCH_SCHEMA",
            "Invalid security scope",
        )
        self._bars = {}
        require(
            isinstance(data["bars"], list) and 1 <= len(data["bars"]) <= 100000,
            "DAILY_RESEARCH_SCHEMA",
            "Invalid daily row count",
        )
        require(
            isinstance(data["windows"], list) and 1 <= len(data["windows"]) <= 20,
            "DAILY_RESEARCH_SCHEMA",
            "Invalid window count",
        )
        for row in data["bars"]:
            key = (row["security"], day(row["date"]))
            require(
                key[0] in securities and key not in self._bars,
                "DAILY_RESEARCH_SCHEMA",
                "Duplicate or foreign bar",
            )
            values = [_number(row[k], positive=True) for k in PRICE_FIELDS]
            validate_ohlc(*values, code="DAILY_RESEARCH_VALUE")
            volume = _number(row["volume"])
            money = _number(row["money"])
            require(
                volume == volume.to_integral_value(),
                "DAILY_RESEARCH_UNIT",
                "Fractional raw share volume",
            )
            require(
                row.get("origin") in {"source_observed", "documented_halt_carry"},
                "DAILY_RESEARCH_SCHEMA",
                "Unqualified bar origin",
            )
            require(
                row["source_ref"] in self._file_paths
                and row.get("historical_available_at") is None,
                "DAILY_RESEARCH_SCHEMA",
                "Unbound source or unsupported historical clock claim",
            )
            if row["origin"] == "documented_halt_carry":
                require(
                    row.get("traded") is False
                    and row.get("suspended") is True
                    and row.get("evidence")
                    and volume == money == 0
                    and all(p in self._file_paths for p in row["evidence"])
                    and len(set(values)) == 1
                    and row.get("carry_from") < row["date"],
                    "DAILY_RESEARCH_HALT",
                    "Invalid explicitly synthetic halt projection",
                )
            else:
                require(
                    row.get("traded") is True
                    and row.get("suspended") is False
                    and volume > 0
                    and money > 0,
                    "DAILY_RESEARCH_VALUE",
                    "Observed trade has no positive volume/money",
                )
            state = row["research_state"]
            require(
                state["status"] == "modeled"
                and state["suspended"] is row["suspended"]
                and state["historical_pit"] is False
                and state["source_historical_available_at"] is None
                and state["model_available_at"] == row["date"] + "T09:00:00+08:00"
                and state["assumption_ids"],
                "DAILY_RESEARCH_STATE",
                "Unqualified research state",
            )
            for field in ("upper_limit", "lower_limit", "reference_price"):
                if state[field] is not None:
                    _number(state[field], positive=True)
            if state["upper_limit"] is not None and state["lower_limit"] is not None:
                require(
                    _number(state["lower_limit"]) <= _number(state["upper_limit"]),
                    "DAILY_RESEARCH_STATE",
                    "Reversed price limits",
                )
            self._bars[key] = row
        self._windows = {}
        for w in data["windows"]:
            sessions = w["sessions"]
            require(
                sessions == sorted(set(day(d) for d in sessions))
                and 3 <= len(sessions) <= 1000
                and w["id"] not in self._windows,
                "DAILY_RESEARCH_CALENDAR",
                "Invalid window calendar",
            )
            start, end = day(w["start"]), day(w["end"])
            warmup = w["warmup_sessions"]
            require(
                type(warmup) is int
                and 1 <= warmup <= len(sessions) - 2
                and sessions[warmup] == start
                and sessions[-2] == end
                and sessions[-1] == w["next_session"]
                and sessions[0] == w["warmup_start"],
                "DAILY_RESEARCH_WARMUP",
                "At least one prior session and a successor are required; warmup must match the window",
            )
            for security in securities:
                for label in sessions:
                    require(
                        (security, label) in self._bars,
                        "DAILY_RESEARCH_COVERAGE",
                        "Missing security/session",
                        {"security": security, "date": label},
                    )
            self._windows[w["id"]] = w
        require(self._windows, "DAILY_RESEARCH_SCHEMA", "No windows")
        # A halt carry must use the immediately previous calendar session.
        for row in self._bars.values():
            if row["origin"] == "documented_halt_carry":
                neighbours = {
                    w["sessions"][w["sessions"].index(row["date"]) - 1]
                    for w in self._windows.values()
                    if row["date"] in w["sessions"] and w["sessions"].index(row["date"]) > 0
                }
                previous = self._bars.get((row["security"], row["carry_from"]))
                require(
                    neighbours == {row["carry_from"]}
                    and previous is not None
                    and previous["close"] == row["close"],
                    "DAILY_RESEARCH_HALT",
                    "Halt carry is not the adjacent close",
                )
        for security, factors in data["factors"].items():
            dates = [day(f["date"]) for f in factors]
            require(
                security in securities and dates == sorted(set(dates)) and dates,
                "DAILY_RESEARCH_FACTOR",
                "Invalid factor dates",
            )
            for factor in factors:
                _number(factor["factor"], positive=True)
                require(
                    factor["source_ref"] in self._file_paths,
                    "DAILY_RESEARCH_FACTOR",
                    "Unbound factor source",
                )
        require(
            set(data["factors"]) == set(securities),
            "DAILY_RESEARCH_FACTOR",
            "Missing security factors",
        )
        for event in data["actions"]:
            require(
                event["security"] in securities
                and day(event["announcement_date"])
                <= day(event["record_date"])
                < day(event["ex_date"]),
                "DAILY_RESEARCH_ACTION",
                "Invalid corporate action dates",
            )
            _number(event["cash_per_share"])
            require(
                event["source_ref"] in self._file_paths
                and event.get("historical_available_at") is None,
                "DAILY_RESEARCH_ACTION",
                "Unbound action or unsupported publication clock",
            )
            if event.get("payment_date") is not None:
                require(
                    day(event["payment_date"]) >= day(event["ex_date"]),
                    "DAILY_RESEARCH_ACTION",
                    "Payment date precedes ex-date",
                )

    def _context(self, window_id, security=None):
        require(window_id in self._windows, "DAILY_RESEARCH_SCOPE", "Unknown fixed window")
        if security is not None:
            require(
                security in self._data["securities"], "DAILY_RESEARCH_SCOPE", "Unbound security"
            )
        return self._windows[window_id]

    def _report(self, **extra):
        return {
            "binding_sha256": self.binding_sha256,
            "role": ROLE,
            "historical_pit": False,
            "execution_permission": False,
            "network_used": False,
            "clock": "declared retrospective model; not verified historical delivery",
            **extra,
        }

    def descriptor(self):
        return deepcopy(
            {
                **self._report(),
                "producer": self._binding["producer"],
                "securities": self._data["securities"],
                "windows": self._data["windows"],
                "units": self._data["units"],
                "assumptions": self._data["assumptions"],
                "limitations": self._data["limitations"],
            }
        )

    def quality(self):
        return deepcopy(self._data["quality"])

    def lineage(self):
        return deepcopy(
            {
                "files": self._binding["files"],
                "receipts": self._receipts,
                "report": self._report(
                    source_revision_policy="unknown unless explicitly supplied by source"
                ),
            }
        )

    def get_trade_days(self, window_id, *, start_date=None, end_date=None):
        w = self._context(window_id)
        start = day(start_date) if start_date is not None else w["warmup_start"]
        end = day(end_date) if end_date is not None else w["next_session"]
        require(
            w["warmup_start"] <= start <= end <= w["next_session"],
            "DAILY_RESEARCH_SCOPE",
            "Calendar request outside the fixed window",
        )
        return {
            "dates": [d for d in w["sessions"] if start <= d <= end],
            "report": self._report(
                calendar_origin=w.get("calendar_origin"), xshe_calendar=w.get("xshe_calendar")
            ),
        }

    def read_window(self, window_id, *, role):
        """Complete engine export; caller must not expose it as a strategy context."""
        require(role == ROLE, "DAILY_RESEARCH_ROLE", "Explicit retrospective engine role required")
        w = self._context(window_id)
        result = {
            "window": w,
            "securities": self._data["securities"],
            "bars": [r for r in self._data["bars"] if r["date"] in w["sessions"]],
            "actions": [a for a in self._data["actions"] if w["start"] <= a["ex_date"] <= w["end"]],
            "listing": self._data["listing"],
            "assumptions": self._data["assumptions"],
            "action_coverage": self._data["action_coverage"],
            "limitations": self._data["limitations"],
            "report": self._report(strategy_visible=False, purpose="engine_input_export"),
        }
        return deepcopy(result)

    @staticmethod
    def _clock(as_of):
        return timestamp(as_of).astimezone(TZ)

    def history(
        self,
        window_id,
        security,
        *,
        as_of,
        count,
        fields=("close",),
        adjustment=None,
        basis_date=None,
    ):
        """Read at most completed model sessions. No missing-row skipping/fill."""
        w, at = self._context(window_id, security), self._clock(as_of)
        require(
            w["start"] <= at.date().isoformat() <= w["next_session"],
            "DAILY_RESEARCH_SCOPE",
            "Clock outside the fixed output window",
        )
        require(
            type(count) is int
            and 1 <= count <= 1000
            and isinstance(fields, (tuple, list))
            and fields
            and len(set(fields)) == len(fields)
            and set(fields) <= set(BAR_FIELDS),
            "DAILY_RESEARCH_ARGUMENT",
            "Invalid history fields/count",
        )
        require(
            adjustment in (None, "pre", "post"), "DAILY_RESEARCH_ARGUMENT", "Unsupported adjustment"
        )
        visible = [
            d
            for d in w["sessions"]
            if datetime.combine(datetime.fromisoformat(d).date(), time(15), TZ) <= at
        ]
        require(len(visible) >= count, "DAILY_RESEARCH_WARMUP", "Not enough completed sessions")
        labels = visible[-count:]
        if adjustment is not None:
            basis = day(basis_date) if basis_date is not None else labels[-1]
            require(
                labels[-1] <= basis <= visible[-1],
                "DAILY_RESEARCH_VISIBILITY",
                "Future or earlier factor basis",
            )
        else:
            require(
                basis_date is None,
                "DAILY_RESEARCH_ARGUMENT",
                "Basis only applies to adjusted prices",
            )
            basis = labels[-1]
        factors = [f for f in self._data["factors"][security] if f["date"] <= basis]

        def factor_at(label):
            candidates = [f for f in factors if f["date"] <= label]
            require(candidates, "DAILY_RESEARCH_FACTOR", "Missing factor anchor")
            return _number(candidates[-1]["factor"], positive=True)

        rows = []
        for label in labels:
            bar = self._bars[(security, label)]
            with localcontext() as ctx:
                ctx.prec = 34
                ratio = (
                    factor_at(label) / factor_at(basis)
                    if adjustment == "pre"
                    else factor_at(label) / _number(factors[0]["factor"], positive=True)
                    if adjustment == "post"
                    else Decimal(1)
                )
                values = {
                    f: str(_number(bar[f]) * ratio) if f in PRICE_FIELDS and adjustment else bar[f]
                    for f in fields
                }
            rows.append(
                {
                    "date": label,
                    "security": security,
                    **values,
                    "origin": bar["origin"],
                    "suspended": bar["suspended"],
                    "source_ref": bar["source_ref"],
                }
            )
        return deepcopy(
            {
                "rows": rows,
                "report": self._report(
                    as_of=at.isoformat(),
                    adjustment=adjustment,
                    basis_date=basis if adjustment else None,
                    price_only_adjustment=True,
                    source_factor_not_complete_action_chain=True,
                    factor_events_through_basis=factors if adjustment else [],
                    model_available_at_rule="daily data at session 15:00 Asia/Shanghai",
                ),
            }
        )

    def _price_point(self, window_id, security, as_of, field, cutoff):
        w, at = self._context(window_id, security), self._clock(as_of)
        label = at.date().isoformat()
        require(
            w["start"] <= label <= w["end"] and label in w["sessions"],
            "DAILY_RESEARCH_SCOPE",
            "Not an output session",
        )
        require(
            at.time() >= cutoff,
            "DAILY_RESEARCH_VISIBILITY",
            "Price is not visible at the model clock",
        )
        row = self._bars[(security, label)]
        require(row["traded"], "DAILY_RESEARCH_HALTED", "A halt carry is not a matching price")
        return {
            "security": security,
            "date": label,
            field: row[field],
            "source_ref": row["source_ref"],
            "report": self._report(
                as_of=at.isoformat(),
                purpose="opening_model" if field == "open" else "closing_valuation",
            ),
        }

    def opening(self, window_id, security, *, as_of):
        return deepcopy(self._price_point(window_id, security, as_of, "open", time(9, 30)))

    def closing(self, window_id, security, *, as_of):
        return deepcopy(self._price_point(window_id, security, as_of, "close", time(15)))

    def state(self, window_id, security, *, as_of, accept_model=False):
        """Daily reconstruction is separate from factual historical pre-open state."""
        w, at = self._context(window_id, security), self._clock(as_of)
        label = at.date().isoformat()
        require(
            label in w["sessions"] and w["start"] <= label <= w["end"],
            "DAILY_RESEARCH_SCOPE",
            "Not an output session",
        )
        row = self._bars[(security, label)]
        require(
            type(accept_model) is bool, "DAILY_RESEARCH_ARGUMENT", "Model opt-in must be boolean"
        )
        actual = {
            "suspended": None,
            "upper_limit": None,
            "lower_limit": None,
            "status": "unavailable",
            "reason": "Historical pre-open status/limit feed was not obtained",
        }
        if at.time() >= time(15):
            actual.update(
                status="partial",
                suspended=row["suspended"],
                suspension_origin=row["suspension_origin"],
            )
        require(
            not accept_model or at.time() >= time(9),
            "DAILY_RESEARCH_VISIBILITY",
            "Daily state model is available from 09:00",
        )
        model = deepcopy(row["research_state"]) if accept_model else None
        return {
            "security": security,
            "date": label,
            "observed": actual,
            "model": model,
            "report": self._report(as_of=at.isoformat(), model_accepted=accept_model),
        }

    def corporate_actions(self, window_id, security, *, as_of):
        w, at = self._context(window_id, security), self._clock(as_of)
        require(
            w["start"] <= at.date().isoformat() <= w["next_session"],
            "DAILY_RESEARCH_SCOPE",
            "Clock outside the fixed output window",
        )
        # Source has date labels, not timestamps: no same-date announcement visibility.
        rows = [
            a
            for a in self._data["actions"]
            if a["security"] == security
            and w["warmup_start"] <= a["ex_date"] <= w["end"]
            and a["announcement_date"] < at.date().isoformat()
        ]
        return deepcopy(
            {
                "rows": rows,
                "coverage": {
                    "source_ref": self._data["action_coverage"][security].get("source_ref"),
                    "historical_action_chain_complete": False,
                    "scope": "Only events whose source announcement date precedes the model clock are returned; future counts and factor-change dates are withheld",
                },
                "report": self._report(
                    as_of=at.isoformat(),
                    model_available_at_rule="day after source announcement date; exact historical publication clock unknown",
                ),
            }
        )
