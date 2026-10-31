"""Scoped native bond bars and latest financial fields from the existing SDK.

Bond quantities stay in named native fields. Financial units follow eltdx 3.0.3
FinanceRecord and its 7709 financial-information field documentation; float32
precision is retained and the SDK update date is never called an announcement.
"""

from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode

from ashare_data.providers.tdx.tdx_collection import validate_page

FUNCTIONS = {"tdx_native_bond_bars", "tdx_native_finance", "tdx_native_depth"}
HOST = "116.205.183.150:7709"
PERIODS = {"1m", "5m", "15m", "30m", "60m", "day"}
FINANCE_FIELDS = {
    "total_shares": ("zong_gu_ben_raw_float", "shares", 10000),
    "circulating_shares": ("liu_tong_gu_ben_raw_float", "shares", 10000),
    "eps": ("eps_raw", "CNY/share", 1),
    "book_value_per_share": ("mei_gu_jing_zi_chan_raw_float", "CNY/share", 1),
    "total_assets": ("zong_zi_chan_raw_float", "CNY", 1000),
    "net_assets": ("jing_zi_chan_raw_float", "CNY", 1000),
    "operating_revenue": ("zhu_ying_shou_ru_raw_float", "CNY", 1000),
    "net_profit": ("jing_li_run_raw_float", "CNY", 1000),
}


def parameters(name, params):
    required = {"symbol", "host", "sdk_python", "sdk_sha256"}
    if name == "tdx_native_bond_bars":
        required |= {"period", "count"}
        valid = re.fullmatch(
            r"sh(?:110|111|113|118)\d{3}|sz(?:123|127|128)\d{3}", params.get("symbol", "")
        )
        if (
            params.get("period") not in PERIODS
            or type(params.get("count")) is not int
            or not 1 <= params["count"] <= 800
        ):
            raise ValueError("native bond period/count requires one bounded page of 1..800 bars")
    elif name == "tdx_native_finance":
        valid = re.fullmatch(r"(?:sh60|sh68|sz00|sz30)\d{4}", params.get("symbol", ""))
    elif name == "tdx_native_depth":
        from ashare_data.providers.free_updates.free_update_sina_minutes import parameters as security_parameters
        required |= {"asset_type"}
        if params.get("asset_type") not in {"stock", "etf"}:
            raise ValueError("native depth is reviewed only for stocks and ETFs; bond price scale is unresolved")
        security_parameters({"symbol":params.get("symbol"), "asset_type":params["asset_type"], "period":"1"})
        valid = True
    else:
        raise ValueError("unreviewed native function")
    if (
        set(params) != required
        or not valid
        or params["host"] != HOST
        or not Path(params["sdk_python"]).is_absolute()
        or "\0" in params["sdk_python"]
        or not re.fullmatch(r"[0-9a-f]{64}", params["sdk_sha256"])
    ):
        raise ValueError("explicit reviewed native identity, host, existing SDK path/hash required")
    return {k: v for k, v in params.items() if k != "sdk_python"} | {
        "operation": {"tdx_native_bond_bars":"bars", "tdx_native_finance":"finance", "tdx_native_depth":"depth"}[name]
    }


def request_url(config):
    return (
        "tcp://"
        + config["host"]
        + "/"
        + config["operation"]
        + "?"
        + urlencode(sorted((k, v) for k, v in config.items() if k not in {"host", "operation"}))
    )


