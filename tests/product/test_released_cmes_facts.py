"""Real temporary SQLite/Parquet releases; no actual CMES source or user state.

The fixture explicitly replaces only the reviewed package row-count constant
with its tiny synthetic row count. Production still requires all 683228 rows.
"""

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from ashare_data.core.model import DataError, canonical
from ashare_data.product import released_cmes_facts as facts
from ashare_data.storage.market_release import MarketRelease, SCHEMA, connection, sql_literal

SID, DAY = "600000.XSHG", "2026-10-20"
POSITIVE = "600053.XSHG"
OBSERVED = "2026-10-23T04:35:10+00:00"


@pytest.fixture
def released(tmp_path, monkeypatch):
    monkeypatch.setattr(facts, "_ST_ROWS", 2)
    for name in ("ASHARE_MARKET_VERIFY_CACHE", "ASHARE_MARKET_READ_REPLICAS"):
        monkeypatch.delenv(name, raising=False)

    def build(*, edit=None, company_date="1999-11-10", cmes=True, physical_date=False,
              selected="record-a", company_record="record-a", no_company=False,
              physical_observed=False):
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        root.mkdir()
        dbpath = root / "meta.sqlite3"
        db = sqlite3.connect(dbpath)
        db.executescript("""
          CREATE TABLE release_instruments(instrument_id INTEGER,security TEXT,asset_class TEXT);
          CREATE TABLE release_identifiers(security TEXT,instrument_id INTEGER,namespace TEXT);
          INSERT INTO release_instruments VALUES(1,'600000.XSHG','equity'),(2,'600053.XSHG','equity');
          INSERT INTO release_identifiers VALUES('600000.XSHG',1,'venue_equity_code'),('600053.XSHG',2,'venue_equity_code');
          CREATE TABLE source_assets(asset_id TEXT PRIMARY KEY,body TEXT);
          CREATE TABLE cmes_imports(import_id TEXT PRIMARY KEY,body TEXT);
          CREATE TABLE cmes_ingested(asset_id TEXT,interpretation_sha256 TEXT,batch_id INTEGER);
          CREATE TABLE batch_inputs(batch_id INTEGER,asset_id TEXT);
          CREATE TABLE cmes_st(security TEXT,trade_date TEXT,name TEXT,status_type TEXT,status_name TEXT,
             source_asset_id TEXT,source_member TEXT,source_row INTEGER,PRIMARY KEY(security,trade_date,status_type));
          CREATE TABLE cmes_business_selection(table_name TEXT,business_key TEXT,record_id TEXT,
             value_sha256 TEXT,policy TEXT,conflict_count INTEGER,PRIMARY KEY(table_name,business_key));
          CREATE TABLE cmes_business_conflicts(table_name TEXT,business_key TEXT,record_id TEXT);
          CREATE TABLE raw_bars(security TEXT,trade_date TEXT,is_st TEXT,tradestatus TEXT);
        """)
        db.execute("INSERT INTO raw_bars VALUES(?,?,NULL,NULL)", (SID, DAY))
        source = {"asset_id": facts._ST_ASSET, "source": "cmes", "kind": "st",
                  "sha256": facts._ST_SHA, "bytes": facts._ST_BYTES, "observed_at": OBSERVED}
        db.execute("INSERT INTO source_assets VALUES(?,?)", (facts._ST_ASSET, json.dumps(source)))
        proof = {"dispositions": {facts._ST_ASSET: {"input_rows": 2, "adopted": 2, "equal_duplicate": 0}}}
        db.execute("INSERT INTO cmes_imports VALUES(?,?)", (facts._ST_IMPORT, json.dumps(proof)))
        db.execute("INSERT INTO cmes_ingested VALUES(?,?,2)", (facts._ST_ASSET, "synthetic-interpretation"))
        db.execute("INSERT INTO batch_inputs VALUES(2,?)", (facts._ST_ASSET,))
        for security, row in ((POSITIVE, 2), ("000001.XSHE", 3)):
            db.execute("INSERT INTO cmes_st VALUES(?,?,?,'ST','风险警示板',?,'ST股票列表.csv',?)",
                       (security, DAY, "ST fixture", facts._ST_ASSET, row))
        db.execute("INSERT INTO cmes_business_selection VALUES(?,?,?,?,?,0)",
                   (facts._COMPANY, canonical({"security": SID}).decode(), selected,
                    "business-value:synthetic", "equal-values-collapse; conflict-retain-prior-accepted"))
        if edit:
            edit(db)
        db.commit()
        db.close()
        raw = dbpath.read_bytes()
        metadata = {"path": dbpath.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        evidence = []
        if not no_company:
            path = root / "company.parquet"
            c = connection()
            try:
                cast = "DATE" if physical_date else "VARCHAR"
                observed_cast = "TIMESTAMPTZ" if physical_observed else "VARCHAR"
                c.execute(f'COPY (SELECT ?::VARCHAR AS record_id,?::VARCHAR AS "security",?::{cast} AS listed_date,'
                          f'?::{observed_cast} AS observed_at) TO ' + sql_literal(path) + ' (FORMAT PARQUET)',
                          [company_record, SID, company_date, OBSERVED])
            finally:
                c.close()
            body = path.read_bytes()
            evidence.append({"path": path.name, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                             "kind": "cmes_table", "table": facts._COMPANY})
        manifest = {"schema": SCHEMA, "release_id": 1, "parent_release_id": None,
                    "parent_manifest_sha256": None, "metadata": metadata, "files": [], "evidence": evidence}
        if cmes:
            manifest["cmes"] = {"classification": "synthetic-test-only"}
        payload = canonical(manifest)
        (root / "releases").mkdir()
        (root / "releases/00000001.json").write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        (root / "CURRENT.json").write_bytes(canonical({"schema": SCHEMA, "release_id": 1,
            "manifest": "releases/00000001.json", "sha256": sha}))
        return MarketRelease(root, 1, expected_manifest_sha256=sha)
    return build


def test_positive_negative_are_separate_derived_facts_and_raw_nulls_stay_null(released):
    release = released()
    reader = facts.ReleasedCmesFacts(release)
    before = deepcopy(release.manifest)
    for sid, expected in ((SID, False), (POSITIVE, True)):
        result = reader.st_status(sid, DAY)
        assert result["value"] is expected and result["status"] == "derived"
        assert result["rule"] == facts.ST_RULE and result["release"] == release.receipt()
        assert result["snapshot"]["member_count"] == 2
        assert result["proof"]["consumer_statement"]["authority"] == "reviewed_consumer_source_scope_policy"
        assert result["historical_available_at"] is None and result["historical_pit"] is False
        assert not {"listed", "active", "suspended", "tradestatus"} & result.keys()
    with release.metadata() as db:
        assert tuple(db.execute("SELECT is_st,tradestatus FROM raw_bars").fetchone()) == (None, None)
    assert release.manifest == before


@pytest.mark.parametrize("day,reason", [("2000-12-29", "outside_reviewed_source_dates"),
                                      ("2026-10-23", "outside_reviewed_source_dates"),
                                      ("2026-10-21", "date_snapshot_missing")])
def test_absence_outside_actual_snapshot_stays_unknown(released, day, reason):
    result = facts.ReleasedCmesFacts(released()).st_status(SID, day)
    assert result["value"] is None and result["status"] == "unknown" and result["reason"] == reason


@pytest.mark.parametrize("security", ["900901.XSHG", "000300.XSHG", "200001.XSHE", "SH.600000", "HK.00700"])
def test_unreviewed_code_domains_do_not_get_negative_membership(released, security):
    value = facts.ReleasedCmesFacts(released()).st_status(security, DAY)
    assert value["status"] == "unknown" and value["reason"] == "unsupported_security_domain"


def test_missing_or_historical_identity_is_unknown(released):
    missing = facts.ReleasedCmesFacts(released()).st_status("601000.XSHG", DAY)
    assert missing["reason"] == "frozen_security_identity_missing"
    release = released(edit=lambda db: db.execute("UPDATE release_instruments SET security='601000.XSHG' WHERE instrument_id=1"))
    assert facts.ReleasedCmesFacts(release).st_status(SID, DAY)["reason"] == "historical_code_identity_unverified"


def test_missing_complete_source_proof_is_unknown(released):
    release = released(edit=lambda db: db.execute("DELETE FROM source_assets"))
    value = facts.ReleasedCmesFacts(release).st_status(SID, DAY)
    assert value["value"] is None and value["reason"] == "complete_source_proof_missing"


@pytest.mark.parametrize("change", [
    "DELETE FROM cmes_st WHERE security='000001.XSHE'",
    "UPDATE cmes_ingested SET batch_id=99",
    "DELETE FROM batch_inputs",
    "UPDATE cmes_st SET status_type='normal' WHERE security='600053.XSHG'",
    "UPDATE cmes_st SET source_row=0 WHERE security='600053.XSHG'",
    "UPDATE cmes_st SET source_row=999999999 WHERE security='600053.XSHG'",
])
def test_incomplete_or_invalid_reviewed_source_is_not_silently_accepted(released, change):
    with pytest.raises(DataError) as error:
        facts.ReleasedCmesFacts(released(edit=lambda db: db.execute(change))).st_status(SID, DAY)
    assert error.value.code in {"CMES_FACT_INTEGRITY", "CMES_FACT_CONFLICT"}


def test_other_snapshot_asset_makes_absence_unknown(released):
    def edit(db):
        db.execute("INSERT INTO cmes_st VALUES('601000.XSHG',?,'ST other','ST','风险警示板','other','other.csv',2)", (DAY,))
    value = facts.ReleasedCmesFacts(released(edit=edit)).st_status(SID, DAY)
    assert value["value"] is None and value["reason"] == "unsupported_snapshot_assets"


@pytest.mark.parametrize("physical_date", [False, True])
@pytest.mark.parametrize("physical_observed", [False, True])
def test_listing_is_explicit_later_observed_event_not_active_interval(released, physical_date, physical_observed):
    release = released(physical_date=physical_date, physical_observed=physical_observed)
    value = facts.ReleasedCmesFacts(release).listing_date(SID)
    assert value["value"] == "1999-11-10" and value["status"] == "source_observed"
    assert value["source_refs"][0]["record_id"] == "record-a"
    assert value["source_refs"][0]["object"]["table"] == facts._COMPANY
    assert value["observed_at"] == OBSERVED and value["historical_pit"] is False
    assert not {"listed", "active", "suspended", "tradestatus"} & value.keys()


@pytest.mark.parametrize("date_value", [None, ""])
def test_missing_listing_date_remains_unknown(released, date_value):
    value = facts.ReleasedCmesFacts(released(company_date=date_value)).listing_date(SID)
    assert value["value"] is None and value["reason"] == "source_reported_listing_date_missing"


@pytest.mark.parametrize("mode", ["counter", "row"])
def test_listing_conflicts_are_explicit(released, mode):
    def edit(db):
        if mode == "counter":
            db.execute("UPDATE cmes_business_selection SET conflict_count=1")
        else:
            db.execute("INSERT INTO cmes_business_conflicts VALUES(?,?,?)", (facts._COMPANY, canonical({"security": SID}).decode(), "other"))
    with pytest.raises(DataError) as error:
        facts.ReleasedCmesFacts(released(edit=edit)).listing_date(SID)
    assert error.value.code == "CMES_FACT_CONFLICT"


def test_selected_record_must_exist_in_verified_typed_object(released):
    with pytest.raises(DataError) as error:
        facts.ReleasedCmesFacts(released(company_record="unselected")).listing_date(SID)
    assert error.value.code == "CMES_FACT_INTEGRITY"


@pytest.mark.parametrize("mode,reason", [("selection", "company_selection_missing"),
                                       ("objects", "company_table_missing"),
                                       ("policy", "company_selection_policy_unreviewed")])
def test_unavailable_company_evidence_does_not_supply_a_date(released, mode, reason):
    def edit(db):
        if mode == "selection":
            db.execute("DELETE FROM cmes_business_selection")
        if mode == "policy":
            db.execute("UPDATE cmes_business_selection SET policy='unreviewed-latest-wins'")
    result = facts.ReleasedCmesFacts(released(edit=edit, no_company=mode == "objects")).listing_date(SID)
    assert result["value"] is None and result["reason"] == reason


def test_invalid_company_date_and_provenance_are_not_accepted(released):
    with pytest.raises(DataError) as error:
        facts.ReleasedCmesFacts(released(company_date="1999-11-40")).listing_date(SID)
    assert error.value.code == "CMES_FACT_DATE"
    with pytest.raises(DataError) as error:
        facts.ReleasedCmesFacts(released(selected=None)).listing_date(SID)
    assert error.value.code == "CMES_FACT_PROVENANCE"


@pytest.mark.parametrize("mode", ["disposition", "source", "json"])
def test_changed_source_proof_is_integrity_error(released, mode):
    def edit(db):
        if mode == "disposition":
            row = json.loads(db.execute("SELECT body FROM cmes_imports").fetchone()[0])
            row["dispositions"][facts._ST_ASSET]["adopted"] = 1
            db.execute("UPDATE cmes_imports SET body=?", (json.dumps(row),))
        elif mode == "source":
            row = json.loads(db.execute("SELECT body FROM source_assets").fetchone()[0])
            row["sha256"] = "a" * 64
            db.execute("UPDATE source_assets SET body=?", (json.dumps(row),))
        else:
            db.execute("UPDATE cmes_imports SET body='not json'")
    with pytest.raises(DataError) as error:
        facts.ReleasedCmesFacts(released(edit=edit)).st_status(SID, DAY)
    assert error.value.code == "CMES_FACT_INTEGRITY"


def test_source_and_metadata_tampering_are_still_rejected(released):
    release = released()
    reader = facts.ReleasedCmesFacts(release)
    reader.st_status(SID, DAY)
    release.metadata_path.write_bytes(release.metadata_path.read_bytes() + b"changed")
    with pytest.raises(DataError):
        reader.st_status(SID, DAY)
    release = released()
    (release.root / release.manifest["evidence"][0]["path"]).write_bytes(b"corrupt")
    with pytest.raises(DataError):
        facts.ReleasedCmesFacts(release).listing_date(SID)


def test_non_cmes_or_changed_pin_refused(released):
    with pytest.raises(DataError) as error:
        facts.ReleasedCmesFacts(released(cmes=False))
    assert error.value.code == "CMES_FACT_RELEASE"
    release = released()
    reader = facts.ReleasedCmesFacts(release)
    release.sha256 = "a" * 64
    with pytest.raises(DataError) as error:
        reader.st_status(SID, DAY)
    assert error.value.code == "CMES_FACT_RELEASE"


@pytest.mark.parametrize("day", ["20261020", "2026-1-2", "bad", None])
def test_date_must_be_exact(released, day):
    with pytest.raises(DataError) as error:
        facts.ReleasedCmesFacts(released()).st_status(SID, day)
    assert error.value.code == "CMES_FACT_DATE"


def test_returned_facts_cannot_mutate_next_proof_or_release(released):
    reader = facts.ReleasedCmesFacts(released())
    result = reader.st_status(SID, DAY)
    result["proof"]["source_asset_id"] = "tampered"
    result["release"]["release_id"] = 99
    again = reader.st_status(SID, DAY)
    assert again["proof"]["source_asset_id"] == facts._ST_ASSET
    assert again["release"]["release_id"] == 1


def test_trading_status_derives_active_only_from_positive_activity_and_never_guesses_suspension(released, monkeypatch):
    release = released()
    reader = facts.ReleasedCmesFacts(release)
    assert reader.trading_status(SID, DAY)["status"] == "unknown"
    assert reader.trading_status(SID, DAY)["reason"] == "daily_observation_missing"

    def mock_bars(volume, turnover):
        monkeypatch.setattr(
            release,
            "read",
            lambda table, security, start, end, series_id=1: (
                [
                    {
                        "open_price": "10.0",
                        "high_price": "10.5",
                        "low_price": "9.9",
                        "close_price": "10.2",
                        "volume_shares": volume,
                        "turnover_cny": turnover,
                        "observed_at": OBSERVED,
                        "row_hash": "hash-1",
                        "response_id": "resp-1",
                    }
                ]
                if series_id == 1
                else []
            ),
        )

    mock_bars("0", "0")
    zero_bar = reader.trading_status(SID, DAY)
    assert zero_bar["status"] == "unknown" and zero_bar["value"] is None
    assert zero_bar["reason"] == "positive_trading_activity_not_observed"

    mock_bars("12500", "127500.0")
    active = reader.trading_status(SID, DAY)
    assert active["status"] == "derived" and active["value"] == "1"
    assert active["rule"] == facts.TRADING_RULE
    assert active["observed_at"] == OBSERVED

