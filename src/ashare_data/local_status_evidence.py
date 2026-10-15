"""Retained, locally reviewed status facts; never inferred from absent quotes.

These report adapters reuse the local acceptance evidence formats. The report
digest identifies the reviewed interpretation; raw hashes identify its sources.
They are retrospective local research facts, not point-in-time certification.
"""

from datetime import date, timedelta
import gzip
import hashlib
from html import unescape
import json
import re
import zlib


def verified_raw(root, sha, *, pdf=False):
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("invalid source digest")
    try:
        raw = gzip.decompress((root / "raw" / (sha + ".gz")).read_bytes())
    except (EOFError, zlib.error) as error:
        raise ValueError("invalid compressed status source") from error
    if hashlib.sha256(raw).hexdigest() != sha or (pdf and not raw.startswith(b"%PDF")):
        raise ValueError("retained status source mismatch")
    return raw


def _load(root, filename):
    path = root / "reports" / filename
    if not path.exists():
        return None, None
    raw = path.read_bytes()
    return json.loads(raw), dict(report=filename, report_sha256=hashlib.sha256(raw).hexdigest())


def _intervals(root, filename, kind, symbol=None):
    data, provenance = _load(root, filename)
    if data is None:
        return [], []
    intervals, errors = [], []
    for item in data["intervals"]:
        if symbol is not None and item.get("symbol") != symbol:
            continue
        try:
            if date.fromisoformat(item["start"]) >= date.fromisoformat(item["end_exclusive"]):
                raise ValueError("invalid interval")
            if not item["receipts"]:
                raise ValueError("no status evidence")
            for receipt in item["receipts"]:
                raw = verified_raw(root, receipt["sha256"], pdf=kind == "issuer_notice")
                if kind == "exchange_notice_republication":
                    if not item["original_author"] or not item["publisher"]:
                        raise ValueError("missing bulletin attribution")
                    content = re.sub(
                        r"\s+",
                        "",
                        unescape(re.sub(r"<[^>]+>", "", raw.decode(receipt["encoding"]))),
                    )
                    fragments = receipt["reviewed_text_fragments"]
                    if not fragments or any(
                        re.sub(r"\s+", "", f) not in content for f in fragments
                    ):
                        raise ValueError("reviewed exchange bulletin text absent")
            intervals.append(dict(item, evidence_kind=kind, **provenance))
        except (OSError, ValueError, KeyError, LookupError, EOFError, zlib.error) as error:
            errors.append(dict(symbol=item.get("symbol"), error=str(error)))
    return intervals, errors


def issuer_status_evidence(root, symbol=None):
    return _intervals(root, "issuer-suspension-evidence.json", "issuer_notice", symbol)


def exchange_republication_evidence(root, symbol=None):
    return _intervals(
        root,
        "exchange-republished-suspension-evidence.json",
        "exchange_notice_republication",
        symbol,
    )


def _query_status_evidence(store, symbol):
    """Fail closed on damaged relevant evidence, rather than filling through it."""
    intervals = []
    for loader in (issuer_status_evidence, exchange_republication_evidence):
        items, errors = loader(store.root, symbol)
        if errors:
            raise ValueError("invalid retained status evidence: " + json.dumps(errors))
        intervals.extend(items)
    for event in store.code_events():
        if symbol in (event["old_symbol"], event["new_symbol"]) and event.get("halt_start"):
            verified_raw(store.root, event["receipt"], pdf=True)
            intervals.append(
                dict(
                    symbol=symbol,
                    start=event["halt_start"],
                    end_exclusive=(
                        date.fromisoformat(event["halt_end"]) + timedelta(days=1)
                    ).isoformat(),
                    status="trading_suspended",
                    evidence_kind="issuer_code_change_notice",
                    receipts=[dict(sha256=event["receipt"], url=event.get("url"))],
                )
            )
    data, provenance = _load(store.root, "source-status-conflict-002647.json")
    if data is not None and data["symbol"] == symbol:
        receipts = [r for notice in data["issuer_notices"] for r in notice["receipts"]]
        if not receipts or not data["announced_suspension_start"] < data["announced_resumption"]:
            raise ValueError("invalid retained suspension followup")
        for receipt in receipts:
            verified_raw(store.root, receipt["sha256"], pdf=True)
        intervals.append(
            dict(
                symbol=symbol,
                start=data["announced_suspension_start"],
                end_exclusive=data["announced_resumption"],
                status="trading_suspended",
                evidence_kind="issuer_notice",
                receipts=receipts,
                **provenance,
            )
        )
    data, provenance = _load(store.root, "trade-status-followup.json")
    if data is not None and data["symbol"] == symbol:
        notice = data["issuer_notice"]
        verified_raw(store.root, notice["sha256"], pdf=True)
        start = (
            date.fromisoformat(notice["stated_final_delisting_period_trading_day"])
            + timedelta(days=1)
        ).isoformat()
        end = notice["stated_formal_exit"]
        if not start <= data["date"] < end:
            raise ValueError("invalid final-trading-day followup interval")
        intervals.append(
            dict(
                symbol=symbol,
                start=start,
                end_exclusive=end,
                status="inferred_nontrading_after_final_session",
                evidence_kind="issuer_final_trading_day_inference",
                receipts=[notice],
                **provenance,
            )
        )
    return intervals


def query_status_evidence(store, symbol):
    try:
        intervals = _query_status_evidence(store, symbol)
        for item in intervals:
            for key in ("start", "end_exclusive"):
                if date.fromisoformat(item[key]).isoformat() != item[key]:
                    raise ValueError("noncanonical status interval date")
            if item["start"] >= item["end_exclusive"] or item.get(
                "status", "trading_suspended"
            ) not in (
                "trading_suspended",
                "listing_suspended",
                "inferred_nontrading_after_final_session",
            ):
                raise ValueError("unsupported nontrading status or interval")
        return intervals
    except (KeyError, TypeError, AttributeError, EOFError, zlib.error) as error:
        raise ValueError("invalid retained status evidence structure") from error
