"""Validate a few ex-date windows from explicit local CMES ZIPs; no download or deployment."""

import argparse
import csv
from decimal import Decimal, localcontext
import hashlib
import io
import json
from pathlib import Path
import zipfile

from ashare_data import Store
from ashare_data.core.model import canonical, digest, require
from ashare_data.providers.cmes_factors import CMESFactorView, import_cmes_factors

WINDOWS = (
    ("600000.XSHG", "SH.600000.csv", "2017-05-25"),
    ("600000.XSHG", "SH.600000.csv", "2024-07-18"),
    ("000001.XSHE", "SZ.000001.csv", "2025-06-12"),
)


def validate(packages, output, *, through):
    packages, output = Path(packages), Path(output)
    require(
        not output.exists(), "CMES_VALIDATION_OUTPUT", "Select a new validation output directory"
    )
    output.mkdir(parents=True)
    store = Store.init(output / "factors")
    dataset_id = import_cmes_factors(
        store,
        hfq_path=packages / "adjustment_factors/hfq/all-market-history.zip",
        qfq_path=packages / "adjustment_factors/qfq/all-market-history.zip",
        members={security: member for security, member, _ in WINDOWS},
        through=through,
    )
    factors = CMESFactorView(store, dataset_id)
    results = []
    for security, member, ex_date in WINDOWS:
        # Scalar evidence only: daily import, mapping and volume-unit normalization belong
        # to the daily provider. This validator does not create production daily bars.
        path = packages / "split_daily" / member.replace(".csv", ".zip")
        raw_zip = path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            content = archive.read(member)
        source_rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        by_date = {r["时间"]: r for r in source_rows}
        previous = max(d for d in by_date if d < ex_date)
        rows = [
            {
                "security": security,
                "date": date,
                **{
                    field: by_date[date][native]
                    for field, native in (
                        ("open", "开盘价"),
                        ("high", "最高价"),
                        ("low", "最低价"),
                        ("close", "收盘价"),
                        ("preclose", "昨收价"),
                    )
                },
            }
            for date in (previous, ex_date)
        ]
        receipt = {
            "rows_sha256": digest(rows),
            "source_member": member,
            "source_member_sha256": hashlib.sha256(content).hexdigest(),
            "source_archive_sha256": hashlib.sha256(raw_zip).hexdigest(),
        }
        receipt["receipt_sha256"] = digest(receipt)
        raw = dict(price_basis="raw", start=previous, end=ex_date, rows=rows, receipt=receipt)
        pre = factors.adjust_prices(raw, adjustment="pre", basis_date=ex_date)
        post = factors.adjust_prices(raw, adjustment="post", basis_date=ex_date)
        before, after = factors.factor_at(security, previous), factors.factor_at(security, ex_date)
        with localcontext() as ctx:
            ctx.prec = 34
            h_ratio = Decimal(before["hfq_factor"]) / Decimal(after["hfq_factor"])
            q_ratio = Decimal(after["qfq_factor"]) / Decimal(before["qfq_factor"])
            delta = abs(Decimal(pre["rows"][0]["close"]) - Decimal(rows[1]["preclose"]))
            require(
                abs(h_ratio - q_ratio) <= Decimal("1e-10"),
                "CMES_VALIDATION_PAIR",
                "HFQ/QFQ directions differ",
            )
            require(
                0 < h_ratio < 1 and delta <= Decimal("0.005"),
                "CMES_VALIDATION_REFERENCE",
                "Adjusted close differs from source ex-date reference",
            )
        require(
            Decimal(pre["rows"][1]["close"]) == Decimal(rows[1]["close"])
            and Decimal(post["rows"][1]["close"]) > Decimal(rows[1]["close"]),
            "CMES_VALIDATION_DIRECTION",
            "Price adjustment direction differs",
        )
        reopened = CMESFactorView(store, dataset_id).adjust_prices(
            raw, adjustment="pre", basis_date=ex_date
        )
        require(
            canonical(reopened) == canonical(pre),
            "CMES_VALIDATION_REPLAY",
            "Reopened fixed query differs",
        )
        results.append(
            {
                "security": security,
                "previous_date": previous,
                "ex_date": ex_date,
                "raw_previous_close": rows[0]["close"],
                "raw_ex_close": rows[1]["close"],
                "source_ex_reference": rows[1]["preclose"],
                "pre_previous_close": pre["rows"][0]["close"],
                "pre_ex_close": pre["rows"][1]["close"],
                "post_previous_close": post["rows"][0]["close"],
                "post_ex_close": post["rows"][1]["close"],
                "reference_abs_difference": str(delta),
                "reference_tolerance": "0.005 CNY",
                "hfq_ratio": str(h_ratio),
                "qfq_inverse_ratio": str(q_ratio),
                "factors": [before, after],
                "raw_receipt": receipt,
                "pre_receipt_sha256": pre["receipt"]["receipt_sha256"],
                "post_receipt_sha256": post["receipt"]["receipt_sha256"],
                "fixed_reopen_identical": True,
            }
        )
    report = {
        "schema": "ad.cmes-factor-validation.v1",
        "factors": factors.describe(),
        "windows": results,
        "checks_passed": True,
        "scope": "provider price-factor convention; not independent corporate-action/payment evidence",
    }
    (output / "report.json").write_bytes(canonical(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--through", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            validate(args.packages, args.output, through=args.through), indent=2, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
