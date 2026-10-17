"""Synthetic data protocol fixtures; never real market or reference trade answers."""

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from ashare_data import DataError, JQObservationInput


def source(tmp_path, specs):
    events = [
        dict(
            kind="identity",
            settings=dict(
                symbol="000001.XSHE",
                start_date="2024-03-04",
                end_date="2024-03-05",
                frequency="minute",
                use_real_price=True,
            ),
        ),
        dict(kind="snapshot", orders=[{"price": "FORBIDDEN_ACCOUNT_RESULT"}]),
    ]
    events.extend(specs)
    for i, event in enumerate(events, 1):
        event.update(case_id="fixture", seq=i, run_tag="synthetic-query")
        event.setdefault("time", "2024-03-04 15:30:00")
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    script = tmp_path / "jq.py"
    script.write_text("# synthetic fixture only\n")
    return JQObservationInput(
        "fixture",
        "000001.XSHE",
        "2024-03-04",
        str(path),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        str(script),
        hashlib.sha256(script.read_bytes()).hexdigest(),
        session_end_date="2024-03-05",
    )


def pair(*, label="probe", value=12.34, end="2024-03-01", field="close", rows=None):
    request = dict(
        kind="data_request",
        api="get_price",
        label=label,
        symbol="000001.XSHE",
        parameters=dict(
            count=1, end_date=end, fields=[field], fq=None, frequency="daily", skip_paused=False
        ),
    )
    result = dict(
        kind="data_result",
        api="get_price",
        label=label,
        symbol="000001.XSHE",
        columns=[field],
        dtypes={field: "float64"},
        rows=[{"index": "2024-03-01 00:00:00", field: value}] if rows is None else rows,
    )
    return [request, result]


def publish(store, src):
    return store.jq_queries(store.import_jq_queries([src], source_kind="synthetic_fixture"))


