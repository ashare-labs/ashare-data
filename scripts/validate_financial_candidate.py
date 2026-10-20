"""Bounded real consumer acceptance, without publishing or rereading price databases."""

import argparse
from decimal import Decimal
import gzip
import json
from pathlib import Path

from ashare_data.core.model import require
from ashare_data.product.financial_release import FinancialTables
from ashare_data.storage.market_release import MarketRelease, canonical, sha_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--daily-validation-plan", required=True, type=Path)
    parser.add_argument("--share-resolutions", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    candidate = json.loads((args.candidate / "candidate.json").read_text())
    parent = MarketRelease(candidate["parent_root"], candidate["parent_release_id"],
                           expected_manifest_sha256=candidate["parent_manifest_sha256"])
    reader = FinancialTables.prepared(args.candidate, parent)
    plan = json.loads(args.daily_validation_plan.read_text())["daily_validation"]
    comparisons, arithmetic_deferred = [], []
    for item in plan["queries"]:
        query = item["query"]
        if query["basis"] != "YTD":
            arithmetic_deferred.append(dict(query=query, reason="existing_FinancialPIT_arithmetic_contract_unchanged"))
            continue
        result = reader.point(query["securities"], query["fields"], query["period"],
                              source="cninfo", as_of=query["as_of"], mode="official_daily")
        expected = item["expected"][0]
        same_value = (result["value"] is None if expected["value"] is None
                      else result["value"] is not None and Decimal(result["value"]) == Decimal(expected["value"]))
        require(same_value and ((result["status"] == "ok") == (expected["status"] == "ok")),
                "FIN_CONSUMER", "Published-table history differs from reviewed official expectation")
        comparisons.append(dict(query=query, expected=expected, actual=result, matched=True))
    bundle = json.loads((args.share_resolutions / "manifest.json").read_text())
    artifact = args.share_resolutions / bundle["records"]["path"]
    require(sha_file(artifact) == bundle["records"]["sha256"], "FIN_CONSUMER", "Share resolution source changed")
    records = [json.loads(line) for line in gzip.decompress(artifact.read_bytes()).splitlines()]
    expected = {(r["security"], r["date"], r["field"]): r["value"]
                for r in records if r["status"] == "source_value_recovered"}
    denied = {(r["security"], r["date"], r["field"])
              for r in records if r["status"] != "source_value_recovered"}
    with reader.sql() as db:
        rows = db.execute("""SELECT security,effective_date,field,value_decimal_text
          FROM reference.share_observations WHERE source='sina_reviewed_share_point'""").fetchall()
        actual = {(a, b, c): d for a, b, c, d in rows}
        require(len(actual) == len(rows) and actual == expected,
                "FIN_CONSUMER", "Confirmed share points differ in the target table")
        quarantined = db.execute("""SELECT normalized_record_json FROM quality.financial_quarantine
          WHERE source_schema='ad.share-resolution-candidate.v1'""").fetchall()
        actual_denied = {(r["security"], r["date"], r["field"]) for r in
                         (json.loads(row[0]) for row in quarantined)}
        require(actual_denied == denied and not actual.keys() & denied,
                "FIN_CONSUMER", "Unresolved share points entered usable results")
        import pandas as pd
        frame = pd.DataFrame([{
            "security": r["security"], "effective_date": r["date"],
            "field": r["field"], "source_revision_id": r["structure_observation_id"],
        } for r in records])
        db.register("negative_source_cells", frame)
        original = db.execute("""SELECT count(*),count(s.value_decimal_text)
          FROM negative_source_cells e JOIN reference.share_observations s
          USING(security,effective_date,field,source_revision_id)
          WHERE s.source='sina_structure'""").fetchone()
        require(original == (len(records), 0), "FIN_CONSUMER", "Original negative source fields were changed")
        coverage = db.execute("""SELECT source,source_state,count(*) FROM meta.financial_coverage
          GROUP BY ALL ORDER BY source,source_state""").fetchall()
        calendars = db.execute("""SELECT exchange,min(date),max(date),count(*)
          FROM reference.exchange_calendar GROUP BY exchange ORDER BY exchange""").fetchall()
    chosen = next(x["actual"] for x in comparisons if x["actual"]["status"] == "ok")
    with reader.sql(market_tables=("bars_1d",), securities=[chosen["security"]]) as db:
        joined = db.execute("""SELECT DISTINCT i.security,b.trade_date,b.close_price,o.canonical_value_decimal_text
          FROM financial.observations o JOIN reference.issuer_instruments r USING(issuer_id)
          JOIN reference.instruments i USING(instrument_id)
          JOIN market.bars_1d b USING(instrument_id)
          WHERE o.observation_id=? AND b.trade_date=CAST(? AS DATE)""",
                            [chosen["observation_ids"][0], chosen["as_of"][:10]]).fetchall()
        require(bool(joined), "FIN_CONSUMER", "Actual financial/market instrument join is empty")
    result = dict(schema="ad.financial-table-consumer-acceptance.v1", candidate_id=candidate["candidate_id"],
                  parent_manifest_sha256=parent.sha256, published=False,
                  official_point_queries=comparisons, official_point_queries_matched=len(comparisons),
                  existing_arithmetic_deferred=arithmetic_deferred, share_points_matched=len(expected),
                  unresolved_share_points_excluded=len(denied), original_negative_cells_preserved=len(records),
                  source_coverage=coverage, calendar_ranges=calendars,
                  actual_market_join=joined, source_to_table_to_reader_verified=True,
                  full_history_complete=False, strict_historical_pit=False)
    with args.output.open("xb") as stream:
        stream.write(canonical(result))
    print(json.dumps({k: result[k] for k in ("official_point_queries_matched", "share_points_matched",
                                           "unresolved_share_points_excluded", "original_negative_cells_preserved",
                                           "actual_market_join")}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
