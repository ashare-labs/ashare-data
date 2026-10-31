import pytest
from ashare_data.core.model import DataError
from ashare_data.storage.reference_coverage import classify
from ashare_data.financials.tushare_reference import active_codes


def test_code_interval_is_not_company_ipo_and_end_is_exclusive():
    universe = {
        "instruments": [
            dict(
                symbol="sz001914",
                ipo="1994-01-01",
                out=None,
                code_valid_from="2019-12-16",
                code_valid_to="2026-10-19",
            )
        ]
    }
    assert not active_codes(universe, "2019-12-13")
    assert active_codes(universe, "2019-12-16") == {"001914.SZ"}
    assert not active_codes(universe, "2026-10-19")


def missing(code="600001.XSHG", outside=False):
    return dict(
        security=code, instrument_id=12, trade_date="2026-10-19", outside_code_interval=outside
    )


def test_dispositions_require_evidence_and_do_not_infer_missing_as_suspension():
    m = missing()
    base = dict(requested_security=m["security"], trade_date=m["trade_date"], instrument_id=12)
    assert classify([m], [], [])[0]["disposition"] == "unresolved_source_gap"
    assert (
        classify([missing(outside=True)], [], [])[0]["disposition"] == "outside_identifier_period"
    )
    status = dict(base, tradestatus="0", row_hash="frozen-row", source="baostock")
    row = classify([m], [], [status])[0]
    assert row["disposition"] == "suspended_not_executable" and not row["no_price_limit_inferred"]
    limit = dict(
        base,
        security="600002.XSHG",
        up_limit="11",
        down_limit="9",
        unit="CNY/share",
        observation_id="exact-source",
    )
    assert classify([m], [limit], [status])[0]["disposition"] == "covered_identity_alias"
    limit["instrument_id"] = 13
    with pytest.raises(DataError, match="identity"):
        classify([m], [limit], [])
    assert (
        classify(
            [m], [], [], listing_ends={m["security"]: dict(valid_to="2026-10-19", sha256="proof")}
        )[0]["disposition"]
        == "outside_listing_period"
    )
    with pytest.raises(DataError, match="Duplicate"):
        classify([m, m], [], [])


def test_zero_alias_limits_are_not_usable_coverage():
    m = missing()
    limit = dict(
        requested_security=m["security"],
        trade_date=m["trade_date"],
        instrument_id=12,
        security="600002.XSHG",
        up_limit="0",
        down_limit="0",
        unit="CNY/share",
        observation_id="zero",
    )
    assert classify([m], [limit], [])[0]["disposition"] == "unresolved_source_gap"


def test_coverage_release_is_reproducible_and_cannot_be_dropped(tmp_path):
    import json
    from copy import deepcopy
    import pandas as pd
    from ashare_data.storage.market_release import MarketRelease, ReleaseWriter, connection
    from ashare_data.storage.reference_coverage import append_to_manifest, CONTRACT
    from test_unit_publication import published_base

    parent, _, _ = published_base(tmp_path)
    data = classify([missing()], [], [])
    path = tmp_path / "coverage.parquet"
    with connection() as c:
        c.register("data", pd.DataFrame(data))
        c.execute("COPY data TO ? (FORMAT PARQUET)", [str(path)])
    report = tmp_path / "summary.json"
    report.write_text(
        json.dumps(
            dict(
                parent_manifest_sha256=parent.sha256,
                input_candidate_days=1,
                dispositions={"unresolved_source_gap": 1},
            )
        )
    )
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        draft = append_to_manifest(
            writer,
            parent,
            parent.manifest,
            rows_path=path,
            summary_path=report,
            proof_paths=[report],
        )
        writer.publish(draft, expected_parent=parent.release_id, verify_unchanged=False)
    current = MarketRelease(parent.root)
    path.rename(path.with_suffix(".retained"))
    result = current.financial().reference_coverage(security="600001.XSHG", day="2026-10-19")
    assert len(result["rows"]) == 1
    assert result["rows"][0]["disposition"] == "unresolved_source_gap"
    assert parent.financial().reference_coverage()["coverage"]["status"] == "unreviewed"
    with ReleaseWriter(parent.root, free_reserve_bytes=0) as writer:
        draft = deepcopy(current.manifest)
        del draft[CONTRACT]
        with pytest.raises(DataError, match="Preserve prior coverage"):
            writer.publish(draft, expected_parent=current.release_id, verify_unchanged=False)
    assert MarketRelease(parent.root).sha256 == current.sha256