def test_query_visibility_empty_nonfinite_precision_and_evidence(store, tmp_path):
    specs = pair() + pair(label="nan", value={"nonfinite": "nan"}) + pair(label="empty", rows=[])
    src = source(tmp_path, specs)
    # Preserve more than 12 source decimal places, rather than round factors.
    path = Path(src.events_path)
    path.write_text(path.read_text().replace('"close": 12.34', '"close": 12.123456789012345678'))
    src = replace(src, events_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    view = publish(store, src)
    manifest = view.describe()
    assert len(manifest["queries"]) == 3
    assert "FORBIDDEN_ACCOUNT_RESULT" not in json.dumps(view.source_evidence())
    rows = []
    for q in manifest["queries"]:
        assert view.read(q["query_id"], as_of="2024-03-04T15:29:00+08:00").rows == ()
        rows.extend(view.read(q["query_id"], as_of="2024-03-04T15:30:00+08:00").rows)
    assert {"index": "2024-03-01 00:00:00", "close": {"nonfinite": "nan"}} in rows
    assert any(r["close"] == "12.123456789012345678" for r in rows)
    assert "observed_empty" in {q["source_status"] for q in manifest["queries"]}
    assert publish(store, src).dataset_id == view.dataset_id
    with pytest.raises(DataError) as exc:
        view.query(
            "get_price",
            specs[0]["parameters"] | {"security": "000001.XSHE"},
            as_of="2024-03-04T15:30:00+08:00",
        )
    assert exc.value.code == "JQ_CONFLICT"


def test_explicit_initialization_keeps_source_clock_and_only_prior_raw_close(store, tmp_path):
    view = publish(store, source(tmp_path, pair()))
    qid = view.describe()["queries"][0]["query_id"]

    def mark(clock):
        return view.read_initialization(
            qid, "000001.XSHE", valuation_date="2024-03-01", as_of=clock
        )

    assert mark("2024-03-04T09:00:00+08:00").rows == ()
    result = mark("2024-03-05T09:00:00+08:00").to_dict()
    assert result["rows"][0]["values"] == {"close": "12.34"}
    assert result["observed_at"] == "2024-03-04T15:30:00+08:00"
    assert result["role"] == "initialization" and result["strategy_visible"] is False
    assert not result["current_quote"] and not result["adjacent_session_certified"]
    for clock in ["2024-03-01T09:00:00+08:00", "2024-05-01T09:00:00+08:00"]:
        with pytest.raises(DataError):
            mark(clock)
    with pytest.raises(DataError):
        view.read_initialization(
            qid, "600000.XSHG", valuation_date="2024-03-01", as_of="2024-03-05T09:00:00+08:00"
        )


@pytest.mark.parametrize("variant", ["nan", "empty", "adjusted", "minute"])
def test_initialization_does_not_promote_missing_adjusted_or_intraday_close(
    store, tmp_path, variant
):
    specs = (
        pair(value={"nonfinite": "nan"})
        if variant == "nan"
        else pair(rows=[])
        if variant == "empty"
        else pair()
    )
    if variant == "adjusted":
        specs[0]["parameters"]["fq"] = "pre"
    if variant == "minute":
        specs[0]["parameters"]["frequency"] = "1m"
    view = publish(store, source(tmp_path, specs))
    qid = view.describe()["queries"][0]["query_id"]
    if variant in {"adjusted", "minute"}:
        with pytest.raises(DataError):
            view.read_initialization(
                qid, "000001.XSHE", valuation_date="2024-03-01", as_of="2024-03-05T09:00:00+08:00"
            )
    else:
        assert (
            view.read_initialization(
                qid, "000001.XSHE", valuation_date="2024-03-01", as_of="2024-03-05T09:00:00+08:00"
            ).rows
            == ()
        )


def test_own_future_and_field_guards_and_recovery(store, tmp_path):
    specs = pair()
    view = publish(store, source(tmp_path, specs))
    params = specs[0]["parameters"] | {"security": "000001.XSHE"}
    for updates, code in [
        ({"end_date": "2024-03-05"}, "JQ_FUTURE_QUERY"),
        ({"fields": ["invalid"]}, "JQ_QUERY_FIELD"),
    ]:
        with pytest.raises(DataError) as exc:
            view.query("get_price", params | updates, as_of="2024-03-04T15:30:00+08:00")
        assert exc.value.code == code
    assert view.query("get_price", params, as_of="2024-03-04T15:29:00+08:00").rows == ()
    result = view.query("get_price", params, as_of="2024-03-04T15:30:00+08:00")
    assert Decimal(result.rows[0]["close"]) == Decimal("12.34")
    result.rows[0]["close"] = "999"
    assert (
        view.query("get_price", params, as_of="2024-03-04T15:30:00+08:00").rows[0]["close"]
        == "12.34"
    )


def test_distinct_adjustment_snapshots_not_conflated(store, tmp_path):
    first, second = pair(value=1.0, field="factor"), pair(value=0.93, field="factor")
    for e in second:
        e["time"] = "2024-03-05 15:30:00"
    view = publish(store, source(tmp_path, first + second))
    params = first[0]["parameters"] | {"security": "000001.XSHE"}
    assert (
        view.query("get_price", params, as_of="2024-03-05T15:29:00+08:00").rows[0]["factor"]
        == "1.0"
    )
    assert (
        view.query("get_price", params, as_of="2024-03-05T15:30:00+08:00").rows[0]["factor"]
        == "0.93"
    )
    assert (
        view.query(
            "get_price",
            params,
            as_of="2024-03-05T15:30:00+08:00",
            observed_at="2024-03-04T15:30:00+08:00",
        ).rows[0]["factor"]
        == "1.0"
    )


def universe_specs():
    return [
        dict(
            kind="capture_start",
            logical_time="2024-03-04 15:30:00",
            observed_wall_utc="2026-10-10T14:00:00Z",
        ),
        dict(
            kind="coverage_request",
            api="get_all_securities",
            params={"date": "2024-03-04", "types": ["stock"]},
        ),
        dict(
            kind="universe_summary",
            query_date="2024-03-04",
            native_count=2,
            native_columns=["display_name", "name", "start_date", "end_date", "type"],
            selected_fields=["code", "start_date", "end_date", "type"],
        ),
        dict(
            kind="universe_rows",
            query_date="2024-03-04",
            offset=0,
            total=2,
            rows=[
                dict(
                    code="000001.XSHE", start_date="1991-01-01", end_date="2200-01-01", type="stock"
                )
            ],
        ),
        dict(
            kind="universe_rows",
            query_date="2024-03-04",
            offset=1,
            total=2,
            rows=[
                dict(
                    code="000002.XSHE", start_date="1992-01-01", end_date="2025-01-01", type="stock"
                )
            ],
        ),
        dict(kind="capture_end", observed_wall_utc="2026-10-10T14:00:01Z"),
    ]


def test_complete_metadata_keeps_future_dates_and_real_capture_distinct(store, tmp_path):
    view = publish(store, source(tmp_path, universe_specs()))
    result = view.query(
        "get_all_securities",
        {"date": "2024-03-04", "types": ["stock"]},
        as_of="2024-03-04T15:30:00+08:00",
    ).to_dict()
    assert len(result["rows"]) == 2
    assert result["physical_capture_interval"]["capture_start"] == "2026-10-10T22:00:00+08:00"
    assert result["observed_at"] == "2024-03-04T15:30:00+08:00"
    assert all(
        r["future_end_date_observation"] and not r["end_date_is_historical_pit_fact"]
        for r in result["rows"]
    )
    assert result["result"]["requested_result_complete"] is True
    assert result["historical_pit"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e.pop(4),
        lambda e: e[4].update(offset=0),
        lambda e: e[4]["rows"][0].update(code="000001.XSHE"),
        lambda e: e[3]["rows"][0].update(trade_price=99),
        lambda e: e[5].update(observed_wall_utc="2026-10-09T14:00:00Z"),
    ],
)
def test_reject_missing_overlapping_contaminated_or_bad_capture(store, tmp_path, change):
    specs = universe_specs()
    change(specs)
    with pytest.raises(DataError):
        publish(store, source(tmp_path, specs))
    assert not (store.root / "jq_queries").exists()


