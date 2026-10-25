"""Read immutable reference APIs without replacing the selected library's prices."""

from decimal import Decimal, InvalidOperation
import hashlib
import inspect
from pathlib import Path

from ashare_data.core.model import DataError, digest, require
from ashare_data.corporate_actions.history import CorporateHistoryView
from ashare_data.reference.historical_market import HistoricalMarketView
from ashare_data.research.daily import DailyResearchView
from ashare_data.research.execution_inputs import ASSUMPTIONS, DailyExecutionView
from ashare_data.storage.immutable import identifier

KINDS = {
    "daily_execution": {"research": "daily_research", "execution": "daily_execution"},
    "historical_market": {"market": "historical_market"},
    "market_release": {"market": "market_release"},
}
FIELDS = ("suspended", "is_st", "reference_price", "upper_limit", "lower_limit")
LIMITATIONS = [
    "Source-scoped retrospective research; no historical PIT or live execution certification.",
    "Derived limits retain their explicit rule assumptions; exceptional overrides are unverified.",
    "Corporate coverage is limited to captured sources, not all-market absence proof.",
    "Corporate dates and gross cash require the consumer's explicit account and tax model.",
]


def gap(error, *, field="references", security=None, date=None):
    return dict(
        code=error.code,
        field=field,
        security=security,
        date=date,
        detail=error.message,
        owner_issue=error.as_dict(),
    )


def normalize(references):
    require(
        isinstance(references, dict),
        "PRODUCT_REFERENCE_MISSING",
        "Configure a reference set for ordinary research publication",
    )
    kind, name = references.get("kind"), references.get("name")
    require(
        isinstance(name, str) and 0 < len(name) <= 100 and kind in KINDS,
        "PRODUCT_REFERENCE_CONFIGURATION",
        "Invalid named reference set",
    )
    if kind == "market_release":
        pin = references.get("release")
        require(isinstance(pin, dict) and isinstance(pin.get("root"), (str, Path))
                and type(pin.get("release_id")) is int and pin["release_id"] > 0,
                "PRODUCT_REFERENCE_CONFIGURATION", "A pinned price release is required")
        frozen = dict(root=str(Path(pin["root"]).expanduser().resolve()),
                      release_id=pin["release_id"], manifest_sha256=identifier(pin.get("manifest_sha256")))
        bindings = {role: dict(kind="market_release", binding_sha256=frozen["manifest_sha256"])
                    for role in ("market", "corporate")}
        return {"name": name, "kind": kind}, {"release": frozen}, bindings
    required = {**KINDS[kind], "corporate": "corporate_history"}
    pins, bindings = {}, {}
    for role, asset_kind in required.items():
        item = references.get(role)
        require(
            isinstance(item, dict) and isinstance(item.get("binding_path"), (str, Path)),
            "PRODUCT_REFERENCE_MISSING",
            "Selected reference asset is missing",
            {"role": role},
        )
        pin = identifier(item.get("binding_sha256"))
        pins[role] = {
            "binding_path": str(Path(item["binding_path"]).expanduser().resolve()),
            "binding_sha256": pin,
        }
        bindings[role] = {"kind": asset_kind, "binding_sha256": pin}
    return {"name": name, "kind": kind}, pins, bindings


def _view(cls, pin, *args):
    return cls(*args, pin["binding_path"], expected_binding_sha256=pin["binding_sha256"])


def _read(operation, response, cls):
    return {
        "operation": operation,
        "response_sha256": digest(response),
        "owner_module_sha256": hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest(),
    }


def _daily(pins, securities, start, end, days, calendar):
    research = _view(DailyResearchView, pins["research"])
    execution = _view(DailyExecutionView, pins["execution"], research)
    descriptor = research.descriptor()
    available = [w for w in descriptor["windows"] if w["start"] <= start <= end <= w["end"]]
    require(
        available and set(securities) <= set(descriptor["securities"]),
        "PRODUCT_REFERENCE_SCOPE",
        "Selected securities/window exceed the chosen reference set",
        {
            "available_windows": [
                {k: w[k] for k in ("id", "start", "end")} for w in descriptor["windows"]
            ],
            "missing_securities": sorted(set(securities) - set(descriptor["securities"])),
        },
    )
    require(
        len(available) == 1,
        "PRODUCT_REFERENCE_AMBIGUOUS",
        "More than one evidence window covers this selection; choose an unambiguous package",
    )
    window = available[0]["id"]
    sessions = research.get_trade_days(window, start_date=calendar[0], end_date=calendar[-1])
    require(
        sessions["dates"] == calendar,
        "PRODUCT_CALENDAR_CONFLICT",
        "The main-library calendar differs from the selected reference calendar",
        {"main_sessions": calendar, "reference_sessions": sessions["dates"]},
    )
    requests = [
        {"security": sid, "date": day, "side": side}
        for sid in securities
        for day in days
        for side in ("BUY", "SELL")
    ]
    options = dict(
        purpose="retrospective_simulation",
        accepted_assumptions=sorted(ASSUMPTIONS),
        cash_credit_policy="pay_date_before_open",
    )
    admission = execution.preflight_orders(window, requests, **options)
    # The public signal export retains unknown observations without granting execution.
    # Its prices and old cash schedule are never substituted into the product input.
    raw = execution.read_window(window, purpose="signal_research")
    states = [
        r
        for r in raw["execution_states"]
        if r["security"] in securities and start <= r["date"] <= end
    ]
    evidence = dict(
        kind="daily_execution",
        window_id=window,
        rows=states,
        admission=admission,
        calendar=sessions,
        report=raw["report"],
        accepted_assumptions=sorted(ASSUMPTIONS),
        cash_credit_policy="pay_date_before_open",
        assumption_definitions=dict(ASSUMPTIONS),
        source_export_sha256=digest(raw),
        source_export_use="state rows only; snapshot prices and legacy cash schedule are not consumed",
        reads=[
            _read("DailyResearchView.descriptor", descriptor, DailyResearchView),
            _read("DailyResearchView.get_trade_days", sessions, DailyResearchView),
            _read("DailyExecutionView.preflight_orders", admission, DailyExecutionView),
            _read("DailyExecutionView.read_window", raw, DailyExecutionView),
        ],
    )
    gaps = [
        {**issue, "detail": issue.get("reason", "Reference admission failed"), "owner_issue": issue}
        for issue in admission["issues"]
        if issue.get("blocking")
    ]
    return evidence, gaps


