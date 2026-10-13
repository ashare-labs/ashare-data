"""Replay fixed real action wires through the pinned SDK and AD, entirely offline.

Use an extracted supplement/acquisition.tar.gz plus the committed fixture set.
Old capture receipts stay byte-identical; new receipts describe offline replay
only. Neither a terminal empty response nor its new local clock proves event
absence or historical availability.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import ashare_data
from ashare_data._bao_receipt.capture import capture_query, save_capture, utc_now
from ashare_data._bao_receipt.protocol import check_identity, decode_request, decode_response


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def verify(fixtures, acquisition, output):
    fixtures, acquisition, output = map(Path, (fixtures, acquisition, output))
    output.mkdir(parents=True, exist_ok=False)
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    manifest = json.loads((fixtures / "manifest.json").read_bytes())
    inventory = json.loads((acquisition / "ACQUISITION_FILES.json").read_bytes())
    for entry in inventory["files"]:
        path = acquisition / entry["path"]
        check("safe/" + entry["path"], path.resolve().is_relative_to(acquisition.resolve()))
        raw = path.read_bytes()
        check("bound/" + entry["path"], sha(raw) == entry["sha256"] and len(raw) == entry["bytes"])
    sdk_root = acquisition / "upstream/baostock-sdk"
    sys.path.insert(0, str(sdk_root))
    import baostock as sdk
    import baostock.common.context as context

    check("pinned_sdk_import", Path(sdk.__file__).resolve().is_relative_to(sdk_root.resolve()))
    resultset = sdk_root / "baostock/data/resultset.py"
    check(
        "reviewed_sdk_empty_branch",
        sha(resultset.read_bytes())
        == "70882a035bc9c20991255d5495c31f68fabc923d313cd35652c33563141797a6",
    )
    cases = []
    with patch("socket.socket", side_effect=AssertionError("Offline replay attempted network")):
        for case in manifest["entries"]:
            folder = fixtures / case["id"]
            for file in case["files"]:
                data = (fixtures / file["path"]).read_bytes()
                check(
                    "fixture/" + file["path"],
                    sha(data) == file["sha256"] and len(data) == file["bytes"],
                )
            original = acquisition / case["source_path"]
            old_receipt_raw = (original / "receipt.json").read_bytes()
            check(
                case["id"] + "/old_receipt_preserved",
                sha(old_receipt_raw) == case["original_receipt_sha256"],
            )
            old_receipt = json.loads(old_receipt_raw)
            sent = (folder / "page-01.request.bin").read_bytes()
            response = (folder / "page-01.response.bin").read_bytes()
            check(
                case["id"] + "/original_request",
                sent == (original / "page-01.request.bin").read_bytes(),
            )
            check(
                case["id"] + "/original_response",
                response == (original / "page-01.response.bin").read_bytes(),
            )

            class ReplaySocket:
                def __init__(self):
                    self.remaining = response
                    self.sent = []

                def send(self, data):
                    check(case["id"] + "/sdk_request_bytes", data == sent and not self.sent)
                    self.sent.append(data)
                    return len(data)

                def recv(self, count):
                    if not self.remaining:
                        raise AssertionError("Unexpected extra read in bounded replay")
                    chunk, self.remaining = self.remaining[:count], self.remaining[count:]
                    return chunk

            context.user_id = decode_request(sent)["user"]
            context.default_socket = ReplaySocket()
            result = capture_query(
                sdk,
                context,
                case["request"],
                task_started_at=utc_now(),
                max_rows=100,
                max_pages=2,
                evidence_kind="offline_test_only",
            )
            receipt = result["receipt"]
            expected = "complete_empty_unknown" if case["native_empty"] else "complete_rows"
            decoded = decode_response(response)
            identity = check_identity(case["request"], decode_request(sent), decoded, 1)
            check(
                case["id"] + "/complete",
                receipt["query_status"] == expected and receipt["query_complete"],
            )
            check(case["id"] + "/identity", identity["state"] == "matched")
            check(
                case["id"] + "/integrity", decoded["integrity"] == "length_terminator_crc_verified"
            )
            check(
                case["id"] + "/SDK_parity",
                result["sdk_rows"]
                == decoded["rows"]
                == json.loads((folder / "sdk-rows.json").read_bytes()),
            )
            check(case["id"] + "/SDK_fields", receipt["sdk_fields"] == decoded["fields"])
            check(
                case["id"] + "/one_terminal_page",
                len(receipt["pages"]) == 1 and receipt["terminal_page_verified"],
            )
            check(
                case["id"] + "/wire_unchanged",
                result["raw_requests"] == [sent] and result["raw_responses"] == [response],
            )
            check(
                case["id"] + "/no_promotion",
                not any(
                    receipt[k]
                    for k in (
                        "complete_history",
                        "confirmed_no_events",
                        "strict_admitted",
                        "production_admitted",
                    )
                ),
            )
            check(
                case["id"] + "/historical_clock_unknown",
                receipt["available_at"] is None and receipt["ingested_at"] is None,
            )
            save_capture(output / "replays" / case["id"], result)
            cases.append(
                dict(
                    id=case["id"],
                    request=case["request"],
                    native_empty=case["native_empty"],
                    raw_request_sha256=sha(sent),
                    raw_response_sha256=sha(response),
                    original_receipt_sha256=sha(old_receipt_raw),
                    original_query_status=old_receipt["query_status"],
                    original_capture_at=old_receipt["operation_completed_at"],
                    new_query_status=receipt["query_status"],
                    new_receipt_id=receipt["receipt_id"],
                    rows=len(result["sdk_rows"]),
                    identity=identity,
                    integrity=decoded["integrity"],
                    historical_available_at=None,
                    full_action_absence=False,
                )
            )
    check("five_native_empty", sum(case["native_empty"] for case in cases) == 5)
    check("ten_nonempty_controls", sum(not case["native_empty"] for case in cases) == 10)
    check("twelve_nonempty_rows", sum(case["rows"] for case in cases) == 12)
    report = dict(
        schema="ad.baostock-native-empty-reanalysis.v1",
        passed=True,
        checks=len(checks),
        check_names=checks,
        module=ashare_data.__file__,
        network_used=False,
        source_archive_sha256=manifest["source_archive_sha256"],
        fixture_manifest_sha256=sha((fixtures / "manifest.json").read_bytes()),
        acquisition_manifest_sha256=sha((acquisition / "ACQUISITION_FILES.json").read_bytes()),
        preserved_acquisition_members=len(inventory["files"]),
        cases=cases,
        parser_issue_resolved=True,
        complete_action_history=False,
        full_action_absence=False,
        historical_available_at=None,
        state_clock_modelled=False,
        real_backtest_executed=False,
        clock_semantics="Original collection clocks are retained as provenance. New receipt clocks only time this offline replay; neither supplies historical market availability.",
    )
    dump(output / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--acquisition", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.fixtures, args.acquisition, args.output)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "passed",
                    "checks",
                    "network_used",
                    "parser_issue_resolved",
                    "full_action_absence",
                )
            }
        )
    )
