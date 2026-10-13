"""Build a bounded execution supplement from captured public evidence, offline.

No original price data is copied or downloaded. The output separately pins the
existing DailyResearchView binding and all supplemental evidence bytes.
"""

import argparse
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ashare_data import DailyResearchView
from ashare_data.daily_execution import DATA_SCHEMA, SCHEMA, DailyExecutionView, standard_limits
from ashare_data.daily_research import ROLE

COMMIT = "c9e9soth3s4j6kcq06p3f7nvt88pi319"


def check(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(path.read_text())


def encode(value):
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fixed(path, raw):
    if path.exists():
        check(path.read_bytes() == raw, "Refusing to replace fixed output: " + str(path))
    else:
        with path.open("xb") as stream:
            stream.write(raw)


def fact(value, status, refs, reason, assumptions=()):
    return dict(
        value=value,
        status=status,
        source_refs=refs,
        reason=reason,
        assumption_ids=list(assumptions),
        historical_available_at=None,
    )


def archive(root, name, table):
    doc = load(root / "raw" / name)
    check(
        doc["repository_owner"] == "chenditc"
        and doc["repository_name"] == "investment_data"
        and doc["query_execution_status"] == "Success"
        and f"FROM {table} AS OF '{COMMIT}'" in doc["sql_query"],
        "Archive identity/query mismatch",
    )
    receipt = load(root / "raw" / (name + ".receipt.json"))
    check(
        receipt["status"] == 200 and receipt["sha256"] == sha((root / "raw" / name).read_bytes()),
        "Archive receipt mismatch",
    )
    return doc["rows"]


def canonical(code):
    check(len(code) == 8 and code[:2] in ("SH", "SZ") and code[2:].isdigit(), "Bad archive symbol")
    return code[2:] + (".XSHE" if code[:2] == "SZ" else ".XSHG")


def official_name_history(root, security):
    """No current-name projection. A known dated flag is extended through dated changes."""
    name = f"szse-names-{security[:6]}.json"
    doc = load(root / "raw" / name)
    receipt = load(root / "raw" / (name + ".receipt.json"))
    query = parse_qs(urlparse(receipt["url"]).query)
    check(
        receipt.get("status") == 200
        and urlparse(receipt["url"]).hostname == "www.szse.cn"
        and query.get("CATALOGID") == ["SSGSGMXX"]
        and query.get("TABKEY") == ["tab2"]
        and query.get("txtZqdm") == [security[:6]]
        and query.get("txtZzrq") == ["2024-03-29"]
        and receipt["sha256"] == sha((root / "raw" / name).read_bytes()),
        "Name-history scope mismatch",
    )
    tab = next(x for x in doc if x["metadata"]["tabkey"] == "tab2")
    check(
        tab["error"] is None
        and tab["metadata"]["catalogid"] == "SSGSGMXX"
        and tab["metadata"]["recordcount"] == len(tab["data"])
        and tab["metadata"]["pagecount"] <= 1,
        "Incomplete official name history",
    )
    check(
        all(x["zqdm"] == security[:6] and x["bgrq"] <= "2024-03-29" for x in tab["data"]),
        "Foreign/date-escaped official name history",
    )
    return tab["data"], "raw/" + name


def calendar_checks(research, root):
    # Only closures overlapping captured ranges, transcribed from bound official notices.
    # This builds a CHECK, not a delivered weekday calendar. Actual sessions stay byte-identical.
    closures = [
        ("2019-09-13", "2019-09-15", "2019"),
        ("2019-10-01", "2019-10-07", "2019"),
        ("2020-01-01", "2020-01-01", "2020"),
        ("2020-01-24", "2020-01-30", "2020"),
        ("2020-01-31", "2020-02-02", "2020-extension"),
        ("2020-04-04", "2020-04-06", "2020"),
        ("2020-05-01", "2020-05-05", "2020"),
        ("2021-09-19", "2021-09-21", "2021"),
        ("2021-10-01", "2021-10-07", "2021"),
        ("2022-01-01", "2022-01-03", "2022"),
        ("2022-01-31", "2022-02-06", "2022"),
        ("2022-04-03", "2022-04-05", "2022"),
        ("2022-04-30", "2022-05-04", "2022"),
        ("2023-09-29", "2023-10-06", "2023"),
        ("2024-01-01", "2024-01-01", "2024"),
        ("2024-02-09", "2024-02-18", "2024"),
    ]

    def ref(label):
        return (
            "raw/sse-2020-extension.html"
            if label == "2020-extension"
            else f"raw/sse-{label}-holidays.html"
        )

    for _, _, label in closures:
        text = (root / ref(label)).read_text()
        check("休市" in text and "上海证券交易所" in text, "Unrecognized official holiday evidence")
    results = {}
    for window in research.descriptor()["windows"]:
        wid = window["id"]
        archive_doc = load(research.path.parent / "raw" / f"dolt-calendar-SSE-{wid}.json")
        rows, differences = archive_doc["rows"], []
        refs = {f"base:raw/dolt-calendar-SSE-{wid}.json", "base:raw/sina-calendar-decoded.json"}
        excluded = []
        for row in rows:
            d = date.fromisoformat(row["date"])
            matches = [(a, b, label) for a, b, label in closures if a <= row["date"] <= b]
            closed = bool(matches) or d.weekday() >= 5
            if closed != (row["is_open_hex"] == "30"):
                differences.append(row["date"])
            if d.weekday() < 5 and matches:
                excluded.append(row["date"])
            refs.update(ref(x[2]) for x in matches)
        check(not differences, "Unexplained calendar differences: " + str(differences))
        if wid == "w2024":
            refs.update(
                f"base:raw/szse-{month}.json"
                for month in ["2023-10", "2023-11", "2023-12", "2024-01", "2024-02", "2024-03"]
            )
        results[wid] = dict(
            sessions=window["sessions"],
            natural_days_checked=len(rows),
            weekday_closures=excluded,
            mismatches=differences,
            source_refs=sorted(refs),
            classification="official_holiday_corroborated_real_sessions",
            xshe_scope="shared mainland sessions, plus independent SZSE traded dates and six direct official months; full official per-day feed not claimed",
            audit_only_weekday_rule=True,
            delivered_sessions_generated=False,
            historical_calendar_publication_clock=None,
        )
    return results


def build(research, root):
    states, intervals, reference_checks, limit_checks, names = [], [], [], [], {}
    base_exports = {
        w["id"]: research.read_window(w["id"], role=ROLE) for w in research.descriptor()["windows"]
    }
    status_by_window = {}
    for wid, base in base_exports.items():
        raw_rows = archive(root, f"dolt-status-{wid}.json", "bao_a_stock_eod_info")
        statuses = {}
        for r in raw_rows:
            key = canonical(r["symbol"]), r["tradedate"]
            check(
                key not in statuses
                and key[0] in base["securities"]
                and base["window"]["start"] <= key[1] <= base["window"]["end"],
                "Foreign/duplicate status row",
            )
            check(
                r["is_st"] in ("0", "1") and r["tradestatus"] in ("0", "1"), "Unknown source flags"
            )
            statuses[key] = r
        status_by_window[wid] = statuses
    for security in base_exports["w2024"]["securities"]:
        if security.endswith("XSHE"):
            history, ref = official_name_history(root, security)
            anchor = status_by_window["w2022"].get((security, "2022-05-31"))
            check(anchor and anchor["is_st"] == "0", "Missing dated non-ST anchor")
            # Current frozen securities have no later changes; future generic histories need explicit classification.
            later = [r for r in history if r["bgrq"] > "2022-05-31"]
            check(not later, "Name history changed; do not silently infer a normal status")
            names[security] = ref
    for wid, base in base_exports.items():
        w = base["window"]
        statuses = status_by_window[wid]
        for security in base["securities"]:
            records = [
                b
                for b in base["bars"]
                if b["security"] == security and w["start"] <= b["date"] <= w["end"]
            ]
            for bar in records:
                label = bar["date"]
                source = statuses.get((security, label))
                source_ref = "raw/dolt-status-" + wid + ".json"
                source_reason = "Current capture of historical BaoStock-labelled daily archive at fixed commit; source delivery clock is unknown"
                refs = [source_ref, "raw/baostock-fields-post.md"]
                if source:
                    st = fact(source["is_st"] == "1", "source_observed", refs, source_reason)
                    suspended = fact(
                        source["tradestatus"] == "0", "source_observed", refs, source_reason
                    )
                    reference = fact(source["preclose"], "source_observed", refs, source_reason)
                    check(
                        suspended["value"] == bar["suspended"],
                        "Independent daily status conflicts with raw trading evidence",
                    )
                    expected = bar["native_prevclose_marker"] or bar["previous_raw_close"]
                    check(
                        Decimal(source["preclose"]) == Decimal(expected),
                        "Independent preclose differs from base",
                    )
                    reference_checks.append(
                        dict(
                            security=security, date=label, reference=source["preclose"], agrees=True
                        )
                    )
                else:
                    if wid == "w2024" and security in names:
                        st = fact(
                            False,
                            "derived",
                            [names[security], "raw/dolt-status-w2022.json"],
                            "Known non-ST on 2022-05-31; complete official security-specific name-change query has no subsequent change through 2024-03-29; retrospective interval inference",
                            ("dated_name_history_status",),
                        )
                    else:
                        st = fact(
                            None,
                            "unavailable",
                            [source_ref, "raw/sse-risk-list.html.receipt.json"]
                            if wid == "w2024"
                            else [source_ref],
                            "Fixed BaoStock archive returns no 2024 rows; no independently verified dated Shanghai risk-warning interval acquired. Current names and normal volume cannot replace it."
                            if wid == "w2024"
                            else "No matching archived daily status; a later dated anchor cannot be projected backwards.",
                        )
                    suspended = fact(
                        bar["suspended"],
                        "derived",
                        ["base:" + bar["source_ref"]],
                        "Positive observed volume excludes a full-day halt retrospectively, not intraday halts",
                        ("retrospective_daily_status",),
                    )
                    reference = fact(
                        bar["native_prevclose_marker"] or bar["previous_raw_close"],
                        "derived",
                        [
                            "base:" + bar["source_ref"],
                            "base:" + base["action_coverage"][security]["source_ref"],
                        ],
                        "Source marker, otherwise prior raw close under scoped no-action check",
                        ("scoped_action_absence",),
                    )
                row = dict(
                    security=security,
                    date=label,
                    is_st=st,
                    suspended=suspended,
                    reference_price=reference,
                )
                if st["value"] is None:
                    for field in ("upper_limit", "lower_limit"):
                        row[field] = fact(
                            None,
                            "unavailable",
                            st["source_refs"],
                            "Unknown ST/risk-warning status prevents ordinary-limit derivation",
                        )
                else:
                    upper, lower = standard_limits(reference["value"], st["value"])
                    for field, value in [("upper_limit", upper), ("lower_limit", lower)]:
                        row[field] = fact(
                            value,
                            "derived",
                            sorted(
                                set(
                                    reference["source_refs"]
                                    + st["source_refs"]
                                    + ["raw/szse-rules-2021.pdf"]
                                )
                            ),
                            "Explicit ordinary-mainboard rule replay; no daily exchange override feed or strict historical rule-state certification",
                            ("standard_mainboard_limits",),
                        )
                    check(
                        Decimal(lower)
                        <= Decimal(bar["low"])
                        <= Decimal(bar["high"])
                        <= Decimal(upper),
                        f"Observed OHLC crosses derived band: {security} {label}",
                    )
                states.append(row)
            cell = [
                r
                for r in states
                if r["security"] == security and w["start"] <= r["date"] <= w["end"]
            ]
            intervals.append(
                dict(
                    window=wid,
                    security=security,
                    first_session=w["start"],
                    last_session=w["end"],
                    observations=len(cell),
                    values=sorted({str(r["is_st"]["value"]) for r in cell}),
                    origins=sorted({r["is_st"]["status"] for r in cell}),
                    effective_period_scope="Only these bound output sessions; no interpolation across disjoint windows",
                    source_refs=sorted({p for r in cell for p in r["is_st"]["source_refs"]}),
                )
            )
    by_key = {(r["security"], r["date"]): r for r in states}
    # A small audit is allowed; never promote this mixed/reconstructed archive table to raw exchange data.
    for filename in ["dolt-gree-final_a_stock_limit.json", "dolt-limit-fixed-points.json"]:
        if not (root / "raw" / filename).exists():
            continue
        for r in archive(root, filename, "final_a_stock_limit"):
            key = canonical(r["symbol"]), r["tradedate"]
            check(key in by_key, "Unbound limit audit point")
            row = by_key[key]
            check(
                all(
                    Decimal(r[source_field]) == Decimal(row[field]["value"])
                    for source_field, field in [
                        ("pre_close", "reference_price"),
                        ("up_limit", "upper_limit"),
                        ("down_limit", "lower_limit"),
                    ]
                ),
                "Archive limit point contradicts rule reconstruction",
            )
            limit_checks.append(
                dict(
                    security=key[0],
                    date=key[1],
                    source_ref="raw/" + filename,
                    agrees=True,
                    source_grade="mixed_source_and_reconstructed_archive_not_raw_exchange",
                )
            )
    actions, action_coverage = [], {}
    for wid, base in base_exports.items():
        action_coverage[wid] = {}
        for security in base["securities"]:
            coverage = base["action_coverage"][security]
            f = coverage["factor_step_crosscheck"][wid]
            check(f["agrees"] is True, "Action/factor coverage conflicts")
            check(
                not any(
                    base["window"]["start"] <= r[4] <= base["window"]["end"]
                    for r in coverage["rights_rows"]
                    if len(r) > 4 and len(r[4]) == 10
                ),
                "Potential rights event needs manual source review",
            )
            action_coverage[wid][security] = dict(
                status="scoped_checked",
                event_count=coverage["window_counts"][wid],
                source_refs=["base:" + coverage["source_ref"], "base:binding-data.json"],
                reason="Captured provider cash/share/rights ledger and factor boundaries; independent daily references corroborate 2020/2022; full issuer announcement chain not claimed",
            )
        for action in base["actions"]:
            a = deepcopy(action)
            a["source_refs"] = ["base:" + action["source_ref"]]
            a["cash_available_at"] = None
            a["new_shares_credit_date"] = None
            a["share_timing_status"] = (
                "not_applicable_explicit_zero_shares"
                if all(
                    Decimal(a[f]) == 0 for f in ("bonus_share_ratio", "capitalization_share_ratio")
                )
                else "requires_separate_credit_and_tradability_evidence"
            )
            actions.append(a)
    calendars = calendar_checks(research, root)
    data = dict(
        schema=DATA_SCHEMA,
        states=states,
        actions=actions,
        action_coverage=action_coverage,
        calendars=calendars,
        quality=dict(
            status_intervals=intervals,
            independent_reference_rows=len(reference_checks),
            reference_mismatches=[],
            limit_audit_points=limit_checks,
            complete_daily_exchange_limit_feed=False,
            source_limit_methodology="raw/investment-limit-doc.md",
            historical_status_delivery_verified=False,
            continuous_full_history_required=False,
        ),
    )
    fixed(root / "execution-data.json", encode(data))
    refs = []
    # Preserve failures too: they explain the precise unavailable cells without defaulting them to normal.
    for p in sorted(
        [root / "execution-data.json"]
        + list((root / "raw").glob("*"))
        + list((root / "derived").glob("*"))
    ):
        if p.is_file():
            refs.append(
                dict(
                    path=p.relative_to(root).as_posix(),
                    bytes=p.stat().st_size,
                    sha256=sha(p.read_bytes()),
                )
            )
    binding = dict(
        schema=SCHEMA,
        historical_pit=False,
        execution_permission=False,
        research_binding_sha256=research.binding_sha256,
        producer=dict(name="ashare-data", version="0.9.1.dev12", source_commit=COMMIT),
        data="execution-data.json",
        files=refs,
    )
    raw = encode(binding)
    fixed(root / "binding.json", raw)
    view = DailyExecutionView(research, root / "binding.json", expected_binding_sha256=sha(raw))
    fixed(root / "reports" / "quality.json", encode(view.quality()))
    fixed(root / "reports" / "reference-checks.json", encode(reference_checks))
    fixed(root / "reports" / "calendar-checks.json", encode(calendars))
    return sha(raw)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("research_binding", type=Path)
    p.add_argument("supplement_root", type=Path)
    p.add_argument("--research-sha256", required=True)
    args = p.parse_args()
    research = DailyResearchView(
        args.research_binding, expected_binding_sha256=args.research_sha256
    )
    print(build(research, args.supplement_root.resolve()))
