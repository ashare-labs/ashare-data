"""Accept a complete closed public batch and exercise the frozen consumer boundary."""

import argparse
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import gzip
import json
from pathlib import Path

from ashare_data.core.model import require
from ashare_data.product.financial_release import FinancialTables
from ashare_data.storage.market_release import MarketRelease, canonical, sha_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    candidate = json.loads((args.candidate / "candidate.json").read_bytes())
    require(len(candidate["batches"]) == 1, "FIN_ACCEPTANCE", "Select one complete public-batch candidate")
    root = Path(candidate["batches"][0]["source_root"])
    tasks = json.loads((root / "tasks.json").read_bytes())
    parent = MarketRelease(candidate["parent_root"], candidate["parent_release_id"],
                           expected_manifest_sha256=candidate["parent_manifest_sha256"])
    reader = FinancialTables.prepared(args.candidate, parent)
    expected = Counter(t["kind"] for t in tasks)
    with reader.sql() as db:
        actual = dict(db.execute("SELECT source,count(*) FROM meta.financial_coverage GROUP BY source").fetchall())
        require(actual == expected, "FIN_ACCEPTANCE", "The frozen task set is not completely accounted for")
        coverage = db.execute("""SELECT source,source_state,count(*),sum(input_rows),
          sum(mapped_rows),sum(quarantined_rows) FROM meta.financial_coverage
          GROUP BY source,source_state ORDER BY source,source_state""").fetchall()
        reports = db.execute("""SELECT r.source,r.statement,min(r.period_end),max(r.period_end),
          count(DISTINCT r.report_version_id),count(DISTINCT i.instrument_id)
          FROM financial.report_versions r JOIN reference.issuer_instruments i USING(issuer_id)
          GROUP BY r.source,r.statement ORDER BY r.source,r.statement""").fetchall()
        shares = db.execute("""SELECT source,field,min(effective_date),max(effective_date),
          count(*),count(value_decimal_text),count(DISTINCT instrument_id)
          FROM reference.share_observations GROUP BY source,field ORDER BY source,field""").fetchall()
        actions = db.execute("""SELECT source,implemented,min(ex_date),max(ex_date),
          count(*),count(DISTINCT instrument_id)
          FROM corporate_action.events GROUP BY source,implemented ORDER BY source,implemented""").fetchall()
        quality = db.execute("""SELECT quality_state,unit_state,count(*)
          FROM financial.observations GROUP BY quality_state,unit_state ORDER BY ALL""").fetchall()
    chosen = next(t for t in tasks if t["id"] == "sina_income-sh600000")
    receipt = json.loads((root / "receipts" / (chosen["id"] + ".json")).read_bytes())
    artifact = root / receipt["normalized"]["path"]
    require(sha_file(artifact) == receipt["normalized"]["sha256"],
            "FIN_ACCEPTANCE", "Consumer source artifact changed")
    with gzip.open(artifact, "rt") as stream:
        report = next(row for row in (json.loads(line) for line in stream)
                      if row.get("period_end") == "2026-06-30")
    captured = datetime.fromisoformat(receipt["observed_at"])
    cutoff = (captured + timedelta(seconds=1)).isoformat()
    query = dict(source="sina", as_of=cutoff, mode="source_research")
    checks = {}
    for field in ("net_profit", "operating_revenue"):
        result = reader.point("600000.XSHG", field, report["period_end"], **query)
        require(result["status"] == "ok" and
                Decimal(result["value"]) == Decimal(report["metrics"][field]["value"]),
                "FIN_ACCEPTANCE", "Source-to-consumer value differs")
        checks[field] = result
    pre_capture = reader.point("600000.XSHG", "net_profit", report["period_end"],
                               source="sina", mode="source_research",
                               as_of=(captured - timedelta(microseconds=1)).isoformat())
    require(pre_capture["value"] is None and "snapshot_not_captured_at_cutoff" in pre_capture["reasons"],
            "FIN_ACCEPTANCE", "Provider snapshot became available before capture")
    historical = reader.point("600000.XSHG", "net_profit", report["period_end"],
                              source="sina", as_of=cutoff)
    require(historical["value"] is None and "historical_visibility_unproven" in historical["reasons"],
            "FIN_ACCEPTANCE", "Provider date claim became historical PIT evidence")
    ratio = reader.common_indicator("600000.XSHG", "netprofit_margin", report["period_end"], **query)
    with localcontext() as ctx:
        ctx.prec = 38
        ratio_expected = 100 * Decimal(checks["net_profit"]["value"]) / Decimal(checks["operating_revenue"]["value"])
    require(ratio["status"] == "ok" and Decimal(ratio["value"]) == ratio_expected,
            "FIN_ACCEPTANCE", "Same-report common indicator differs")
    result = dict(schema="ad.financial-public-table-acceptance.v1", candidate_id=candidate["candidate_id"],
                  parent_manifest_sha256=parent.sha256, published=False, full_history_complete=False,
                  complete_frozen_task_set_accounted=True, tasks=len(tasks), source_coverage=coverage,
                  report_ranges=reports, share_ranges=shares, action_ranges=actions, field_quality=quality,
                  whole_batch_validation=candidate["validation"], financial_points=checks,
                  pre_capture_denied=pre_capture, historical_pit_denied=historical,
                  common_indicator=ratio, source_artifact=receipt["normalized"])
    with args.output.open("xb") as stream:
        stream.write(canonical(result))
    print(json.dumps({k: result[k] for k in ("tasks", "source_coverage", "report_ranges",
                                            "share_ranges", "action_ranges", "field_quality")},
                     ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