def test_source_errors_are_observations_and_hashes_fail_closed(store, tmp_path):
    specs = [
        dict(
            kind="field_request",
            field="vwap",
            params=dict(
                security="000001.XSHE",
                fields=["vwap"],
                frequency="daily",
                fq=None,
                end_date="2024-03-01",
                count=1,
            ),
        ),
        dict(
            kind="field_exception",
            field="vwap",
            error="SOURCE_ONLY",
            error_type="AssertionError",
            native_repr="AssertionError('SOURCE_ONLY')",
        ),
    ]
    src = source(tmp_path, specs)
    with pytest.raises(DataError) as exc:
        publish(store, replace(src, events_sha256="0" * 64))
    assert exc.value.code == "JQ_SOURCE_HASH"
    view = publish(store, src)
    record = view.describe()["queries"][0]
    observed = view.read(record["query_id"], as_of="2024-03-04T15:30:00+08:00").to_dict()
    assert observed["result"]["status"] == "observed_source_error"
    with pytest.raises(DataError) as exc:
        view.query("get_price", specs[0]["params"], as_of="2024-03-04T15:30:00+08:00")
    assert exc.value.code == "JQ_QUERY_FIELD" and "SOURCE_ONLY" not in str(exc.value)
    path = store.root / "jq_queries" / (view.dataset_id + ".json")
    path.chmod(0o644)
    path.write_text(path.read_text() + " ")
    with pytest.raises(DataError) as exc:
        view.describe()
    assert exc.value.code == "JQ_INTEGRITY"
