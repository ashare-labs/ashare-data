"""Independent, bounded BaoStock response validation from pinned SDK schemas.

Uncompressed length/CRC contract is checked against the saved real type-46 wire.
Saved real compressed type-96 samples support diagnostic decoding. Its outer
integrity algorithm remains unverified: it cannot receive strict admission.
"""

from datetime import date
import json
import re
import zlib

SEP = "\x01"
END = b"<![CDATA[]]>\n"
HEADER = re.compile(rb"([0-9.]{7})\x01([0-9A-Z]{2})\x01([0-9]{10})")
MAX_BYTES = 4 * 1024 * 1024
METHODS = {
    "query_stock_basic": dict(
        request="45",
        response="46",
        request_keys=["code", "code_name"],
        response_keys=["code", "code_name", "fields"],
    ),
    "query_trade_dates": dict(
        request="33",
        response="34",
        request_keys=["start_date", "end_date"],
        response_keys=["start_date", "end_date", "fields"],
    ),
    "query_dividend_data": dict(
        request="13",
        response="14",
        request_keys=["code", "year", "yearType"],
        response_keys=["code", "year", "yearType", "fields"],
    ),
    "query_adjust_factor": dict(
        request="15",
        response="16",
        request_keys=["code", "start_date", "end_date"],
        response_keys=["code", "start_date", "end_date", "fields"],
    ),
    "query_history_k_data_plus": dict(
        request="95",
        response="96",
        request_keys=["code", "fields", "start_date", "end_date", "frequency", "adjustflag"],
        response_keys=["code", "fields", "start_date", "end_date", "frequency", "adjustflag"],
    ),
}


class FrameError(ValueError):
    pass


def fields(value):
    result = [x.strip() for x in value.split(",")]
    if not all(result) or len(set(result)) != len(result):
        raise FrameError("empty/duplicate fields")
    return result


def header(raw):
    if len(raw) > MAX_BYTES:
        raise FrameError("frame byte limit")
    match = HEADER.fullmatch(raw[:21])
    if not match:
        raise FrameError("invalid or incomplete 21-byte header")
    return match[1].decode(), match[2].decode(), int(match[3])


def crc_checked_core(raw):
    try:
        core, crc = raw.rsplit(b"\x01", 1)
    except ValueError:
        raise FrameError("missing CRC")
    if not crc.isdigit() or int(crc) != zlib.crc32(core):
        raise FrameError("CRC mismatch")
    return core


def decode_request(raw):
    version, kind, length = header(raw)
    if not raw.endswith(b"\n"):
        raise FrameError("incomplete outbound request")
    core = crc_checked_core(raw[:-1])
    body = core[21:].decode("utf8")
    if len(body) != length:
        raise FrameError("outbound declared length mismatch")
    a = body.split(SEP)
    if len(a) < 4 or a[0] not in METHODS:
        raise FrameError("unrecognized outbound method")
    spec = METHODS[a[0]]
    if kind != spec["request"] or len(a) != 4 + len(spec["request_keys"]):
        raise FrameError("outbound schema mismatch")
    if not a[2].isdigit() or not a[3].isdigit():
        raise FrameError("invalid outbound page")
    return dict(
        method=a[0],
        user=a[1],
        page=int(a[2]),
        page_size=int(a[3]),
        params=dict(zip(spec["request_keys"], a[4:], strict=True)),
        version=version,
        type=kind,
    )


