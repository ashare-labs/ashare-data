"""Explicit, offline access to provider backfills under a later quote code.

These observations stay separate from native-code bars and their coverage.
An immutable capture and a retained issuer code-change notice are both required.
"""

import base64
import gzip
import hashlib
import json
from pathlib import Path
import re
import zlib

from ashare_data.market.client import _security
from ashare_data.providers.baostock_collection import FIELDS, read_capture, verify_capture
from ashare_data.storage.local_daily import digest, parse_day
from ashare_data.providers.daily_sources import decimal, validate_bars


def read_alias_capture(path):
    path = Path(path)
    if not path.is_dir():
        return read_capture(path)
    receipt = json.loads((path / "receipt.json").read_text())
    return dict(
        receipt=receipt,
        request=receipt["request"],
        sdk_rows=json.loads((path / "sdk-rows.json").read_text()),
        raw_requests=[
            base64.b64encode((path / f"page-{i:02d}.request.bin").read_bytes()).decode()
            for i in range(1, len(receipt["pages"]) + 1)
        ],
        raw_responses=[
            base64.b64encode((path / f"page-{i:02d}.response.bin").read_bytes()).decode()
            for i in range(1, len(receipt["pages"]) + 1)
        ],
    )


def get_alias_price(store, security, capture, start="2010-01-01", end=None):
    """Return a whole provider series with explicit historical/provider identities.

    No network, quote ingestion, native coverage changes or mixed-source fields.
    The caller explicitly selects the capture version; no implicit latest alias.
    """
    import pandas as pd

    symbol = _security(security)
    end = end or store.meta("end")
    parse_day(start)
    parse_day(end)
    if start > end:
        raise ValueError("invalid date interval")
    try:
        envelope = read_alias_capture(capture)
        raw_rows, quality = verify_capture(envelope)
        request = envelope["request"]
        params = request["params"]
    except (
        OSError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        EOFError,
        zlib.error,
    ) as error:
        raise ValueError("invalid alias capture structure or compressed content") from error
    if (
        request["method"] != "query_history_k_data_plus"
        or params.get("fields") != FIELDS
        or params.get("frequency") != "d"
        or params.get("adjustflag") != "3"
    ):
        raise ValueError("alias capture must contain raw daily OHLCV and amount fields")
    provider_symbol = params["code"].replace(".", "")
    events = [
        event
        for event in store.code_events()
        if event["old_symbol"] == symbol and event["new_symbol"] == provider_symbol
    ]
    if len(events) != 1:
        raise ValueError("alias capture needs one matching retained issuer code-change event")
    event = events[0]
    parse_day(event["issuer_ipo"])
    parse_day(event["effective_date"])
    if event["issuer_ipo"] >= event["effective_date"]:
        raise ValueError("invalid alias effective interval")
    sha = event["receipt"]
    if not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("invalid issuer notice identity")
    try:
        notice = gzip.decompress((store.root / "raw" / (sha + ".gz")).read_bytes())
    except (OSError, EOFError, zlib.error) as error:
        raise ValueError("invalid retained issuer notice compressed content") from error
    if hashlib.sha256(notice).hexdigest() != sha or not notice.startswith(b"%PDF"):
        raise ValueError("issuer code-change notice hash or format mismatch")
    converted = []
    for row in raw_rows:
        parse_day(row["date"])
        if (
            row["code"] != params["code"]
            or not params["start_date"] <= row["date"] <= params["end_date"]
            or row["adjustflag"] != "3"
            or row["tradestatus"] not in ("0", "1")
            or row["isST"] not in ("0", "1")
        ):
            raise ValueError("unsupported or inconsistent alias daily identity/flags")
        converted.append(
            dict(
                symbol=symbol,
                provider_symbol=provider_symbol,
                date=row["date"],
                source="baostock_alias",
                **{
                    key: decimal(row[key])
                    for key in ("open", "high", "low", "close", "volume", "amount", "preclose")
                },
                tradestatus=row["tradestatus"],
                is_st=row["isST"],
                capture_id=envelope["receipt"]["receipt_id"],
                code_event_id=digest(event),
                issuer_notice_sha256=sha,
                observed_at=envelope["receipt"]["operation_completed_at"],
            )
        )
    validate_bars(
        [row for row in converted if row["tradestatus"] == "1"],
        params["start_date"],
        params["end_date"],
    )
    days = [
        row[0]
        for row in store.db.execute(
            "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
        )
    ]
    allowed = {
        d
        for d in store.expected(symbol, start, end, days)
        if event["issuer_ipo"] <= d < event["effective_date"]
    }
    columns = (
        "symbol provider_symbol date source open high low close volume amount preclose "
        "tradestatus is_st capture_id code_event_id issuer_notice_sha256 observed_at"
    ).split()
    frame = pd.DataFrame(
        [row for row in converted if row["date"] in allowed], columns=columns
    ).sort_values("date", ignore_index=True)
    frame.attrs = dict(
        source="baostock_alias",
        historical_symbol=symbol,
        provider_symbol=provider_symbol,
        capture=str(Path(capture).resolve()),
        capture_id=envelope["receipt"]["receipt_id"],
        code_event=event,
        valid_from=event["issuer_ipo"],
        valid_to_exclusive=event["effective_date"],
        wire_verification=quality,
        price_basis="raw",
        price_unit="CNY/share",
        volume_unit="shares",
        amount_unit="CNY",
        included_in_native_coverage=False,
        interpretation="Retrospective provider backfill under a later code; explicitly mapped by an issuer notice. Not an original historical-code quote or point-in-time publication.",
    )
    return frame
