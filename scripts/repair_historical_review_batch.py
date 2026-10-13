"""Rebuild the fixed reviewed historical batch without modifying its original.

F3 corrects issuer payment scope, not the raw PDF or cash/ex-price amounts.
F2 names the existing generic vs issuer-fixed ST rule dependencies explicitly.
All network captures, unknown historical times and the 232/696 gap stay fixed.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from ashare_data import HistoricalMarketView
from ashare_data.model import canonical, require
from build_historical_market import build

ORIGINAL_SHA = "f8d4bb3c46c26780adc1ee60c8385c05439b10e968200f14e5112fb042eb4437"


def repair(source_root, staging, destination):
    source, stage = Path(source_root).resolve(), Path(staging).resolve()
    HistoricalMarketView(source / "binding.json", expected_binding_sha256=ORIGINAL_SHA)
    require(not stage.exists(), "HISTORICAL_FILE", "Do not replace an earlier correction")
    shutil.copytree(source, stage)
    before = (stage / "curation.json").read_bytes()
    curation = json.loads(before)
    for rule in curation["rules"]:
        if rule["depends_on_st"]:
            rule["st_policy"] = "daily_status"
        if rule["securities"] == ["600518.XSHG"]:
            rule["st_policy"] = "fixed_event"
            rule["event_id"] = "600518_2019-05-21_other_risk_warning"
    gree = next(a for a in curation["actions"] if a["event_id"] == "000651-2022-04-08-distribution")
    gree.update(
        payment_date_scope="CSDC Shenzhen delegated A-share cash distribution only; account-specific realization unproved",
        company_self_payment_date=None,
        company_self_payment_date_status="unknown: issuer p2 lists separate company-paid holders without dating their payment",
        company_self_payment_scope="Separately listed issuer-paid holders in issuer PDF p2 section V.2; no delegated date backfill",
    )
    after = canonical(curation)
    (stage / "curation.json").write_bytes(after)
    result = build(stage, destination)
    return {
        **result,
        "original_binding_sha256": ORIGINAL_SHA,
        "curation_before_sha256": hashlib.sha256(before).hexdigest(),
        "curation_after_sha256": hashlib.sha256(after).hexdigest(),
        "changed_source_input_files": ["curation.json"],
        "network_used": False,
        "raw_captures_changed": False,
        "old_package_modified": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root")
    parser.add_argument("staging")
    parser.add_argument("destination")
    args = parser.parse_args()
    print(
        json.dumps(
            repair(args.source_root, args.staging, args.destination), ensure_ascii=False, indent=2
        )
    )
