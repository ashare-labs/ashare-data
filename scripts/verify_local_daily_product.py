"""Verify a configured real-source publication offline, without collecting or copying the library."""

import argparse
import hashlib
import json
from pathlib import Path

from ashare_data import Store
from ashare_data.core.model import digest
from ashare_data.product import LocalDailyProduct
from ashare_data.product.configuration import resolve


def verify(deployment, output, securities, windows, reference_set=None):
    config = resolve(deployment, reference_set=reference_set)
    product = LocalDailyProduct(config["daily_root"], references=config["references"])
    output = Path(output).resolve()
    store_root = output / "store"
    results = []
    for start, end in windows:
        prices = product.prices(securities, start, end)
        coverage = product.coverage(securities, start, end)
        if not coverage["ready"]:
            raise AssertionError(json.dumps(coverage["gaps"], ensure_ascii=False))
        release = product.publish(store_root, securities, start, end)
        assert release == product.publish(store_root, securities, start, end)
        view = Store(store_root).local_daily_product(release["dataset_id"])
        document, schedule = view.read(), view.read_corporate_schedule()
        indexed = {(r["security"], r["date"]): r for r in document["daily"]}
        for original in prices["rows"]:
            saved = indexed[original["security"], original["date"]]
            assert all(saved[key] == value for key, value in original.items())
        assert release["coverage_sha256"] == coverage["coverage_sha256"]
        assert view.read_coverage() == coverage
        assert len(document["reference_evidence"]["market"]["rows"]) == len(prices["rows"])
        assert document["calendar"].index(start) == 120
        assert document["calendar"][-1] > end
        assert (
            hashlib.sha256(view.portfolio_input_path.read_bytes()).hexdigest()
            == release["portfolio_input_sha256"]
        )
        results.append(
            dict(
                start=start,
                end=end,
                securities=sorted(securities),
                original_price_rows=len(prices["rows"]),
                prices_sha256=digest(prices["rows"]),
                formal_library_root=str(config["daily_root"]),
                price_receipt=prices["receipt"],
                corporate_events=[
                    {
                        k: row.get(k)
                        for k in (
                            "security",
                            "record_date",
                            "ex_date",
                            "payment_date",
                            "cash_per_share",
                            "share_credit_date",
                            "new_shares_tradable_date",
                        )
                    }
                    for row in schedule["rows"]
                ],
                release=release,
            )
        )
    report = dict(
        schema="ad-product-source-verification.v1",
        network_used=False,
        mode="covered_research",
        reference_set=reference_set or config["default_reference_set"],
        results=results,
        limits=["This verifies data publication, not native account execution or task recovery."],
    )
    (output / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--reference-set")
    parser.add_argument("--security", action="append", required=True)
    parser.add_argument(
        "--window", action="append", required=True, help="START:END, repeat for independent windows"
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    windows = [item.split(":") for item in args.window]
    if any(len(item) != 2 for item in windows):
        parser.error("Each window must be START:END")
    report = verify(args.deployment, args.output, args.security, windows, args.reference_set)
    print(
        json.dumps(
            {
                "output": str(args.output / "verification.json"),
                "windows": len(report["results"]),
                "dataset_ids": [r["release"]["dataset_id"] for r in report["results"]],
            }
        )
    )


if __name__ == "__main__":
    main()
