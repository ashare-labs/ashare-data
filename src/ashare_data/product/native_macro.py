"""Explicit native macro queries pinned to one common immutable release.

Observation cutoffs use actual source reception, never invented announcement
availability. Values and units are returned exactly in their native contract.
"""

from contextlib import closing
from datetime import date, datetime
import json

from ashare_data.core.model import require
from ashare_data.storage.market_release import connection, sql_literal
from ashare_data.storage.native_macro_release import CONTRACT, verify_contract, MAX_ROWS


class NativeMacro:
    def __init__(self, release):
        require(
            CONTRACT in release.manifest,
            "MACRO_RELEASE",
            "Native macro sources are absent from this release",
        )
        self.release = release
        verify_contract(release.root, release.manifest)
        self.contract = release.manifest[CONTRACT]
        self.definitions = {
            int(row): value
            for chain in self.contract["chains"].values()
            for row, value in chain["definitions"].items()
        }
        self.files = [
            f
            for chain in self.contract["chains"].values()
            for bundle in chain["bundles"]
            for f in bundle["data_files"]
        ]

    def sql(self):
        c = connection(memory="256MB", threads=1)
        try:
            paths = ",".join(sql_literal(self.release.verify(f)) for f in self.files)
            c.execute("CREATE SCHEMA macro")
            c.execute(
                "CREATE VIEW macro.source_observations AS SELECT * FROM read_parquet(["
                + paths
                + "],union_by_name=true)"
            )
            c.execute("""CREATE VIEW macro.current_source_observations AS SELECT * FROM macro.source_observations
                QUALIFY row_number() OVER(PARTITION BY business_key ORDER BY sequence DESC)=1""")
            return c
        except BaseException:
            c.close()
            raise

    def records(self, row, start, end, *, received_by=None, history=False, limit=MAX_ROWS):
        require(
            row in self.definitions and 1 <= limit <= MAX_ROWS,
            "MACRO_QUERY",
            "Choose an explicit native menu row and bounded result",
        )
        require(
            date.fromisoformat(start) <= date.fromisoformat(end),
            "MACRO_QUERY",
            "Use an ordered period range",
        )
        parameters = [row, start, end]
        cutoff = ""
        if received_by is not None:
            moment = datetime.fromisoformat(received_by)
            require(
                moment.tzinfo is not None, "MACRO_TIME", "Received-by cutoff must include timezone"
            )
            cutoff = " AND observed_at<=?"
            parameters.append(moment)
        latest = (
            ""
            if history
            else " QUALIFY row_number() OVER(PARTITION BY business_key ORDER BY sequence DESC)=1"
        )
        sql = (
            "SELECT * FROM macro.source_observations WHERE menu_row=? AND period BETWEEN ? AND ?"
            + cutoff
            + latest
            + " ORDER BY period,sequence LIMIT ?"
        )
        with closing(self.sql()) as c:
            cursor = c.execute(sql, [*parameters, limit + 1])
            names = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
        require(len(rows) <= limit, "MACRO_QUERY_BOUND", "Result exceeds the explicit query limit")
        output = []
        for values in rows:
            record = dict(zip(names, values))
            record["period"] = record["period"].isoformat()
            record["observed_at"] = record["observed_at"].isoformat()
            record["data"] = json.loads(record.pop("payload_json"))
            record["units"] = json.loads(record.pop("units_json"))
            record.update(
                release_id=self.release.release_id,
                manifest_sha256=self.release.sha256,
                visibility="received",
                historical_pit_certified=False,
            )
            output.append(record)
        return output

    def snapshot(self, rows):
        output = {}
        for row in rows:
            for record in self.records(row, "1900-01-01", "2100-12-31"):
                output[record["business_key"]] = record
        return output

    def point(self, row, field, period, *, received_by=None, visibility="received"):
        require(
            visibility == "received",
            "MACRO_VISIBILITY",
            "Historical publication time is unavailable; use explicit received visibility",
        )
        require(
            field in self.definitions.get(row, {}).get("units", {}),
            "MACRO_FIELD",
            "Choose a reviewed native source field",
        )
        records = self.records(row, period, period, received_by=received_by)
        require(len(records) <= 1, "MACRO_QUERY", "Ambiguous native statistical period")
        if not records:
            return {
                "status": "missing",
                "menu_row": row,
                "field": field,
                "period": period,
                "release_id": self.release.release_id,
                "manifest_sha256": self.release.sha256,
            }
        record = records[0]
        return {k: v for k, v in record.items() if k not in {"data", "units"}} | {
            "status": "received_source_value",
            "field": field,
            "value": record["data"][field],
            "unit": record["units"][field],
        }
