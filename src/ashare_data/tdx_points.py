"""Pinned, offline historical point observations; no network client or bar synthesis."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import struct

from .model import DataError, canonical, day, digest, require, symbol, timestamp
from .storage import identifier, immutable_write

SCHEMA = "ad.tdx-history-points.v1"
MAX_BYTES = 1024 * 1024
ROLE = "retrospective_point_research"
SERIES_FIELDS = {"exchange", "market_id", "code", "trading_date", "points", "reserved_zero",
                 "prev_close", "open_price", "date_selector_raw", "raw_payload"}
POINT_FIELDS = {"index", "time_label", "time", "price", "price_milli", "volume", "price_field",
                "avg_field", "avg_price", "price_raw", "avg_raw", "price_delta_raw",
                "aux_delta_raw", "record_hex"}
NULL_FIELDS = {"price_field", "avg_field", "avg_price", "avg_raw"}


@dataclass(frozen=True)
class TDXPointInput:
    security: str
    session_date: str
    response_path: str
    response_sha256: str
    host: str
    captured_start: str
    captured_end: str
    decoder_version: str
    decoder_commit: str
    decoder_binary_sha256: str


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        require(key not in out, "TDX_SCHEMA", "duplicate JSON key")
        out[key] = value
    return out


def _bad_constant(value):
    raise ValueError("nonfinite JSON number")


def _json(raw, *, exact=False):
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_constant,
                          **({"parse_float": Decimal} if exact else {}))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DataError("TDX_SCHEMA", "invalid bounded JSON") from exc


def _file(path, sha=None):
    try:
        path = Path(path).expanduser()
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BYTES,
                "TDX_INPUT", "provide a regular local file of at most 1 MiB")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, "TDX_INPUT", "source exceeds 1 MiB")
    except (OSError, TypeError, ValueError) as exc:
        raise DataError("TDX_INPUT", "cannot read pinned local file") from exc
    if sha is not None:
        require(hashlib.sha256(raw).hexdigest() == identifier(sha),
                "TDX_INTEGRITY", "pinned file hash mismatch")
    return raw


def _keys(value, keys):
    require(type(value) is dict and set(value) == keys, "TDX_SCHEMA", "unexpected source fields")


def _integer(value, *, negative=False):
    require(type(value) is int and (-2**63 if negative else 0) <= value < 2**63,
            "TDX_SCHEMA", "expected bounded source integer")
    return value


def _hex(value):
    require(type(value) is str and len(value) <= 2 * MAX_BYTES and len(value) % 2 == 0
            and re.fullmatch("[0-9a-f]*", value), "TDX_SCHEMA", "invalid hexadecimal source bytes")
    return bytes.fromhex(value)


def _record_values(raw):
    """Check the three signed wire integers against the supplied decoded record."""
    values, pos = [], 0
    for _ in range(3):
        require(pos < len(raw), "TDX_WIRE", "truncated source record")
        first = raw[pos]
        magnitude, shift, n = first & 63, 6, 1
        byte = first
        pos += 1
        while byte & 128:
            require(pos < len(raw) and n < 10, "TDX_WIRE", "invalid variable-length integer")
            byte = raw[pos]
            magnitude += (byte & 127) << shift
            pos, shift, n = pos + 1, shift + 7, n + 1
        value = -magnitude if first & 64 else magnitude
        values.append(_integer(value, negative=True))
    require(pos == len(raw), "TDX_WIRE", "unconsumed source record bytes")
    return values


def _extract(source):
    require(isinstance(source, TDXPointInput), "TDX_INPUT", "provide TDXPointInput")
    security, session = symbol(source.security), day(source.session_date)
    require(type(source.host) is str and re.fullmatch(r"[A-Za-z0-9.-]{1,253}:7709", source.host),
            "TDX_INPUT", "provide explicit public-node host and port")
    start, end = timestamp(source.captured_start), timestamp(source.captured_end)
    require(start <= end <= start + timedelta(minutes=10) and start.date().isoformat() > session,
            "TDX_CLOCK", "historical capture must be bounded and after the requested session")
    require(type(source.decoder_version) is str and re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", source.decoder_version)
            and type(source.decoder_commit) is str and re.fullmatch(r"[0-9a-f]{40}", source.decoder_commit),
            "TDX_INPUT", "pin decoder version and source commit")
    identifier(source.decoder_binary_sha256)
    response = _json(_file(source.response_path, source.response_sha256), exact=True)
    _keys(response, SERIES_FIELDS)
    exchange, market = ("sz", 0) if security.endswith("XSHE") else ("sh", 1)
    require(response["exchange"] == exchange and type(response["market_id"]) is int
            and response["market_id"] == market and response["code"] == security[:6]
            and response["trading_date"] == session, "TDX_SCOPE", "source request identity mismatch")
    require(all(response[k] is None for k in ("reserved_zero", "open_price", "date_selector_raw")),
            "TDX_SCHEMA", "unexpected historical-series fields")
    points = response["points"]
    require(type(points) is list and 1 <= len(points) <= 240,
            "TDX_SCHEMA", "expected 1..240 indexed source points")
    packet = response["raw_payload"]
    _keys(packet, {"encoding", "data", "sha256"})
    require(packet["encoding"] == "hex", "TDX_SCHEMA", "raw payload must be hexadecimal")
    raw = _hex(packet["data"])
    require(hashlib.sha256(raw).hexdigest() == identifier(packet["sha256"]),
            "TDX_WIRE", "raw payload hash mismatch")
    require(len(raw) >= 6 and int.from_bytes(raw[:2], "little") == len(points),
            "TDX_WIRE", "payload point count mismatch")
    # The header's float is source prev-close, not a historical current quote.
    prev_close = response["prev_close"]
    require(type(prev_close) in (int, Decimal) and Decimal(prev_close).is_finite()
            and 0 < Decimal(prev_close) < Decimal("1e12"), "TDX_SCHEMA", "invalid header previous close")
    require(struct.pack("<f", float(prev_close)) == raw[2:6], "TDX_WIRE", "header mismatch")
    rows, records, price = [], [], 0
    for index, point in enumerate(points):
        _keys(point, POINT_FIELDS)
        require(type(point["index"]) is int and point["index"] == index
                and all(point[k] is None for k in NULL_FIELDS), "TDX_SCHEMA", "unexpected point fields")
        minute = 571 + index if index < 120 else 661 + index
        label = f"{minute // 60:02}:{minute % 60:02}"
        at = f"{session}T{label}:00+08:00"
        require(point["time_label"] == label and point["time"] == at,
                "TDX_LABEL", "label disagrees with requested date and protocol index")
        delta = _integer(point["price_delta_raw"], negative=True)
        aux = _integer(point["aux_delta_raw"], negative=True)
        volume = _integer(point["volume"])
        record = _hex(point["record_hex"])
        require(_record_values(record) == [delta, aux, volume], "TDX_WIRE", "decoded values mismatch")
        records.append(record)
        price += delta
        require(_integer(point["price_raw"]) == price and price > 0
                and _integer(point["price_milli"]) == price * 10,
                "TDX_WIRE", "decoded price accumulation mismatch")
        require(type(point["price"]) in (int, Decimal) and point["price"] == Decimal(price) / 100,
                "TDX_WIRE", "price disagrees with exact wire price")
        rows.append({"security": security, "kind": "historical_intraday_point", "source_index": index,
                     "source_label_at": at, "observed_at": end.isoformat(),
                     "values": {"price": format(Decimal(price) / 100, "f"),
                                "volume_lots": str(volume), "volume_shares": str(volume * 100)},
                     "source_record_hex": point["record_hex"], "executable": False if volume == 0 else None,
                     "quality": "source_zero_volume" if volume == 0 else "source_reported_point",
                     "strategy_visible": False, "historical_pit": False})
    require(raw[6:] == b"".join(records), "TDX_WIRE", "source record bytes disagree with payload")
    metadata = asdict(source)
    metadata["captured_start"], metadata["captured_end"] = start.isoformat(), end.isoformat()
    metadata["response_path"] = str(Path(source.response_path).expanduser().absolute())
    metadata["raw_payload_sha256"] = packet["sha256"]
    return metadata, rows


def import_points(store, source, *, source_kind):
    require(source_kind in ("local_tdx_history_capture", "synthetic_fixture"),
            "TDX_INPUT", "explicit source classification required")
    metadata, rows = _extract(source)
    body = {"schema": SCHEMA, "source_kind": source_kind, "source": metadata, "rows": rows,
            "protocol": "TDX 7709 / 0x0fb4", "timezone": "Asia/Shanghai",
            "price_basis": "provider_point_price_unchanged_no_client_adjustment",
            "units": {"price": "CNY/share", "volume_lots": "lots", "volume_shares": "lots * 100"},
            "volume_precision": "one source lot; converted shares do not recover sub-lot precision",
            "label_basis": "request date plus protocol point index; date/time are not echoed in payload",
            "coverage": {"point_count": len(rows), "full_240_point_grid": len(rows) == 240,
                         "complete_minute_ohlcv": False},
            "historical_pit": False, "strategy_visible": False, "live_execution_permission": False,
            "matching_eligibility": "not_certified; consumer must explicitly own its point-price model",
            "read_role": ROLE,
            "unknown": ["open", "high", "low", "minute_close_semantics", "amount", "bid", "ask",
                        "paused", "is_st", "price_limits", "historical_publication_at",
                        "historical_receive_at", "point_interval_boundaries", "provider_adjustment_guarantee"],
            "limitations": ["Retrospective chart points are not JQ current_data or complete minute bars.",
                            "A source label does not establish historical availability or tradability.",
                            "Zero-volume points cannot establish an executable price.",
                            "No standard answers, filling, resampling, daily-close substitution or trading logic."]}
    dataset_id = digest(body)
    with store._writer():
        root = store.root / "tdx_points"
        root.mkdir(exist_ok=True)
        immutable_write(root / (dataset_id + ".json"), canonical(body))
    return dataset_id


class TDXPointView:
    """Exact-label retrospective reads, separate from simulation as-of interfaces."""

    def __init__(self, store, dataset_id):
        self._root = store.root
        self.dataset_id = identifier(dataset_id)
        self._load()

    def _load(self):
        body = _json(_file(self._root / "tdx_points" / (self.dataset_id + ".json"), self.dataset_id))
        require(body.get("schema") == SCHEMA, "TDX_SCHEMA", "unsupported sealed schema")
        return body

    def describe(self):
        body = self._load()
        body.pop("rows")
        return {"dataset_id": self.dataset_id, **body}

    def read(self, security, *, at, role):
        require(role == ROLE, "TDX_ROLE", "only explicit retrospective_point_research is supported")
        body = self._load()
        at = timestamp(at, minute=True).isoformat()
        require(symbol(security) == body["source"]["security"]
                and at[:10] == body["source"]["session_date"], "TDX_SCOPE", "outside captured request")
        rows = [row for row in body["rows"] if row["source_label_at"] == at]
        return {"dataset_id": self.dataset_id, "role": role, "requested_label_at": at,
                "status": "available" if rows else "unavailable",
                "reason": None if rows else "NO_EXACT_SOURCE_POINT", "rows": rows,
                "historical_pit": False, "strategy_visible": False, "retrospective": True,
                "matching_eligibility": body["matching_eligibility"],
                "label_basis": body["label_basis"], "source": body["source"]}
