"""Read bounded public captures without executing JavaScript response bodies."""

import calendar
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re

from ashare_data.factors.joinquant.panel import UNITS, canonical_day


def load_json(path):
    return json.loads(Path(path).read_text(), parse_float=str)


def _json_response(path):
    obj = load_json(path)
    if obj.get("success") is not True or obj.get("result") is None:
        return []
    if obj["result"]["pages"] != 1:
        raise ValueError("financial capture is incomplete; pagination required")
    return obj["result"]["data"]


def read_captures(root, scope):
    root = Path(root)
    raw = root / "raw"
    source_refs = []
    for path in sorted(raw.iterdir()):
        if path.name.endswith(".receipt.json"):
            continue
        if path.name == "eastmoney-probe.json":
            continue
        receipt_path = path.with_name(path.name + ".receipt.json")
        if not receipt_path.exists():
            raise ValueError(f"missing source receipt: {path.name}")
        receipt = load_json(receipt_path)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if receipt["sha256"] != digest or receipt.get("returncode", 0) != 0:
            raise ValueError(f"source integrity/acquisition failure: {path.name}")
        source_refs.append(
            {
                "path": "raw/" + path.name,
                "sha256": digest,
                "bytes": len(content),
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
        rows = load_json(path)["data"]
        if [row["jyrq"] for row in rows] != expected or any(
            row["jybz"] not in {"0", "1"} for row in rows
        ):
            raise ValueError("incomplete calendar month or unknown flag")
        flags.update({row["jyrq"]: row["jybz"] == "1" for row in rows})
    for start, end in scope["input_windows"]:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        if any((a + timedelta(days=i)).isoformat() not in flags for i in range((b - a).days + 1)):
            raise ValueError("calendar civil-day coverage hole")
    sessions = sorted(d for d, opened in flags.items() if opened)
    items = []
    for security_item in scope["securities"]:
        security = security_item["security"]
        code = ("sh" if security.endswith("XSHG") else "sz") + security[:6]
        daily = load_json(root / "derived" / (code + "-daily.json"))
        if (
            daily["security"] != security
            or daily["raw_sha256"]
            != hashlib.sha256((raw / (code + "-daily.js")).read_bytes()).hexdigest()
        ):
            raise ValueError("decoded projection identity mismatch")
        if (
            daily["decoder_sha256"]
            != "39a599c94dde4df1c2eb0882d4bff9560160cfd52ead0797cebb04b3122c2f52"
        ):
            raise ValueError("unknown decoder")
        bars = {}
        for row in daily["rows"]:
            day = canonical_day(row["date"][:10])
            if day in bars or not flags.get(day):
                raise ValueError("duplicate/off-calendar market row")
            bars[day] = {
                k: str(row[k]) for k in ("open", "high", "low", "close", "volume", "amount")
            }
        text = (raw / (code + "-hfq.js")).read_text()
        matched = re.fullmatch(r"var " + code + r"hfq=(\{.*\})\s*;?\s*(?:/\*.*\*/\s*)?", text, re.S)
        if not matched:
            raise ValueError("unexpected factor envelope")
        factor_obj = json.loads(matched.group(1), parse_float=str)
        if factor_obj["total"] != len(factor_obj["data"]):
            raise ValueError("incomplete native factors")
        factors = sorted(
            ({"date": canonical_day(f["d"]), "value": f["f"]} for f in factor_obj["data"]),
            key=lambda f: f["date"],
        )
        if len({f["date"] for f in factors}) != len(factors):
            raise ValueError("ambiguous factor dates")
        text = (raw / (code + "-shares.js")).read_text()
        # Sina's fixed inert leading comment is discarded, never interpreted.
        matched = re.fullmatch(
            r"(?:/\*.*?\*/\s*)?var KKE_ShareAmount_" + code + r"=\((\[.*\])\)\s*;?\s*", text, re.S
        )
        if not matched:
            raise ValueError("unexpected shares envelope")
        from decimal import Decimal

        shares = [
            {"date": canonical_day(row["date"]), "value": str(Decimal(str(row["amount"])) * 10000)}
            for row in json.loads(matched.group(1), parse_float=str)
        ]
        tencent = {}
        for path in sorted(raw.glob(code + "-tencent-*.js")):
            text = path.read_text()
            if not text.startswith("joinquant_batch="):
                raise ValueError("unexpected Tencent envelope")
            data = json.loads(text.split("=", 1)[1], parse_float=str)
            if data["code"] != 0:
                continue
            for row in data["data"][code].get("day", []):
                day = canonical_day(row[0])
                if day not in sessions:
                    continue
                converted = {
                    "open": row[1],
                    "close": row[2],
                    "high": row[3],
                    "low": row[4],
                    "volume": str(Decimal(row[5]) * 100),
                    "amount": str(Decimal(row[8]) * 10000),
                }
                if day in tencent and tencent[day] != converted:
                    raise ValueError("cross-capture source conflict")
                tencent[day] = converted
        financials = []
        income = _json_response(raw / (code + "-income.json"))
        kind_prefix = {"银行": "B", "保险": "I"}.get(income[0]["ORG_TYPE"] if income else None)
        cash_path = raw / (code + ("-cash-" + kind_prefix if kind_prefix else "-cash") + ".json")
        cash = _json_response(cash_path) if cash_path.exists() else []
        for kind, rows in (("income", income), ("cash", cash)):
            for row in rows:
                if row["SECUCODE"] != security[:6] + "." + code[:2].upper():
                    raise ValueError("financial security mismatch")
                fields = (
                    ["NETCASH_OPERATE"]
                    if kind == "cash"
                    else ["OPERATE_INCOME", "TOTAL_OPERATE_INCOME", "NETPROFIT"]
                )
                financials.append(
                    {
                        "kind": kind,
                        "period": row["REPORT_DATE"][:10],
                        "notice_date": row["NOTICE_DATE"][:10],
                        "update_date": row["UPDATE_DATE"][:10],
                        "currency": row["CURRENCY"],
                        "values": {f: row.get(f) for f in fields},
                    }
                )
        items.append(
            {
                "security": security,
                "sina": bars,
                "tencent": tencent,
                "factors": factors,
                "shares": shares,
                "financials": financials,
            }
        )
    return {
        "schema": "ad.factor-panel-batch-inputs.v1",
        "sources": source_refs,
        "items": items,
        "units": UNITS,
        "raw_price_adjustment": "none",
        "sessions": sessions,
        "SSE_calendar_application": "cross_market_research_only",
        "input_contains_JQ_values": False,
        "historical_pit_certified": False,
        "production_eligible": False,
    }
