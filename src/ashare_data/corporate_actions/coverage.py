"""Semantic corporate table coverage; provider names only select legacy adapters."""

from ashare_data.core.model import DataError, day, timestamp

ROLES = frozenset({"distributions", "rights_issues", "capital_changes"})
ACTION_ROLES = ROLES - {"capital_changes"}
LEGACY_ROLES = {
    "sina_actions": ACTION_ROLES,
    "sina_structure": frozenset({"capital_changes"}),
}


def declared_tables(document):
    """A source table locator and explicit parse result are required for every role."""
    if not isinstance(document, dict) or document.get("schema") != "ad.corporate-coverage.v1":
        return None
    try:
        if day(document["start"]) > day(document["end"]):
            return None
        tables = document["tables"]
        if not isinstance(tables, dict) or not tables or not set(tables) <= ROLES:
            return None
        for row in tables.values():
            if not isinstance(row, dict):
                return None
            locator, count = row.get("source_table"), row.get("row_count")
            if not isinstance(locator, str) or not locator.strip() or len(locator) > 512:
                return None
            if type(count) is not int or not 0 <= count <= 100000:
                return None
            if not ((row.get("state") == "business_rows_observed" and count > 0)
                    or (row.get("state") == "provider_no_rows_reported" and count == 0
                        and row.get("explicit_empty_marker") is True)):
                return None
        return tables
    except (KeyError, TypeError, ValueError, DataError):
        return None


def covered_roles(source, start, end, *, empty_only=False):
    if source["status"] != "parsed_provider_tables" or source.get("unparsed_records"):
        return set()
    if "corporate_coverage" not in source:
        return set() if empty_only else set(LEGACY_ROLES.get(source["kind"], ()))
    document = source["corporate_coverage"]
    tables = declared_tables(document)
    if tables is None or not document["start"] <= start <= end <= document["end"]:
        return set()
    try:
        if str(timestamp(source["coverage_observed_at"]).date()) <= end:
            return set()
    except (KeyError, TypeError, ValueError, DataError):
        return set()
    return {role for role, row in tables.items()
            if not empty_only or row["state"] == "provider_no_rows_reported"}


def is_corporate_source(source):
    return "corporate_coverage" in source or source["kind"] in {
        *LEGACY_ROLES, "sina_action_detail", "eastmoney_dividends",
    }
