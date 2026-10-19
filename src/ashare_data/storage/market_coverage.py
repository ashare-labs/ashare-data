"""Refresh changed security-day observations without inventing expected coverage."""

from pathlib import Path
import shutil
import sqlite3

from ashare_data.core.model import require
from ashare_data.storage.market_release import canonical, sql_literal

VERSION = "frozen-expectations-current-observations.v1"


def refresh(writer, c, release, changes, evidence, metadata):
    scopes = {(item["key"][1], item["key"][2]) for item in changes}
    previous = [item for item in evidence if item.get("kind") == "bar_day_checks"]
    result = [
        item
        for item in evidence
        if item.get("kind") != "bar_day_checks"
        or (item.get("series_id"), item.get("instrument_id")) not in scopes
    ]
    for number, change in enumerate(changes):
        _, series, instrument = change["key"]
        checks = [
            item
            for item in previous
            if (item.get("series_id"), item.get("instrument_id")) == (series, instrument)
        ]
        if checks:
            paths = ",".join(sql_literal(release.verify(item)) for item in checks)
            prior = (
                "select trade_date,expected_state,expected_count from read_parquet(["
                + paths
                + "],union_by_name=true)"
            )
        elif change.get("old"):
            prior = (
                "select distinct trade_date,'unknown' expected_state,NULL::BIGINT expected_count from read_parquet("
                + sql_literal(release.verify(change["old"]))
                + ")"
            )
        else:
            prior = "select NULL::DATE trade_date,'unknown' expected_state,NULL::BIGINT expected_count where false"
        c.execute("create or replace temp table prior_expectations as " + prior)
        require(
            c.execute(
                "select count(*)-count(distinct trade_date) from prior_expectations"
            ).fetchone()[0]
            == 0,
            "MARKET_COVERAGE",
            "Prior expectations have duplicate security days",
        )
        if change.get("new"):
            observed = (
                "select trade_date,count(*) observed_count from read_parquet("
                + sql_literal(writer.root / change["new"]["path"])
                + ") group by trade_date"
            )
        else:
            observed = "select NULL::DATE trade_date,0::BIGINT observed_count where false"
        query = f"""select {series}::INTEGER series_id,{instrument}::INTEGER instrument_id,
          coalesce(p.trade_date,o.trade_date) trade_date,
          coalesce(p.expected_state,'unknown') expected_state,p.expected_count::BIGINT expected_count,
          coalesce(o.observed_count,0)::BIGINT observed_count,
          case when p.expected_count is null then NULL else greatest(p.expected_count-coalesce(o.observed_count,0),0) end::BIGINT missing_count,
          case when p.expected_state='expected' and coalesce(o.observed_count,0)=0 then 1 else 0 end::INTEGER missing_day,
          'Prior frozen expectations retained; newly observed dates have unknown expectations' coverage_evidence
          from prior_expectations p full outer join ({observed}) o using(trade_date) order by trade_date"""
        item = writer.parquet(c, query, [], "replacement-coverage-" + str(number))
        item.update(
            kind="bar_day_checks", check_version=VERSION, series_id=series, instrument_id=instrument
        )
        result.append(item)
    checks = [item for item in result if item.get("kind") == "bar_day_checks"]
    counts = {}
    if checks:
        paths = ",".join(sql_literal(writer.root / item["path"]) for item in checks)
        rows = c.execute(
            """select series_id,expected_state,count(*),
          sum(case when observed_count>0 then 1 else 0 end),
          sum(case when expected_state='expected' and observed_count=0 then 1 else 0 end),
          sum(observed_count) from read_parquet(["""
            + paths
            + "],union_by_name=true) group by 1,2"
        ).fetchall()
        for series, state, days, present, missing, observed in rows:
            counts.setdefault(str(series), {}).update(
                {
                    state + "_days": days,
                    state + "_observed_days": present,
                    state + "_missing_days": missing,
                    state + "_observed_rows": observed,
                }
            )
    coverage = {
        "check_version": VERSION,
        "counts_by_series": counts,
        "expected_minute_count": None,
        "expectation_scope": "Prior release-frozen expectations retained. New dates and scopes remain unknown; a market-calendar expansion is not inferred from new bars.",
        "changed_scopes": [{"series_id": s, "instrument_id": i} for s, i in sorted(scopes)],
    }
    # Keep the proposed metadata as input evidence; derive a separate immutable output.
    result.append({**metadata, "kind": "proposed_metadata"})
    target = writer.root / "work/replacement-reference.sqlite3"
    shutil.copyfile(writer.root / metadata["path"], target)
    with sqlite3.connect(target) as db:
        db.execute(
            "create table if not exists release_contracts(name TEXT PRIMARY KEY,body TEXT NOT NULL)"
        )
        db.execute(
            "insert or replace into release_contracts values ('coverage',?)",
            (canonical(coverage).decode(),),
        )
        db.commit()
    metadata = writer.object(target, "sqlite3")
    source = writer.root / "work/coverage-producer.py"
    source.write_bytes(Path(__file__).read_bytes())
    result.append(
        {**writer.object(source, "py"), "kind": "coverage_producer", "check_version": VERSION}
    )
    return metadata, list({item["path"]: item for item in result}.values()), coverage
