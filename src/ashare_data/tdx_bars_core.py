"""Bounded native-bar collection. Original orchestration; no TDX decoder copied."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import time

SCHEMA = "ad.tdx-native-bars.v1"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 32
MAX_OFFSET = 65535  # The reviewed SDK/protocol encodes start as u16.


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def request(security, host, frequency, start_offset, count, page_size, max_pages,
            request_timeout, min_interval):
    check(isinstance(security, str) and re.fullmatch(r"\d{6}\.(XSHG|XSHE)", security),
          "security must be six digits plus .XSHG/.XSHE")
    check(isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,253}:7709", host),
          "explicit TDX host:7709 required")
    check(frequency in ("1m", "5m"), "only native 1m/5m are supported")
    for value, low, high, name in ((start_offset, 0, MAX_OFFSET, "start_offset"),
                                  (count, 1, MAX_PAGES * 800, "count"),
                                  (page_size, 1, 800, "page_size"),
                                  (max_pages, 1, MAX_PAGES, "max_pages")):
        check(type(value) is int and low <= value <= high, f"invalid bounded {name}")
    for value, low, high, name in ((request_timeout, 1, 15, "request_timeout"),
                                  (min_interval, 1, 10, "min_interval")):
        check(type(value) in (int, float) and low <= value <= high, f"invalid {name}")
    return dict(security=security, host=host, frequency=frequency, start_offset=start_offset,
                count=count, page_size=page_size, max_pages=max_pages,
                request_timeout=request_timeout, min_interval=min_interval)


def validate_page(rows, raw, requested, received_at):
    """Check retained native records; hashes bind the SDK decode to its payload.

    This is not an independent decoder or market authenticity certification.
    The payload does not echo the request's symbol or offset.
    """
    check(type(rows) is list and len(rows) <= requested, "row count exceeds request")
    check(2 <= len(raw) <= MAX_PAGE_BYTES, "invalid payload length")
    check(int.from_bytes(raw[:2], "little") == len(rows), "wire count mismatch")
    check(raw[2:] == b"".join(bytes.fromhex(r["record_hex"]) for r in rows),
          "raw records do not cover payload exactly")
    observed = datetime.fromisoformat(received_at)
    check(observed.utcoffset() is not None, "receipt needs timezone")
    labels = []
    for row in rows:
        dt = datetime.fromisoformat(row["time"])
        check(dt.utcoffset() is not None and dt.second == dt.microsecond == 0,
              "invalid minute label")
        check(dt <= observed, "source label after receipt")
        prices = []
        for name in ("open", "high", "low", "close"):
            milli = row[name + "_price_milli"]
            check(type(milli) is int and 0 < milli < 10**15, "invalid price integer")
            price = Decimal(milli) / 1000
            check(Decimal(str(row[name])) == price, "price/decoded integer mismatch")
            prices.append(price)
        op, high, low, close = prices
        check(low <= min(op, close) <= max(op, close) <= high, "invalid OHLC")
        for name in ("volume_wire_value", "volume_lots", "amount"):
            check(type(row[name]) in (int, float), "invalid numeric source field")
            value = Decimal(str(row[name]))
            check(value.is_finite() and 0 <= value < Decimal("1e20"), "invalid quantity")
        for name in ("volume_raw", "amount_raw"):
            check(type(row[name]) is int and 0 <= row[name] < 2**32, "invalid raw u32")
        labels.append(dt)
    check(labels == sorted(labels) and len(set(labels)) == len(labels),
          "duplicate or reversed page labels")
    return labels


def error_kind(exc):
    text = (type(exc).__name__ + " " + str(exc)).lower()
    if any(x in text for x in ("429", "rate limit", "throttl")):
        return "rate_limited"
    if any(x in text for x in ("403", "401", "unauthorized", "forbidden", "authentication")):
        return "authorization_failure"
    if isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text:
        return "timeout"
    return "source_error"


def save_journal(root, journal):
    temporary = root / "journal.tmp"
    temporary.write_bytes(encoded(journal))
    temporary.replace(root / "journal.json")


def collect_pages(root, config, fetch, identity, *, sleep=time.sleep):
    """One connection, actual-row offsets, short pages continue, explicit local caps."""
    root = Path(root)
    journal = dict(schema=SCHEMA, sdk=identity, pages=[], stop_reason="local_page_limit",
                   accepted_rows=0, next_offset=config["start_offset"], complete_history=False)
    known, previous_min = set(), None
    last_request = None
    save_journal(root, journal)
    for index in range(config["max_pages"]):
        offset = journal["next_offset"]
        if offset > MAX_OFFSET:
            journal["stop_reason"] = "protocol_offset_limit"
            break
        if last_request is not None:
            sleep(max(0, config["min_interval"] - (time.monotonic() - last_request)))
        count = min(config["page_size"], config["count"] - journal["accepted_rows"])
        entry = dict(index=index, offset=offset, requested=count, request_started_at=now(),
                     status="request_pending", accepted=False)
        journal["pages"].append(entry)
        save_journal(root, journal)
        last_request = time.monotonic()
        try:
            page = fetch(offset, count)
            entry["response_completed_at"] = now()
            raw = bytes(page.raw_payload)
            rows = [{**asdict(bar), "time": bar.time.isoformat()} for bar in page.bars]
            # Retain returned data even if validation below rejects it.
            stem = f"page-{index:03d}"
            check(len(raw) <= MAX_PAGE_BYTES and len(rows) <= 800, "oversized source page")
            (root / (stem + ".bin")).write_bytes(raw)
            (root / (stem + ".json")).write_bytes(encoded(rows))
            entry.update(payload_file=stem + ".bin", rows_file=stem + ".json",
                         payload_sha256=sha(raw), rows_sha256=sha(encoded(rows)),
                         received=len(rows), first=rows[0]["time"] if rows else None,
                         last=rows[-1]["time"] if rows else None)
            code = ("sh" if config["security"].endswith("XSHG") else "sz") + config["security"][:6]
            check(page.full_code == code and page.start == offset and page.request_count == count
                  and page.period_name == config["frequency"] and page.adjust_mode_raw == 0,
                  "SDK request context mismatch")
            labels = validate_page(rows, raw, count, entry["response_completed_at"])
            if not labels:
                entry["status"] = journal["stop_reason"] = "remote_empty_page"
                break
            if known.intersection(labels):
                entry["status"] = journal["stop_reason"] = "repeated_or_overlapping_page"
                break
            if previous_min is not None and max(labels) >= previous_min:
                entry["status"] = journal["stop_reason"] = "non_monotonic_page"
                break
            entry.update(accepted=True, status="accepted" if len(rows) == count else "short_page_continue")
            known.update(labels)
            previous_min = min(labels)
            journal["accepted_rows"] += len(rows)
            journal["next_offset"] += len(rows)
            if journal["accepted_rows"] == config["count"]:
                journal["stop_reason"] = "requested_count_reached"
                break
        except Exception as exc:
            entry.update(status=error_kind(exc), error_type=type(exc).__name__,
                         error=str(exc)[:400], failed_at=now())
            journal["stop_reason"] = entry["status"]
            break
        finally:
            save_journal(root, journal)
    journal["completed_at"] = now()
    save_journal(root, journal)
    return journal
