"""Verify local sealed FI/SV acquisition and integration evidence without network reads."""

import argparse
import hashlib
import json
from pathlib import Path
import re

from ashare_data.f1_acceptance import instant
from ashare_data.f1_financial import FinancialLedger, RESEARCH_MODE
from ashare_data.f1_fi_sv_scoped import (
    SHARE_MODE,
    fi02_lagged_candidate,
    observe_fi_sv_batch,
    sv_stage_candidate,
)
from ashare_data.f1_precision import require


def verify(root):
    root = root.resolve()
    contract = json.loads((Path(__file__).parents[1] / "docs/f1-fi-sv-round14.json").read_text())

    def path(relative):
        value = root / relative
        require(
            not value.is_symlink() and value.resolve().is_relative_to(root), "unsafe evidence path"
        )
        return value

    def read(relative):
        return json.loads(path(relative).read_text())

    for relative, expected in contract["evidence_files"].items():
        require(
            hashlib.sha256(path(relative).read_bytes()).hexdigest() == expected,
            "evidence hash changed: " + relative,
        )
    manifest = read("evidence/source-freeze-fi-sv-round13.json")
    for relative, expected in manifest["files"].items():
        require(
            hashlib.sha256(path(relative).read_bytes()).hexdigest() == expected,
            "frozen round13 file changed: " + relative,
        )
    folder = "fi-sv-confirmation-round14/"
    preflight = read(folder + "execution-preflight.json")
    auth = read(folder + "authorization-binding.json")
    require(
        auth["recorded_at"] == preflight["recorded_at"]
        and auth["parent_authorization"] == preflight["parent_authorization"],
        "authorization transcription differs",
    )
    require(
        instant(auth["assembled_at"]) > instant(auth["recorded_at"]),
        "later extraction time must be explicit",
    )
    for relative, expected in preflight["files"].items():
        require(
            hashlib.sha256(path(folder + relative).read_bytes()).hexdigest() == expected,
            "pre-query snapshot changed",
        )
    for suffix in ("before", "after-call1-full", "after-call2", "after-call3", "after-call4"):
        text = path(folder + "credits-" + suffix + ".txt").read_text()
        require(re.findall(r"积分中心 (\d+)", text) == ["45"], "credit baseline changed")
    lock = read("fi-sv-followup-round13/prediction-lock.json")
    require(lock["sha256"] == contract["prediction_lock_sha256"], "different frozen experiment")
    calls, records = [], []
    for index, planned in enumerate(lock["payload"]["calls"]):
        text = path(folder + f"call{index + 1}-cell.txt").read_text()
        captured = {}
        for tag in ("BEGIN", "RESULT"):
            lines = [
                line.split(" ", 1)[1]
                for line in text.splitlines()
                if line.startswith("AD_F1_R14_" + tag + " ")
            ]
            require(len(lines) == 1, "missing or repeated dispatch/result")
            captured[tag] = json.loads(lines[0])
        begin, result = captured["BEGIN"], captured["RESULT"]
        require(
            begin["api"] == result["api"] == planned["api"]
            and begin["params"]
            == {k: v for k, v in planned.items() if k not in {"api", "max_values"}},
            "executed request differs from lock",
        )
        require(
            begin["call_index"] == result["call_index"] == index
            and begin["expected_values"] == len(result["rows"]) == planned["max_values"],
            "call accounting differs",
        )
        require(begin["global_before"] == 229 + len(records), "global pre-count differs")
        records.extend(result["rows"])
        require(
            result["cumulative_calls"] == index + 1
            and result["cumulative_values"] == len(records)
            and result["global_actual"] == 229 + len(records),
            "global actual count differs",
        )
        calls.append(result)
    require(
        calls == read(folder + "call-results.json")
        and records == read(folder + "reference-records.json"),
        "raw output transcription differs",
    )
    require(len(records) == 11, "actual value budget differs")
    release = read(folder + "browser-release.json")
    require(
        release["released"] is True
        and release["global_after"] == 240
        and release["credit_deduction"] == release["paid_amount"] == 0,
        "browser release/accounting differs",
    )
    before, after = read(folder + "notebook-before.json"), read(folder + "notebook-reloaded.json")
    require(before["cell_count"] == 29 and after["cell_count"] == 34, "notebook layout differs")

    def normalized(value):
        return re.sub(r"^In\s*\[[^]]*\]:", "In []:", value)

    require(
        all(
            normalized(b["text"]) == normalized(a["text"])
            for b, a in zip(before["cells"][:-1], after["cells"][:28], strict=True)
        ),
        "existing notebook contents changed",
    )
    for index, captured in enumerate(calls):
        require(
            "AD_F1_R14_RESULT " + json.dumps(captured, sort_keys=True)
            in after["cells"][28 + index]["text"],
            "persisted notebook output differs",
        )
    inputs = read(folder + "integration-inputs.json")
    frozen = read("evidence/fi-sv-comparison-round14.json")
    fi, sv = [], []
    for old in frozen["replay"]["FI02"]:
        result = fi02_lagged_candidate(
            FinancialLedger(inputs["financial_packs"][old["security"]]),
            old["security"],
            old["date"],
            mode=RESEARCH_MODE,
        )
        require(
            all(result[k] == old[k] for k in ("prediction", "exact_ratio", "annual_bridge_oracle")),
            "ported FI02 arithmetic differs",
        )
        fi.append(result)
    for old in frozen["replay"]["SV"]:
        result = sv_stage_candidate(
            inputs["market"], inputs["sessions"], inputs["shares"], old["date"], mode=SHARE_MODE
        )
        require(
            all(result[k]["prediction"] == old[k]["prediction"] for k in ("SV01", "SV02")),
            "ported SV arithmetic differs",
        )
        require(
            result["SV02"]["raw_cap_prediction"] == old["SV02"]["raw_cap_prediction"]
            and [v["prediction"] for v in result["daily_inputs"]]
            == [v["prediction"] for v in old["daily_inputs"]],
            "ported raw-stage arithmetic differs",
        )
        sv.append(result)
    observation = observe_fi_sv_batch(lock, records, auth)
    require(
        [r["zero_ULP_pass"] for r in observation["rows"]]
        == [r["zero_ULP_pass"] for r in frozen["comparison"]["rows"]],
        "old and integrated comparison disagree",
    )
    require(
        {"FI02": fi, "SV": sv, "observation": observation} == read(folder + "scoped-replay.json"),
        "saved replay differs",
    )
    require(
        observation["passed_cells"] == 10
        and observation["named_passed_cells"] == 2
        and observation["raw_passed_cells"] == 8
        and not observation["finite_numeric_batch_passed"],
        "frozen one-ULP failure was not preserved",
    )
    return {
        "frozen_round13_files_unchanged": len(manifest["files"]),
        "round14_artifacts_verified": len(contract["evidence_files"]),
        "ported_FI02_cases": len(fi),
        "ported_SV_cases": len(sv),
        "named_passes": 2,
        "raw_stage_passes": 8,
        "SV02_named_candidate_passed": False,
        "global_actual": 240,
        "credits": 45,
        "JQ_calls_this_replay": 0,
        "browser_actions_this_replay": 0,
        "independent_review_completed": False,
        "global_unexposed_proven": False,
        "strict_PIT_certified": False,
        "all_twelve_slots_passed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_root", type=Path)
    print(json.dumps(verify(parser.parse_args().evidence_root), ensure_ascii=False, indent=2))