def capture_native(capture, config, python):
    """Same recorder/cooldown as HTTP, with a separate, explicitly reviewed protocol."""
    from ashare_data.providers.free_updates.free_update_worker import SourceBlocked

    host, url = config["host"].split(":")[0], request_url(config)
    if host in capture.blocked or capture.attempts >= capture.request_limit:
        raise SourceBlocked("native host is cooling down or request budget exhausted")
    if not Path(python).is_file():
        raise ValueError("selected existing SDK interpreter is absent; no automatic install")
    capture.network_before(url)
    time.sleep(2)  # Serial batch jobs also obey this per-connection minimum.
    try:
        result = subprocess.run(
            [python, "-B", "-E", "-s", str(Path(__file__).with_name("free_update_tdx_worker.py"))],
            input=json.dumps(config).encode(),
            capture_output=True,
            timeout=22,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        capture.transport_failure(url, host, exc)
        raise SourceBlocked(
            "native transport failed; checkpoint and cooldown, no host failover"
        ) from exc
    if len(result.stdout) > 16 * 1024 * 1024:
        raise ValueError("native response exceeds bounded size")
    payload = json.loads(result.stdout)
    status = 200 if payload.get("ok") is True else "native_error"
    if status != 200 and payload.get("error_type") != "ValueError":
        status = "transport_error"
    capture.record(url, host, status, result.stdout, {}, protocol="tdx-sdk")
    if status != 200:
        if status == "transport_error":
            capture.blocked.add(host)
        raise ValueError("native request failed: " + str(payload.get("error")))
    return result.stdout


def mapped_finance(native):
    values = {}
    for field, (source, unit, multiplier) in FINANCE_FIELDS.items():
        value = Decimal(str(native[source]))
        if not value.is_finite():
            raise ValueError("nonfinite native financial value")
        values[field] = {
            "value": format(value * multiplier, "f"),
            "unit": unit,
            "native_field": source,
            "native_value": native[source],
            "multiplier": multiplier,
        }
    return values


def collect(capture, name, params):
    config = parameters(name, params)
    if hasattr(capture, "native"):
        body = capture.native(request_url(config))  # Offline capture reader, no SDK execution.
    else:
        body = capture_native(capture, config, params["sdk_python"])
    envelope = json.loads(body)
    files = envelope.get("sdk_files", {})
    fingerprint = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        envelope.get("ok") is not True
        or envelope.get("request") != config
        or envelope.get("sdk_version") != "3.0.3"
        or fingerprint != params["sdk_sha256"]
    ):
        raise ValueError("native SDK/request identity mismatch")
    native = envelope["data"]
    symbol = params["symbol"]
    received = capture.receipts[-1]["received_at"]
    if name == "tdx_native_bond_bars":
        if (
            native["exchange"] + native["code"] != symbol
            or native["market_id"] != int(symbol.startswith("sh"))
            or native["period_name"] != params["period"]
            or native["start"] != 0
            or native["request_count"] != params["count"]
            or native["adjust_mode_raw"] != 0
            or native["adjust_mode"] != "none"
        ):
            raise ValueError("native bond identity/frequency/basis differs from request")
        validate_page(
            native["bars"], bytes.fromhex(native["raw_payload"]), params["count"], received
        )
        rows = [
            {
                "security": symbol,
                "asset_type": "convertible_bond",
                "frequency": params["period"],
                "day": r["time"],
                "price_basis": "unadjusted",
                "native_fields": r,
                **{
                    key: str(Decimal(r[key + "_price_milli"]) / 1000)
                    for key in ("open", "high", "low", "close")
                },
            }
            for r in native["bars"]
        ]
        scope = "one source-returned rolling page; latest at most requested 800 native bars"
    elif name == "tdx_native_depth":
        from ashare_data.providers.free_updates.free_update_quotes import parse
        rows = parse(native, params, received)
        scope = "one current five-level stock/ETF quote snapshot; source raw clock and receipt time retained separately"
    else:
        records = native["records"]
        wire = bytes.fromhex(native["raw_payload"])
        if (
            len(records) != 1
            or wire[:2] != b"\x01\x00"
            or wire[2:] != bytes.fromhex(records[0]["record_hex"])
        ):
            raise ValueError("native finance wire/count mismatch")
        record = records[0]
        if record["exchange"] + record["code"] != symbol or record["market_id"] != int(
            symbol.startswith("sh")
        ):
            raise ValueError("native financial security mismatch")
        rows = [
            {
                "security": symbol,
                "source_updated_date": record["updated_date"],
                "report_period": None,
                "announcement_date": None,
                "first_publication_at": None,
                "metrics": mapped_finance(record),
                "native_fields": record,
                "precision": "source IEEE754 float32; converted values are approximate",
            }
        ]
        scope = "latest native financial snapshot, eight mapped fields; no report-period or announcement inference"
    capture.dataset_metadata = {
        "complete_returned_window": True,
        "source_count": len(rows),
        "scope": scope,
        "whole_market": False,
        "complete_history": False,
        "sdk_sha256": params["sdk_sha256"],
        "host": params["host"],
        "increment": "overlapping daily snapshot; received history and revisions retained",
    }
    return rows


def validate(rows, job, as_of, numeric):
    name, params = job["function"], job["parameters"]
    if name not in FUNCTIONS:
        return None
    parameters(name, params)
    if name == "tdx_native_depth":
        from ashare_data.providers.free_updates.free_update_quotes import validate as validate_depth
        return validate_depth(rows, params, as_of)
    labels = []
    for r in rows:
        if r["security"] != params["symbol"]:
            raise ValueError("native security mismatch")
        if name == "tdx_native_bond_bars":
            dt = datetime.fromisoformat(r["day"])
            if (
                dt.utcoffset() is None
                or dt.utcoffset().total_seconds() != 28800
                or dt.date().isoformat() > as_of
                or r["frequency"] != params["period"]
                or r["asset_type"] != "convertible_bond"
                or r["price_basis"] != "unadjusted"
            ):
                raise ValueError("native bond timestamp/frequency/basis mismatch")
            for field in ("open", "high", "low", "close"):
                if Decimal(r[field]) != Decimal(r["native_fields"][field + "_price_milli"]) / 1000:
                    raise ValueError("native bond price scale changed")
            labels.append(r["day"])
        else:
            if (
                r["metrics"] != mapped_finance(r["native_fields"])
                or any(
                    r[k] is not None
                    for k in ("report_period", "announcement_date", "first_publication_at")
                )
                or r["source_updated_date"] != r["native_fields"]["updated_date"]
                or r["source_updated_date"] > as_of
            ):
                raise ValueError("native financial units or date meaning changed")
            datetime.strptime(r["source_updated_date"], "%Y-%m-%d")
            labels.append(r["source_updated_date"])
    if not rows or labels != sorted(set(labels)):
        raise ValueError("empty, duplicate or unordered native records")
    return {
        "contract_reviewed": True,
        "business_key": ["security", "day"]
        if name.endswith("bars")
        else ["security", "source_updated_date"],
        "source_rows": len(rows),
        "unique_keys": len(labels),
        "gaps": [],
        "latest_period": max(labels),
        "latest_announcement": None,
        "frequency": "native rolling bars" if name.endswith("bars") else "native financial update",
        "units": {
            "OHLC": "native bond CNY quote, price_milli / 1000",
            "quantities": "native wire/SDK fields, no stock-lot conversion",
        }
        if name.endswith("bars")
        else {k: v[1] for k, v in FINANCE_FIELDS.items()},
        "limitations": [
            "explicit identities and returned window; no full-market/full-history claim",
            "bar labels are source labels; interval open/close convention unasserted",
        ]
        if name.endswith("bars")
        else [
            "latest source snapshot only; update date is neither report period nor announcement date",
            "float32 precision; zero fields and unknown native fields retained, no industry formula inference",
            "net profit follows vendor native definition; parent/consolidated scope is not inferred",
        ],
    }