def decode_response(raw):
    version, kind, length = header(raw)
    if not raw.endswith(END):
        raise FrameError("missing response terminator")
    if kind == "96":
        compressed = raw[21 : 21 + length]
        if len(compressed) != length:
            raise FrameError("compressed payload truncated")
        # Observed public 0.9.4 wire profile, not a proven checksum algorithm.
        tail = raw[21 + length :]
        if not re.fullmatch(rb"\x01[0-9]{1,10}\n<!\[CDATA\[\]\]>\n", tail):
            raise FrameError("unsupported compressed outer envelope")
        decoder = zlib.decompressobj()
        try:
            body_bytes = decoder.decompress(compressed, MAX_BYTES + 1)
        except zlib.error as e:
            raise FrameError("invalid zlib stream") from e
        if (
            len(body_bytes) > MAX_BYTES
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise FrameError("incomplete/oversized/trailing zlib stream")
        body = body_bytes.decode("utf8").rstrip("\n")
        # SDK exposes the decompressed body; do not pretend an inferred CRC
        # convention is verified. Preserve both byte layers for diagnostics.
        chunks = body.split(SEP)
        # The real 96 payload ends with adjustflag ('3'), not an inner CRC.
        # Outer integrity remains unknown; never discard a schema field.
        integrity = "compressed_outer_integrity_unverified"
    else:
        core = crc_checked_core(raw[: -len(END)])
        body = core[21:].decode("utf8")
        if len(body) != length:
            raise FrameError("declared character length mismatch")
        chunks = body.split(SEP)
        integrity = "length_terminator_crc_verified"
    if len(chunks) < 2:
        raise FrameError("missing provider result code")
    result = dict(
        version=version,
        type=kind,
        error_code=chunks[0],
        error_msg=chunks[1],
        frame_complete=True,
        integrity=integrity,
        rows=[],
        fields=[],
        echo={},
    )
    if chunks[0] != "0":
        return result
    if len(chunks) < 7:
        raise FrameError("incomplete success response schema")
    method = chunks[2]
    if method not in METHODS:
        raise FrameError("unknown response method")
    spec = METHODS[method]
    if len(chunks) != 7 + len(spec["response_keys"]):
        raise FrameError("response identity fields missing or trailing")
    if not chunks[4].isdigit() or not chunks[5].isdigit():
        raise FrameError("invalid response page metadata")
    echo = dict(zip(spec["response_keys"], chunks[7:], strict=True))
    names = fields(echo["fields"])
    record = json.loads(chunks[6]).get("record")
    if not isinstance(record, list):
        raise FrameError("record is not a list")
    rows = []
    for row in record:
        if (
            not isinstance(row, list)
            or len(row) != len(names)
            or not all(isinstance(v, str) for v in row)
        ):
            raise FrameError("invalid record width/type")
        rows.append(dict(zip(names, row, strict=True)))
    result.update(
        method=method,
        user=chunks[3],
        page=int(chunks[4]),
        page_size=int(chunks[5]),
        echo=echo,
        fields=names,
        rows=rows,
    )
    return result


def check_identity(request, sent, response, page_number, page_size=2000):
    """Compare intended params, actual sent envelope, response echoes and rows.

    Empty records still require matching response metadata. Hash equality alone
    never serves as semantic identity validation.
    """
    method = request["method"]
    params = {k: str(v) for k, v in request["params"].items()}
    issues = []
    unknown = []
    spec = METHODS[method]
    for label, value, expected in [
        ("sent_method", sent["method"], method),
        ("sent_page", sent["page"], page_number),
        ("sent_page_size", sent["page_size"], page_size),
    ]:
        if value != expected:
            issues.append(label)
    defaults = {"code_name": ""}
    for key in spec["request_keys"]:
        target = params.get(key, defaults.get(key))
        if target is None:
            unknown.append("unspecified_" + key)
        elif (
            fields(sent["params"][key]) != fields(target)
            if key == "fields"
            else sent["params"][key] != target
        ):
            issues.append("sent_" + key)
    if response["error_code"] != "0":
        return dict(state="not_applicable_error_response", issues=issues, unknown=unknown)
    for label, value, expected in [
        ("response_type", response["type"], spec["response"]),
        ("method", response["method"], method),
        ("user", response["user"], sent["user"]),
        ("page", response["page"], page_number),
        ("page_size", response["page_size"], page_size),
    ]:
        if value != expected:
            issues.append(label)
    echo = response["echo"]
    for key in spec["request_keys"]:
        expected = params.get(key, defaults.get(key))
        if key not in echo:
            unknown.append("missing_echo_" + key)
        elif expected is not None and (
            fields(echo[key]) != fields(expected) if key == "fields" else echo[key] != expected
        ):
            issues.append("echo_" + key)
    names = response["fields"]
    rows = response["rows"]
    if method != "query_trade_dates" and "code" not in names:
        unknown.append("row_code_field_absent")
    date_key = {
        "query_trade_dates": "calendar_date",
        "query_history_k_data_plus": "date",
        "query_adjust_factor": "dividOperateDate",
    }.get(method)
    if method == "query_dividend_data":
        if params.get("yearType") == "operate":
            date_key = "dividOperateDate"
        else:
            unknown.append("dividend_non_operate_year_contract_unverified")
    if date_key and date_key not in names:
        unknown.append("row_date_field_absent")
    seen = set()
    for index, row in enumerate(rows):
        if "code" in row and row["code"] != params.get("code"):
            issues.append(f"row[{index}].code")
        if date_key and date_key in row:
            try:
                observed = date.fromisoformat(row[date_key])
            except ValueError:
                issues.append(f"row[{index}].date_invalid")
                continue
            if (
                "start_date" in params
                and not params["start_date"] <= observed.isoformat() <= params["end_date"]
            ):
                issues.append(f"row[{index}].date_outside_request")
            if method == "query_dividend_data" and str(observed.year) != params["year"]:
                issues.append(f"row[{index}].year")
            label = (row.get("code"), row.get("time", row[date_key]))
            if label in seen:
                issues.append(f"row[{index}].duplicate_label")
            seen.add(label)
        if method == "query_history_k_data_plus":
            if "adjustflag" not in row:
                unknown.append("row_adjustflag_absent")
            elif row["adjustflag"] != params["adjustflag"]:
                issues.append(f"row[{index}].adjustflag")
    return dict(
        state="mismatch" if issues else "unknown" if unknown else "matched",
        issues=sorted(set(issues)),
        unknown=sorted(set(unknown)),
    )
