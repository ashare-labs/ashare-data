"""Bounded, receipt-checked source projection for the six-factor research view."""

import calendar
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re


def load(path):
    return json.loads(path.read_text(), parse_float=str)


def read_sources(root, scope):
    root = Path(root)
    raw, sources = root / "raw", []
    for path in sorted(raw.iterdir()):
        if path.name.endswith(".receipt.json") or path.name.startswith("balance-field-probe"):
            continue
        receipt = load(path.with_name(path.name + ".receipt.json"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if receipt["sha256"] != digest or receipt["returncode"] != 0:
            raise ValueError("source receipt mismatch: " + path.name)
        sources.append(
            {
                "path": str(path.relative_to(root)),
                "sha256": digest,
                "bytes": path.stat().st_size,
                "url": receipt["url"],
            }
        )
    flags = {}
    for path in sorted(raw.glob("szse-????-??.json")):
        month = path.stem.removeprefix("szse-")
        first = date.fromisoformat(month + "-01")
        expected = [
            (first + timedelta(days=i)).isoformat()
            for i in range(calendar.monthrange(first.year, first.month)[1])
        ]
        rows = load(path)["data"]
        if [r["jyrq"] for r in rows] != expected or any(r["jybz"] not in {"0", "1"} for r in rows):
            raise ValueError("incomplete calendar month")
        flags.update({r["jyrq"]: r["jybz"] == "1" for r in rows})

    def inside(day):
        return any(start <= day <= end for start, end in scope["input_windows"])

    for start, end in scope["input_windows"]:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        if any((a + timedelta(days=i)).isoformat() not in flags for i in range((b - a).days + 1)):
            raise ValueError("calendar civil-day coverage gap")
    sessions = sorted(day for day, opened in flags.items() if opened and inside(day))
    items = []
    for stock in scope["securities"]:
        security = stock["security"]
        code = ("sh" if security.endswith("XSHG") else "sz") + security[:6]
        daily_path = root / "derived" / (code + "-daily.json")
        daily = load(daily_path)
        if (
            daily["security"] != security
            or daily["raw_sha256"]
            != hashlib.sha256((raw / (code + "-daily.js")).read_bytes()).hexdigest()
            or daily["decoder_sha256"]
            != "39a599c94dde4df1c2eb0882d4bff9560160cfd52ead0797cebb04b3122c2f52"
        ):
            raise ValueError("unverified decoded source")
        # Pin the derived projection as well as the original response.
        sources.append(
            {
                "path": str(daily_path.relative_to(root)),
                "sha256": hashlib.sha256(daily_path.read_bytes()).hexdigest(),
                "bytes": daily_path.stat().st_size,
                "role": "hash_pinned_decoder_projection",
            }
        )
        bars = {}
        for row in daily["rows"]:
            day = row["date"][:10]
            if day in bars or day not in sessions:
                raise ValueError("duplicate/off-calendar price row")
            bars[day] = {
                k: str(row[k]) for k in ("open", "high", "low", "close", "volume", "amount")
            }
        text = (raw / (code + "-hfq.js")).read_text()
        match = re.fullmatch(r"var " + code + r"hfq=(\{.*\})\s*;?\s*(?:/\*.*\*/\s*)?", text, re.S)
        if not match:
            raise ValueError("unknown factor envelope")
        factors = json.loads(match.group(1), parse_float=str)
        if factors["total"] != len(factors["data"]):
            raise ValueError("factor capture truncated")
        factors = sorted(
            ({"date": r["d"], "value": str(r["f"])} for r in factors["data"]),
            key=lambda r: r["date"],
        )
        tencent = {}
        for path in sorted(raw.glob(code + "-tencent-*.js")):
            text = path.read_text()
            if not text.startswith("f1_batch="):
                raise ValueError("unknown Tencent envelope")
            response = json.loads(text.split("=", 1)[1], parse_float=str)
            if response["code"] != 0:
                raise ValueError("Tencent returned error")
            for row in response["data"][code].get("day", []):
                day = row[0]
                if day not in sessions:
                    continue
                value = {
                    "open": row[1],
                    "close": row[2],
                    "high": row[3],
                    "low": row[4],
                    "volume": str(Decimal(row[5]) * 100),
                    "amount": str(Decimal(row[8]) * 10000),
                }
                if day in tencent and tencent[day] != value:
                    raise ValueError("conflicting Tencent captures")
                tencent[day] = value
        reports = []
        for kind, fields in (
            (
                "balance",
                [
                    "TOTAL_CURRENT_ASSETS",
                    "INVENTORY",
                    "TOTAL_CURRENT_LIAB",
                    "TOTAL_LIABILITIES",
                    "TOTAL_ASSETS",
                ],
            ),
            ("income", ["PARENT_NETPROFIT"]),
            ("cash", ["NETCASH_OPERATE"]),
        ):
            path = raw / f"{code}-{kind}.json"
            response = load(path)
            if (
                response.get("success") is not True
                or not response.get("result")
                or response["result"]["pages"] != 1
            ):
                raise ValueError("financial source incomplete: " + path.name)
            for row in response["result"]["data"]:
                if (
                    row["SECUCODE"] != security[:6] + "." + code[:2].upper()
                    or row["ORG_TYPE"] != "通用"
                    or row["CURRENCY"] != "CNY"
                ):
                    raise ValueError("financial identity/unit/presentation mismatch")
                if (
                    not scope["financial_period_range"][0]
                    <= row["REPORT_DATE"][:10]
                    <= scope["financial_period_range"][1]
                ):
                    continue
                reports.append(
                    {
                        "kind": kind,
                        "period": row["REPORT_DATE"][:10],
                        "notice_date": row["NOTICE_DATE"][:10],
                        "update_date": row["UPDATE_DATE"][:10],
                        "values": {f: row.get(f) for f in fields},
                        "source_path": str(path.relative_to(root)),
                    }
                )
        items.append(
            {
                "security": security,
                "sina": bars,
                "tencent": tencent,
                "factors": factors,
                "reports": reports,
            }
        )
    return {
        "schema": "ad.f2-independent-inputs.v1",
        "units": {"price": "CNY/share", "volume": "shares", "financial": "consolidated_CNY"},
        "sources": sources,
        "sessions": sessions,
        "input_windows": scope["input_windows"],
        "items": items,
        "input_contains_JQ_values": False,
        "raw_price_adjustment": "none",
        "historical_pit_certified": False,
    }
