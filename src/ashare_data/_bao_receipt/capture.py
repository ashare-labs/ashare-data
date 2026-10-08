"""Capture an explicitly supplied SDK/socket query, never login or connect.

The socket records faults before the SDK can swallow them. Query completion
requires valid frames + identity + successful SDK + supported pagination.
"""

from datetime import datetime, timezone
from pathlib import Path
import contextlib
import hashlib
import io
import json
import time
from .protocol import METHODS, MAX_BYTES, decode_request, decode_response, check_identity
from .timing import read_clock, wall_clock_order, monotonic_duration


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def bh(raw):
    return hashlib.sha256(raw).hexdigest()


def sh(value):
    return bh(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def aware(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt


class CaptureLimit(Exception):
    pass


class SocketMonitor:
    def __init__(self, socket, clock, max_pages):
        self.socket, self.clock, self.max_pages = socket, clock, max_pages
        self.exchanges = []
        self.faults = []
        self.time_events = []
        self.clock_errors = []

    def stamp(self, event):
        value = read_clock(self.clock, self.clock_errors, event)
        self.time_events.append(dict(event=event, at=value))
        return value

    def fault(self, kind, error):
        item = dict(
            kind=kind, error_type=type(error).__name__, message=str(error), at=self.stamp(kind)
        )
        self.faults.append(item)
        if self.exchanges:
            self.exchanges[-1]["faults"].append(item)

    def send(self, data, *args, **kwargs):
        page = len(self.exchanges) + 1
        self.exchanges.append(
            dict(
                request_started_at=self.stamp(f"page_{page}_request_started"),
                request_bytes=bytes(data),
                chunks=[],
                first_byte_received_at=None,
                last_byte_received_at=None,
                faults=[],
                send_attempted=False,
                sent_bytes=None,
                send_state="not_attempted",
            )
        )
        if len(self.exchanges) > self.max_pages:
            self.exchanges[-1]["send_state"] = "blocked_by_page_limit"
            error = CaptureLimit("local page limit before send")
            self.fault("local_page_cap", error)
            raise error
        try:
            self.exchanges[-1]["send_attempted"] = True
            sent = self.socket.send(data, *args, **kwargs)
            self.exchanges[-1]["sent_bytes"] = sent
            self.exchanges[-1]["send_state"] = (
                "accepted_by_socket" if sent == len(data) else "partial_send"
            )
            if sent != len(data):
                raise ConnectionError("partial socket send")
            return sent
        except BaseException as error:
            if self.exchanges[-1]["sent_bytes"] is None:
                self.exchanges[-1]["send_state"] = "send_exception_count_unknown"
            self.fault("send_failure", error)
            raise

    def recv(self, *args, **kwargs):
        if not self.exchanges:
            raise RuntimeError("recv without captured send")
        try:
            data = self.socket.recv(*args, **kwargs)
            if not data:
                raise ConnectionError("EOF before complete SDK frame")
            ex = self.exchanges[-1]
            when = self.stamp(f"page_{len(self.exchanges)}_recv_{len(ex['chunks']) + 1}")
            if not ex["chunks"]:
                ex["first_byte_received_at"] = when
            ex["last_byte_received_at"] = when
            ex["chunks"].append(bytes(data))
            if sum(map(len, ex["chunks"])) > MAX_BYTES:
                raise CaptureLimit("local response byte limit")
            return data
        except BaseException as error:
            self.fault(
                "receive_limit" if isinstance(error, CaptureLimit) else "receive_failure", error
            )
            raise

    def __getattr__(self, name):
        return getattr(self.socket, name)


def code_kind(code):
    if code == "0":
        return "success"
    if code is None:
        return "no_sdk_result"
    if code.startswith("10001"):
        return "session_or_authorization_error"
    if code.startswith("10002"):
        return "network_error"
    if code == "10004012":
        return "unsupported_field"
    return "provider_or_sdk_error"


def capture_query(
    sdk,
    ctx,
    request,
    *,
    task_started_at,
    max_rows=60,
    max_pages=4,
    clock=utc_now,
    monotonic=time.monotonic_ns,
    evidence_kind="live_local_capture",
):
    """Do not call concurrently: pinned SDK uses a global default_socket.

    Caller supplies a connected socket, explicit bounds/deadline and an
    authorized request. This module performs no connection/authentication.
    Returned raw SDK rows are always diagnostic; only independently verified
    prefix rows are offered for isolated research after an incomplete query.
    """
    if request.get("method") not in METHODS:
        raise ValueError("unsupported request method")
    if not 1 <= max_rows <= 5000 or not 1 <= max_pages <= 4:
        raise ValueError("research limits")
    if not isinstance(request.get("params"), dict):
        raise ValueError("explicit params required")
    method = request["method"]
    params = request["params"]
    if method != "query_trade_dates" and not params.get("code"):
        raise ValueError("no universe queries")
    source_hashes = {p.name: bh(p.read_bytes()) for p in sorted(Path(__file__).parent.glob("*.py"))}
    clock_errors = []
    started_at = read_clock(clock, clock_errors, "query_started")
    start_ns = read_clock(monotonic, clock_errors, "monotonic_operation_start")
    original = ctx.default_socket
    monitor = SocketMonitor(original, clock, max_pages)
    ctx.default_socket = monitor
    rows = []
    rs = None
    exception = None
    stop = "not_started"
    transcript = io.StringIO()
    try:
        with contextlib.redirect_stdout(transcript):
            rs = getattr(sdk, method)(**params)
            while True:
                if rs.error_code != "0":
                    stop = "sdk_error"
                    break
                if len(rows) >= max_rows:
                    stop = "local_row_cap"
                    break
                if not rs.next():
                    stop = "sdk_iterator_exhausted"
                    break
                rows.append(dict(zip(rs.fields, rs.get_row_data(), strict=True)))
    except BaseException as error:
        exception = dict(type=type(error).__name__, message=str(error))
        stop = "caller_exception"
    finally:
        # Restore the SDK global even if either clock provider fails.
        ctx.default_socket = original
        completed_at = read_clock(clock, clock_errors, "operation_completed")
        end_ns = read_clock(monotonic, clock_errors, "monotonic_operation_end")
    # These are separate from receipt/valid response completion: even a failed
    # SDK invocation returns at operation_completed_at.
    pages = []
    all_rows = []
    research_rows = []
    prefix_open = True
    invalid = False
    unknown = False
    identity_bad = False
    business_codes = []
    raw_artifacts = []
    request_artifacts = []
    duplicate_labels = set()
    for n, ex in enumerate(monitor.exchanges, 1):
        raw = b"".join(ex["chunks"])
        request_bytes = ex["request_bytes"]
        raw_artifacts.append(raw)
        request_artifacts.append(request_bytes)
        page = dict(
            page_index=n,
            request_started_at=ex["request_started_at"],
            first_byte_received_at=ex["first_byte_received_at"],
            last_byte_received_at=ex["last_byte_received_at"],
            raw_response_sha256=bh(raw) if raw else None,
            raw_response_bytes=len(raw),
            raw_request_sha256=bh(request_bytes),
            faults=ex["faults"],
            frame_state="unknown",
            raw_request_hash_scope="buffer presented to proxy; not proof of network delivery",
            send_attempted=ex["send_attempted"],
            sent_bytes=ex["sent_bytes"],
            send_state=ex["send_state"],
            identity_state="unknown",
            decoded_rows_sha256=None,
            rows=[],
            research_prefix_eligible=False,
        )
        try:
            sent = decode_request(request_bytes)
            decoded = decode_response(raw)
            page["sent_envelope"] = sent
            page["response_metadata"] = {k: v for k, v in decoded.items() if k != "rows"}
            page["frame_state"] = (
                "complete_verified"
                if decoded["integrity"] == "length_terminator_crc_verified"
                else "complete_integrity_unknown"
            )
            unknown |= page["frame_state"] == "complete_integrity_unknown"
            identity = check_identity(request, sent, decoded, n)
            page["identity"] = identity
            page["identity_state"] = identity["state"]
            identity_bad |= identity["state"] == "mismatch"
            unknown |= identity["state"] == "unknown"
            page["rows"] = decoded["rows"]
            page["decoded_rows_sha256"] = sh(decoded["rows"])
            all_rows.extend(decoded["rows"])
            if decoded["error_code"] != "0":
                business_codes.append(decoded["error_code"])
            # Cross-page duplication would make pagination completion unsafe.
            for row in decoded["rows"]:
                label = row.get(
                    "time", row.get("date", row.get("calendar_date", row.get("dividOperateDate")))
                )
                if label is not None:
                    key = (row.get("code"), label)
                    if key in duplicate_labels:
                        identity_bad = True
                        page["identity_state"] = "mismatch"
                        page["cross_page_duplicate"] = True
                    duplicate_labels.add(key)
            eligible = (
                not ex["faults"]
                and page["frame_state"] == "complete_verified"
                and page["identity_state"] == "matched"
                and decoded["error_code"] == "0"
            )
            prefix_open &= eligible
            page["research_prefix_eligible"] = prefix_open
            if prefix_open:
                for index, row in enumerate(decoded["rows"]):
                    research_rows.append(
                        dict(
                            fields=row,
                            raw_record_sha256=sh(row),
                            page_index=n,
                            row_index=index,
                            raw_response_sha256=page["raw_response_sha256"],
                            raw_request_sha256=page["raw_request_sha256"],
                            market_time=row.get("time", row.get("date", row.get("calendar_date"))),
                            available_at=None,
                            ingested_at=None,
                            evidence_kind=evidence_kind,
                            quality="offline_test_only"
                            if evidence_kind.startswith("offline")
                            else "isolated_research",
                        )
                    )
        except Exception as error:
            invalid = True
            prefix_open = False
            page["frame_state"] = "invalid_or_incomplete"
            page["validation_error"] = dict(type=type(error).__name__, message=str(error))
        pages.append(page)
    sdk_code = getattr(rs, "error_code", None)
    decode_match = rows == all_rows
    pagination = "unknown"
    if pages and all(p.get("response_metadata", {}).get("error_code") == "0" for p in pages):
        lengths = [len(p["rows"]) for p in pages]
        if any(n != 2000 for n in lengths[:-1]) or lengths[-1] > 2000:
            pagination = "invalid_page_sequence"
        elif lengths[-1] == 0:
            pagination = "empty_terminal_page" if len(pages) > 1 else "complete_empty_response"
        elif lengths[-1] < 2000:
            pagination = "short_terminal_page"
        else:
            pagination = "full_page_without_terminal_evidence"
    terminal_page = pagination in (
        "empty_terminal_page",
        "complete_empty_response",
        "short_terminal_page",
    )
    # A cap check in the iterator is not itself evidence of truncation. A
    # complete, validated short terminal page + identical consumed rows can
    # prove completion without another rs.next()/network request.
    terminal_verified = (
        bool(pages)
        and not monitor.faults
        and not invalid
        and not unknown
        and not identity_bad
        and not business_codes
        and sdk_code == "0"
        and exception is None
        and decode_match
        and terminal_page
    )
    prefix_match = rows == all_rows[: len(rows)]
    if stop != "local_row_cap":
        cap_decision = "not_iterator_stop"
    elif terminal_verified:
        cap_decision = "exact_cap_complete_terminal"
    elif decode_match and not terminal_page:
        cap_decision = "cap_without_terminal_evidence"
    elif prefix_match and len(rows) < len(all_rows):
        cap_decision = "cap_truncated_decoded_rows"
    else:
        cap_decision = "cap_with_other_validation_failure"
    # Capture the final validation boundary BEFORE assessing the full chain.
    validation_at = read_clock(clock, clock_errors, "validation_completed")
    events = [
        dict(event="task_started", at=task_started_at),
        dict(event="query_started", at=started_at),
    ]
    events += monitor.time_events
    events += [
        dict(event="operation_completed", at=completed_at),
        dict(event="validation_completed", at=validation_at),
    ]
    wall = wall_clock_order(events)
    elapsed = monotonic_duration(start_ns, end_ns)
    clock_state = (
        "invalid"
        if "invalid" in (wall["state"], elapsed["state"])
        else "unknown"
        if "unknown" in (wall["state"], elapsed["state"])
        else "valid"
    )
    clock_valid = clock_state == "valid"
    if any(f["kind"] == "local_page_cap" for f in monitor.faults):
        status = "local_page_cap"
    elif monitor.faults:
        status = "transport_incomplete"
    elif invalid:
        status = "invalid_or_incomplete_frame"
    elif exception:
        status = "caller_exception"
    elif identity_bad:
        status = "identity_mismatch"
    elif business_codes:
        status = "provider_error_response"
    elif sdk_code != "0":
        status = "sdk_error"
    elif not pages:
        status = "no_captured_response"
    elif unknown:
        status = "validation_unknown"
    elif not prefix_match:
        status = "sdk_decode_mismatch"
    elif stop == "local_row_cap" and not terminal_verified:
        status = "local_row_cap"
    elif not decode_match:
        status = "sdk_decode_mismatch"
    elif pagination in ("unknown", "invalid_page_sequence", "full_page_without_terminal_evidence"):
        status = "pagination_unresolved"
    else:
        status = "complete_rows" if rows else "complete_empty_unknown"
    data_status = status
    if status in ("complete_rows", "complete_empty_unknown") and not clock_valid:
        status = "clock_order_invalid" if clock_state == "invalid" else "clock_evidence_unknown"
    # Prefix eligibility also requires legal preceding-page progression.
    if pagination == "invalid_page_sequence":
        research_rows = []
    if stop == "local_row_cap":
        research_rows = research_rows[:max_rows]
    complete = status in ("complete_rows", "complete_empty_unknown")
    transport_complete = (
        bool(pages)
        and not monitor.faults
        and all(p["frame_state"] == "complete_verified" for p in pages)
    )
    receipt = dict(
        schema_version=4,
        request=request,
        evidence_kind=evidence_kind,
        task_started_at=task_started_at,
        request_started_at=started_at,
        operation_completed_at=completed_at,
        response_completed_at=completed_at if transport_complete else None,
        validation_completed_at=validation_at,
        duration_ns=elapsed["elapsed_ns"],
        clock_order_valid=clock_valid,
        clock_order_state=clock_state,
        wall_clock_evidence=wall,
        monotonic_evidence=elapsed,
        wall_clock_events=events,
        clock_read_errors=clock_errors + monitor.clock_errors,
        query_status=status,
        transport_complete=transport_complete,
        query_complete=complete,
        data_completion_status=data_status,
        terminal_page_verified=terminal_verified,
        row_limit=max_rows,
        row_limit_reached=len(rows) >= max_rows,
        row_limit_decision=cap_decision,
        iterator_stop=stop,
        pagination_state=pagination,
        sdk_error_code=sdk_code,
        sdk_error_kind=code_kind(sdk_code),
        sdk_exception=exception,
        transport_faults=monitor.faults,
        sdk_fields=getattr(rs, "fields", []),
        sdk_rows_sha256=sh(rows),
        decoded_rows_match_sdk=decode_match,
        pages=pages,
        local_visibility_at=completed_at if complete else None,
        available_at=None,
        ingested_at=None,
        complete_history=False,
        confirmed_no_events=False,
        strict_admitted=False,
        production_admitted=False,
        capture_source_sha256=source_hashes,
        sdk_identity=dict(
            expected_version="0.9.4",
            expected_wheel_sha256="0bf71c6069ab5890ff3596632f9c3f8f1fbc6bfcac582c2f9d6a5c11ab2cfaf8",
            provided_module_path=getattr(sdk, "__file__", None),
            caller_responsible_for_pinned_sdk=True,
        ),
        time_semantics="wall-clock events include final validation; monotonic duration is separate operation-only evidence; offline clocks are simulations",
    )
    receipt["receipt_id"] = sh(receipt)
    for r in research_rows:
        r["receipt_id"] = receipt["receipt_id"]
    return dict(
        receipt=receipt,
        sdk_rows=rows,
        research_rows=research_rows,
        raw_responses=raw_artifacts,
        raw_requests=request_artifacts,
        sdk_transcript=transcript.getvalue(),
    )


def locally_visible(receipt, as_of):
    """Necessary successful-query visibility only; does not grant strict PIT."""
    core = dict(receipt)
    identifier = core.pop("receipt_id", None)
    if identifier != sh(core):
        return False
    value = receipt.get("local_visibility_at")
    try:
        return bool(
            receipt.get("query_complete")
            and receipt.get("clock_order_state") == "valid"
            and value
            and aware(value) <= aware(as_of)
        )
    except (ValueError, TypeError):
        return False


def save_capture(out, result, *, clock=utc_now):
    """Write a new immutable receipt and raw artifacts; never amend its hash."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    index = []
    for n, (sent, raw) in enumerate(zip(result["raw_requests"], result["raw_responses"]), 1):
        for kind, data in [("request", sent), ("response", raw)]:
            path = out / f"page-{n:02d}.{kind}.bin"
            path.write_bytes(data)
            index.append(dict(path=path.name, bytes=len(data), sha256=bh(data)))
    for name, value in [
        ("receipt", result["receipt"]),
        ("sdk-rows", result["sdk_rows"]),
        ("research-rows", result["research_rows"]),
    ]:
        p = out / (name + ".json")
        p.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        index.append(dict(path=p.name, bytes=p.stat().st_size, sha256=bh(p.read_bytes())))
    (out / "sdk-transcript.txt").write_text(result["sdk_transcript"])
    errors = []
    recorded = read_clock(clock, errors, "staging_recorded")
    timing = wall_clock_order(
        [
            dict(event="validation_completed", at=result["receipt"]["validation_completed_at"]),
            dict(event="staging_recorded", at=recorded),
        ]
    )
    eligible = timing["state"] == "valid" and locally_visible(result["receipt"], recorded)
    storage = dict(
        receipt_id=result["receipt"]["receipt_id"],
        staging_recorded_at=recorded,
        storage_clock_evidence=timing,
        clock_read_errors=errors,
        storage_eligible=eligible,
        stored_visibility_at=recorded if eligible else None,
        monotonic_storage_elapsed_ns=None,
        time_evidence_kind=result["receipt"]["evidence_kind"],
        ingested_at=None,
        artifacts=index,
    )
    storage["storage_id"] = sh(storage)
    (out / "storage-record.json").write_text(
        json.dumps(storage, ensure_ascii=False, indent=2) + "\n"
    )
    return storage


def stored_locally_visible(receipt, storage, as_of):
    """Persistence gate includes its separately recorded clock boundary."""
    core = dict(storage)
    identifier = core.pop("storage_id", None)
    if identifier != sh(core) or storage.get("receipt_id") != receipt.get("receipt_id"):
        return False
    value = storage.get("stored_visibility_at")
    try:
        return bool(
            storage.get("storage_eligible")
            and value
            and locally_visible(receipt, as_of)
            and aware(value) <= aware(as_of)
        )
    except (ValueError, TypeError):
        return False
