"""F2-REF-REVIEW-001: portable, offline regression tests for the public compare().

Run with Python 3.9+; stdlib only. No market data, credentials, jqfactor, or network.
    python3 test_reference_acceptance_regression.py --comparator /path/compare_reference.py

Tests assert the corrected behavior: the sealed buggy comparator must fail the
top/bottom cases; a corrected comparator must pass all five cases. All payloads
are synthetic. The 252/3096 shapes exist solely because compare() requires them;
each counterexample changes just one reference cell to NaN.
"""

import argparse
import contextlib
from datetime import date, timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
FIELDS = ["BIAS5", "CCI20", "PSY", "quick_ratio", "debt_to_asset_ratio",
          "net_operating_cash_flow_coverage"]
SECURITIES = [f"{i:06}.XSHE" for i in range(1, 13)]
DATES = ["2020-01-13", "2023-04-25", "2023-04-27", "2024-05-06"]
COMPARATOR = None


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def build_fixture(base, missing):
    """Build valid frozen-grid plumbing; no real-data fixture is needed."""
    repo, root = base / "repo", base / "reference"
    queries = [{"date": d, "fields": FIELDS[:3] if i == 1 else FIELDS,
                "securities": SECURITIES, "count": 1,
                "maximum_returned_values": 36 if i == 1 else 72}
               for i, d in enumerate(DATES)]
    thresholds = {f: {"atol": 1e-6, "rtol": 0.0001} for f in FIELDS}
    thresholds.update(BIAS5={"atol": 0.01, "rtol": 0.001},
                      CCI20={"atol": 0.1, "rtol": 0.001},
                      PSY={"atol": 1e-6, "rtol": 0})
    scope = {
        "synthetic": True, "fields": {f: {} for f in FIELDS},
        "thresholds": thresholds, "coverage_min": 0.95,
        "numeric_pass_min": 0.95, "minimum_panel_size": 10,
        "rank_rule": {"spearman_min": 0.98, "pair_concordance_min": 0.97,
                      "top_k": 3, "top_overlap_min": 1.0,
                      "bottom_overlap_min": 1.0,
                      "tie_break": "security ascending; Spearman average ties",
                      "all_eligible_panels_must_pass": True},
        "proposed_JQ_reference": {"queries": queries},
    }
    unused = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(39)]
    predictions = {"synthetic": True, "rows": [
        {"security": s, "date": d, "field": f, "value": str(i)}
        for d in DATES + unused for i, s in enumerate(SECURITIES, 1) for f in FIELDS
    ]}
    assert len(predictions["rows"]) == 3096
    paths = {"scope": repo / "docs/f2-batch-scope.json",
             "inputs": repo / ".local-evidence/f2-batch/derived/dataset.json",
             "predictions": repo / ".local-evidence/f2-batch/results/local-panel.json"}
    for name, value in [("scope", scope), ("inputs", {"synthetic": True}),
                        ("predictions", predictions)]:
        dump(paths[name], value)
    hashes = {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in paths.items()}
    dump(root / "frozen-protection-before.json", {"hashes": hashes, "commit": "0" * 40})
    for index, q in enumerate(queries, 1):
        rows = []
        for f in q["fields"]:
            for i, s in enumerate(SECURITIES, 1):
                absent = (q["date"], s) in missing and f == "PSY"
                rows.append({"security": s, "date": q["date"], "field": f,
                             "value": None if absent else str(i),
                             "missing_kind": "nan" if absent else "none",
                             "float64_bits_be": "7ff8000000000000" if absent
                             else struct.pack(">d", float(i)).hex()})
        payload = {"synthetic": True, "call": index, "request": q,
                   "scope_sha256": hashes["scope"],
                   "local_predictions_sha256": hashes["predictions"],
                   "returned_cells": len(rows), "rows": rows,
                   "nan_cells": sum(r["value"] is None for r in rows)}
        dump(root / "raw" / f"call-{index}.json", payload)
        (root / "raw" / f"call-{index}-output.txt").write_text(
            "SYNTHETIC OFFLINE TEST; NOT A JOINQUANT RESPONSE\n"
            "F2_252_JSON_BEGIN\n" + json.dumps(payload) +
            f"\nF2_252_JSON_END\nF2_252_COMPLETE call={index}\n")
    return repo, root


