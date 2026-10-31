"""Streaming CMES files and explicit mappings; no network, extraction or price adjustment."""

from bisect import bisect_right
from contextlib import contextmanager
from copy import deepcopy
import csv
from datetime import datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import io
from io import TextIOWrapper
import json
from pathlib import Path
import re
import zipfile
from zipfile import ZipFile

from ashare_data.core.model import DataError, TZ, canonical, day, digest, require, symbol
from ashare_data.market.increment import exact_number, normalize_values
from ashare_data.storage.immutable import identifier, immutable_write
from ashare_data.storage.market_release import fingerprint, sha_file

MAPPING_VERSION = "cmes-files.v2"
# Different CMES exports of the same frequency can have different units.
BAR_PROFILES = {
    "daily-lots-kcny": {"table": "bars_1d", "series_id": 1, "volume": 100, "amount": 1000},
    # Index levels are not CNY/share. This price-only projection makes no claim
    # about vendor volume/amount units; original fields remain in the source ZIP.
    "daily-index-points": {"table": "bars_1d", "series_id": 1, "volume": None, "amount": None,
                           "asset_class": "index", "price_unit": "index_point"},
    "minute-shares-cny": {"table": "bars_1m", "series_id": 7, "volume": 1, "amount": 1},
    "minute-lots-cny": {"table": "bars_1m", "series_id": 7, "volume": 100, "amount": 1},
}
BAR_COLUMNS = dict(zip(
    ("open", "high", "low", "close", "volume", "amount"),
    ("开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"),
))


def normalize_security(value):
    """Map explicit SH/SZ/BJ identifiers; never infer venue from a bare number."""
    require(isinstance(value, str), "CMES_SECURITY", "Security must be a string")
    value = value.strip()
    match = re.fullmatch(r"(SH|SZ|BJ)\.([0-9]{6})", value)
    if match:
        venue, code = match.groups()
        return "BJ." + code if venue == "BJ" else code + {"SH": ".XSHG", "SZ": ".XSHE"}[venue]
    require(bool(re.fullmatch(r"[0-9]{6}\.(XSHG|XSHE)", value)),
            "CMES_SECURITY", "Use SH./SZ./BJ. or the canonical exchange-qualified identifier")
    return value


def native_security(value):
    value = normalize_security(value)
    return value if value.startswith("BJ.") else (
        "SH." if value.endswith(".XSHG") else "SZ."
    ) + value[:6]


def normalize_date(value):
    """CMES calendar previous dates also use YYYYMMDD."""
    value = str(value).strip()
    if re.fullmatch(r"[0-9]{8}", value):
        value = value[:4] + "-" + value[4:6] + "-" + value[6:]
    return day(value)


def _observation_time():
    return datetime.now(timezone.utc).isoformat()


def source_asset(path, *, kind, profile=None):
    """Observe bytes now; the importer freezes the first observation in its candidate."""
    path = Path(path).expanduser().resolve()
    require(path.is_file() and path.suffix.lower() in {".zip", ".csv"},
            "CMES_INPUT", "Choose an existing ZIP or CSV", {"path": str(path)})
    before = fingerprint(path)
    digest = sha_file(path)
    require(before == fingerprint(path), "CMES_INPUT_CHANGED", "Input changed while hashing")
    return {
        "asset_id": "cmes:" + digest, "source": "cmes", "source_version": digest,
        "path": str(path), "sha256": digest, "bytes": before[0], "kind": kind,
        "profile": profile, "mapping_version": MAPPING_VERSION,
        "original_retention": "external file; never copied or modified by importer",
        "observed_at": _observation_time(),
        "observed_at_semantics": "local file hash validation; first observation within this candidate import",
        "historical_available_at": None, "historical_pit_verified": False,
    }


@contextmanager
def csv_members(path, *, securities=None, bars=False):
    """Yield (member name, open-binary callable); skip unselected named bar members."""
    path = Path(path)
    selected = set(securities) if securities is not None else None

    def include(name):
        if name.endswith("/") or Path(name).name.startswith(".") or "__MACOSX" in Path(name).parts:
            return False
        require(Path(name).suffix.lower() == ".csv", "CMES_MEMBER", "Expected CSV members")
        if bars and selected is not None:
            try:
                security = normalize_security(Path(name).stem)
            except DataError:
                return True  # A multi-security CSV is filtered by its explicit code column.
            return security in selected
        return True

    if path.suffix.lower() == ".zip":
        with ZipFile(path) as archive:
            names = archive.namelist()
            require(len(names) == len(set(names)), "CMES_MEMBER", "Duplicate ZIP member names")
            members = [name for name in sorted(names) if include(name)]
            yield ((name, lambda name=name: archive.open(name)) for name in members)
    else:
        yield iter([(path.name, lambda: path.open("rb"))])


