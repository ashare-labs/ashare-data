"""Pinned, offline SZSE month-calendar projection for explicit engine research.

Read the source daily flags, including closed days. Neither weekdays nor JQ
query arrays supply sessions; no prices, callbacks or account facts are inputs.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from calendar import monthrange
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path

from ashare_data.core.model import DataError, day, require, symbol, timestamp
from ashare_data.storage.immutable import identifier

ROLE = "engine_calendar_retrospective_research"
SCHEMA = "ad.bounded-calendar-binding.v1"
MAX_BYTES = 2 * 1024 * 1024


def _read(path):
    require(
        path.is_file() and path.stat().st_size <= MAX_BYTES,
        "CALENDAR_BINDING_FILE",
        "Missing or oversized calendar evidence",
    )
    return path.read_bytes()


def _json(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise DataError("CALENDAR_BINDING_SCHEMA", "Invalid calendar JSON") from exc


class BoundedCalendarView:
    """A fixed owner binding; every read rechecks binding and source digests."""

    def __init__(self, binding_path, *, expected_binding_sha256):
        self.path = Path(binding_path).resolve()
        self.binding_sha256 = identifier(expected_binding_sha256)

    def _open(self):
        raw = _read(self.path)
        require(
            hashlib.sha256(raw).hexdigest() == self.binding_sha256,
            "CALENDAR_BINDING_INTEGRITY",
            "Calendar binding hash mismatch",
        )
        binding = _json(raw)
        require(
            isinstance(binding, dict) and binding.get("schema") == SCHEMA,
            "CALENDAR_BINDING_SCHEMA",
            "Unsupported calendar binding",
        )
        require(
            {
                "window_id",
                "security",
                "market",
                "role",
                "start_date",
                "end_date",
                "primary_source",
                "historical_pit",
                "strategy_visible",
            }
            <= set(binding)
            and type(binding["window_id"]) is str
            and binding["window_id"],
            "CALENDAR_BINDING_SCHEMA",
            "Incomplete calendar binding",
        )
        require(
            binding.get("role") == ROLE
            and binding.get("market") == "XSHE"
            and binding.get("historical_pit") is False
            and binding.get("strategy_visible") is False,
            "CALENDAR_BINDING_SCHEMA",
            "Invalid role or exchange scope",
        )
        security = symbol(binding["security"])
        require(security.endswith(".XSHE"), "CALENDAR_BINDING_SCOPE", "Exchange/security mismatch")
        start, end = day(binding["start_date"]), day(binding["end_date"])
        require(
            start <= end and start[:7] == end[:7],
            "CALENDAR_BINDING_SCOPE",
            "One bounded month required",
        )
        source = binding["primary_source"]
        require(
            type(source) is dict
            and {
                "path",
                "sha256",
                "kind",
                "url",
                "requested_month",
                "captured_start",
                "captured_end",
            }
            <= set(source)
            and type(source["url"]) is str,
            "CALENDAR_BINDING_SCHEMA",
            "Incomplete calendar source",
        )
        require(
            source["kind"] in ("public_szse_calendar_capture", "synthetic_fixture")
            and source["requested_month"] == start[:7],
            "CALENDAR_BINDING_SCHEMA",
            "Invalid source month",
        )
        first, last = timestamp(source["captured_start"]), timestamp(source["captured_end"])
        require(first <= last, "CALENDAR_BINDING_CLOCK", "Reversed source capture interval")

        def checked(item):
            require(
                type(item) is dict
                and {"path", "sha256"} <= set(item)
                and type(item["path"]) is str,
                "CALENDAR_BINDING_FILE",
                "Invalid evidence reference",
            )
            relative = Path(item["path"])
            p = self.path.parent / relative
            require(
                not relative.is_absolute()
                and not p.is_symlink()
                and p.resolve().is_relative_to(self.path.parent),
                "CALENDAR_BINDING_FILE",
                "Evidence must remain inside the bound directory",
            )
            content = _read(p)
            require(
                hashlib.sha256(content).hexdigest() == identifier(item["sha256"]),
                "CALENDAR_BINDING_INTEGRITY",
                "Calendar evidence hash mismatch",
            )
            return content

        payload = _json(checked(source))
        require(
            isinstance(payload, dict)
            and set(payload) == {"data", "nowdate"}
            and type(payload["data"]) is list,
            "CALENDAR_BINDING_SCHEMA",
            "Invalid source response",
        )
        month = date.fromisoformat(start[:7] + "-01")
        expected = [
            month.replace(day=n).isoformat()
            for n in range(1, monthrange(month.year, month.month)[1] + 1)
        ]
        flags = {}
        for row in payload["data"]:
            require(
                type(row) is dict
                and set(row) == {"jyrq", "jybz", "zrxh"}
                and row["jybz"] in ("0", "1")
                and type(row["zrxh"]) is int,
                "CALENDAR_BINDING_SCHEMA",
                "Invalid source daily flag",
            )
            label = day(row["jyrq"])
            require(label not in flags, "CALENDAR_BINDING_SCHEMA", "Duplicate source date")
            require(
                row["zrxh"] == (date.fromisoformat(label).weekday() + 1) % 7 + 1,
                "CALENDAR_BINDING_SCHEMA",
                "Source weekday label mismatch",
            )
            flags[label] = row["jybz"] == "1"
        require(
            list(flags) == expected,
            "CALENDAR_BINDING_COVERAGE",
            "Source month is missing or unordered",
        )
        require(
            type(binding.get("evidence", [])) is list and len(binding.get("evidence", [])) <= 20,
            "CALENDAR_BINDING_SCHEMA",
            "Invalid bounded evidence list",
        )
        for item in binding.get("evidence", []):
            checked(item)
        corroboration = binding.get("corroboration")
        if corroboration is not None:
            other = _json(checked(corroboration))
            require(
                type(other) is dict
                and type(other.get("dates")) is list
                and len(other["dates"]) <= 20000
                and all(type(d) is str for d in other["dates"])
                and other["dates"] == sorted(set(other["dates"]))
                and other.get("synthetic_dates_added") == 0,
                "CALENDAR_BINDING_SCHEMA",
                "Invalid corroborating source dates",
            )
            require(
                all(flag == (label in other["dates"]) for label, flag in flags.items()),
                "CALENDAR_BINDING_CONFLICT",
                "Source calendars disagree",
            )
        return binding, flags

    def read_carry(self, window_id, security, *, role):
        """Return daily states and strict neighbours for the declared calendar window."""
        require(
            role == ROLE,
            "CALENDAR_BINDING_ROLE",
            "Explicit engine calendar retrospective role required",
        )
        binding, flags = self._open()
        require(
            window_id == binding["window_id"] and security == binding["security"],
            "CALENDAR_BINDING_SCOPE",
            "Security/window is outside the bound scope",
        )
        start, end = binding["start_date"], binding["end_date"]
        source = binding["primary_source"]
        capture = {"started_at": source["captured_start"], "completed_at": source["captured_end"]}
        refs = [{"path": source["path"], "sha256": source["sha256"], "url": source["url"]}]
        sessions = [d for d, flag in flags.items() if flag]
        rows = []
        for label, flag in flags.items():
            if not start <= label <= end:
                continue
            previous, following = bisect_left(sessions, label) - 1, bisect_right(sessions, label)
            require(
                previous >= 0 and following < len(sessions),
                "CALENDAR_BINDING_BOUNDARY",
                "Missing actual outer session; no endpoint clamping",
            )
            rows.append(
                dict(
                    calendar_date=label,
                    is_open=flag,
                    status="supported",
                    previous_session=sessions[previous],
                    next_session=sessions[following],
                    source_refs=refs,
                    source_capture_time=capture,
                    unknowns=[],
                )
            )
        scope = dict(
            security=security,
            market="XSHE",
            start_date=start,
            end_date=end,
            source_month=source["requested_month"],
            full_exchange_calendar=False,
        )
        return deepcopy(
            dict(
                schema="ad.bounded-calendar-read.v1",
                binding_sha256=self.binding_sha256,
                window_id=window_id,
                security=security,
                role=role,
                historical_pit=False,
                strategy_visible=False,
                matching_eligible=False,
                network_used=False,
                synthetic=source["kind"] == "synthetic_fixture",
                scope=scope,
                source_capture_time=capture,
                source_refs=refs,
                calendar=dict(
                    status="supported",
                    scope=scope,
                    daily_states=rows,
                    links=[dict(r, session_date=r["calendar_date"]) for r in rows if r["is_open"]],
                    open_sessions=[r["calendar_date"] for r in rows if r["is_open"]],
                ),
                neighbour_semantics="Strictly earlier/later open sessions for both open and closed queried dates",
                unknowns=[
                    "historical_publication_time",
                    "prices",
                    "actions",
                    "security_lifecycle",
                    "cash_settlement_eligibility",
                ],
                contains_account_or_callback_results=False,
            )
        )

    def read_calendar(self, window_id, security, calendar_date, *, role):
        result = self.read_carry(window_id, security, role=role)
        label = day(calendar_date)
        rows = [r for r in result["calendar"]["daily_states"] if r["calendar_date"] == label]
        require(len(rows) == 1, "CALENDAR_BINDING_SCOPE", "Date is outside the bound read window")
        result.pop("calendar")
        # Date flags/neighbours are known; other capability unknowns remain in the envelope.
        row = rows[0].copy()
        row.pop("unknowns")
        result.update(row)
        return result
