"""Pinned CMES factor observations and price-only daily projections, entirely offline."""

from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
import csv
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from ashare_data.core.model import DataError, canonical, day, digest, require, symbol
from ashare_data.storage.immutable import identifier, immutable_write

SCHEMA = "ad.cmes-factors.v1"
MAX_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024
PRICE_FIELDS = ("open", "high", "low", "close")
POLICY = {
    "id": "cmes-factor-ratio.v1",
    "matching": "last source effective date <= requested date; inclusive; no backfill",
    "hfq": "price multiplier",
    "qfq": "price divisor; hfq * qfq is constant within each security snapshot",
    "pre": "raw * hfq(date) / hfq(basis_date)",
    "post": "raw * hfq(date) / hfq(first source anchor)",
    "basis_date": "explicit factor cutoff for both modes; every bar date <= cutoff <= through",
    "pair_relative_tolerance": "1e-10",
    "decimal_precision": 34,
    "rounding": "ROUND_HALF_EVEN; no price tick quantization",
}
LIMITATIONS = [
    "Retrospective provider factors; a fixed version does not prove historical availability.",
    "Only OHLC is adjusted. Volume, amount, money and source preclose remain raw.",
    "Factor effective dates and the 1900 anchor are not listing, dividend payment or share credit dates.",
    "Factors do not replace corporate actions or authorize account/execution changes.",
    "Pin the raw price release/receipt as well as this factor dataset for reproducible research.",
]


def _positive(value):
    try:
        require(
            isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool),
            "CMES_FACTOR_VALUE",
            "Expected a positive decimal",
        )
        text = str(value)
        require(len(text) <= 128, "CMES_FACTOR_VALUE", "Decimal exceeds the input bound")
        number = Decimal(text)
        require(
            number.is_finite() and Decimal("1e-30") <= number < Decimal("1e30"),
            "CMES_FACTOR_VALUE",
            "Expected a bounded positive finite decimal",
        )
        return number
    except (InvalidOperation, ValueError) as exc:
        raise DataError("CMES_FACTOR_VALUE", "Invalid decimal") from exc


def _mapping(members):
    require(
        isinstance(members, dict) and 1 <= len(members) <= 10000,
        "CMES_FACTOR_INPUT",
        "Provide an explicit security-to-CSV-member mapping",
    )
    for security, member in members.items():
        if not (isinstance(security, str) and re.fullmatch(r"BJ\.[0-9]{6}", security)):
            symbol(security)
        require(
            isinstance(member, str) and re.fullmatch(r"(?:SH|SZ|BJ)\.[0-9]{6}\.csv", member),
            "CMES_FACTOR_INPUT",
            "Use exact CMES CSV member names; aliases are not inferred",
        )
    require(len(set(members.values())) == len(members), "CMES_FACTOR_INPUT", "Duplicate member")
    return dict(sorted(members.items()))


def _parse_csv(raw, kind):
    """Both source conventions use the same parser; preserve their exact decimal text."""
    try:
        require(0 < len(raw) <= MAX_MEMBER_BYTES, "CMES_FACTOR_INPUT", "CSV exceeds bounds")
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        field = kind + "_factor"
        require(reader.fieldnames == ["date", field], "CMES_FACTOR_SCHEMA", "Unexpected CSV fields")
        events = {}
        for row in reader:
            require(set(row) == {"date", field}, "CMES_FACTOR_SCHEMA", "Malformed CSV row")
            label = day(row["date"])
            require(label not in events, "CMES_FACTOR_SCHEMA", "Duplicate factor effective date")
            _positive(row[field])
            events[label] = row[field]
            require(len(events) <= 10000, "CMES_FACTOR_INPUT", "Too many factor events")
        require(events, "CMES_FACTOR_SCHEMA", "Empty factor history")
        return events
    except (UnicodeError, csv.Error) as exc:
        raise DataError("CMES_FACTOR_SCHEMA", "Invalid factor CSV") from exc


def _archive(path, members, kind):
    path = Path(path).expanduser()
    try:
        require(path.stat().st_size <= MAX_BYTES, "CMES_FACTOR_INPUT", "Archive exceeds 64 MiB")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, "CMES_FACTOR_INPUT", "Archive exceeds 64 MiB")
        entries, total_bytes = {}, 0
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            require(len(infos) <= 20000, "CMES_FACTOR_INPUT", "Too many ZIP members")
            inventory = {}
            for info in infos:
                inventory.setdefault(info.filename, []).append(info)
            for security, member in members.items():
                matches = inventory.get(member, [])
                require(
                    len(matches) == 1,
                    "CMES_FACTOR_MEMBER",
                    "Missing or duplicate ZIP member",
                    {"kind": kind, "member": member},
                )
                info = matches[0]
                require(
                    0 < info.file_size <= MAX_MEMBER_BYTES,
                    "CMES_FACTOR_INPUT",
                    "CSV member exceeds bounds",
                )
                total_bytes += info.file_size
                require(
                    total_bytes <= MAX_BYTES, "CMES_FACTOR_INPUT", "Selected CSVs exceed 64 MiB"
                )
                content = archive.read(info)
                _parse_csv(content, kind)
                entries[security] = {
                    "member": member,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "csv": content.decode("utf-8"),
                }
        return {"archive_sha256": hashlib.sha256(raw).hexdigest(), "members": entries}
    except (OSError, zipfile.BadZipFile, RuntimeError, UnicodeError) as exc:
        raise DataError("CMES_FACTOR_INPUT", "Cannot read the selected factor archive") from exc


