"""Streaming CMES files and explicit mappings; no network, extraction or price adjustment."""

from contextlib import contextmanager
import csv
from datetime import datetime
from decimal import Decimal, localcontext
from io import TextIOWrapper
from pathlib import Path
import re
from zipfile import ZipFile

from ashare_data.core.model import DataError, TZ, day, require
from ashare_data.market.increment import exact_number, normalize_values
from ashare_data.storage.market_release import fingerprint, sha_file

MAPPING_VERSION = "cmes-files.v1"
# Different CMES exports of the same frequency can have different units.
BAR_PROFILES = {
    "daily-lots-kcny": {"table": "bars_1d", "series_id": 1, "volume": 100, "amount": 1000},
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


def source_asset(path, *, kind, profile=None):
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
        "observed_at": None, "historical_pit_verified": False,
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
                        values[key] = scale(values[key], contract[key])
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
