"""Synthetic issuer and archive fixtures; no acceptance answers or market data."""
import json

import pytest

from ashare_data import DataError
import test_independent_queries as fixtures
from test_independent_queries import view, write
from ashare_data.independent_queries import ROLE

COMMIT = "a" * 32


@pytest.fixture
def delivery(tmp_path):
    return fixtures.delivery.__wrapped__(tmp_path)


@pytest.fixture
def archive(delivery):
    root, b = delivery
    log = write(root, "log.json", {"query_execution_status": "Success", "rows": [
        {"commit_hash": COMMIT, "date": "2026-01-02 15:12:34.000"}]})
    query = ("SELECT tradedate,symbol,open,high,low,close,volume,amount FROM ts_a_stock_eod_price "
             f"AS OF '{COMMIT}' WHERE symbol='000001.SZ' AND tradedate BETWEEN '2024-03-01' "
             "AND '2024-03-04' ORDER BY tradedate LIMIT 12;")
    rows = [{"tradedate": d, "symbol": "000001.SZ", "open": "12", "high": "14", "low": "10",
             "close": "13", "volume": "1.17", "amount": "1.479"}
            for d in ["2024-03-01", "2024-03-04"]]
    response = {"query_execution_status": "Success", "repository_owner": "fixture",
                "repository_name": "fixture", "sql_query": query, "rows": rows}
    b["sources"]["sina"].update(write(root, "archive.json", response), kind="dolt_sql_snapshot",
        repository_owner="fixture", repository_name="fixture", archive_commit=COMMIT,
        commit_log=log, unit_policy="tushare_daily_lots_100_and_amount_1000.v1")
    b["products"]["daily"].update(kind="archive_daily", expected_dates=["2024-03-01", "2024-03-04"])
    return delivery


def read(archive):
    return view(archive).read_archive_daily("daily", "000001.XSHE", source="sina", role=ROLE)


def test_archive_preserves_native_precision_and_distinct_clock_semantics(archive):
    r = read(archive)
    assert r["rows"][0]["native_fields"]["volume"] == "1.17"
    assert r["rows"][0]["fields"]["volume"]["value"] == "117.00"
    assert r["rows"][0]["fields"]["money"]["value"] == "1479.000"
    assert r["archive_version"]["commit"] == COMMIT
    for key in ("original_source_capture_time", "source_publication_time", "source_revision_id", "commit_time_timezone"):
        assert r["archive_version"][key] is None
    assert r["rows"][0]["fields"]["pre_close"]["state"] == "unsupported"
    assert not r["historical_pit"] and not r["matching_feed"]


@pytest.mark.parametrize("change", ["status", "unpinned", "wrong_commit", "security", "missing", "extra", "duplicate", "amount_null", "negative", "ohlc", "owner"])
def test_archive_rejects_incomplete_or_wrong_pinned_results(archive, change):
    root, b = archive
    response = json.loads((root / "archive.json").read_text())
    if change == "status":
        response["query_execution_status"] = "Error"
    elif change == "unpinned":
        response["sql_query"] = response["sql_query"].replace(f" AS OF '{COMMIT}'", "")
    elif change == "wrong_commit":
        response["sql_query"] = response["sql_query"].replace(COMMIT, "b" * 32)
    elif change == "security":
        response["rows"][0]["symbol"] = "000002.SZ"
    elif change == "missing":
        response["rows"].pop()
    elif change == "extra":
        response["rows"].append(dict(response["rows"][-1], tradedate="2024-03-05"))
    elif change == "duplicate":
        response["rows"][1] = response["rows"][0]
    elif change == "amount_null":
        response["rows"][0]["amount"] = None
    elif change == "negative":
        response["rows"][0]["volume"] = "-1"
    elif change == "ohlc":
        response["rows"][0]["high"] = "1"
    else:
        response["repository_owner"] = "wrong"
    b["sources"]["sina"].update(write(root, "archive.json", response))
    with pytest.raises(DataError):
        read(archive)


def test_commit_log_rechecked_after_open(archive):
    v = view(archive)
    (archive[0] / "log.json").write_text('{}')
    with pytest.raises(DataError, match="digest"):
        v.read_archive_daily("daily", "000001.XSHE", source="sina", role=ROLE)


def test_unknown_commit_cannot_borrow_another_archive_clock(archive):
    root, b = archive
    b["sources"]["sina"]["commit_log"] = write(root, "log.json", {
        "query_execution_status": "Success", "rows": [{"commit_hash": "b" * 32, "date": "2020"}]})
    with pytest.raises(DataError, match="absent"):
        read(archive)


