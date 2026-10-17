"""Purpose-gated daily simulation inputs over an immutable research snapshot.

This is an offline data adapter, not a broker or matching engine. A research
clock is never evidence that a source value was delivered at that time.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
from pathlib import Path

from ashare_data.research.daily import DailyResearchView, ROLE, TZ, _json, _number
from ashare_data.core.model import DataError, day, require, timestamp
from ashare_data.storage.immutable import identifier

SCHEMA = "ad.daily-execution-binding.v1"
DATA_SCHEMA = "ad.daily-execution-data.v1"
PURPOSES = {"signal_research", "retrospective_simulation", "historical_pit", "live_execution"}
FIELDS = ("is_st", "suspended", "reference_price", "upper_limit", "lower_limit")
ASSUMPTIONS = {
    "retrospective_daily_status": "Daily status is replayed at 09:00; historical delivery is unproved.",
    "shared_market_calendar": "Use the bounded calendar corroborated by holiday notices and independent dates.",
    "standard_mainboard_limits": "Apply the ordinary mainboard rule; exceptional exchange overrides are not certified.",
    "scoped_action_absence": "Absence is limited to the captured action ledger and reference/factor checks.",
    "gross_cash_zero_withholding": "Gross dividend with zero withholding is a consumer accounting model.",
    "dated_name_history_status": "Extend the dated status anchor through the complete captured official name-change query.",
}
CASH_POLICIES = {
    "pay_date_before_open": "09:00:00",
    "pay_date_after_close": "15:30:00",
}


def standard_limits(reference, is_st):
    """Explicit 2019-2024 ordinary mainboard model, never an unknown-ST fallback."""
    require(type(is_st) is bool, "DAILY_EXECUTION_VALUE", "ST flag must be known")
    ref = _number(reference, positive=True)
    require(
        len(str(reference)) <= 48 and ref < Decimal("1e24"),
        "DAILY_EXECUTION_VALUE",
        "Reference exceeds the bounded decimal domain",
    )
    with localcontext() as context:
        context.prec = 34
        tick = Decimal("0.01")
        require(
            ref == ref.quantize(tick), "DAILY_EXECUTION_VALUE", "Reference exceeds cent precision"
        )
        ratio = Decimal("0.05") if is_st else Decimal("0.10")
        upper = max((ref * (1 + ratio)).quantize(tick, rounding=ROUND_HALF_UP), ref + tick)
        lower = min((ref * (1 - ratio)).quantize(tick, rounding=ROUND_HALF_UP), ref - tick)
        require(lower > 0, "DAILY_EXECUTION_VALUE", "Unsupported nonpositive lower limit")
        return str(upper), str(lower)


class DailyExecutionView:
    """A small, separately pinned supplement; the original research bytes are reused.

    ``read_window`` is an engine-only export. Strategy code uses ``history``.
    Unknown fields cannot be waived by accepting models. Strict PIT and live
    execution are always refused by this retrospective-only data product.
    """

    def __init__(self, research, binding_path, *, expected_binding_sha256):
        require(
            isinstance(research, DailyResearchView),
            "DAILY_EXECUTION_BASE",
            "Research view required",
        )
        self.research = research
        try:
            self._load(binding_path, expected_binding_sha256)
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            IndexError,
            OverflowError,
            AttributeError,
            InvalidOperation,
        ) as exc:
            raise DataError("DAILY_EXECUTION_SCHEMA", "Malformed execution supplement") from exc

    def _load(self, binding_path, expected):
        self.path = Path(binding_path).resolve()
        self.binding_sha256 = identifier(expected)
        raw = self.path.read_bytes()
        require(
            len(raw) <= 2 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == expected,
            "DAILY_EXECUTION_INTEGRITY",
            "Supplement binding digest mismatch",
        )
        binding = _json(raw)
        require(
            binding.get("schema") == SCHEMA
            and binding.get("historical_pit") is False
            and binding.get("execution_permission") is False,
            "DAILY_EXECUTION_SCHEMA",
            "Unsupported schema or promoted execution claim",
        )
        require(
            binding.get("research_binding_sha256") == self.research.binding_sha256,
            "DAILY_EXECUTION_BASE",
            "Supplement is bound to a different research snapshot",
        )
        refs = binding["files"]
        require(
            isinstance(refs, list) and 1 <= len(refs) <= 1000,
            "DAILY_EXECUTION_SCHEMA",
            "Invalid supplement inventory",
        )
        contents, total = {}, 0
        for item in refs:
            relative = Path(item["path"])
            path = self.path.parent / relative
            require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and not path.is_symlink()
                and path.resolve().is_relative_to(self.path.parent)
                and relative.as_posix() not in contents
                and path.is_file(),
                "DAILY_EXECUTION_FILE",
                "Unsafe, duplicate or missing evidence path",
            )
            require(
                path.stat().st_size <= 16 * 1024 * 1024,
                "DAILY_EXECUTION_FILE",
                "Oversized evidence",
            )
            content = path.read_bytes()
            total += len(content)
            require(total <= 64 * 1024 * 1024, "DAILY_EXECUTION_FILE", "Supplement exceeds 64 MiB")
            require(
                len(content) == item["bytes"]
                and hashlib.sha256(content).hexdigest() == identifier(item["sha256"]),
                "DAILY_EXECUTION_INTEGRITY",
                "Evidence digest mismatch",
                {"path": item["path"]},
            )
            contents[relative.as_posix()] = content
        require(binding["data"] in contents, "DAILY_EXECUTION_SCHEMA", "Unbound supplement data")
        self._binding, self._contents = binding, contents
        self._data = _json(contents[binding["data"]])
        self._base_refs = {"base:" + x["path"] for x in self.research.lineage()["files"]}
        self._windows = {
            w["id"]: self.research.read_window(w["id"], role=ROLE)
            for w in self.research.descriptor()["windows"]
        }
        self._validate()

    def _refs(self, refs):
        require(
            isinstance(refs, list)
            and refs
            and len(set(refs)) == len(refs)
            and set(refs) <= self._base_refs | set(self._contents),
            "DAILY_EXECUTION_SOURCE",
            "Missing or unbound source references",
        )

    def _validate(self):
        data = self._data
        require(data.get("schema") == DATA_SCHEMA, "DAILY_EXECUTION_SCHEMA", "Invalid data schema")
        require(
            set(data["calendars"]) == set(self._windows),
            "DAILY_EXECUTION_COVERAGE",
            "Missing calendar scope",
        )
        require(
            set(data["action_coverage"]) == set(self._windows),
            "DAILY_EXECUTION_COVERAGE",
            "Missing action scope",
        )
        expected, self._states = set(), {}
        for wid, base in self._windows.items():
            w = base["window"]
            output = [d for d in w["sessions"] if w["start"] <= d <= w["end"]]
            expected.update((s, d) for s in base["securities"] for d in output)
            calendar = data["calendars"][wid]
            self._refs(calendar["source_refs"])
            require(
                calendar["sessions"] == w["sessions"] and calendar["mismatches"] == [],
                "DAILY_EXECUTION_CALENDAR",
                "Calendar differs from the pinned real sessions",
            )
            require(
                set(data["action_coverage"][wid]) == set(base["securities"]),
                "DAILY_EXECUTION_COVERAGE",
                "Missing security action coverage",
            )
            for coverage in data["action_coverage"][wid].values():
                self._refs(coverage["source_refs"])
                require(
                    coverage["status"] in {"scoped_checked", "unavailable"},
                    "DAILY_EXECUTION_SCHEMA",
                    "Unqualified action absence",
                )
        require(
            isinstance(data["states"], list) and len(data["states"]) <= 100000,
            "DAILY_EXECUTION_SCHEMA",
            "Invalid state inventory",
        )
        for row in data["states"]:
            key = row["security"], day(row["date"])
            require(
                key in expected and key not in self._states,
                "DAILY_EXECUTION_COVERAGE",
                "Duplicate or foreign state",
                {"security": key[0], "date": key[1]},
            )
            for field in FIELDS:
                fact = row[field]
                require(
                    fact["status"] in {"source_observed", "derived", "unavailable"}
                    and fact.get("historical_available_at") is None,
                    "DAILY_EXECUTION_SOURCE",
                    "Unqualified field or historical clock claim",
                )
                self._refs(fact["source_refs"])
                require(
                    (fact["value"] is None) == (fact["status"] == "unavailable"),
                    "DAILY_EXECUTION_VALUE",
                    "Unknown must remain null and unavailable",
                )
                require(
                    isinstance(fact["assumption_ids"], list)
                    and set(fact["assumption_ids"]) <= set(ASSUMPTIONS),
                    "DAILY_EXECUTION_SCHEMA",
                    "Unknown field assumption",
                )
                if fact["value"] is not None:
                    if field in ("is_st", "suspended"):
                        require(
                            type(fact["value"]) is bool,
                            "DAILY_EXECUTION_VALUE",
                            "Flag must be boolean",
                        )
                    else:
                        _number(fact["value"], positive=True)
            st, ref = row["is_st"]["value"], row["reference_price"]["value"]
            upper, lower = row["upper_limit"]["value"], row["lower_limit"]["value"]
            if upper is not None and lower is not None:
                require(_number(lower) < _number(upper), "DAILY_EXECUTION_VALUE", "Reversed limits")
            if any(row[f]["status"] == "derived" for f in ("upper_limit", "lower_limit")):
                require(
                    st is not None and ref is not None,
                    "DAILY_EXECUTION_VALUE",
                    "Cannot derive limits from unknown inputs",
                )
                require(
                    "2019-01-01" <= key[1] <= "2024-12-31"
                    and key[0].startswith(("000", "600", "601")),
                    "DAILY_EXECUTION_VALUE",
                    "Rule model outside bounded mainboard scope",
                )
                require(
                    (upper, lower) == standard_limits(ref, st)
                    and all(
                        "standard_mainboard_limits" in row[f]["assumption_ids"]
                        for f in ("upper_limit", "lower_limit")
                    ),
                    "DAILY_EXECUTION_VALUE",
                    "Unqualified or incorrect limit derivation",
                )
            self._states[key] = row
        missing = expected - set(self._states)
        require(
            not missing,
            "DAILY_EXECUTION_COVERAGE",
            "Missing state rows; preserve explicit unknowns",
            {"missing": [dict(security=s, date=d) for s, d in sorted(missing)]},
        )
        self._actions = {}
        base_actions = {
            (a["security"], a["ex_date"]): a for b in self._windows.values() for a in b["actions"]
        }
        for action in data["actions"]:
            key = action["security"], day(action["ex_date"])
            require(
                key in base_actions and key not in self._actions,
                "DAILY_EXECUTION_ACTION",
                "Duplicate or foreign action",
            )
            base = base_actions[key]
            for field in (
                "type",
                "announcement_date",
                "record_date",
                "cash_per_share",
                "bonus_share_ratio",
                "capitalization_share_ratio",
                "payment_date",
            ):
                require(
                    action.get(field) == base.get(field),
                    "DAILY_EXECUTION_ACTION",
                    "Action differs from base source",
                    {"field": field},
                )
            require(
                action.get("cash_available_at") is None,
                "DAILY_EXECUTION_ACTION",
                "Unproved cash availability must remain null",
            )
            self._refs(action["source_refs"])
            self._actions[key] = action
        require(
            set(self._actions) == set(base_actions), "DAILY_EXECUTION_ACTION", "Missing base action"
        )

    def _context(self, window_id):
        require(window_id in self._windows, "DAILY_EXECUTION_SCOPE", "Unknown fixed window")
        return self._windows[window_id]

    def _report(self, **extra):
        return dict(
            binding_sha256=self.binding_sha256,
            research_binding_sha256=self.research.binding_sha256,
            historical_pit=False,
            execution_permission=False,
            network_used=False,
            **extra,
        )

    def descriptor(self):
        return deepcopy(
            dict(
                producer=self._binding["producer"],
                purposes=sorted(PURPOSES),
                assumptions=ASSUMPTIONS,
                cash_credit_policies=CASH_POLICIES,
                windows=list(self._windows),
                report=self._report(),
            )
        )

    def quality(self):
        counts = {f: dict(Counter(r[f]["status"] for r in self._states.values())) for f in FIELDS}
        return deepcopy(
            dict(
                field_counts=counts,
                evidence_checks=self._data["quality"],
                states=len(self._states),
                report=self._report(),
            )
        )

    def preflight(self, window_id, *, purpose, accepted_assumptions=(), cash_credit_policy=None):
        """Return every blocker without deleting a security, session, or fixed window."""
        base = self._context(window_id)
        require(
            isinstance(purpose, str) and purpose in PURPOSES,
            "DAILY_EXECUTION_PURPOSE",
            "Unknown purpose",
        )
        require(
            isinstance(accepted_assumptions, (list, tuple, set, frozenset))
            and all(isinstance(x, str) for x in accepted_assumptions)
            and set(accepted_assumptions) <= set(ASSUMPTIONS),
            "DAILY_EXECUTION_ARGUMENT",
            "Unknown or malformed assumption selection",
        )
        require(
            cash_credit_policy is None
            or (isinstance(cash_credit_policy, str) and cash_credit_policy in CASH_POLICIES),
            "DAILY_EXECUTION_ARGUMENT",
            "Unsupported cash-credit policy",
        )
        accepted, issues, required = set(accepted_assumptions), [], set()
        w = base["window"]

        def issue(code, field, reason, security=None, date=None, refs=None):
            issues.append(
                dict(
                    code=code,
                    window=window_id,
                    security=security,
                    date=date,
                    field=field,
                    reason=reason,
                    source_refs=refs or [],
                    blocking=purpose != "signal_research",
                )
            )

        if purpose in {"historical_pit", "live_execution"}:
            issue(
                "HISTORICAL_DELIVERY_UNVERIFIED",
                "historical_available_at",
                "Receipt dates are current observations; no historical pre-open delivery or live entitlement is established.",
            )
        required.update(
            ("retrospective_daily_status", "shared_market_calendar", "scoped_action_absence")
        )
        for (security, label), row in sorted(self._states.items()):
            if not w["start"] <= label <= w["end"]:
                continue
            for field in FIELDS:
                fact = row[field]
                required.update(fact["assumption_ids"])
                if fact["value"] is None:
                    issue(
                        "FIELD_UNAVAILABLE",
                        field,
                        fact["reason"],
                        security,
                        label,
                        fact["source_refs"],
                    )
        for security, coverage in self._data["action_coverage"][window_id].items():
            if coverage["status"] == "unavailable":
                issue(
                    "ACTION_COVERAGE_UNAVAILABLE",
                    "corporate_actions",
                    coverage["reason"],
                    security,
                    w["start"],
                    coverage["source_refs"],
                )
        for action in self._actions.values():
            if not w["start"] <= action["ex_date"] <= w["end"]:
                continue
            security, label = action["security"], action["ex_date"]

            def action_issue(field, reason):
                issue(
                    "ACTION_TIMING_UNSUPPORTED",
                    field,
                    reason,
                    security,
                    label,
                    action["source_refs"],
                )

            i = w["sessions"].index(label)
            if action["record_date"] != w["sessions"][i - 1]:
                action_issue(
                    "record_date",
                    "Record/ex dates must be adjacent exchange sessions for this consumer contract",
                )
            if _number(action["cash_per_share"]) > 0:
                required.add("gross_cash_zero_withholding")
                if action["payment_date"] is None:
                    action_issue("payment_date", "Source payment date is unavailable")
                elif (
                    action["payment_date"] not in w["sessions"] or action["payment_date"] > w["end"]
                ):
                    action_issue(
                        "payment_date",
                        "Cash event requires settlement beyond the bound output window",
                    )
                if cash_credit_policy is None:
                    action_issue(
                        "cash_available_at",
                        "Source gives a payment date, not intraday spendability; explicitly choose a cash-credit policy",
                    )
            shares = _number(action["bonus_share_ratio"]) + _number(
                action["capitalization_share_ratio"]
            )
            if shares and (
                action.get("new_shares_credit_date") != label
                or action.get("new_shares_tradable_date") != label
            ):
                action_issue(
                    "new_shares_credit_date/new_shares_tradable_date",
                    "Nonzero shares need distinct source credit and tradable dates, both equal to ex-date in this contract",
                )
            if action.get("type") != "cash_dividend":
                action_issue("type", "Unsupported action type")
        if purpose != "signal_research":
            for assumption in sorted(required - accepted):
                issue("MODEL_NOT_ACCEPTED", assumption, ASSUMPTIONS[assumption])
        return deepcopy(
            dict(
                ready=not any(x["blocking"] for x in issues),
                purpose=purpose,
                issues=issues,
                required_assumptions=sorted(required),
                accepted_assumptions=sorted(accepted),
                cash_credit_policy=cash_credit_policy,
                securities=list(base["securities"]),
                output_sessions=w["output_sessions"],
                report=self._report(),
            )
        )

    def _admit(self, window_id, **options):
        report = self.preflight(window_id, **options)
        require(
            report["ready"],
            "DAILY_EXECUTION_NOT_READY",
            "Requested purpose has unresolved fields or models",
            report,
        )
        return report

    def read_window(self, window_id, *, purpose, accepted_assumptions=(), cash_credit_policy=None):
        """Engine input only: includes warmup; never expose this as strategy context."""
        admission = self._admit(
            window_id,
            purpose=purpose,
            accepted_assumptions=accepted_assumptions,
            cash_credit_policy=cash_credit_policy,
        )
        base = self._context(window_id)
        w = base["window"]
        actions = []
        for action in self._actions.values():
            if w["start"] <= action["ex_date"] <= w["end"]:
                a = deepcopy(action)
                a["model_cash_available_at"] = None
                if purpose == "retrospective_simulation" and _number(a["cash_per_share"]) > 0:
                    a["model_cash_available_at"] = (
                        a["payment_date"] + "T" + CASH_POLICIES[cash_credit_policy] + "+08:00"
                    )
                a["model_receivable_at"] = a["ex_date"] + "T09:00:00+08:00"
                actions.append(a)
        bars = [{k: v for k, v in r.items() if k != "research_state"} for r in base["bars"]]
        return deepcopy(
            dict(
                window=w,
                securities=base["securities"],
                bars=bars,
                execution_states=[
                    r for (s, d), r in sorted(self._states.items()) if w["start"] <= d <= w["end"]
                ],
                actions=actions,
                calendar=self._data["calendars"][window_id],
                action_coverage=self._data["action_coverage"][window_id],
                admission=admission,
                report=self._report(
                    strategy_visible=False,
                    simulation_admitted=purpose == "retrospective_simulation",
                ),
            )
        )

    def state(
        self, window_id, security, *, as_of, accepted_assumptions=(), cash_credit_policy=None
    ):
        """Accepted simulation state only, from 09:00; contains no current-day OHLCV."""
        base, at = self._context(window_id), timestamp(as_of).astimezone(TZ)
        label, w = at.date().isoformat(), base["window"]
        require(
            security in base["securities"]
            and w["start"] <= label <= w["end"]
            and (security, label) in self._states,
            "DAILY_EXECUTION_SCOPE",
            "Not a fixed output security/session",
        )
        require(at.time() >= time(9), "DAILY_EXECUTION_VISIBILITY", "State replay begins at 09:00")
        admission = self._admit(
            window_id,
            purpose="retrospective_simulation",
            accepted_assumptions=accepted_assumptions,
            cash_credit_policy=cash_credit_policy,
        )
        return deepcopy(
            dict(
                row=self._states[(security, label)],
                admission=admission,
                report=self._report(
                    as_of=at.isoformat(), model_available_at=label + "T09:00:00+08:00"
                ),
            )
        )

    def history(self, *args, **kwargs):
        return self.research.history(*args, **kwargs)

    def cash_state(
        self,
        window_id,
        security,
        ex_date,
        *,
        as_of,
        accepted_assumptions=(),
        cash_credit_policy=None,
    ):
        """Per-share cash schedule; a consumer multiplies by eligible record-date holdings."""
        base, at = self._context(window_id), timestamp(as_of).astimezone(TZ)
        key, w = (security, day(ex_date)), base["window"]
        require(
            key in self._actions
            and w["start"] <= key[1] <= w["end"]
            and w["start"] <= at.date().isoformat() <= w["end"],
            "DAILY_EXECUTION_SCOPE",
            "Action/clock outside the output window",
        )
        a = self._actions[key]
        require(_number(a["cash_per_share"]) > 0, "DAILY_EXECUTION_ACTION", "Not a cash event")
        require(
            a["announcement_date"] < at.date().isoformat(),
            "DAILY_EXECUTION_VISIBILITY",
            "Action not yet visible under the research clock",
        )
        self._admit(
            window_id,
            purpose="retrospective_simulation",
            accepted_assumptions=accepted_assumptions,
            cash_credit_policy=cash_credit_policy,
        )
        ex_at = datetime.fromisoformat(a["ex_date"] + "T09:00:00+08:00")
        credit_at = datetime.fromisoformat(
            a["payment_date"] + "T" + CASH_POLICIES[cash_credit_policy] + "+08:00"
        )
        return dict(
            cash_per_eligible_share=a["cash_per_share"],
            phase="not_ex" if at < ex_at else "receivable" if at < credit_at else "model_available",
            source_cash_available_at=None,
            model_cash_available_at=credit_at.isoformat(),
            report=self._report(),
        )

    def preflight_orders(
        self, window_id, requests, *, purpose, accepted_assumptions=(), cash_credit_policy=None
    ):
        """Gate explicit proposed orders; retain all other observations as warnings."""
        from ashare_data.research.order_inputs import preflight_orders

        return preflight_orders(
            self,
            window_id,
            requests,
            purpose=purpose,
            accepted_assumptions=accepted_assumptions,
            cash_credit_policy=cash_credit_policy,
        )

    def order_state(
        self, window_id, security, *, as_of, side, accepted_assumptions=(), cash_credit_policy=None
    ):
        """State permission applies only to this security/session/side, never the full window."""
        from ashare_data.research.order_inputs import order_state

        return order_state(
            self,
            window_id,
            security,
            as_of=as_of,
            side=side,
            accepted_assumptions=accepted_assumptions,
            cash_credit_policy=cash_credit_policy,
        )

    def order_limit_scenarios(
        self, window_id, security, *, as_of, side, accept_ordinary_scenarios=False
    ):
        """Explicit ordinary-limit sensitivity with unknown facts unchanged; no execution admission."""
        from ashare_data.research.order_inputs import order_limit_scenarios

        return order_limit_scenarios(
            self,
            window_id,
            security,
            as_of=as_of,
            side=side,
            accept_ordinary_scenarios=accept_ordinary_scenarios,
        )
