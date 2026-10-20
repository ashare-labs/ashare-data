"""Bounded offline acceptance of the reviewed Sina unit overlay.

The existing financial objects are read in small groups. Only this connection's
observation view is narrowed; source files and the shared publication stay intact.
"""

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
import argparse
import json
import os
from pathlib import Path
import tempfile
import time

from ashare_data.product.financial_release import FinancialTables
from ashare_data.storage.financial_release import immutable_json
from ashare_data.storage.market_release import MarketRelease, sha_file, sql_literal


def validate(base, units, output):
    base, units, output = Path(base), Path(units), Path(output)
    metadata = json.loads((base / "candidate.json").read_bytes())
    unit_manifest = json.loads((units / "candidate.json").read_bytes())
    parent = MarketRelease(metadata["parent_root"], metadata["parent_release_id"],
                           expected_manifest_sha256=metadata["parent_manifest_sha256"])
    reader = FinancialTables.prepared(base, parent).with_units(
        units, expected_sha256=sha_file(units / "candidate.json"))
    output.mkdir(parents=True, exist_ok=True)
    total, groups = Counter(), Counter()
    fields = [f for f in reader.files if f["table"] == "financial.observations"]
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="unit-validation-", dir=output) as temporary:
        os.environ["ASHARE_DUCKDB_TEMP_DIRECTORY"] = temporary
        os.environ["ASHARE_DUCKDB_MEMORY_LIMIT"] = "4GB"
        os.environ["ASHARE_DUCKDB_MAX_TEMP_DIRECTORY_SIZE"] = "16GB"
        with reader.sql() as db:
            for first in range(0, len(fields), 40):
                part = fields[first:first + 40]
                paths = ",".join(sql_literal(base / f["path"]) for f in part)
                db.execute("CREATE OR REPLACE VIEW financial.observations AS SELECT DISTINCT * "
                           "FROM read_parquet([" + paths + "],union_by_name=false)")
                values = db.execute("""SELECT
                    count(*) AS observations,
                    count(*) FILTER (WHERE unit_state='unverified' AND value_decimal_text IS NOT NULL) AS before,
                    count(unit_ready_value_decimal_text) AS recovered,
                    count(*) FILTER (WHERE unit_ready_value_decimal_text IS NOT NULL
                        AND unit_ready_value_decimal_text IS DISTINCT FROM value_decimal_text) AS changed_values,
                    count(*) FILTER (WHERE unit_ready_value_decimal_text IS NOT NULL
                        AND raw_value_type IN ('null','absent')) AS missing_filled,
                    count(*) FILTER (WHERE unit_ready_value_decimal_text IS NOT NULL
                        AND canonical_value_decimal_text IS NOT NULL) AS originals_overwritten,
                    count(*) FILTER (WHERE unit_ready_value_decimal_text IS NOT NULL
                        AND regexp_full_match(value_decimal_text,'[+-]?0+([.]0+)?')) AS zero_values,
                    count(*) FILTER (WHERE unit_ready_value_decimal_text IS NOT NULL
                        AND starts_with(value_decimal_text,'-')) AS negative_values
                  FROM financial.unit_resolved_observations""").fetchone()
                names = [r[0] for r in db.description]
                total.update(dict(zip(names, values)))
                assert values[0] == sum(f["rows"] for f in part), "Overlay multiplied or dropped source observations"
                for statement, unit, count in db.execute("""SELECT statement,resolved_unit,count(*)
                      FROM financial.unit_resolved_observations
                      WHERE unit_ready_value_decimal_text IS NOT NULL GROUP BY ALL""").fetchall():
                    groups[(statement, unit)] += count
                print(json.dumps({"files": min(first + 40, len(fields)), "total_files": len(fields),
                                  "recovered": total["recovered"]}), flush=True)
        assert total["observations"] == metadata["validation"]["table_rows"]["financial.observations"]
        assert total["before"] == unit_manifest["coverage"]["numeric_unit_unverified_before"]
        assert total["recovered"] == unit_manifest["coverage"]["numeric_unit_recoverable"]
        assert total["changed_values"] == total["missing_filled"] == total["originals_overwritten"] == 0
        cutoff = unit_manifest["rules"][0]["available_from"]
        later = (datetime.fromisoformat(cutoff) + timedelta(seconds=1)).isoformat()
        earlier = (datetime.fromisoformat(cutoff) - timedelta(microseconds=1)).isoformat()
        queries = {}
        cases = [
            ("cash", "fzb", "CURFDS", "货币资金", "843614518.54", "CNY"),
            ("profit", "lrb", "TOTPROFIT", "利润总额", "256302090.66", "CNY"),
            ("eps", "lrb", "BASICEPS", "基本每股收益", "0.1049", "CNY/share"),
            ("investing", "llb", "INVNETCASHFLOW", "投资活动产生的现金流量净额", "-218217641.77", "CNY"),
        ]
        for name, statement, field, title, expected, unit in cases:
            value = reader.source_point(
                "600012.XSHG", field, "2010-03-31", statement=statement, title=title,
                source="sina", as_of=later, mode="source_research")
            assert value["status"] == "ok" and Decimal(value["value"]) == Decimal(expected) and value["unit"] == unit
            queries[name] = value
        for name, as_of, mode in (
            ("before_unit_evidence", earlier, "source_research"),
            ("historical_still_unproven", later, "historical_pit"),
        ):
            value = reader.source_point(
                "600012.XSHG", "TOTPROFIT", "2010-03-31", statement="lrb", title="利润总额",
                source="sina", as_of=as_of, mode=mode)
            assert value["value"] is None
            queries[name] = value
        value = reader.source_point(
            "600000.XSHG", "MINYSHARINCO", "2026-06-30", statement="lrb", title="归属于少数股东的其他综合收益",
            source="sina", as_of=later, mode="source_research")
        assert value["value"] is None and "unit_unverified" in value["reasons"]
        queries["ps4_amount_not_misclassified_as_per_share"] = value
        with reader.sql() as db:
            assert db.execute("SELECT count(*) FROM financial.visibility").fetchone()[0] == 0
    result = {
        "unit_candidate_id": unit_manifest["candidate_id"],
        "unit_candidate_sha256": sha_file(units / "candidate.json"),
        "base_candidate_id": metadata["candidate_id"], "totals": dict(total),
        "resolved_groups": [{"statement": s, "unit": u, "rows": n}
                            for (s, u), n in sorted(groups.items())],
        "queries": queries, "seconds": round(time.monotonic() - start, 2),
        "temporary_directory_removed": not Path(temporary).exists(),
        "shared_publication_changed": False, "source_collectors_queried_or_changed": False,
    }
    immutable_json(output / "acceptance.json", result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True)
    p.add_argument("--units", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    result = validate(a.base, a.units, a.output)
    print(json.dumps({"accepted": result["unit_candidate_id"], "totals": result["totals"]}))