def _historical(pins, securities, start, end, days, calendar):
    view = _view(HistoricalMarketView, pins["market"])
    states, admissions, gaps = [], [], []
    for sid in securities:
        for day in days:
            state = None
            for side in ("BUY", "SELL"):
                try:
                    response = view.order_state(
                        sid,
                        as_of=day + "T09:00:00+08:00",
                        side=side,
                        accept_daily_replay=True,
                        accept_limit_model=True,
                    )
                    state = response["state"]
                    claimed = state.get("source_claimed_limits", {})
                    if side == "BUY" and claimed.get("validation") == "conflict_with_rule_model":
                        gaps.append(
                            dict(
                                code="PRODUCT_LIMIT_SOURCE_CONFLICT",
                                security=sid,
                                date=day,
                                field="price_limits",
                                detail="Captured source limits conflict with the selected rule model",
                                owner_issue=claimed,
                            )
                        )
                    admissions.append(
                        {
                            "security": sid,
                            "date": day,
                            **{k: v for k, v in response.items() if k != "state"},
                            "state_sha256": digest(state),
                            "response_sha256": digest(response),
                        }
                    )
                    for issue in response["issues"]:
                        # A known suspension is valid data; the account must refuse trading.
                        if issue != "DAILY_SUSPENDED":
                            gaps.append(
                                dict(
                                    code=issue,
                                    security=sid,
                                    date=day,
                                    field="state",
                                    detail="HistoricalMarketView.order_state refused this use",
                                    owner_issue=issue,
                                )
                            )
                except DataError as exc:
                    gaps.append(gap(exc, field="state", security=sid, date=day))
            if state is not None:
                states.append(state)
    descriptor = view.descriptor()
    return dict(
        kind="historical_market",
        rows=states,
        admissions=admissions,
        accepted_assumptions={"accept_daily_replay": True, "accept_limit_model": True},
        reads=[_read("HistoricalMarketView.descriptor", descriptor, HistoricalMarketView)],
        descriptor=descriptor,
    ), gaps


def _compatible(rows, states, start):
    gaps, indexed = [], {(r["security"], r["date"]): r for r in states}
    for row in rows:
        if row["date"] < start:
            continue
        sid, date = row["security"], row["date"]
        state = indexed.get((sid, date))
        if state is None:
            gaps.append(
                dict(
                    code="PRODUCT_STATE_MISSING",
                    security=sid,
                    date=date,
                    field="state",
                    detail="No source state exists for this security/session",
                )
            )
            continue
        for field in FIELDS:
            fact = state[field]
            if field == "is_st":
                continue  # Its necessity is determined by the public order-use gate.
            value = fact.get("value")
            valid = type(value) is bool if field == "suspended" else value is not None
            if not valid or fact["status"] in {"unavailable", "not_applicable"}:
                gaps.append(
                    dict(
                        code="PRODUCT_STATE_UNAVAILABLE",
                        security=sid,
                        date=date,
                        field=field,
                        detail="Unknown or unlimited state is unsupported",
                        owner_issue=fact,
                    )
                )
        checks = [
            ("suspended", row.get("tradestatus"), {"0": True, "1": False}),
            ("is_st", row.get("is_st"), {"0": False, "1": True}),
        ]
        for field, value, mapping in checks:
            reference = state[field]["value"]
            if value in mapping and reference is not None and mapping[value] != reference:
                gaps.append(
                    dict(
                        code="PRODUCT_SOURCE_STATE_CONFLICT",
                        security=sid,
                        date=date,
                        field=field,
                        detail="Main-library and reference status disagree",
                        main_value=value,
                        reference_fact=state[field],
                    )
                )
        try:
            if row.get("preclose") is not None and state["reference_price"]["value"] is not None:
                if Decimal(row["preclose"]) != Decimal(state["reference_price"]["value"]):
                    gaps.append(
                        dict(
                            code="PRODUCT_REFERENCE_PRICE_CONFLICT",
                            security=sid,
                            date=date,
                            field="reference_price",
                            detail="Main preclose differs from reference price",
                            main_value=row["preclose"],
                            reference_fact=state["reference_price"],
                        )
                    )
            if state["suspended"]["value"] is True and Decimal(str(row.get("volume"))) > 0:
                gaps.append(
                    dict(
                        code="PRODUCT_SOURCE_STATE_CONFLICT",
                        security=sid,
                        date=date,
                        field="suspended",
                        detail="Reference suspension conflicts with observed positive volume",
                    )
                )
            if (
                state["upper_limit"]["value"] is not None
                and state["lower_limit"]["value"] is not None
            ):
                lower = Decimal(str(state["lower_limit"]["value"]))
                upper = Decimal(str(state["upper_limit"]["value"]))
                if any(
                    not lower <= Decimal(str(row[field])) <= upper
                    for field in ("open", "high", "low", "close")
                ):
                    gaps.append(
                        dict(
                            code="PRODUCT_PRICE_LIMIT_CONFLICT",
                            security=sid,
                            date=date,
                            field="daily",
                            detail="Observed raw OHLC exceeds the selected reference limit band",
                        )
                    )
        except (InvalidOperation, TypeError, ValueError):
            gaps.append(
                dict(
                    code="PRODUCT_REFERENCE_VALUE",
                    security=sid,
                    date=date,
                    field="reference_price",
                    detail="Source compatibility comparison has invalid values",
                )
            )
    return gaps