class AvailablePoolAcceptance(unittest.TestCase):
    def evaluate(self, missing=()):
        with tempfile.TemporaryDirectory(prefix="f2-selection-regression-") as tmp:
            repo, root = build_fixture(Path(tmp), set(missing))
            with contextlib.redirect_stdout(io.StringIO()):
                COMPARATOR.compare(repo, root)
            result = json.loads((root / "results/comparison.json").read_text())
        # A missing response still consumes one of the 252 requested cells.
        self.assertEqual(result["reference_cells"], 252)
        self.assertEqual(sum(q["nan"] for q in result["query_checks"]), len(missing))
        return result

    def test_complete_pools_remain_accepted(self):
        result = self.evaluate()
        for factor in result["summary"].values():
            self.assertTrue(factor["frozen_practical_acceptance"])
            self.assertEqual(factor["top_replacements"], 0)
            self.assertEqual(factor["bottom_replacements"], 0)

    def check_missing_selected_reference(self, security, tail):
        result = self.evaluate([(DATES[0], security)])
        factor = result["summary"]["PSY"]
        panel = next(p for p in result["panels"] if p["date"] == DATES[0] and p["field"] == "PSY")
        self.assertEqual(factor["requested"], 48)
        self.assertEqual(factor["paired"], 47)
        self.assertTrue(factor["numeric_usable"])
        self.assertEqual(panel["n"], 11)
        self.assertEqual(panel["spearman"], 1)
        self.assertEqual(panel[tail]["overlap"], 1)  # Intersection hides the lost member.
        self.assertEqual(panel["available_pools"][tail]["replacement_count"], 1)
        self.assertEqual(
            (factor["rank_usable"], factor["frozen_practical_acceptance"]),
            (False, False),
            f"A {tail}3 member was replaced despite common-pool agreement; reject selection acceptance",
        )

    def test_missing_top_reference_must_reject_selection_acceptance(self):
        self.check_missing_selected_reference(SECURITIES[-1], "top")

    def test_missing_bottom_reference_must_reject_selection_acceptance(self):
        self.check_missing_selected_reference(SECURITIES[0], "bottom")

    def test_missing_middle_preserves_frozen_95_percent_rule(self):
        factor = self.evaluate([(DATES[0], SECURITIES[5])])["summary"]["PSY"]
        self.assertEqual(factor["paired"], 47)
        self.assertEqual(factor["top_replacements"], 0)
        self.assertEqual(factor["bottom_replacements"], 0)
        self.assertTrue(factor["frozen_practical_acceptance"])

    def test_below_95_percent_coverage_remains_rejected(self):
        factor = self.evaluate([(d, SECURITIES[5]) for d in DATES[:3]])["summary"]["PSY"]
        self.assertEqual(factor["paired"], 45)
        self.assertFalse(factor["numeric_usable"])
        self.assertFalse(factor["rank_usable"])
        self.assertFalse(factor["frozen_practical_acceptance"])


def main():
    global COMPARATOR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparator", type=Path,
                        default=Path(__file__).with_name("sealed_compare_reference.py"))
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("comparator_under_test", args.comparator)
    COMPARATOR = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(COMPARATOR)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AvailablePoolAcceptance)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.result:
        dump(args.result, {"comparator": str(args.comparator),
                           "comparator_sha256": hashlib.sha256(args.comparator.read_bytes()).hexdigest(),
                           "tests": result.testsRun, "failures": len(result.failures),
                           "errors": len(result.errors), "success": result.wasSuccessful(),
                           "failed_test_names": [t.id() for t, _ in result.failures],
                           "all_fixtures_synthetic": True, "new_JQ_calls": 0})
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