def _series(document):
    require(
        document.get("schema") == SCHEMA and document.get("policy") == POLICY,
        "CMES_FACTOR_SCHEMA",
        "Unsupported factor schema or policy",
    )
    through = day(document["through"])
    members = _mapping(document["members"])
    sources = document["sources"]
    require(set(sources) == {"hfq", "qfq"}, "CMES_FACTOR_SCHEMA", "Both factor sources required")
    parsed = {}
    for kind, source in sources.items():
        identifier(source["archive_sha256"])
        require(
            set(source["members"]) == set(members),
            "CMES_FACTOR_SCHEMA",
            "Factor security scopes differ",
        )
        parsed[kind] = {}
        for security, entry in source["members"].items():
            raw = entry["csv"].encode("utf-8")
            require(
                entry["member"] == members[security]
                and hashlib.sha256(raw).hexdigest() == identifier(entry["sha256"]),
                "CMES_FACTOR_INTEGRITY",
                "Factor member identity differs",
            )
            parsed[kind][security] = _parse_csv(raw, kind)
    result = {}
    for security in members:
        hfq, qfq = parsed["hfq"][security], parsed["qfq"][security]
        require(
            hfq.keys() == qfq.keys(),
            "CMES_FACTOR_PAIR",
            "Factor event dates differ",
            {"security": security},
        )
        labels = sorted(hfq)
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            anchor = _positive(hfq[labels[0]]) * _positive(qfq[labels[0]])
            for label in labels:
                product = _positive(hfq[label]) * _positive(qfq[label])
                require(
                    abs(product / anchor - 1) <= Decimal(POLICY["pair_relative_tolerance"]),
                    "CMES_FACTOR_PAIR",
                    "hfq/qfq do not have reciprocal normalization",
                    {"security": security, "date": label},
                )
        selected = [d for d in labels if d <= through]
        require(selected, "CMES_FACTOR_SCOPE", "No source anchor through the selected cutoff")
        result[security] = [dict(date=d, hfq_factor=hfq[d], qfq_factor=qfq[d]) for d in selected]
    return result


def import_cmes_factors(store, *, hfq_path, qfq_path, members, through):
    """Seal selected CSV originals in an existing Store; never select a default dataset.

    ``members`` is supplied by the security-mapping owner, not derived from code prefixes.
    ``through`` is an explicit data cutoff, not a claim of historical publication time.
    """
    members = _mapping(members)
    document = {
        "schema": SCHEMA,
        "policy": POLICY,
        "through": day(through),
        "members": members,
        "sources": {
            kind: _archive(path, members, kind)
            for kind, path in (("hfq", hfq_path), ("qfq", qfq_path))
        },
    }
    _series(document)
    raw = canonical(document)
    require(len(raw) <= MAX_BYTES, "CMES_FACTOR_INPUT", "Selected factor snapshot exceeds 64 MiB")
    dataset_id = hashlib.sha256(raw).hexdigest()
    with store._writer():
        root = store.root / "cmes_factors"
        root.mkdir(exist_ok=True)
        immutable_write(root / (dataset_id + ".json"), raw)
    return dataset_id


class CMESFactorView:
    """Load and verify a full SHA256 version once; later source replacements cannot affect it."""

    def __init__(self, store, dataset_id):
        self.dataset_id = identifier(dataset_id)
        path = store.root / "cmes_factors" / (self.dataset_id + ".json")
        try:
            require(
                path.stat().st_size <= MAX_BYTES, "CMES_FACTOR_INPUT", "Snapshot exceeds bounds"
            )
            raw = path.read_bytes()
            require(
                hashlib.sha256(raw).hexdigest() == self.dataset_id,
                "CMES_FACTOR_INTEGRITY",
                "Factor snapshot hash differs",
            )
            self._document = json.loads(raw)
            self._events = _series(self._document)
            self._dates = {s: [r["date"] for r in rows] for s, rows in self._events.items()}
        except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise DataError(
                "CMES_FACTOR_INTEGRITY", "Missing or malformed factor snapshot"
            ) from exc

    @property
    def through(self):
        return self._document["through"]

    def describe(self):
        return deepcopy(
            {
                "schema": SCHEMA,
                "dataset_id": self.dataset_id,
                "through": self.through,
                "policy": POLICY,
                "securities": list(self._events),
                "sources": {
                    kind: {
                        "archive_sha256": source["archive_sha256"],
                        "members": {
                            s: {k: v for k, v in entry.items() if k != "csv"}
                            for s, entry in source["members"].items()
                        },
                    }
                    for kind, source in self._document["sources"].items()
                },
                "historical_pit": False,
                "network_used": False,
                "limitations": LIMITATIONS,
            }
        )

    def _at(self, security, label):
        label = day(label)
        require(label <= self.through, "CMES_FACTOR_SCOPE", "Date exceeds the fixed factor cutoff")
        require(
            security in self._events,
            "CMES_FACTOR_MISSING",
            "Security not in factor snapshot",
            {"security": security},
        )
        index = bisect_right(self._dates[security], label) - 1
        require(
            index >= 0,
            "CMES_FACTOR_MISSING",
            "No factor at or before the requested date",
            {"security": security, "date": label},
        )
        return self._events[security][index]

    def factor_at(self, security, date):
        event = self._at(security, date)
        return {
            "security": security,
            "requested_date": date,
            "dataset_id": self.dataset_id,
            "effective_date": event["date"],
            "hfq_factor": event["hfq_factor"],
            "qfq_factor": event["qfq_factor"],
        }

    def adjust_prices(self, raw, *, adjustment, basis_date):
        return adjust_prices(raw, factors=self, adjustment=adjustment, basis_date=basis_date)


