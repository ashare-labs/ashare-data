"""Reuse reviewed Sina corporate actions and the existing TDX F10 wire contract.

Numeric F10 columns without a published meaning stay native. A bounded current
window is useful without claiming the provider's entire historical archive.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import urlencode

from ashare_data.corporate_actions.sources import PARSERS
from ashare_data.financials.expansion import security, url_for

F10_ENTRIES = {"CWServ.tdxf10_gg_comreq", "CWServ.tdxf10_gg_gsgk",
               "CWServ.tdxf10_gg_jyfx", "HQServ.hq_nlp_gpsj"}
FUNCTIONS = {"sina_corporate_actions", "tdx_f10_company", "tdx_f10_business", "tdx_f10_valuation"}

# Existing eltdx F10 business-composition field documentation defines these
# percent columns. It does not specify monetary scale; keep that distinction.
BUSINESS_FIELDS = {
    "revenue": ("N003", "native amount; currency/scale not established"),
    "revenue_share": ("N004", "percent"),
    "cost": ("N005", "native amount; currency/scale not established"),
    "cost_share": ("N006", "percent"),
    "gross_profit": ("N007", "native amount; currency/scale not established"),
    "profit_share": ("N008", "percent"),
    "gross_margin": ("N009", "percent"),
}


def business_metrics(native):
    mapped = {}
    for name, (field, unit) in BUSINESS_FIELDS.items():
        raw, value = native.get(field), None
        try:
            parsed = Decimal(str(raw))
            if parsed.is_finite():
                value = format(parsed, "f")
        except InvalidOperation:
            pass
        mapped[name] = {"value": value, "unit": unit, "native_field": field, "native_value": raw}
    return mapped


def reviewed_url(url):
    return any(url == "http://static.tdx.com.cn:7615/TQLEX?" + urlencode({"Entry": e})
               for e in F10_ENTRIES)


def tables(payload):
    if payload.get("ErrorCode") != 0 or not isinstance(payload.get("ResultSets"), list):
        raise ValueError("F10 response error or missing tables")
    result = []
    for number, table in enumerate(payload["ResultSets"]):
        columns = table.get("ColName")
        if columns is None:
            columns = [c["Name"] for c in table.get("ColDes", [])]
        rows = table.get("Content")
        if (not isinstance(columns, list) or not columns or len(set(columns)) != len(columns)
                or not isinstance(rows, list) or any(not isinstance(r, list) or len(r) != len(columns) for r in rows)
                or table.get("Count", table.get("RowNum", len(rows))) != len(rows)):
            raise ValueError("F10 columns or table row count changed")
        result.append({"key": table.get("ResultSetKey") or f"table{number}",
                       "rows": [dict(zip(columns, row)) for row in rows]})
    return result


def call(capture, entry, body):
    if entry not in F10_ENTRIES:
        raise ValueError("unreviewed F10 entry")
    url = "http://static.tdx.com.cn:7615/TQLEX?" + urlencode({"Entry": entry})
    return tables(json.loads(capture.post_json(url, body).decode("utf-8-sig"), parse_float=str))


def date(value):
    return datetime.strptime(str(value).replace("-", ""), "%Y%m%d").date().isoformat()


def collect(capture, name, params):
    symbol = params["symbol"]
    if not re.fullmatch(r"(?:sh60|sh68|sz00|sz30)\d{4}", symbol):
        raise ValueError("explicit SH/SZ stock required")
    code = symbol[2:]
    if name == "sina_corporate_actions":
        parsed = PARSERS["sina_actions"](capture.get(url_for("sina_actions", symbol)), security(symbol))
        records = []
        for category in ("actions", "nonimplemented", "unparsed_records"):
            for item in parsed.get(category, []):
                records.append({"security": symbol, "category": category, "observation": item,
                    "announcement_date": item.get("fields", {}).get("announcement_date"),
                    "source_locator": item["locator"], "first_publication_at": None})
        capture.dataset_metadata = {"complete_returned_window": True, "source_count": len(records),
            "table_states": parsed.get("table_states"), "scope": "current complete dividend/rights tables for requested stock",
            "price_adjustment_equivalence": False, "whole_market": False}
        return bind_keys(records, name)
    identity = call(capture, "CWServ.tdxf10_gg_comreq", {"Params": ["gpquery", code]})
    if (len(identity) != 1 or len(identity[0]["rows"]) != 1
            or identity[0]["rows"][0].get("T002") != code
            or identity[0]["rows"][0].get("sc") != int(symbol.startswith("sh"))):
        raise ValueError("F10 security or exchange mismatch")
    issuer = identity[0]["rows"][0]
    common = {"security": symbol, "name": issuer["T003"], "source": "TDX F10",
              "identity": issuer, "first_publication_at": None}
    if name == "tdx_f10_company":
        data = call(capture, "CWServ.tdxf10_gg_gsgk", {"Params": ["8", code, ""]})
        if len(data) != 1 or len(data[0]["rows"]) != 1:
            raise ValueError("F10 listing profile missing or ambiguous")
        native = data[0]["rows"][0]
        rows = [common | {"listing_date": date(native["T031"]), "market": native["ssbk"],
                           "share_class": native["T035"], "native_fields": native}]
        limitations = "identity and listing subsection; other native IPO fields are not assigned guessed units"
    elif name == "tdx_f10_business":
        available = call(capture, "CWServ.tdxf10_gg_comreq", {"Params": ["zygcfx", code]})
        periods = [str(r["T002"]) for t in available for r in t["rows"]]
        if not periods or len(periods) != len(set(periods)):
            raise ValueError("business report-period list absent or duplicate")
        selected = max(periods, key=date)
        data = call(capture, "CWServ.tdxf10_gg_jyfx", {"Params": [code, "zygc", selected]})
        if len(data) != 3 or data[2]["rows"][0].get("rq") != selected:
            raise ValueError("business period differs from request")
        rows = [common | {"report_period": date(selected), "table": t["key"], "ordinal": n,
                "segment_name": r["N002"], "native_fields": r, "announcement_date": None,
                "metrics": business_metrics(r),
                "numeric_unit": "ratios are percent; monetary values retain unverified native scale"}
                for t in data[:2] for n, r in enumerate(t["rows"])]
        limitations = "latest source report; named revenue/cost/profit native values and percent ratios; monetary scale remains native"
    elif name == "tdx_f10_valuation":
        pages = params.get("pages", 1)
        if type(pages) is not int or not 1 <= pages <= 10:
            raise ValueError("valuation pages must be 1..10")
        rows, total = [], None
        for page in range(pages):
            data = call(capture, "HQServ.hq_nlp_gpsj", [{"ReqId": "200191", "Code": code+"|"+str(issuer["sc"]),
                "BeginDate": "0", "EndDate": "0", "Page": str(page), "PageSize": "20", "modname": "mod_gpsj.dll"}])
            if len(data) != 2 or len(data[0]["rows"]) != 1:
                raise ValueError("valuation pagination metadata missing")
            meta, values = data[0]["rows"][0], data[1]["rows"]
            count, current_total = int(meta["num"]), int(meta["total_num"])
            total = current_total if total is None else total
            if count != len(values) or total != current_total or count != min(20, total-page*20):
                raise ValueError("valuation pagination/count changed")
            rows.extend(common | {"date": date(r["DATE"]), "pe_ttm": r["PETTM"], "pb_mrq": r["PBMRQ"],
                                  "native_fields": r} for r in values)
            if len(rows) >= total:
                break
        if len({r["date"] for r in rows}) != len(rows):
            raise ValueError("valuation repeated page/date")
        rows.sort(key=lambda r: r["date"])
        capture.dataset_metadata = {"source_total": total, "pages_received": page+1,
                                   "complete_history": len(rows) == total}
        limitations = "explicit recent page window; PE TTM and PB MRQ native ratios, other native columns unconverted"
    else:
        raise ValueError("unsupported reviewed legacy reference")
    capture.dataset_metadata.update(complete_returned_window=True, source_count=len(rows),
        whole_market=False, requested_security=symbol, scope=limitations,
        transport="existing public HTTP gateway; no TLS or authenticated-source claim")
    return bind_keys(rows, name)


def business_key(r, name):
    if name == "sina_corporate_actions":
        item, fields = r["observation"], r["observation"].get("fields", {})
        if r["category"] == "unparsed_records":
            return [r["category"], r["source_locator"]]
        return [r["category"], item.get("type"), fields.get("announcement_date"),
                fields.get("ex_date"), fields.get("record_date")]
    if name == "tdx_f10_company":
        return r["security"]
    if name == "tdx_f10_business":
        return [r["report_period"], r["table"], r["native_fields"].get("N000"),
                r["native_fields"].get("N001"), r["segment_name"]]
    return r["date"]


def bind_keys(rows, name):
    return [r | {"native_business_identity": business_key(r, name)} for r in rows]


def validate(rows, job, as_of, numeric):
    name = job["function"]
    if name not in FUNCTIONS:
        return None
    symbol = job["parameters"]["symbol"]
    keys, dates = [], []
    for r in rows:
        if r["security"] != symbol or r["first_publication_at"] is not None:
            raise ValueError("legacy reference security/visibility mismatch")
        if name == "sina_corporate_actions":
            item = r["observation"]
            if item["security"] != security(symbol):
                raise ValueError("corporate action identity mismatch")
            fields = item.get("fields", {})
            if fields.get("cash_per_share") is not None:
                if numeric(fields["cash_per_share"]) < 0 or item.get("cash_basis") != "gross_pre_tax_per_old_eligible_share":
                    raise ValueError("dividend amount/basis mismatch")
            if r["announcement_date"]:
                dates.append(date(r["announcement_date"]))
        else:
            if r["identity"]["T002"] != symbol[2:] or r["identity"]["sc"] != int(symbol.startswith("sh")):
                raise ValueError("F10 request identity not retained")
            if name == "tdx_f10_company":
                dates.append(date(r["listing_date"]))
            elif name == "tdx_f10_business":
                dates.append(date(r["report_period"]))
                if r.get("metrics") != business_metrics(r["native_fields"]):
                    raise ValueError("F10 business field/unit mapping changed")
            else:
                dates.append(date(r["date"]))
                numeric(r["pe_ttm"]), numeric(r["pb_mrq"])
        if r["native_business_identity"] != business_key(r, name):
            raise ValueError("legacy stable business key differs")
        keys.append(json.dumps(r["native_business_identity"]))
    if not rows or len(keys) != len(set(keys)) or any(d > as_of for d in dates):
        raise ValueError("legacy reference empty, duplicate keys or future observation date")
    return {"contract_reviewed": True, "business_key": ["security", "native_business_identity"],
        "source_rows": len(rows), "unique_keys": len(keys), "gaps": [],
        "latest_period": max(dates) if dates else None, "latest_announcement": max(dates) if name == "sina_corporate_actions" and dates else None,
        "frequency": "source_event_or_snapshot", "units": {"cash_per_share": "CNY per old eligible share before tax",
            "pe_ttm/pb_mrq": "native ratios", "business_ratios": "percent",
            "business_amounts": "native amount; currency/scale not established",
            "other_F10_values": "native, unmapped; no guessed conversions"},
        "limitations": ["explicit securities/window only; raw fields and unknown units retained", "source dates do not certify historical first-online time"]}
