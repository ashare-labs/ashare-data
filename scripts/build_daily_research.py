"""Build a local daily research package from captured inputs; never downloads.

The input directory contains strategy-manifest.json, raw source bytes/receipts,
bounded decoded daily files and an explicit facts.json. See the contract docs.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from ashare_data import DataError
from ashare_data.daily_research import DailyResearchView, ROLE, SCHEMA

COMMIT = "c9e9soth3s4j6kcq06p3f7nvt88pi319"
BASELINE = "090a623dcc27f3a086696989ea15aa1cb529b0de"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def write_fixed(path, content):
    if path.exists():
        check(path.read_bytes() == content, f"Refusing to replace fixed output: {path.name}")
    else:
        with path.open("xb") as stream:
            stream.write(content)


def load(path):
    return json.loads(path.read_text())


def ref(path, root):
    raw = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "sha256": sha(raw), "bytes": len(raw)}


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.table = None
        self.tables = {}
        self.row = []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table" and attrs.get("id") in {
            "sharebonus_1",
            "sharebonus_2",
            "comInfo1",
            "sharebonusdetail",
        }:
            self.table = attrs["id"]
            self.tables[self.table] = []
        if self.table and tag == "tr":
            self.row = []
        if self.table and tag in {"td", "th"}:
            self.cell = ""

    def handle_data(self, text):
        if self.cell is not None:
            self.cell += text

    def handle_endtag(self, tag):
        if self.table and tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join(self.cell.split()))
            self.cell = None
        if self.table and tag == "tr":
            self.tables[self.table].append(self.row)
        if self.table and tag == "table":
            self.table = None


def parse_tables(path, code):
    text = path.read_bytes().decode("gb18030")
    identities = re.findall(r'var\s+(?:page_symbol|fullcode)\s*=\s*"([a-z]{2}\d{6})"\s*;', text)
    check(
        identities and set(identities) == {code},
        f"Missing or incorrect HTML security identity: {path.name}",
    )
    p = Tables()
    p.feed(text)
    return p.tables


def build(root, output="binding.json"):
    check(
        re.fullmatch(r"binding(?:-[a-z0-9-]+)?\.json", output),
        "Output must be binding.json or binding-<label>.json inside the package",
    )
    raw, derived = root / "raw", root / "derived"
    contract = load(root / "strategy-manifest.json")
    facts = load(root / "facts.json")
    check(
        contract["baselines"]["ad"] == BASELINE,
        "Source validation failed: contract['baselines']['ad'] == BASELINE",
    )
    check(
        contract["data_requirements"]["warmup_open_sessions_before_start"] >= 120,
        "Source validation failed: contract['data_requirements']['warmup_open_sessions_before_start'] >= 120",
    )
    calendar = load(raw / "sina-calendar-decoded.json")
    check(
        calendar["raw_sha256"] == sha((raw / "sina-calendar.txt").read_bytes()),
        "Source validation failed: calendar['raw_sha256'] == sha((raw / 'sina-calendar.txt').read_bytes())",
    )
    check(
        calendar["decoder_sha256"] == sha((raw / "sina-klc-decoder.js").read_bytes()),
        "Source validation failed: calendar['decoder_sha256'] == sha((raw / 'sina-klc-decoder.js').read_bytes())",
    )
    dates = calendar["dates"]
    check(
        dates == sorted(set(dates)) and calendar["synthetic_dates_added"] == 0,
        "Source validation failed: dates == sorted(set(dates)) and calendar['synthetic_dates_added'] == 0",
    )
    windows, calendar_checks = [], []
    for cw in contract["windows"]:
        archive = load(raw / f"dolt-calendar-SSE-{cw['id']}.json")
        check(
            archive["repository_owner"] == "chenditc"
            and archive["repository_name"] == "investment_data",
            "Source validation failed: archive['repository_owner'] == 'chenditc' and archive['repository_name'] == 'investment_data'",
        )
        check(
            f"AS OF '{COMMIT}'" in archive["sql_query"]
            and archive["query_execution_status"] == "Success",
            "Source validation failed: f\"AS OF '{COMMIT}'\" in archive['sql_query'] and archive['query_execution_status'] == 'Success'",
        )
        rows = archive["rows"]
        check(rows and len(rows) < 400, "Source validation failed: rows and len(rows) < 400")
        first, last = date.fromisoformat(rows[0]["date"]), date.fromisoformat(rows[-1]["date"])
        check(
            [r["date"] for r in rows]
            == [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)],
            "Source validation failed: [r['date'] for r in rows] == [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]",
        )
        check(
            all((r["exchange"] == "SSE" and r["is_open_hex"] in ("30", "31") for r in rows)),
            "Source validation failed: all((r['exchange'] == 'SSE' and r['is_open_hex'] in ('30', '31') for r in rows))",
        )
        check(
            all(((r["date"] in dates) == (r["is_open_hex"] == "31") for r in rows)),
            "Source validation failed: all(((r['date'] in dates) == (r['is_open_hex'] == '31') for r in rows))",
        )
        warm = [d for d in dates if d < cw["start"]][
            -contract["data_requirements"]["warmup_open_sessions_before_start"] :
        ]
        out = [d for d in dates if cw["start"] <= d <= cw["end"]]
        following = next(d for d in dates if d > cw["end"])
        check(
            rows[0]["date"] <= warm[0] and rows[-1]["date"] >= following,
            "Source validation failed: rows[0]['date'] <= warm[0] and rows[-1]['date'] >= following",
        )
        windows.append(
            dict(
                id=cw["id"],
                start=cw["start"],
                end=cw["end"],
                warmup_start=warm[0],
                warmup_sessions=len(warm),
                output_sessions=len(out),
                next_session=following,
                sessions=warm + out + [following],
                calendar_origin="SSE-labelled public archive and Sina agree",
                xshe_calendar="explicit shared-session research assumption; partial direct SZSE corroboration",
            )
        )
        calendar_checks.append(
            dict(window=cw["id"], archive_natural_days=len(rows), agreement=True)
        )
    official_checks = []
    for p in sorted(raw.glob("szse-????-??.json")):
        doc = load(p)
        check(
            doc["data"] and all((x["jybz"] in ("0", "1") for x in doc["data"])),
            "Source validation failed: doc['data'] and all((x['jybz'] in ('0', '1') for x in doc['data']))",
        )
        check(
            all(((x["jyrq"] in dates) == (x["jybz"] == "1") for x in doc["data"])),
            "Source validation failed: all(((x['jyrq'] in dates) == (x['jybz'] == '1') for x in doc['data']))",
        )
        official_checks.append(
            dict(month=p.stem[5:], source=str(p.relative_to(root)), days=len(doc["data"]))
        )
    scope = sorted({d for w in windows for d in w["sessions"]})
    bars, factors, listing, actions, action_coverage, coverage, audits = [], {}, [], [], {}, [], []
    decoder_sha = sha((raw / "sina-klc-decoder.js").read_bytes())
    for security in contract["securities"]:
        code = ("sz" if security.endswith("XSHE") else "sh") + security[:6]
        source_ref = f"raw/{code}-daily.js"
        decoded = load(derived / f"{code}-daily.json")
        check(
            decoded["security"] == security and decoded["decoder_sha256"] == decoder_sha,
            "Source validation failed: decoded['security'] == security and decoded['decoder_sha256'] == decoder_sha",
        )
        check(
            decoded["raw_sha256"] == sha((root / source_ref).read_bytes()),
            "Source validation failed: decoded['raw_sha256'] == sha((root / source_ref).read_bytes())",
        )
        source_rows = {r["date"][:10]: r for r in decoded["rows"]}
        check(
            len(source_rows) == len(decoded["rows"]),
            "Source validation failed: len(source_rows) == len(decoded['rows'])",
        )
        # Original source strings remain in the parsed HTML table/JSON, not zero-filled.
        tables = parse_tables(raw / f"{code}-actions.html", code)
        dividends = [
            r
            for r in tables["sharebonus_1"]
            if len(r) == 9 and re.fullmatch(r"\d{4}-\d\d-\d\d", r[0]) and r[4] == "实施"
        ]
        rights = [r for r in tables["sharebonus_2"] if r and re.fullmatch(r"\d{4}-\d\d-\d\d", r[0])]
        ledger = []
        for r in dividends:
            if not any(w["warmup_start"] <= r[5] <= w["next_session"] for w in windows):
                continue
            check(
                all((re.fullmatch("\\d+(?:\\.\\d+)?", v) for v in r[1:4])),
                "Source validation failed: all((re.fullmatch('\\\\d+(?:\\\\.\\\\d+)?', v) for v in r[1:4]))",
            )
            event = dict(
                security=security,
                type="cash_dividend",
                announcement_date=r[0],
                ex_date=r[5],
                record_date=r[6],
                cash_per_share=str(Decimal(r[3]) / 10),
                bonus_share_ratio=str(Decimal(r[1]) / 10),
                capitalization_share_ratio=str(Decimal(r[2]) / 10),
                payment_date=None,
                new_shares_tradable_date=None if r[7] == "--" else r[7],
                raw_fields=r,
                origin="provider_event_ledger",
                source_ref=f"raw/{code}-actions.html",
                historical_available_at=None,
                payment_time=None,
                source_revision=None,
            )
            for extra in facts.get("actions", []):
                if (extra["security"], extra["ex_date"]) == (security, r[5]):
                    check(
                        extra["cash_per_share"] == event["cash_per_share"],
                        "Source validation failed: extra['cash_per_share'] == event['cash_per_share']",
                    )
                    event.update(extra)
            ledger.append(event)
            actions.append(event)
        action_coverage[security] = dict(
            source_ref=f"raw/{code}-actions.html",
            dividend_records=len(dividends),
            rights_records=len(rights),
            observation="full provider tables captured; absence is provider-scoped, not certified issuer-chain completeness",
            historical_action_chain_complete=False,
            rights_rows=rights,
            window_counts={
                w["id"]: sum(w["start"] <= e["ex_date"] <= w["end"] for e in ledger)
                for w in windows
            },
        )
        # A current page contains a dated historical listing fact. It is not a historical universe list.
        profiles = parse_tables(raw / f"{code}-profile.html", code)["comInfo1"]
        flat = [cell for row in profiles for cell in row]
        idx = flat.index("上市日期：")
        listed = flat[idx + 1]
        date.fromisoformat(listed)
        check(listed < scope[0], "Source validation failed: listed < scope[0]")
        listing.append(
            dict(
                security=security,
                listing_date=listed,
                origin="provider_historical_listing_date",
                source_ref=f"raw/{code}-profile.html",
                current_name_used_as_historical=False,
                delisting_date=None,
                historical_st=None,
                eligibility="scoped research model; observed trades corroborate existence on traded dates",
            )
        )
        text = (raw / f"{code}-hfq.js").read_text()
        prefix = "var " + code + "hfq="
        check(text.startswith(prefix), code)
        hfq, end = json.JSONDecoder().raw_decode(text[len(prefix) :])
        check(
            re.fullmatch(";?\\s*(?:/\\*[A-Za-z0-9+/\\s]+\\*/\\s*)?", text[len(prefix) + end :]),
            code,
        )
        check(
            len(hfq["data"]) == hfq["total"],
            "Source validation failed: len(hfq['data']) == hfq['total']",
        )
        factors[security] = sorted(
            [
                dict(date=f["d"], factor=f["f"], source_ref=f"raw/{code}-hfq.js")
                for f in hfq["data"]
            ],
            key=lambda f: f["date"],
        )
        check(
            Decimal(factors[security][0]["factor"]) == 1,
            "Source validation failed: Decimal(factors[security][0]['factor']) == 1",
        )
        steps = [
            f["date"]
            for i, f in enumerate(factors[security])
            if i and Decimal(f["factor"]) != Decimal(factors[security][i - 1]["factor"])
        ]
        action_coverage[security]["factor_step_crosscheck"] = {}
        for w in windows:
            factor_dates = [d for d in steps if w["start"] <= d <= w["end"]]
            ledger_dates = sorted(
                e["ex_date"] for e in ledger if w["start"] <= e["ex_date"] <= w["end"]
            )
            action_coverage[security]["factor_step_crosscheck"][w["id"]] = dict(
                factor_dates=factor_dates,
                ledger_dates=ledger_dates,
                agrees=factor_dates == ledger_dates,
                independent=False,
                reason="Same provider; detects internal coverage inconsistency, not issuer-chain completeness",
            )
            check(
                factor_dates == ledger_dates,
                f"Unexplained action/factor boundary: {security} {w['id']}",
            )
        for label in scope:
            r = source_rows.get(label)
            if r is not None:
                row = dict(
                    security=security,
                    date=label,
                    **{f: str(r[f]) for f in ("open", "high", "low", "close", "volume")},
                    money=str(r["amount"]),
                    traded=True,
                    suspended=False,
                    origin="source_observed",
                    suspension_origin="positive full-day source volume excludes full-day halt; not pre-open evidence",
                    source_ref=source_ref,
                    historical_available_at=None,
                )
                volume, money = Decimal(row["volume"]), Decimal(row["money"])
                check(
                    Decimal(row["low"]) * volume - 1 <= money <= Decimal(row["high"]) * volume + 1,
                    f"Volume/money units inconsistent with raw price range: {security} {label}",
                )
            else:
                halt = next(
                    (h for h in facts["halts"] if h["security"] == security and h["date"] == label),
                    None,
                )
                check(halt, f"Unexplained missing day {security} {label}")
                prior = dates[dates.index(label) - 1]
                check(prior in source_rows, "Source validation failed: prior in source_rows")
                close = str(source_rows[prior]["close"])
                row = dict(
                    security=security,
                    date=label,
                    **dict.fromkeys(("open", "high", "low", "close"), close),
                    volume="0",
                    money="0",
                    traded=False,
                    suspended=True,
                    origin="documented_halt_carry",
                    suspension_origin="independent exchange notice and resumption evidence",
                    evidence=halt["evidence"],
                    carry_from=prior,
                    source_ref=source_ref,
                    historical_available_at=None,
                )
            prior = dates[dates.index(label) - 1]
            previous = source_rows.get(prior)
            if previous is None:
                previous_row = next(
                    (b for b in bars if b["security"] == security and b["date"] == prior), None
                )
                previous_close = previous_row["close"] if previous_row else None
            else:
                previous_close = str(previous["close"])
            marker = str(r["prevclose"]) if r is not None and "prevclose" in r else None
            for event in ledger:
                if event["ex_date"] == label and event.get("ex_price_cash_deduction") is not None:
                    expected = (
                        Decimal(previous_close) - Decimal(event["ex_price_cash_deduction"])
                    ).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                    check(
                        marker is not None and Decimal(marker) == expected,
                        "Source validation failed: marker is not None and Decimal(marker) == expected",
                    )
            reference = marker or previous_close
            row["previous_raw_close"] = previous_close
            row["native_prevclose_marker"] = marker
            row["research_state"] = dict(
                status="modeled",
                suspended=row["suspended"],
                upper_limit=str(
                    (Decimal(reference) * Decimal("1.1")).quantize(
                        Decimal(".01"), rounding=ROUND_HALF_UP
                    )
                )
                if reference
                else None,
                lower_limit=str(
                    (Decimal(reference) * Decimal("0.9")).quantize(
                        Decimal(".01"), rounding=ROUND_HALF_UP
                    )
                )
                if reference
                else None,
                reference_price=reference,
                reference_origin="source_native_marker" if marker else "previous_session_raw_close",
                assumption_ids=["standard_board_non_st_limits", "retrospective_daily_state"],
                historical_pit=False,
                model_available_at=label + "T09:00:00+08:00",
                source_historical_available_at=None,
            )
            bars.append(row)
        # Independent fixed archive audit, restricted to the explicitly requested points.
        audit_path = raw / f"dolt-audit-{security[:6]}.json"
        differences = []
        max_volume_difference = Decimal(0)
        max_money_difference = Decimal(0)
        audit = load(audit_path)
        check(
            audit["query_execution_status"] == "Success"
            and f"AS OF '{COMMIT}'" in audit["sql_query"],
            "Source validation failed: audit['query_execution_status'] == 'Success' and f\"AS OF '{COMMIT}'\" in audit['sql_query']",
        )
        check(len(audit["rows"]) <= 12, "Source validation failed: len(audit['rows']) <= 12")
        check(
            "FROM ts_a_stock_eod_price" in audit["sql_query"]
            and audit["repository_owner"] == "chenditc"
            and audit["repository_name"] == "investment_data",
            f"Incorrect independent archive identity: {security}",
        )
        expected_points = {w[k] for w in windows for k in ("warmup_start", "start", "end")}
        for event in facts.get("actions", []):
            if event["security"] == security:
                expected_points.update((event["record_date"], event["ex_date"]))
        audit_dates = [r["tradedate"] for r in audit["rows"]]
        check(
            len(audit_dates) == len(set(audit_dates)) and set(audit_dates) == expected_points,
            f"Incomplete or unexpected independent boundary points: {security}",
        )
        for r in audit["rows"]:
            label = r["tradedate"]
            check(
                r["symbol"] == security[:6] + (".SZ" if security.endswith("XSHE") else ".SH"),
                "Source validation failed: r['symbol'] == security[:6] + ('.SZ' if security.endswith('XSHE') else '.SH')",
            )
            if label not in source_rows:
                differences.append(dict(date=label, field="source_row", kind="archive_only"))
                continue
            primary = source_rows[label]
            for f in ("open", "high", "low", "close"):
                if Decimal(str(primary[f])) != Decimal(r[f]):
                    differences.append(
                        dict(date=label, field=f, primary=str(primary[f]), independent=r[f])
                    )
            vd = Decimal(str(primary["volume"])) - Decimal(r["volume"]) * 100
            md = Decimal(str(primary["amount"])) - Decimal(r["amount"]) * 1000
            max_volume_difference = max(max_volume_difference, abs(vd))
            max_money_difference = max(max_money_difference, abs(md))
            if vd != 0 or abs(md) > Decimal("1"):
                differences.append(
                    dict(
                        date=label,
                        field="volume_money",
                        volume_difference=str(vd),
                        money_difference=str(md),
                    )
                )
        check(not differences, f"Independent archive mismatch: {security}: {differences}")
        audits.append(
            dict(
                security=security,
                rows=len(audit["rows"]),
                source_ref=str(audit_path.relative_to(root)),
                volume_conversion="archive lots * 100",
                money_conversion="archive thousands CNY * 1000",
                max_volume_difference=str(max_volume_difference),
                max_money_difference=str(max_money_difference),
                differences=differences,
            )
        )
        for w in windows:
            selected = [b for b in bars if b["security"] == security and b["date"] in w["sessions"]]
            observed = sum(b["traded"] for b in selected)
            coverage.append(
                dict(
                    security=security,
                    window=w["id"],
                    warmup_start=w["warmup_start"],
                    warmup_sessions=w["warmup_sessions"],
                    output_sessions=w["output_sessions"],
                    next_session=w["next_session"],
                    expected_rows=len(w["sessions"]),
                    observed_rows=observed,
                    documented_halt_carries=len(selected) - observed,
                    missing_rows=0,
                    corporate_actions=action_coverage[security]["window_counts"][w["id"]],
                    historical_status="unavailable; daily retrospective model separate",
                    limits="explicit model only",
                )
            )
    bars.sort(key=lambda r: (r["date"], r["security"]))
    assumptions = facts["assumptions"]
    quality = dict(
        coverage=coverage,
        calendar_checks=calendar_checks,
        direct_szse_checks=official_checks,
        independent_audits=audits,
        raw_observed_rows=sum(b["traded"] for b in bars),
        documented_halt_carries=sum(not b["traded"] for b in bars),
        missing_rows=0,
        strict_pit=False,
        all_output_windows_preserved=True,
        economic_acceptance="consumer must explicitly resolve or accept model limitations",
    )
    data = dict(
        schema="ad.daily-research-data.v1",
        units=dict(price="CNY/share", volume="share", money="CNY"),
        securities=contract["securities"],
        windows=windows,
        bars=bars,
        listing=listing,
        actions=actions,
        factors=factors,
        action_coverage=action_coverage,
        assumptions=assumptions,
        quality=quality,
        limitations=facts["limitations"],
    )
    name = Path(output).stem + "-data.json"
    write_fixed(root / name, encode(data))
    files = sorted(
        {p for d in (raw, derived) for p in d.glob("*") if p.is_file()}
        | {root / "strategy-manifest.json", root / "facts.json", root / name}
    )
    binding = dict(
        schema=SCHEMA,
        role=ROLE,
        historical_pit=False,
        execution_permission=False,
        data=name,
        producer=dict(name="ashare-data", version="0.9.1.dev11", baseline=BASELINE),
        files=[ref(p, root) for p in files],
    )
    content = encode(binding)
    write_fixed(root / output, content)
    expected = sha(content)
    write_fixed(root / (output + ".sha256"), (expected + "\n").encode())
    view = DailyResearchView(root / output, expected_binding_sha256=expected)
    (root / "reports/coverage.json").write_bytes(encode(view.quality()))
    return expected, quality


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--output", default="binding.json")
    args = p.parse_args()
    try:
        digest, quality = build(args.root.resolve(), args.output)
    except (ValueError, KeyError, DataError) as exc:
        raise SystemExit(f"Package rejected: {exc}") from exc
    print(
        json.dumps(
            dict(
                binding_sha256=digest,
                observed=quality["raw_observed_rows"],
                halt_carries=quality["documented_halt_carries"],
            )
        )
    )


if __name__ == "__main__":
    main()