@pytest.mark.parametrize("encoding", ["30", "31", "01", "", None])
def test_calendar_decodes_ascii_flags_without_guessing(archive, encoding):
    root, b = archive
    response = json.loads((root / "archive.json").read_text())
    response["sql_query"] = ("SELECT exchange,date,HEX(is_open) AS is_open_hex FROM ts_trade_day_calendar "
        f"AS OF '{COMMIT}' WHERE exchange='SSE' AND date BETWEEN '2024-03-01' "
        "AND '2024-03-04' ORDER BY date LIMIT 20;")
    response["rows"] = [{"exchange": "SSE", "date": d, "is_open_hex": encoding}
                        for d in ["2024-03-01", "2024-03-04"]]
    b["sources"]["sina"].update(write(root, "archive.json", response))
    b["products"]["daily"].update(kind="archive_calendar", exchange="SSE")
    v = view(archive)
    if encoding not in {"30", "31"}:
        with pytest.raises(DataError):
            v.read_archive_calendar("daily", "000001.XSHE", source="sina", role=ROLE)
    else:
        r = v.read_archive_calendar("daily", "000001.XSHE", source="sina", role=ROLE)
        assert r["rows"][0]["is_open"] is (encoding == "31")
        assert r["calendar_exchange"] == "SSE" and not r["official_calendar_certified"]


@pytest.fixture
def issuer(delivery):
    root, b = delivery
    doc = {**write(root, "issuer.pdf", b"synthetic document fixture"),
           "document_date": "2024-02-27", "captured_start": "2026-01-01T00:00:00Z",
           "captured_end": "2026-01-01T00:00:01Z", "source_publication_time": None}
    rows = [{"event_id": "date-resolved", "period": "2024-03", "resolution": "resolved_schedule",
             "ex_date": "2024-03-04", "facts": {"superseded_ex_date": "2024-03-01"},
             "citations": [{"document": "issuer", "pages": [1]}]},
            {"event_id": "partial", "period": "2024", "resolution": "partial_history", "ex_date": None,
             "facts": {"bonus_shares_per_10": "4"}, "citations": [{"document": "issuer", "pages": [2]}]}]
    payload = {"schema": "ad.issuer-action-evidence.v1", "security": "000001.XSHE",
               "documents": {"issuer": doc}, "rows": rows}
    b["sources"]["sina"].update(write(root, "review.json", payload), kind="reviewed_issuer_actions")
    b["products"]["daily"].update(kind="action_evidence", event_ids=["date-resolved", "partial"])
    return delivery


def test_issuer_resolution_preserves_unknown_dates_and_pins_originals(issuer):
    v = view(issuer)
    r = v.read_action_evidence("daily", "000001.XSHE", role=ROLE)
    assert r["rows"][1]["ex_date"] is None
    assert not r["historical_action_chain_complete"] and not r["adjustment_factor_certified"]
    (issuer[0] / "issuer.pdf").write_bytes(b"changed")
    with pytest.raises(DataError, match="digest"):
        v.read_action_evidence("daily", "000001.XSHE", role=ROLE)


@pytest.mark.parametrize("change", ["citation", "page", "publication_clock", "resolved_without_date", "duplicate", "wrong_security"])
def test_invalid_issuer_review_refused(issuer, change):
    root, b = issuer
    data = json.loads((root / "review.json").read_text())
    if change == "citation":
        data["rows"][0]["citations"][0]["document"] = "missing"
    elif change == "page":
        data["rows"][0]["citations"][0]["pages"] = [0]
    elif change == "publication_clock":
        data["documents"]["issuer"]["source_publication_time"] = "2024-02-27T00:00:00Z"
    elif change == "resolved_without_date":
        data["rows"][0]["ex_date"] = None
    elif change == "duplicate":
        data["rows"][1] = data["rows"][0]
    else:
        data["security"] = "000002.XSHE"
    b["sources"]["sina"].update(write(root, "review.json", data))
    with pytest.raises(DataError):
        view(issuer).read_action_evidence("daily", "000001.XSHE", role=ROLE)


def test_new_reads_require_explicit_retrospective_role(archive, issuer):
    with pytest.raises(DataError, match="role"):
        view(archive).read_archive_daily("daily", "000001.XSHE", source="sina", role="matching")
    with pytest.raises(DataError, match="role"):
        view(issuer).read_action_evidence("daily", "000001.XSHE", role="strategy")
