"""Synthetic wire controls for batch-only metadata and terminal-page extensions."""

import base64
import copy
import json
from types import SimpleNamespace
import zlib

import pytest

from ashare_data.providers.baostock_receipt.capture import capture_query
from ashare_data.providers.baostock_receipt.protocol import decode_response, FrameError
from ashare_data.providers.baostock_collection import verify_capture, FIELDS


@pytest.mark.parametrize(
    "progress,expected_delays,exit_code",
    [
        ([10, 0, 20, None], [10, 30, 10], 0),
        ([0, 0, 0], [10, 30], 2),
    ],
)
def test_reconnect_budget_is_bounded_without_stopping_batches_that_keep_progressing(
    tmp_path, monkeypatch, progress, expected_delays, exit_code
):
    from ashare_data.providers import baostock_collection as module

    outcomes = iter(progress)

    def collect(*args, **kwargs):
        completed = next(outcomes)
        return dict(
            outcome="complete" if completed is None else "transport_failed",
            completed=completed or 0,
        )

    monkeypatch.setattr(module, "history_requests", lambda *a, **k: [])
    monkeypatch.setattr(module, "collect", collect)
    delays = []
    monkeypatch.setattr(module.time, "sleep", delays.append)
    assert module.main(["--root", str(tmp_path), "--all-bars"]) == exit_code
    assert delays == expected_delays


SEP = "\x01"
END = b"<![CDATA[]]>\n"


def frame(body, kind, response=True, compressed=False):
    payload = zlib.compress(body.encode()) if compressed else body.encode()
    length = len(payload) if compressed else len(body)
    head = f"00.9.10{SEP}{kind}{SEP}{length:010}"
    core = head.encode() + payload
    return core + SEP.encode() + str(zlib.crc32(core)).encode() + (END if response else b"\n")


def metadata_capture(sdk_name):
    names = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
    rawrow = ["sh.600001", "SYNTHETIC TEST", "2010-01-01", "", "1", "1"]
    request = dict(method="query_stock_basic", params=dict(code="", code_name=""))
    sent = frame(SEP.join(["query_stock_basic", "anonymous", "1", "2000", "", ""]), "45", False)
    raw = frame(
        SEP.join(
            [
                "0",
                "synthetic",
                "query_stock_basic",
                "anonymous",
                "1",
                "2000",
                json.dumps(dict(record=[rawrow])),
                "",
                "",
                ",".join(names),
            ]
        ),
        "46",
    )

    class Socket:
        def send(self, data):
            return len(data)

        def recv(self, count):
            return raw

    ctx = SimpleNamespace(default_socket=Socket())

    class SDK:
        def query_stock_basic(self, **kwargs):
            ctx.default_socket.send(sent)
            ctx.default_socket.recv(99999)
            return Result()

    class Result:
        fields = names
        error_code = "0"
        current = 0

        def next(self):
            self.current += 1
            return self.current == 1

        def get_row_data(self):
            return [rawrow[0], sdk_name, *rawrow[2:]]

    capture = capture_query(
        SDK(), ctx, request, task_started_at="2020-01-01T00:00:00+00:00", allow_universe=True
    )
    return dict(
        request=request,
        receipt=capture["receipt"],
        sdk_rows=capture["sdk_rows"],
        raw_requests=[base64.b64encode(x).decode() for x in capture["raw_requests"]],
        raw_responses=[base64.b64encode(x).decode() for x in capture["raw_responses"]],
    )


def test_known_sdk_name_loss_preserves_raw_name():
    envelope = metadata_capture("SYNTHETICTEST")
    rows, quality = verify_capture(envelope)
    assert rows[0]["code_name"] == "SYNTHETIC TEST"
    assert quality == "wire_names_preserved_sdk_whitespace_loss"
    with pytest.raises(ValueError, match="unexplained"):
        verify_capture(metadata_capture("DIFFERENT"))
    tampered = copy.deepcopy(envelope)
    tampered["raw_responses"][0] = base64.b64encode(b"tampered").decode()
    with pytest.raises(ValueError, match="hash"):
        verify_capture(tampered)


def test_empty_terminal_daily_schema_is_exact():
    def response(frequency="d", fields=FIELDS):
        body = SEP.join(
            [
                "0",
                "synthetic",
                "query_history_k_data_plus",
                "anonymous",
                "3",
                "2000",
                "",
                "sh.600001",
                fields,
                "2010-01-01",
                "2026-10-14",
                frequency,
                "3",
            ]
        )
        raw = zlib.compress(body.encode())
        return f"00.9.10{SEP}96{SEP}{len(raw):010}".encode() + raw + b"\x01123\n" + END

    assert decode_response(response())["rows"] == []
    with pytest.raises(FrameError):
        decode_response(response(frequency="5"))
    with pytest.raises(FrameError):
        decode_response(response(fields="date,code,close"))


def test_empty_terminal_membership_schema_is_exact():
    def response(fields):
        return frame(
            SEP.join(
                [
                    "0",
                    "synthetic",
                    "query_all_stock",
                    "anonymous",
                    "4",
                    "2000",
                    "",
                    "2022-08-01",
                    fields,
                ]
            ),
            "36",
        )

    decoded = decode_response(response("code,tradeStatus,code_name"))
    assert decoded["rows"] == [] and decoded["integrity"] == "length_terminator_crc_verified"
    with pytest.raises(FrameError):
        decode_response(response("code,code_name"))