def assess(references, securities, start, end, rows, calendar, *, price_release=None):
    """Return source-bound state and the original corporate response; never write."""
    selection, pins, bindings = normalize(references)
    if selection["kind"] == "market_release":
        return _released(selection, pins, bindings, securities, start, end, rows, calendar, price_release)
    days = [d for d in calendar if start <= d <= end]
    gaps, market, schedule = [], None, None
    try:
        market, issues = (_daily if selection["kind"] == "daily_execution" else _historical)(
            pins, securities, start, end, days, calendar
        )
        gaps.extend(issues)
        gaps.extend(_compatible(rows, market["rows"], start))
    except DataError as exc:
        gaps.append(gap(exc, field="market_reference"))
    try:
        view = _view(CorporateHistoryView, pins["corporate"])
        schedule = view.export_aq(
            securities,
            start=start,
            end=end,
            tax_model="gross_cash_no_tax",
            accept_source_scoped_coverage=True,
        )
        gaps.extend(
            dict(
                code="PRODUCT_CORPORATE_GAP",
                field="corporate_actions",
                detail=i.get("reason", "Corporate source gap"),
                owner_issue=i,
            )
            for i in schedule["issues"]
        )
    except DataError as exc:
        gaps.append(gap(exc, field="corporate_actions"))
    evidence = dict(
        schema="ad-product-reference-evidence.v1",
        scope={"securities": securities, "start": start, "end": end},
        reference_set=selection,
        source_bindings=bindings,
        market=market,
        corporate={
            "binding_sha256": pins["corporate"]["binding_sha256"],
            "response_sha256": digest(schedule) if schedule is not None else None,
        },
    )
    reads = list(market["reads"]) if market else []
    if schedule is not None:
        reads.append(_read("CorporateHistoryView.export_aq", schedule, CorporateHistoryView))
    return evidence, schedule, gaps, reads


def _released(selection, pins, bindings, securities, start, end, rows, calendar, price_release):
    from ashare_data.product.released_corporate import ReleasedCorporateView
    from ashare_data.product.released_references import ReleasedReferences

    reader = ReleasedReferences(pins["release"], price_release)
    try:
        days = [d for d in calendar if start <= d <= end]
        market, gaps = reader.market(securities, start, end, days, rows)
        gaps.extend(_compatible(rows, market["rows"], start))
        view = ReleasedCorporateView(reader, securities, start, end)
        schedule = view.export_aq(securities, start=start, end=end, tax_model="gross_cash_no_tax",
                                  accept_source_scoped_coverage=True)
        events = {row["event_id"]: row for row in schedule["rows"]}
        gaps.extend(dict(code="PRODUCT_CORPORATE_GAP", field=i.get("field", "corporate_actions"),
                         security=i.get("security") or events.get(i.get("event_id"), {}).get("security"),
                         date=events.get(i.get("event_id"), {}).get("ex_date"),
                         detail=i.get("reason", "Corporate source gap"), owner_issue=i)
                    for i in schedule["issues"])
        market["reads"] = [_read("ReleasedReferences.market", market, ReleasedReferences)]
        evidence = dict(schema="ad-product-reference-evidence.v1",
                        scope=dict(securities=securities, start=start, end=end),
                        reference_set=selection, source_bindings=bindings, market=market,
                        corporate=dict(binding_sha256=reader.release.sha256,
                                       response_sha256=digest(schedule)))
        return evidence, schedule, gaps, [*market["reads"],
                _read("ReleasedCorporateView.export_aq", schedule, ReleasedCorporateView)]
    finally:
        reader.close()