def csv_rows(open_binary, *, encoding, required):
    with open_binary() as binary, TextIOWrapper(binary, encoding=encoding, newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        header = reader.fieldnames
        require(header is not None and len(header) == len(set(header)) and set(required) <= set(header),
                "CMES_COLUMNS", "Missing or duplicate CMES columns", {"columns": header})
        for number, row in enumerate(reader, 2):
            require(None not in row and None not in row.values(),
                    "CMES_COLUMNS", "CSV record width differs from header", {"record": number})
            yield number, row


def scale(value, multiplier):
    if not value.strip():
        return None
    # Parse strings before multiplying, with sufficient precision for DECIMAL(38,12).
    number = exact_number(value)
    with localcontext() as context:
        context.prec = 60
        return str(number * Decimal(multiplier))


def bar_rows(path, *, profile, securities=None, encoding="utf-8-sig"):
    """Yield selected source records with dispositions and exact original locators."""
    require(profile in BAR_PROFILES, "CMES_PROFILE", "Choose an explicit CMES unit profile")
    contract = BAR_PROFILES[profile]
    selected = {normalize_security(s) for s in securities} if securities is not None else None
    with csv_members(path, securities=selected, bars=True) as members:
        for member, opener in members:
            try:
                member_security = normalize_security(Path(member).stem)
            except DataError:
                member_security = None
            for number, native in csv_rows(opener, encoding=encoding,
                                           required=["时间", *BAR_COLUMNS.values()]):
                security = normalize_security(native.get("股票代码") or member_security)
                require(member_security is None or member_security == security,
                        "CMES_SECURITY", "CSV code and member filename disagree",
                        {"member": member, "record": number})
                if selected is not None and security not in selected:
                    continue
                item = dict(security=security, source_symbol=native_security(security),
                            source_member=member, source_row=number, native=native,
                            table=contract["table"], series_id=contract["series_id"])
                try:
                    if contract["table"] == "bars_1d":
                        item["trade_date"] = normalize_date(native["时间"])
                        item["label_at"] = None
                    else:
                        stamp = datetime.strptime(native["时间"], "%Y-%m-%d %H:%M:%S")
                        require(stamp.strftime("%Y-%m-%d %H:%M:%S") == native["时间"]
                                and stamp.second == 0, "CMES_TIME", "Expected a whole minute label")
                        item.update(trade_date=stamp.date().isoformat(),
                                    label_at=stamp.replace(tzinfo=TZ).isoformat())
                    values = {key: native[column].strip() for key, column in BAR_COLUMNS.items()}
                    for key in ("volume", "amount"):
                        values[key] = scale(values[key], contract[key]) if contract[key] is not None else None
                    item.update(normalize_values(values))
                    preclose = native.get("昨收价", "").strip()
                    item["preclose"] = exact_number(preclose) if preclose else None
                    item.update(disposition="adopted", reason=None)
                except (ValueError, DataError) as error:
                    item.update(disposition="quarantined", reason=str(error))
                yield item


def reference_rows(path, *, kind, encoding="utf-8-sig", securities=None):
    """Calendar flags and positive ST membership stay separate from OHLCV facts."""
    require(kind in {"calendar", "st"}, "CMES_KIND", "Expected calendar or st reference")
    selected = {normalize_security(s) for s in securities} if securities is not None else None
    required = ["交易所", "时间", "是否交易", "上一交易日"] if kind == "calendar" else [
        "股票代码", "股票名称", "时间", "类型", "类型名称",
    ]
    with csv_members(path) as members:
        for member, opener in members:
            for number, native in csv_rows(opener, encoding=encoding, required=required):
                item = dict(source_member=member, source_row=number,
                            trade_date=normalize_date(native["时间"]))
                if kind == "calendar":
                    venue = {"SSE": "XSHG", "SZSE": "XSHE", "BSE": "BJ"}.get(native["交易所"])
                    require(venue is not None and native["是否交易"] in {"0", "1"},
                            "CMES_CALENDAR", "Unknown venue or calendar flag")
                    previous = native["上一交易日"].strip()
                    item.update(venue=venue, is_open=int(native["是否交易"]),
                                previous_date=normalize_date(previous) if previous else None)
                else:
                    security = normalize_security(native["股票代码"])
                    if selected is not None and security not in selected:
                        continue
                    require(native["类型"].strip() != "", "CMES_ST", "ST type must be explicit")
                    item.update(security=security, name=native["股票名称"],
                                status_type=native["类型"], status_name=native["类型名称"])
                yield item


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
