"""Validate small, closed append-only financial increments against a frozen release."""

from ashare_data.core.model import require
from ashare_data.financials import tables as t
from ashare_data.storage.market_release import sql_literal


def validate_append(c, registry, parent, files, verify):
    from ashare_data.storage.financial_release import table_views, validate_tables

    require(
        sum(f["rows"] for f in files) <= 10000,
        "FIN_INCREMENT_BOUND",
        "Closed increment validation is limited to 10000 physical rows",
    )
    prior = parent.manifest["financial_table_validation"]
    require(
        prior["table_set_sha256"]
        == parent.manifest["financial_reference_tables"]["table_set_sha256"]
        and prior["duplicate_conflicts"] == prior["broken_relationships"] == 0,
        "FIN_INCREMENT_PARENT",
        "Require a reconciled immutable parent",
    )
    table_views(c, files, verify)
    new = validate_tables(c, registry)  # Every new endpoint must be in this closed candidate.
    require(
        c.execute("SELECT count(*) FROM financial.links WHERE relation='replaces'").fetchone()[0]
        == 0,
        "FIN_INCREMENT_REVISION",
        "Replacement graphs require full composition validation",
    )
    counts, overlap, added = {}, {}, {}
    for table, key in t.KEYS.items():
        selected = [f for f in parent.manifest["evidence"] if f.get("table") == table]
        match = None
        if selected and new["table_rows"][table]:
            paths = ",".join(sql_literal(parent.verify(f)) for f in selected)
            # Push the bounded set of new keys before full-row DISTINCT, not afterwards.
            match = (
                "SELECT DISTINCT p.* FROM read_parquet(["
                + paths
                + "]) p SEMI JOIN "
                + table
                + " n USING ("
                + key
                + ")"
            )
            bad = c.execute(
                "SELECT count(*) FROM ((" + match + ") EXCEPT SELECT * FROM " + table + ")"
            ).fetchone()[0]
            require(bad == 0, "FIN_ID_CONFLICT", "Existing identity has changed values: " + table)
            overlap[table] = c.execute("SELECT count(*) FROM (" + match + ")").fetchone()[0]
        else:
            overlap[table] = 0
        added[table] = new["table_rows"][table] - overlap[table]
        counts[table] = prior["table_rows"][table] + added[table]
        if table == "meta.financial_coverage":
            if match:
                c.execute("CREATE TEMP VIEW increment_coverage_overlap AS " + match)
                sub = c.execute(
                    "SELECT coalesce(sum(input_rows),0),coalesce(sum(mapped_rows),0),coalesce(sum(quarantined_rows),0) FROM increment_coverage_overlap"
                ).fetchone()
            else:
                sub = (0, 0, 0)
    # Context identity is a composite constraint distinct from observation identity.
    observed = [
        f for f in parent.manifest["evidence"] if f.get("table") == "financial.observations"
    ]
    if observed and new["table_rows"]["financial.observations"]:
        paths = ",".join(sql_literal(parent.verify(f)) for f in observed)
        columns = [r[0] for r in c.execute("DESCRIBE financial.contexts").fetchall()]
        match = (
            "SELECT DISTINCT "
            + ",".join("p." + x for x in columns)
            + " FROM read_parquet(["
            + paths
            + "]) p SEMI JOIN financial.contexts n "
            + "USING(report_version_id,extraction_version_id,context_id)"
        )
        bad = c.execute(
            "SELECT count(*) FROM ((" + match + ") EXCEPT SELECT * FROM financial.contexts)"
        ).fetchone()[0]
        require(bad == 0, "FIN_CONTEXT", "Existing context identity has changed attributes")
    names = ["input_rows", "mapped_rows", "quarantined_rows"]
    result = dict(
        table_rows=counts,
        **{k: prior[k] + new[k] - v for k, v in zip(names, sub)},
        duplicate_conflicts=0,
        broken_relationships=0,
        complete_history_asserted=False,
        validation_scope="closed_bounded_append_against_verified_parent",
        parent_manifest_sha256=parent.sha256,
        appended_rows=added,
        identical_overlap_rows=overlap,
    )
    require(
        result["input_rows"] == result["mapped_rows"] + result["quarantined_rows"],
        "FIN_INPUT_ACCOUNTING",
        "Combined input dispositions do not balance",
    )
    return result
