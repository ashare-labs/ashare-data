"""Offline frozen-output/oracle checks for three unaligned research entries.

This is reproducible integration evidence, not independent review sign-off.
Private observations are read only from the pinned local evidence directory.
"""

import argparse
import ast
from decimal import Decimal, localcontext
from fractions import Fraction as F
import hashlib
import importlib.util
import json
from pathlib import Path

from ashare_data import f1
from ashare_data import f1_remaining, f1_vroc6_snapshot


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def node(path, *names):
    tree = ast.parse(path.read_text())
    for name in names:
        tree = next(n for n in tree.body if getattr(n, "name", None) == name)
    return ast.dump(tree, include_attributes=False)


def verify(repo, evidence):
    record = read(repo / "docs/f1-remaining-research.json")
    assert sha(evidence / "manifest.json") == record["local_evidence"]["manifest_sha256"]
    manifest = read(evidence / "manifest.json")
    for name, ref in manifest["files"].items():
        path = evidence / name
        assert not Path(name).is_absolute() and ".." not in Path(name).parts
        assert path.resolve().is_relative_to(evidence.resolve()) and not path.is_symlink()
        assert path.stat().st_size == ref["bytes"] and sha(path) == ref["sha256"], name
    source = evidence / "pv01/code/ashare_data/f1_price_volume.py"
    port = Path(f1_remaining.__file__)
    assert node(source, "_decimal") == node(port, "_decimal")
    assert node(source, "NativeSeries", "roc120") == node(port, "_ROCSeries", "roc120")
    assert sha(Path(f1_vroc6_snapshot.__file__)) == sha(
        evidence / "pv05/research/native_snapshot.py"
    )

    native = evidence / "pv01"
    inputs = read(native / "inputs.json")
    predictions = read(native / "results/native-predictions.json")
    roc_count = 0
    for saved in predictions["results"]:
        if saved["slot"] != "PV01":
            continue
        security = saved["rows"][0]["security"]
        result = f1.roc120_native_candidate(
            native,
            security,
            [r["date"] for r in saved["rows"]],
            expected_inputs_sha256=predictions["inputs_sha256"],
            definition=f1.ROC120_RESEARCH_DEFINITION,
            role=f1.RESEARCH_ROLE,
        )
        assert all(result[key] == value for key, value in saved.items() if key != "phase")
        item = next(item for item in inputs["items"] if item["security"] == security)
        rows = {r["date"]: r for r in item["rows"]}
        for row in result["rows"]:
            index = item["sessions"].index(row["date"])
            dates = item["sessions"][index - 120 : index + 1]
            assert len(dates) == 121
            adjusted = [
                F(rows[d]["close"])
                * F([factor["value"] for factor in item["factors"] if factor["date"] <= d][-1])
                for d in [dates[0], dates[-1]]
            ]
            exact = 100 * (adjusted[-1] / adjusted[0] - 1)
            with localcontext() as context:
                context.prec = 50
                assert Decimal(row["value"]) == Decimal(exact.numerator) / Decimal(
                    exact.denominator
                )
            roc_count += 1
    assert roc_count == 24
    assert read(evidence / "reviews/pv-review-verdict.json")["ROC120"]["match"] == 0

    volume = f1.vroc6_fixed_snapshot_candidate(
        evidence / "pv05",
        expected_binding_sha256=record["slots"]["PV05"]["binding_sha256"],
        definition=f1.VROC6_RESEARCH_DEFINITION,
        role=f1.VROC6_RESEARCH_ROLE,
    )
    saved_volume = read(evidence / "pv05/results/native-research-result.json")
    assert all(volume[key] == value for key, value in saved_volume.items())
    quantities = []
    for row in volume["rows"]:
        fraction = F(row["raw_volume_shares"]) / F(row["native_factor_text"])
        integer, remainder = divmod(fraction.numerator, fraction.denominator)
        rounded = integer + (
            2 * remainder > fraction.denominator
            or (2 * remainder == fraction.denominator and integer % 2 == 1)
        )
        assert rounded == row["local_research_quantity"]
        quantities.append(rounded)
    volume_exact = F(100 * (quantities[-1] - quantities[0]), quantities[0])
    assert volume_exact == F(
        int(volume["value_exact"]["numerator"]), int(volume["value_exact"]["denominator"])
    )
    assert volume["fixed_demo_JQ_target_compared"] is False
    assert volume["source_implementation_independent_review_completed"] is False

    financial = evidence / "fi03"
    pack = read(financial / "financial-chain.json")["input_pack"]
    f1.verify_pack_files(pack, financial)
    frozen_file = financial / "code/f1_financial.py"
    spec = importlib.util.spec_from_file_location("ashare_data._frozen_fi03", frozen_file)
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    ledger, old_ledger = f1.FinancialLedger(pack), original.FinancialLedger(pack)
    frozen = read(financial / "frozen-predictions.json")["fi03_precision_diagnostics"]
    archived = read(financial / "frozen-archive-replay.json")["rows"]
    fi_rows = []
    for day in record["slots"]["FI03"]["fixed_regression_dates"]:
        cutoff = day + "T15:00:00+08:00"
        result = f1.reported_operating_revenue_ttm(
            ledger,
            pack["security"],
            cutoff,
            definition=f1.FI03_RESEARCH_DEFINITION,
            mode=f1.RESEARCH_MODE,
        )
        before = original.financial_factor_candidate(
            old_ledger, "FI03", pack["security"], cutoff, mode=f1.RESEARCH_MODE
        )
        assert result["value"] == before["value"]
        assert result["components"] == before["components"]
        saved = next(r for r in frozen + archived if r["as_of"] == cutoff)
        assert result["value"] == saved["value"]
        component = result["components"]["operating_revenue"]
        total = F()
        for quarter in component["quarters"]:
            amount = sum(
                (
                    term["coefficient"] * F(ledger.by_id[term["record_id"]]["value"])
                    for term in quarter["terms"]
                ),
                F(),
            )
            assert amount == F(quarter["value"])
            total += amount
        assert total == F(result["value"]) and component["quarter_sum_equals_bridge"]
        fi_rows.append(result)
    historical = read(evidence / "reviews/fi-round7-replay.json")["FI03"]
    assert historical  # The original discrepancy records remain byte-bound above.
    return {
        "status": "software_checks_passed_review_pending",
        "verified_evidence_files": len(manifest["files"]),
        "ROC120_frozen_predictions_and_fraction_oracle": roc_count,
        "ROC120_method_AST_unchanged": True,
        "VROC6_core_bytes_unchanged": True,
        "VROC6_frozen_full_result_unchanged": True,
        "VROC6_exact_quantity_oracle_rows": len(quantities),
        "FI03_frozen_values_components_and_fraction_oracle": len(fi_rows),
        "source_files_for_FI03_verified": len(pack["sources"]),
        "integration_independent_review_completed": False,
        "new_named_factor_acceptances": 0,
        "original_alignment_failures_preserved": True,
        "new_market_reads": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    evidence = (args.evidence or repo / ".local-evidence/f1-remaining").resolve()
    print(json.dumps(verify(repo, evidence), indent=2))


if __name__ == "__main__":
    main()
