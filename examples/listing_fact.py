"""Public offline path; requires the separately delivered, reviewed real evidence package."""
from __future__ import annotations

import argparse
import json

from ashare_data import DataError, Store


def demonstrate(store, evidence_directory):
    snapshot_id = store.import_listing_evidence(evidence_directory)
    assert store.import_listing_evidence(evidence_directory) == snapshot_id
    view = store.listing_fact(snapshot_id)
    rows = [view.get("300750.XSHE", f"2026-09-{day}").to_dict() for day in (28, 29, 30)]
    assert all(row["value"] == 2018 and row["precision"] == "year" for row in rows)
    refusals = {}
    calls = {
        "exact_actual_date": lambda: view.get("300750.XSHE", "2026-09-30", field="initial_listing_date"),
        "historical_eligibility": lambda: view.require_eligible("300750.XSHE", "2026-09-30"),
        "historical_receipt": lambda: view.get("300750.XSHE", "2026-09-30", visibility="received",
                                               knowledge_at="2026-09-30T15:00:00+08:00"),
        "historical_pit": lambda: view.get("300750.XSHE", "2026-09-30", visibility="verified",
                                            knowledge_at="2026-09-30T15:00:00+08:00"),
    }
    date_rows = []
    if "initial_listing_date" in view.descriptor().supported_fields:
        del calls["exact_actual_date"]
        for day in (28, 29, 30):
            fact = view.get("300750.XSHE", f"2026-09-{day}", field="initial_listing_date")
            assert fact.value == "2018-06-11"
            assert fact.source_fields["document_published_date"] == "2018-08-24"
            assert fact.source_fields["document_published_timezone"] is None
            date_rows.append(fact.to_dict())
    for label, call in calls.items():
        try:
            call()
        except DataError as error:
            refusals[label] = error.code
        else:
            raise AssertionError(f"Unexpected admission: {label}")
    for source in view.lineage():
        import hashlib
        assert hashlib.sha256(view.evidence(source.path)).hexdigest() == source.source_sha256
    result = {"descriptor": view.descriptor().to_dict(), "rows": rows, "refusals": refusals,
            "validation": view.validate(), "execution_permission": False}
    if date_rows:
        result["date_rows"] = date_rows
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    print(json.dumps(demonstrate(Store.init(args.store), args.evidence), ensure_ascii=False,
                     sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
