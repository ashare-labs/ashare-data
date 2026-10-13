"""Offline acceptance of the native-empty fix plus the sealed source supplement."""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile
from unittest.mock import patch

import ashare_data
from ashare_data import DataError, MinuteEvidenceView
from ashare_data._bao_receipt.capture import sh
from ashare_data._bao_receipt.protocol import check_identity, decode_request, decode_response

BASE_SHA = "04b46591ea45e74b7abcc223c94ce11b80642f0d8199a1c00a3cdcf104fe0091"


def verify(binding, expected_sha, base):
    binding, base = Path(binding), Path(base)
    root = binding.parent
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def read(name):
        return json.loads((root / name).read_bytes())

    with patch(
        "socket.socket", side_effect=AssertionError("Offline verification attempted network")
    ):
        view = MinuteEvidenceView(binding, expected_binding_sha256=expected_sha)
        check(
            "fixed_base",
            hashlib.sha256((base / "binding.json").read_bytes()).hexdigest() == BASE_SHA,
        )
        before_data = json.loads((base / "data.json").read_bytes())
        data = read("data.json")
        for entry in json.loads((base / "binding.json").read_bytes())["files"]:
            if entry["path"] != "data.json":
                check(
                    "retained/" + entry["path"],
                    hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest()
                    == entry["sha256"],
                )
        for key in data:
            if key != "minute_evidence":
                check("unchanged_data/" + key, data[key] == before_data[key])
        spec = data["minute_evidence"]
        for key in spec:
            if key not in {"assessments", "actions"}:
                check("unchanged_minute/" + key, spec[key] == before_data["minute_evidence"][key])
        for key in spec["actions"]:
            if key not in {"unknown_domains", "source_refs"}:
                check(
                    "unchanged_action_scope/" + key,
                    spec["actions"][key] == before_data["minute_evidence"]["actions"][key],
                )
        admission = view.admission_report()
        check(
            "nine_gates_preserved",
            admission["blocking_gates"] == [f"G{i:02}" for i in range(3, 12)],
        )
        old_status = {
            g["gate_id"]: g["status"] for g in before_data["minute_evidence"]["assessments"]
        }
        check(
            "no_gate_promoted",
            all(g["status"] == old_status[g["gate_id"]] for g in admission["assessments"]),
        )
        check(
            "no_execution",
            not admission["ready_for_execution"] and not admission["real_backtest_executed"],
        )
        try:
            view.export_backtest()
        except DataError as error:
            check("execution_export_rejected", error.code == "MINUTE_GATES_BLOCKED")
        else:
            raise AssertionError("Execution export unexpectedly succeeded")
        actions = view.corporate_actions(
            spec["securities"], start=spec["actions"]["start"], end=spec["actions"]["end"]
        )
        check(
            "no_action_absence_promotion", not actions["complete"] and not actions["absence_proved"]
        )
        proof = read("parser/report.json")
        before = read("parser/before-parser.json")
        check(
            "old_five_failures_retained",
            sum(c["outcome"] == "rejected" for c in before["cases"]) == 5,
        )
        before_cases = {c["id"]: c for c in before["cases"]}
        check(
            "proof_limits",
            proof["parser_issue_resolved"]
            and not proof["full_action_absence"]
            and proof["historical_available_at"] is None,
        )
        with tarfile.open(root / "supplement/acquisition.tar.gz") as archive:

            def source(name):
                member = archive.getmember(name)
                check("source_file/" + name, member.isfile())
                return archive.extractfile(member).read()

            for case in proof["cases"]:
                label = case["id"]
                folder = "parser/replays/" + label + "/"
                raw = (root / folder / "page-01.response.bin").read_bytes()
                sent = (root / folder / "page-01.request.bin").read_bytes()
                check(
                    label + "/response_original",
                    raw == source("raw/bao-actions/" + label + "/page-01.response.bin"),
                )
                check(
                    label + "/request_original",
                    sent == source("raw/bao-actions/" + label + "/page-01.request.bin"),
                )
                check(
                    label + "/hashes",
                    hashlib.sha256(raw).hexdigest()
                    == case["raw_response_sha256"]
                    == before_cases[label]["response_sha256"],
                )
                receipt = read(folder + "receipt.json")
                core = dict(receipt)
                identifier = core.pop("receipt_id")
                check(label + "/new_receipt_hash", identifier == sh(core) == case["new_receipt_id"])
                old = source("raw/bao-actions/" + label + "/receipt.json")
                check(
                    label + "/old_receipt_hash",
                    hashlib.sha256(old).hexdigest() == case["original_receipt_sha256"],
                )
                check(
                    label + "/old_receipt_status",
                    json.loads(old)["query_status"] == case["original_query_status"],
                )
                decoded = decode_response(raw)
                check(
                    label + "/identity",
                    check_identity(case["request"], decode_request(sent), decoded, 1)
                    == case["identity"],
                )
                check(
                    label + "/rows",
                    decoded["rows"] == read(folder + "sdk-rows.json")
                    and len(decoded["rows"]) == case["rows"],
                )
                expected = "complete_empty_unknown" if case["native_empty"] else "complete_rows"
                check(
                    label + "/status",
                    receipt["query_status"] == case["new_query_status"] == expected,
                )
                check(
                    label + "/clock_and_scope",
                    receipt["evidence_kind"] == "offline_test_only"
                    and receipt["available_at"] is None
                    and not receipt["confirmed_no_events"]
                    and not receipt["strict_admitted"],
                )
            coverage = read("supplement/action-coverage-supplement.json")
            action_count = 0
            for entry in coverage["tdx"]:
                rows = json.loads(source(entry["source_ref"]))
                action_count += len(rows)
                check(
                    entry["security"] + "/tdx_action_rows",
                    len(rows) == entry["returned_rows"]
                    and not entry["full_corporate_action_absence_proved"],
                )
            check("281_tdx_action_rows", action_count == 281)
            announcement_count = 0
            for entry in coverage["cninfo"]:
                pages = [json.loads(source(path)) for path in entry["source_refs"]]
                rows = [row for page in pages for row in (page.get("announcements") or [])]
                unique = {row["announcementId"] for row in rows}
                announcement_count += len(unique)
                check(
                    entry["security"] + "/cninfo_pages",
                    len(unique) == len(rows) == entry["total"]
                    and not (pages[-1].get("announcements") or []),
                )
            check("254_announcements", announcement_count == 254)
        check(
            "28025_detail_rows",
            sum(c["tdx_detail_rows"] for c in read("supplement/minute-coverage.json")) == 28025,
        )
        opening = read("supplement/opening-crosschecks.json")
        check(
            "six_opening_counterexamples",
            len(opening) == 6
            and all(
                c["hand_calculations"]["tdx_point_difference_lots"] == 0
                and c["bar_start"] is c["bar_end"] is None
                for c in opening
            ),
        )
        check("five_repaired_empty_queries", sum(c["native_empty"] for c in proof["cases"]) == 5)
        check("twelve_nonempty_rows", sum(c["rows"] for c in proof["cases"]) == 12)
    return dict(
        schema="ad.minute-supplement-acceptance.v1",
        passed=len(checks),
        failed=0,
        checks=checks,
        binding_sha256=expected_sha,
        module=ashare_data.__file__,
        network_used=False,
        ready_for_execution=False,
        real_backtest_executed=False,
        historical_pit=False,
        full_action_absence=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--base", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.binding, args.sha256, args.base), ensure_ascii=False, indent=2))
