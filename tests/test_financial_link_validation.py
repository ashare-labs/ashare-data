"""Typed relationship checks preserve both endpoint directions and reject nulls."""
import pytest
from ashare_data.core.model import DataError
from ashare_data.financials.tables import ENDPOINTS
from ashare_data.storage.financial_release import validate_link_endpoints
from ashare_data.storage.market_release import connection


@pytest.mark.parametrize("invalid", [None, "unknown_type", "null_type", "null_id", "wrong_type_id"])
def test_all_endpoint_types_and_both_directions(invalid):
    with connection() as c:
        for schema in {table.split(".")[0] for table, _ in ENDPOINTS.values()}:
            c.execute("CREATE SCHEMA " + schema)
        for kind, (table, key) in ENDPOINTS.items():
            c.execute("CREATE TABLE " + table + "(" + key + " VARCHAR)")
            c.execute("INSERT INTO " + table + " VALUES (?)", [kind + "-id"])
        c.execute("CREATE TABLE financial.links(from_type VARCHAR,from_id VARCHAR,to_type VARCHAR,to_id VARCHAR)")
        rows = [(kind, kind + "-id", "capture", "capture-id") for kind in ENDPOINTS]
        rows += [("capture", "capture-id", kind, kind + "-id") for kind in ENDPOINTS]
        c.executemany("INSERT INTO financial.links VALUES (?,?,?,?)", rows * 2)
        if invalid == "unknown_type":
            c.execute("INSERT INTO financial.links VALUES ('unknown','report-id','report','report-id')")
        elif invalid == "null_type":
            c.execute("INSERT INTO financial.links VALUES ('report','report-id',NULL,'capture-id')")
        elif invalid == "null_id":
            c.execute("INSERT INTO financial.links VALUES ('report',NULL,'capture','capture-id')")
        elif invalid == "wrong_type_id":
            c.execute("INSERT INTO financial.links VALUES ('report','report-id','capture','report-id')")
        if invalid:
            with pytest.raises(DataError, match="endpoint"):
                validate_link_endpoints(c)
        else:
            validate_link_endpoints(c)
        assert not c.execute("SELECT table_name FROM duckdb_tables() WHERE table_name='financial_required_endpoints'").fetchall()