def adjust_prices(raw, *, factors=None, adjustment=None, basis_date=None):
    """Project the existing daily-price envelope; raw is unchanged and needs no factor input."""
    require(adjustment in (None, "pre", "post"), "CMES_FACTOR_ARGUMENT", "Use None/pre/post")
    if adjustment is None:
        require(basis_date is None, "CMES_FACTOR_ARGUMENT", "Raw prices have no adjustment basis")
        return raw
    require(isinstance(factors, CMESFactorView), "CMES_FACTOR_ARGUMENT", "Pinned factors required")
    basis_date = day(basis_date)
    require(basis_date <= factors.through, "CMES_FACTOR_SCOPE", "Basis exceeds the fixed cutoff")
    require(
        isinstance(raw, dict)
        and raw.get("price_basis") == "raw"
        and isinstance(raw.get("rows"), list)
        and len(raw["rows"]) <= 100000
        and isinstance(raw.get("receipt"), dict),
        "CMES_FACTOR_BARS",
        "Expected a bounded raw daily-price result with its receipt",
    )
    if "end" in raw:
        require(day(raw["end"]) <= basis_date, "CMES_FACTOR_SCOPE", "Query end exceeds basis")
    input_hash = digest(raw["rows"])
    require(
        raw["receipt"].get("rows_sha256", input_hash) == input_hash,
        "CMES_FACTOR_BARS",
        "Raw rows differ from their receipt",
    )
    result = deepcopy(raw)
    anchors, seen = {}, set()
    with localcontext(Context(prec=POLICY["decimal_precision"], rounding=ROUND_HALF_EVEN)):
        for row in result["rows"]:
            require(
                isinstance(row, dict) and {"security", "date", *PRICE_FIELDS} <= row.keys(),
                "CMES_FACTOR_BARS",
                "Missing daily bar fields",
            )
            security, label = row["security"], day(row["date"])
            require(label <= basis_date, "CMES_FACTOR_SCOPE", "Bar date exceeds basis")
            require((security, label) not in seen, "CMES_FACTOR_BARS", "Duplicate security/date")
            seen.add((security, label))
            event = factors._at(security, label)
            if security not in anchors:
                anchors[security] = (
                    factors._at(security, basis_date)
                    if adjustment == "pre"
                    else factors._events[security][0]
                )
            anchor = anchors[security]
            ratio = _positive(event["hfq_factor"]) / _positive(anchor["hfq_factor"])
            for field in PRICE_FIELDS:
                if row[field] is not None:
                    row[field] = str(_positive(row[field]) * ratio)
            row.update(adjustment_factor=str(ratio), factor_effective_date=event["date"])
    description = {
        "policy": deepcopy(POLICY),
        "dataset_id": factors.dataset_id,
        "mode": adjustment,
        "basis_date": basis_date,
        "through": factors.through,
        "price_anchor_dates": {
            s: (basis_date if adjustment == "pre" else r["date"]) for s, r in anchors.items()
        },
        "anchor_effective_dates": {s: r["date"] for s, r in anchors.items()},
        "adjusted_fields": list(PRICE_FIELDS),
        "unadjusted_fields": sorted({k for r in raw["rows"] for k in r} - set(PRICE_FIELDS)),
        "historical_pit": False,
        "execution_permission": False,
    }
    receipt = {
        "schema": "ad-adjusted-daily-read.v1",
        "raw_receipt": deepcopy(raw["receipt"]),
        "raw_rows_sha256": input_hash,
        "rows_sha256": digest(result["rows"]),
        "adjustment": description,
        "network_used": False,
    }
    result.update(
        price_basis=adjustment,
        adjustment=description,
        receipt={**receipt, "receipt_sha256": digest(receipt)},
        raw_limitations=result.get("limitations", []),
        limitations=list(LIMITATIONS),
    )
    return result
