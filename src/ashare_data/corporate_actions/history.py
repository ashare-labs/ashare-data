"""Version-pinned corporate actions and historical share observations.

This is an evidence interface, not an account ledger. Native cash/stock rights
stay separate from research price factors and from issuer-level share changes.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from ashare_data.core.model import DataError, day, require, symbol, timestamp
from ashare_data.storage.immutable import identifier
from ashare_data.corporate_actions.identity import associate_actions

SCHEMA = "ad.corporate-history-binding.v1"
DATA_SCHEMA = "ad.corporate-history-data.v1"
TZ = ZoneInfo("Asia/Shanghai")
DATE_FIELDS = frozenset(
    {
        "announcement_date",
        "plan_announcement_date",
        "record_date",
        "ex_date",
        "payment_date",
        "share_credit_date",
        "new_shares_tradable_date",
        "price_adjustment_date",
        "subscription_start",
        "subscription_end",
        "effective_date",
        "registration_date",
        "account_code_change_date",
    }
)
PUBLICATION_DATE_FIELDS = frozenset({"announcement_date", "plan_announcement_date"})
ECONOMIC_DATE_FIELDS = DATE_FIELDS - PUBLICATION_DATE_FIELDS
# Use the same invariants on individual observations and consolidated fields.
# Credit and tradability remain independent dates; neither is forced to equal ex-date.
ACTION_DATE_ORDER = (
    ("record_date", "ex_date"),
    ("record_date", "payment_date"),
    ("record_date", "share_credit_date"),
    ("record_date", "new_shares_tradable_date"),
    ("ex_date", "payment_date"),
    ("ex_date", "new_shares_tradable_date"),
    ("subscription_start", "subscription_end"),
)
VALUE_FIELDS = frozenset(
    {
        "cash_per_share",
        "bonus_share_ratio",
        "capitalization_share_ratio",
        "rights_ratio",
        "rights_price",
        "base_shares",
        "ex_reference_cash_deduction",
        "total_shares_before",
        "total_shares_after",
        "shares_added",
        "eligible_shares",
    }
)
SHARE_FIELDS = frozenset(
    {
        "total_shares",
        "float_a_shares",
        "float_b_shares",
        "float_h_shares",
        "restricted_a_shares",
        "preferred_shares",
    }
)
SUPPORTED_TYPES = frozenset({"distribution", "rights_issue", "security_change", "reorganization"})


def _json(raw):
    return json.loads(
        raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON"))
    )


def _number(value):
    require(
        isinstance(value, str) and len(value) <= 64,
        "CORPORATE_VALUE",
        "Values must be bounded decimal strings",
    )
    try:
        v = Decimal(value)
        require(
            v.is_finite() and 0 <= v < Decimal("1e20") and v.as_tuple().exponent >= -24,
            "CORPORATE_VALUE",
            "Invalid economic amount",
        )
        return v
    except InvalidOperation as exc:
        raise DataError("CORPORATE_VALUE", "Invalid decimal") from exc


def event_key(row):
    return row.get("_event_key") or ":".join((row["security"], row["type"], row["observation_id"]))


def unlocated(row):
    return not any(row["fields"].get(k) is not None for k in ECONOMIC_DATE_FIELDS)


def intersects(row, start, end, date_role="any"):
    fields = row["fields"]
    if date_role not in {"any", "economic"}:
        return fields.get(date_role) is not None and start <= fields[date_role] <= end
    roles = DATE_FIELDS if date_role == "any" else ECONOMIC_DATE_FIELDS
    dates = [v for k, v in fields.items() if k in roles and v is not None]
    if any(start <= d <= end for d in dates):
        return True
    # Unsettled rights can straddle a window even when neither endpoint is inside.
    effective = [fields[k] for k in ECONOMIC_DATE_FIELDS if fields.get(k) is not None]
    return bool(effective and min(effective) <= end and max(effective) >= start)


def chronology_conflicts(fields):
    return [
        (first, last)
        for first, last in ACTION_DATE_ORDER
        if fields.get(first) is not None
        and fields.get(last) is not None
        and fields[first] > fields[last]
    ]


def date_candidates(item):
    """Selected date, or finite unresolved alternatives; never superseded values."""
    if item.get("value") is not None:
        return [item["value"]]
    if item.get("status") == "conflict":
        return sorted({r["value"] for r in item["observations"] if r["value"] is not None})
    return []


def consolidate(rows):
    visible_ids = {r["observation_id"] for r in rows}
    groups = {}
    for row in rows:
        groups.setdefault(event_key(row), []).append(row)
    events = []
    for key, observations in sorted(groups.items()):
        fields, conflicts = {}, []
        for name in sorted({k for row in observations for k in row["fields"]}):
            known = [row for row in observations if row["fields"].get(name) is not None]
            values = {}
            for row in known:
                value = row["fields"][name]
                canonical = _number(value) if name in VALUE_FIELDS else value
                values.setdefault(canonical, []).append(row)
            refs = [
                {
                    "observation_id": r["observation_id"],
                    "source_id": r["source_id"],
                    "locator": r["locator"],
                    "value": r["fields"][name],
                }
                for r in known
            ]
            item = dict(
                value=known[0]["fields"][name] if len(values) == 1 else None,
                status="source_observed" if len(values) == 1 else "missing",
                observations=refs,
            )
            if len(values) == 1 and any(
                r.get("source_status") == "document_extracted" for r in known
            ):
                item["status"] = "document_extracted"
            if len(values) > 1:
                item["status"] = "conflict"
                # A reviewed document must name each field it resolves explicitly.
                resolved = [r for r in observations if name in r.get("resolves", {})]
                resolved = [
                    r
                    for r in resolved
                    if not any(
                        r["observation_id"] in other.get("_supersedes_observations", [])
                        for other in resolved
                    )
                ]
                if len({r["fields"][name] for r in resolved}) == 1:
                    item.update(
                        value=resolved[0]["fields"][name],
                        status="document_resolved_conflict",
                        resolution=resolved[0]["resolves"][name],
                    )
                conflicts.append(dict(field=name, **deepcopy(item)))
            # An explicit cited withdrawal is different from an absent source value.
            withdrawals = [
                r
                for r in observations
                if name in r.get("resolves", {}) and r["fields"].get(name) is None
            ]
            active = [
                r
                for r in withdrawals
                if not any(
                    r["observation_id"] in other.get("_supersedes_observations", [])
                    for other in observations
                    if name in other.get("resolves", {})
                )
            ]
            if active:
                item.update(
                    value=None, status="document_withdrawn", resolution=active[0]["resolves"][name]
                )
            fields[name] = item
        first = observations[0]
        identity_issues = {}
        for row in observations:
            for issue in row.get("_identity_issues", []):
                if "visibility_groups" in issue:
                    matches = [
                        sorted(set(group) & visible_ids) for group in issue["visibility_groups"]
                    ]
                    if sum(bool(group) for group in matches) < 2:
                        continue
                    issue = dict(
                        issue,
                        visibility_groups=[group for group in matches if group],
                        observation_ids=sorted(set(issue["observation_ids"]) & visible_ids),
                    )
                elif not set(issue["observation_ids"]) <= visible_ids:
                    continue
                identity_issues[json.dumps(issue, sort_keys=True)] = issue
        events.append(
            dict(
                event_id=key,
                security=first["security"],
                type=first["type"],
                fields=fields,
                conflicts=conflicts,
                observations=deepcopy(
                    [
                        {k: v for k, v in row.items() if not k.startswith("_")}
                        for row in observations
                    ]
                ),
                identity_method=first.get("_identity_method", "source_observation"),
                identity_issues=list(identity_issues.values()),
                cash_basis=first.get("cash_basis"),
                currency="CNY",
                historical_pit=False,
                actual_cash_available_at=None,
                actual_share_credit_at=None,
            )
        )
    return events


def event_window_state(event, start, end, date_role):
    fields = {k: v["value"] for k, v in event["fields"].items()}
    row = {"fields": fields}
    publication = PUBLICATION_DATE_FIELDS
    if date_role in publication:
        if any(intersects(r, start, end, date_role) for r in event["observations"]):
            return "matched"
        return None
    if date_role == "any" and any(
        intersects(r, start, end, role) for r in event["observations"] for role in publication
    ):
        return "matched"
    candidates = {
        name: date_candidates(event["fields"].get(name, {})) for name in ECONOMIC_DATE_FIELDS
    }
    possible_dates = [d for dates in candidates.values() for d in dates]
    if not possible_dates:
        return "unlocated"
    role = "economic" if date_role == "any" else date_role
    if intersects(row, start, end, role):
        return "matched"
    # An unsettled interpretation can span a window without any candidate date
    # landing inside it. Keep the full possible economic lifetime, bounded by
    # the earliest/latest candidate, and let coverage propagate its conflicts.
    if role == "economic" and min(possible_dates) <= end and max(possible_dates) >= start:
        return "ambiguous_date"
    if role in candidates and any(start <= d <= end for d in candidates[role]):
        return "ambiguous_date"
    # Missing required dates cannot turn an unsettled captured entitlement into
    # an empty ready interval. Use known chronology to avoid blocking unrelated
    # earlier/later windows; optional dates for a zero-stock/zero-cash leg do not
    # create an open settlement window.
    if event["type"] == "distribution":
        required = {"record_date", "ex_date"}
        if fields.get("cash_per_share") is None or Decimal(fields["cash_per_share"]) > 0:
            required.add("payment_date")
        if any(
            fields.get(k) is None or Decimal(fields[k]) > 0
            for k in ("bonus_share_ratio", "capitalization_share_ratio")
        ):
            required.update({"share_credit_date", "new_shares_tradable_date"})
        for name in required:
            item = event["fields"].get(name, {})
            if fields.get(name) is not None or item.get("status") == "conflict":
                continue
            if role not in {"economic", name}:
                continue
            before = [a for a, z in ACTION_DATE_ORDER if z == name]
            after = [z for a, z in ACTION_DATE_ORDER if a == name]
            earliest = max((min(candidates[k]) for k in before if candidates[k]), default=start)
            latest = min((max(candidates[k]) for k in after if candidates[k]), default=end)
            if earliest <= end and latest >= start:
                return "unknown_required_date"
    return None


class CorporateHistoryView:
    """Immutable, locally captured history; opening verifies every bound byte."""

    def __init__(self, binding_path, *, expected_binding_sha256):
        try:
            self._load(binding_path, expected_binding_sha256)
        except (OSError, KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            raise DataError(
                "CORPORATE_SCHEMA", "Malformed or unreadable corporate history"
            ) from exc

    def _load(self, path, digest):
        self.path = Path(path).resolve()
        self.binding_sha256 = identifier(digest)
        raw = self.path.read_bytes()
        require(
            len(raw) <= 4 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == digest,
            "CORPORATE_INTEGRITY",
            "Binding digest mismatch",
        )
        b = _json(raw)
        require(
            b.get("schema") == SCHEMA
            and b.get("historical_pit") is False
            and b.get("execution_permission") is False,
            "CORPORATE_SCHEMA",
            "Unsupported binding or admission claims",
        )
        files = b["files"]
        require(
            isinstance(files, list) and 1 <= len(files) <= 5000,
            "CORPORATE_SCHEMA",
            "Invalid file inventory",
        )
        contents, total = {}, 0
        for ref in files:
            p = Path(ref["path"])
            target = self.path.parent / p
            require(
                not p.is_absolute()
                and ".." not in p.parts
                and not target.is_symlink()
                and target.resolve().is_relative_to(self.path.parent)
                and p.as_posix() not in contents
                and target.is_file(),
                "CORPORATE_FILE",
                "Invalid or duplicate evidence path",
            )
            require(
                target.stat().st_size <= 32 * 1024 * 1024,
                "CORPORATE_FILE",
                "Evidence file too large",
            )
            content = target.read_bytes()
            total += len(content)
            require(total <= 256 * 1024 * 1024, "CORPORATE_FILE", "Evidence package too large")
            require(
                len(content) == ref["bytes"]
                and hashlib.sha256(content).hexdigest() == identifier(ref["sha256"]),
                "CORPORATE_INTEGRITY",
                "Bound evidence digest mismatch",
                {"path": ref["path"]},
            )
            contents[p.as_posix()] = content
        require(b["data"] in contents, "CORPORATE_SCHEMA", "Unbound data file")
        data = _json(contents[b["data"]])
        require(
            data["schema"] == DATA_SCHEMA
            and data["historical_pit"] is False
            and data["market_complete"] is False,
            "CORPORATE_SCHEMA",
            "Invalid data claims",
        )
        self._scope = {r["security"]: r for r in data["scope"]["securities"]}
        require(
            len(self._scope) == len(data["scope"]["securities"]) and 1 <= len(self._scope) <= 1000,
            "CORPORATE_SCHEMA",
            "Invalid security scope",
        )
        for security, r in self._scope.items():
            require(
                symbol(security) == security and day(r["start"]) <= day(r["end"]),
                "CORPORATE_SCOPE",
                "Invalid scoped window",
            )
        sources = data["sources"]
        for source in sources.values():
            require(
                source["body"] in contents and source["receipt"] in contents,
                "CORPORATE_SCHEMA",
                "Unbound source evidence",
            )
            receipt = _json(contents[source["receipt"]])
            require(
                receipt["ok"] is True
                and receipt["request"]["kind"] == source["kind"]
                and receipt["request"].get("security") == source.get("security")
                and hashlib.sha256(contents[source["body"]]).hexdigest() == receipt["sha256"]
                and source["sha256"] == receipt["sha256"]
                and len(contents[source["body"]]) == receipt["bytes"]
                and timestamp(receipt["started_at"]) <= timestamp(receipt["received_at"]),
                "CORPORATE_INTEGRITY",
                "Source receipt does not match captured bytes",
            )
            require(
                source["observed_at"] == receipt["received_at"],
                "CORPORATE_INTEGRITY",
                "Source clock differs from receipt",
            )
        for key, entry in data["source_coverage"].items():
            require(
                key in sources
                and entry["security"] == sources[key]["security"]
                and entry["kind"] == sources[key]["kind"]
                and entry.get("absence_proved", False) is False,
                "CORPORATE_SCHEMA",
                "Invalid source coverage claim",
            )
            entry.setdefault("absence_proved", False)
            for name in ("native_action_rows", "native_share_rows", "nonimplemented_rows"):
                entry.setdefault(name, None)
            entry.setdefault("unparsed_records", [])
        for boundary in data.get("security_boundaries", []):
            require(
                boundary["security"] in self._scope
                and boundary["source_id"] in sources
                and sources[boundary["source_id"]]["kind"] == "issuer_pdf"
                and sources[boundary["source_id"]]["security"] == boundary["security"],
                "CORPORATE_SCHEMA",
                "Unbound identity boundary",
            )
            day(boundary["valid_through"])
            symbol(boundary["successor_security"])
        seen = set()
        require(
            len(data["actions"]) <= 100000 and len(data["shares"]) <= 100000,
            "CORPORATE_SCHEMA",
            "Too many observations",
        )
        for row in data["actions"] + data["shares"]:
            require(
                row["observation_id"] not in seen
                and row["source_id"] in sources
                and row["security"] in self._scope
                and sources[row["source_id"]].get("security") == row["security"]
                and row["historical_available_at"] is None
                and isinstance(row["locator"], str)
                and row["locator"],
                "CORPORATE_SCHEMA",
                "Foreign or duplicate observation",
            )
            seen.add(row["observation_id"])
            timestamp(row["observed_at"])
            supporting = row.get("supporting_documents", [])
            require(
                all(
                    d["source_id"] in sources
                    and sources[d["source_id"]]["kind"] == "issuer_pdf"
                    and sources[d["source_id"]]["security"] == row["security"]
                    and d["document_text"] in contents
                    for d in supporting
                ),
                "CORPORATE_SCHEMA",
                "Invalid supporting document",
            )
            source_ids = [row["source_id"]] + [d["source_id"] for d in supporting]
            classification = row.get("classification_evidence")
            if classification:
                require(
                    classification["source_id"] in sources
                    and sources[classification["source_id"]]["kind"] == "issuer_pdf"
                    and sources[classification["source_id"]]["security"] == row["security"]
                    and classification["type"] == row["type"],
                    "CORPORATE_SCHEMA",
                    "Unbound event classification",
                )
                day(classification["announcement_date"])
                source_ids.append(classification["source_id"])
            require(
                timestamp(row["observed_at"])
                == max(timestamp(sources[s]["observed_at"]) for s in source_ids),
                "CORPORATE_SCHEMA",
                "Observation clock excludes a supporting source",
            )
            if "type" in row:
                require(row["type"] in SUPPORTED_TYPES, "CORPORATE_SCHEMA", "Unknown event type")
                require(
                    row.get("currency") == "CNY"
                    and row.get("cash_basis")
                    in {
                        "gross_pre_tax_per_old_eligible_share",
                        "subscription_outflow_per_new_share_not_dividend",
                    },
                    "CORPORATE_SCHEMA",
                    "Unspecified currency or entitlement basis",
                )
                for name, value in row["fields"].items():
                    require(
                        name in DATE_FIELDS | VALUE_FIELDS | {"successor_security"},
                        "CORPORATE_SCHEMA",
                        "Unknown action field",
                    )
                    if value is not None:
                        if name in DATE_FIELDS:
                            day(value)
                        elif name in VALUE_FIELDS:
                            _number(value)
                        elif name == "successor_security":
                            symbol(value)
                chronology = chronology_conflicts(row["fields"])
                require(
                    not chronology,
                    "CORPORATE_DATE",
                    "Settlement precedes record date"
                    if any(a == "record_date" and z != "ex_date" for a, z in chronology)
                    else "Reversed event chronology",
                )
                if row.get("resolves"):
                    require(
                        sources[row["source_id"]]["kind"] == "issuer_pdf"
                        and set(row["resolves"]) <= row["fields"].keys()
                        and row.get("citations"),
                        "CORPORATE_SCHEMA",
                        "Uncited conflict resolution",
                    )
                if sources[row["source_id"]]["kind"] == "issuer_pdf":
                    require(
                        row.get("citations") and row.get("document_text") in contents,
                        "CORPORATE_SCHEMA",
                        "Document observations require text and page citations",
                    )
                    require(
                        contents[sources[row["source_id"]]["body"]].startswith(b"%PDF-"),
                        "CORPORATE_SCHEMA",
                        "Document capture is not PDF",
                    )
                    texts = {
                        row["source_id"]: contents[row["document_text"]].decode(),
                        **{
                            d["source_id"]: contents[d["document_text"]].decode()
                            for d in supporting
                        },
                    }
                    for citation in row["citations"]:
                        text = texts.get(citation.get("source_id", row["source_id"]), "")
                        pages = re.split(r"\n--- PAGE (\d+) ---\n", text)
                        page_text = dict(zip(pages[1::2], pages[2::2])).get(
                            str(citation["page"]), ""
                        )
                        require(
                            type(citation["page"]) is int
                            and citation["page"] >= 1
                            and citation["quote"]
                            and citation["quote"] in page_text,
                            "CORPORATE_SCHEMA",
                            "Document quote missing from bound text",
                        )
            else:
                day(row["date"])
                if row["announcement_date"] is not None:
                    day(row["announcement_date"])
                require(
                    row["fields"] and set(row["fields"]) <= SHARE_FIELDS,
                    "CORPORATE_SCHEMA",
                    "Invalid share fields",
                )
                for value in row["fields"].values():
                    if value is not None:
                        _number(value)
        self._actions = associate_actions(data["actions"], sources)
        self._data, self._binding = data, b

    def descriptor(self):
        return deepcopy(
            dict(
                schema=SCHEMA,
                binding_sha256=self.binding_sha256,
                scope=self._data["scope"],
                historical_pit=False,
                market_complete=False,
                execution_permission=False,
                event_identity_version=2,
                units={
                    "cash": "CNY/old eligible share",
                    "shares": "share",
                    "bonus_and_capitalization": "new shares/old eligible share",
                    "rights_price": "CNY/new share",
                },
                limits=self._data["limitations"],
            )
        )

    def lineage(self):
        return deepcopy(
            dict(
                binding_sha256=self.binding_sha256,
                sources=self._data["sources"],
                files=self._binding["files"],
                acquisition_failures=self._data["acquisition_failures"],
            )
        )

    def _range(self, securities, start, end):
        securities = [securities] if isinstance(securities, str) else list(securities)
        require(
            securities and len(securities) == len(set(securities)),
            "CORPORATE_SCOPE",
            "Empty or duplicate security request",
        )
        start, end = day(start), day(end)
        require(start <= end, "CORPORATE_SCOPE", "Reversed date range")
        for security in securities:
            require(
                security in self._scope
                and self._scope[security]["start"] <= start
                and end <= self._scope[security]["end"],
                "CORPORATE_SCOPE",
                "Request exceeds captured scope",
            )
        return securities, start, end

    def _visible(self, rows, visibility, as_of):
        require(
            visibility in {"posthoc", "announced_next_day", "received", "verified"},
            "CORPORATE_VISIBILITY",
            "Unknown visibility mode",
        )
        require(
            visibility != "verified",
            "PIT_UNAVAILABLE",
            "Original historical release times and revision chains are unproven",
        )
        if visibility == "posthoc":
            require(
                as_of is None,
                "CORPORATE_VISIBILITY",
                "Posthoc reads do not accept a knowledge clock",
            )
            return rows
        require(as_of is not None, "CORPORATE_VISIBILITY", "An aware as_of is required")
        at = timestamp(as_of).astimezone(TZ)
        if visibility == "received":
            return [r for r in rows if timestamp(r["observed_at"]) <= at]
        visible = []
        for r in rows:
            ann = (
                r.get("announcement_date")
                if "type" not in r
                else r["fields"].get("announcement_date")
            )
            if r.get("classification_evidence") and ann is not None:
                ann = max(ann, r["classification_evidence"]["announcement_date"])
            if (
                ann is not None
                and datetime.combine(
                    datetime.fromisoformat(ann).date() + timedelta(days=1), time(), TZ
                )
                <= at
            ):
                visible.append(r)
        return visible

    def events(self, securities, *, start, end, date_role="any", visibility="posthoc", as_of=None):
        securities, start, end = self._range(securities, start, end)
        require(
            date_role in {"any", "economic"} or date_role in DATE_FIELDS,
            "CORPORATE_DATE",
            "Unknown date role",
        )
        candidates = [r for r in self._actions if r["security"] in securities]
        visible = self._visible(candidates, visibility, as_of)
        selected = []
        for event in consolidate(visible):
            placement = event_window_state(event, start, end, date_role)
            if placement is not None:
                selected.append(dict(event, window_placement=placement))
        all_keys = {
            e["event_id"]
            for e in consolidate(candidates)
            if event_window_state(e, start, end, date_role) is not None
        }
        visible_ids = {r["observation_id"] for r in visible}
        relevant_keys = all_keys | {r["event_id"] for r in selected}
        visibility_gaps = [
            dict(
                event_id=event_key(r),
                observation_id=r["observation_id"],
                source_id=r["source_id"],
                announcement_date=None,
                reason="announcement_visibility_unknown",
            )
            for r in candidates
            if visibility == "announced_next_day"
            and r["fields"].get("announcement_date") is None
            and event_key(r) in relevant_keys
        ]
        return deepcopy(
            dict(
                rows=selected,
                event_identity_version=2,
                binding_sha256=self.binding_sha256,
                start=start,
                end=end,
                date_role=date_role,
                visibility=visibility,
                withheld_observations=sum(
                    r["observation_id"] not in visible_ids
                    for r in candidates
                    if event_key(r) in relevant_keys
                ),
                visibility_gaps=visibility_gaps,
                unlocated_event_ids=[
                    r["event_id"] for r in selected if r["window_placement"] == "unlocated"
                ],
                historical_pit=False,
                complete=False,
                absence_proved=False,
                source_scope="captured dividend/rights ledgers plus cited issuer documents; not all action types",
            )
        )

    def share_history(
        self,
        security,
        *,
        start,
        end,
        source="sina_structure",
        include_prior=False,
        visibility="posthoc",
        as_of=None,
    ):
        _, start, end = self._range(security, start, end)
        require(
            source in {"sina_structure", "sina_float"},
            "CORPORATE_SOURCE",
            "Select an explicit share source",
        )
        peers = self._visible(
            [r for r in self._data["shares"] if r["security"] == security], visibility, as_of
        )
        rows = [r for r in peers if self._data["sources"][r["source_id"]]["kind"] == source]
        prior = (
            max((r["date"] for r in rows if r["date"] < start), default=None)
            if include_prior
            else None
        )
        selected = sorted(
            (r for r in rows if start <= r["date"] <= end or r["date"] == prior),
            key=lambda r: (r["date"], r["observation_id"]),
        )
        return deepcopy(
            dict(
                rows=[{**r, "diagnostics": self._share_diagnostics(r, peers)} for r in selected],
                source=source,
                unit="share",
                historical_pit=False,
                daily_register_complete=False,
                field_semantics_verified=False,
                source_coverage={
                    k: deepcopy(v)
                    for k, v in self._data["source_coverage"].items()
                    if v["security"] == security and v["kind"] == source
                },
                binding_sha256=self.binding_sha256,
            )
        )

    def _share_diagnostics(self, row, peers):
        """Compare source claims without repairing values or guessing component definitions."""
        issues = []
        for peer in peers:
            if peer["date"] != row["date"] or peer["source_id"] == row["source_id"]:
                continue
            a, b = row["fields"].get("float_a_shares"), peer["fields"].get("float_a_shares")
            if a is None or b is None:
                continue
            quantum = sum(
                (
                    _number(r.get("source_quantum_shares", {}).get("float_a_shares") or "0")
                    for r in (row, peer)
                ),
                Decimal(0),
            )
            delta = _number(a) - _number(b)
            if abs(delta) > quantum / 2:
                issues.append(
                    dict(
                        reason="cross_source_float_disagreement",
                        fields=["float_a_shares"],
                        peer_observation_id=peer["observation_id"],
                        peer_source_id=peer["source_id"],
                        source_value=a,
                        peer_value=b,
                        difference=format(delta, "f"),
                    )
                )
        total = row["fields"].get("total_shares")
        components = {
            k: v
            for k, v in row["fields"].items()
            if k in {"float_a_shares", "float_b_shares", "float_h_shares"} and v is not None
        }
        if total is not None and len(components) >= 2:
            quantum = sum(
                (
                    _number(row.get("source_quantum_shares", {}).get(k) or "0")
                    for k in list(components) + ["total_shares"]
                ),
                Decimal(0),
            )
            if sum(map(_number, components.values())) > _number(total) + quantum / 2:
                issues.append(
                    dict(
                        reason="reported_float_components_exceed_total",
                        fields=sorted(components),
                        components=components,
                        reported_total=total,
                        interpretation="Source labels cannot be admitted as mutually exclusive A/B/H classes",
                    )
                )
        return issues

    def shares_on(
        self,
        security,
        on_date,
        *,
        source="sina_structure",
        carry="none",
        field=None,
        visibility="posthoc",
        as_of=None,
    ):
        require(
            carry in {"none", "last_observation"}, "CORPORATE_SHARES", "Unknown share carry model"
        )
        require(field is None or field in SHARE_FIELDS, "CORPORATE_SHARES", "Unknown share field")
        for boundary in self._data.get("security_boundaries", []):
            require(
                security != boundary["security"] or day(on_date) <= boundary["valid_through"],
                "SECURITY_IDENTITY_ENDED",
                "Old-code share observations cannot be carried into its successor",
            )
        result = self.share_history(
            security,
            start=on_date,
            end=on_date,
            source=source,
            include_prior=carry == "last_observation",
            visibility=visibility,
            as_of=as_of,
        )
        rows = result["rows"]
        if not rows:
            return dict(status="missing", value=None, query_date=on_date, **result)
        newest = max(r["date"] for r in rows)
        rows = [r for r in rows if r["date"] == newest]

        def selected(row):
            return row["fields"] if field is None else {field: row["fields"].get(field)}

        distinct = {json.dumps(selected(r), sort_keys=True) for r in rows}
        if len(distinct) != 1:
            return dict(status="conflict", value=None, query_date=on_date, **result)
        conflicts = [
            d for r in rows for d in r["diagnostics"] if field is None or field in d["fields"]
        ]
        if conflicts:
            return dict(
                status="source_conflict",
                value=None,
                source_value=deepcopy(selected(rows[0])),
                conflicts=conflicts,
                reference_date=newest,
                query_date=on_date,
                **result,
            )
        if not any(value is not None for value in selected(rows[0]).values()):
            return dict(
                status="missing", value=None, reference_date=newest, query_date=on_date, **result
            )
        return dict(
            status="observed_on_date" if newest == on_date else "carried_observation_model",
            value=deepcopy(selected(rows[0])),
            reference_date=newest,
            query_date=on_date,
            effective_daily_state_verified=False,
            **result,
        )

    def coverage(self, securities, *, start, end):
        securities, start, end = self._range(securities, start, end)
        result = []
        for security in securities:
            events = self.events(security, start=start, end=end, date_role="economic")["rows"]
            missing = []
            for event in self.events(security, start=start, end=end)["rows"]:
                if not any(
                    event["fields"].get(f, {}).get("value")
                    for f in ("record_date", "ex_date", "effective_date", "subscription_start")
                ):
                    missing.append(
                        dict(
                            event_id=event["event_id"],
                            field="economic_date",
                            reason="unknown_event_placement",
                        )
                    )
            for event in events:
                missing += [
                    dict(event_id=event["event_id"], field="event_identity", **issue)
                    for issue in event["identity_issues"]
                ]
                needed = (
                    ["effective_date", "account_code_change_date", "successor_security"]
                    if event["type"] == "security_change"
                    else ["record_date", "ex_date"]
                )
                fields = event["fields"]
                if event["type"] == "distribution":
                    needed += ["cash_per_share", "bonus_share_ratio", "capitalization_share_ratio"]
                    cash = fields.get("cash_per_share", {}).get("value")
                    if cash is None or Decimal(cash) > 0:
                        needed.append("payment_date")
                    if any(
                        fields.get(f, {}).get("value") is None or Decimal(fields[f]["value"]) > 0
                        for f in ("bonus_share_ratio", "capitalization_share_ratio")
                    ):
                        needed += ["share_credit_date", "new_shares_tradable_date"]
                else:
                    missing.append(
                        dict(
                            event_id=event["event_id"],
                            field="account_treatment",
                            reason="unsupported_event_type",
                        )
                    )
                missing += [
                    dict(
                        event_id=event["event_id"],
                        field=f,
                        reason=fields.get(f, {}).get("status", "missing"),
                    )
                    for f in needed
                    if fields.get(f, {}).get("value") is None
                ]
                for observation in event["observations"]:
                    missing += [
                        dict(
                            event_id=event["event_id"], source_id=observation["source_id"], **issue
                        )
                        for issue in observation.get("source_issues", [])
                        if issue["field"] == "implementation_status"
                        or (
                            issue["field"] in needed
                            and not (
                                fields[issue["field"]]["value"] is not None
                                and fields[issue["field"]]["status"]
                                in {"document_extracted", "document_resolved_conflict"}
                            )
                        )
                    ]
                for first, last in chronology_conflicts({k: v["value"] for k, v in fields.items()}):
                    missing.append(
                        dict(
                            event_id=event["event_id"],
                            field=last,
                            reason="conflicting_consolidated_chronology",
                            before=first,
                        )
                    )
            shares = self.share_history(security, start=start, end=end, include_prior=True)["rows"]
            sources = {
                k: deepcopy(v)
                for k, v in self._data["source_coverage"].items()
                if v["security"] == security
            }
            source_gaps = []
            for source_id, source in sources.items():
                if source["kind"] in {
                    "sina_actions",
                    "sina_structure",
                    "sina_action_detail",
                    "eastmoney_dividends",
                }:
                    if (
                        source["status"] in {"parse_failed", "partial_provider_tables"}
                        or source["unparsed_records"]
                    ):
                        source_gaps.append(
                            dict(
                                source_id=source_id,
                                reason="source_parse_incomplete",
                                status=source["status"],
                                unparsed_records=source["unparsed_records"],
                                parse_error=source.get("parse_error"),
                            )
                        )
            history_observed = any(
                r["security"] == security and not unlocated(r) for r in self._actions
            )
            ledger = [v for v in sources.values() if v["kind"] == "sina_actions"]
            blank_source = not history_observed and not any(
                (v.get("native_action_rows") or 0) + (v.get("nonimplemented_rows") or 0) > 0
                for v in ledger
            )
            if blank_source:
                source_gaps.append(dict(reason="source_has_no_locatable_history"))
            result.append(
                dict(
                    security=security,
                    events=len(events),
                    event_field_gaps=missing,
                    source_gaps=source_gaps,
                    interval_state="unlocated_events"
                    if any(e["window_placement"] == "unlocated" for e in events)
                    else "events_observed"
                    if events
                    else "source_unresolved"
                    if source_gaps
                    else "empty_source_scoped_interval",
                    share_observations=len(shares),
                    share_diagnostic_rows=sum(bool(r["diagnostics"]) for r in shares),
                    share_diagnostics=[
                        dict(
                            date=r["date"],
                            observation_id=r["observation_id"],
                            issues=r["diagnostics"],
                        )
                        for r in shares
                        if r["diagnostics"]
                    ],
                    share_dates=sorted({r["date"] for r in shares}),
                    share_observations_by_source={
                        kind: len(
                            self.share_history(
                                security, start=start, end=end, source=kind, include_prior=True
                            )["rows"]
                        )
                        for kind in ("sina_structure", "sina_float")
                    },
                    source_coverage=sources,
                    provider_tables_captured=all(
                        any(
                            v["kind"] == k and v["status"] == "parsed_provider_tables"
                            for v in sources.values()
                        )
                        for k in ("sina_actions", "sina_structure")
                    ),
                    economic_schedule_fields_complete=bool(events) and not missing,
                    identity_boundaries=[
                        deepcopy(b)
                        for b in self._data.get("security_boundaries", [])
                        if b["security"] == security and end > b["valid_through"]
                    ],
                    absence_proved=False,
                    all_action_types_complete=False,
                    daily_share_register_complete=False,
                )
            )
        return dict(
            rows=result,
            start=start,
            end=end,
            historical_pit=False,
            market_complete=False,
            binding_sha256=self.binding_sha256,
        )

    def export_aq(self, securities, *, start, end, tax_model, accept_source_scoped_coverage=False):
        """Engine-side schedules and blockers; never places orders or alters raw prices."""
        require(
            tax_model in {"gross_cash_no_tax", "external_tax_ledger"},
            "CORPORATE_TAX",
            "Choose gross research cash or a separate consumer tax ledger",
        )
        coverage = self.coverage(securities, start=start, end=end)
        require(
            type(accept_source_scoped_coverage) is bool,
            "CORPORATE_SCOPE",
            "Coverage acceptance must be boolean",
        )
        events = self.events(securities, start=start, end=end, date_role="economic")["rows"]
        issues = [gap for row in coverage["rows"] for gap in row["event_field_gaps"]]
        for row in coverage["rows"]:
            issues += [dict(security=row["security"], **gap) for gap in row["source_gaps"]]
            if row["identity_boundaries"]:
                issues.append(
                    dict(
                        security=row["security"],
                        reason="security_identity_boundary_requires_account_conversion",
                    )
                )
            if not row["provider_tables_captured"]:
                issues.append(
                    dict(security=row["security"], reason="source_table_coverage_missing")
                )
        if not accept_source_scoped_coverage:
            issues.append(dict(reason="source_scoped_coverage_not_accepted"))
        output = []
        for event in events:
            fields = {k: v["value"] for k, v in event["fields"].items()}
            output.append(
                dict(
                    event_id=event["event_id"],
                    entitlement_key=event["event_id"],
                    settlement_keys={
                        phase: event["event_id"] + ":" + phase
                        for phase in ("record", "cash", "stock_credit", "stock_tradable")
                    },
                    revision_observation_ids=[r["observation_id"] for r in event["observations"]],
                    identity_method=event["identity_method"],
                    identity_issues=event["identity_issues"],
                    window_placement=event["window_placement"],
                    security=event["security"],
                    type=event["type"],
                    **fields,
                    field_lineage=event["fields"],
                    conflicts=event["conflicts"],
                    tax_basis="gross entitlement; tax timing/holding period is a consumer obligation",
                    payment_scope="CSDC delegated distribution only; issuer self-payment may differ",
                    share_scope="eligible unrestricted shareholder only; no fractional-share rounding supplied",
                )
            )
        return dict(
            schema="ad.aq-corporate-actions.v1",
            event_identity_version=2,
            rows=output,
            issues=issues,
            ready_for_declared_date_model=not issues,
            tax_model=tax_model,
            source_scoped_coverage_accepted=accept_source_scoped_coverage,
            economic_result_certified=False,
            historical_pit=False,
            execution_permission=False,
            strategy_visible=False,
            coverage=coverage,
            binding_sha256=self.binding_sha256,
            requirements=[
                "Deduplicate each entitlement and settlement phase by its stable key across windows; reconcile later revisions instead of booking a second entitlement",
                "Use raw market prices for matching",
                "Process record-date entitlements in the native account ledger",
                "Choose an explicit intraday cash/share availability model; dates do not certify intraday credit",
                "Apply taxes and fractional share rules outside this data interface",
                "Block rights/reorganization/code-change events until the account supports their specific treatment",
                "Historical exchange price limits and trading-state certification remain separate inputs",
            ],
        )
