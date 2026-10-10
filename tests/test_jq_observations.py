"""Synthetic protocol fixtures only. Real market exports stay outside the repository."""

from dataclasses import replace
import hashlib
import json

import pytest

from ashare_data import DataError, JQObservationInput, Store


def fixture_source(tmp_path, *, case="fixture", security="000001.XSHE", paused=False):
    root = tmp_path / case
    root.mkdir()
    events = []

    def add(kind, time="10:00:00", **payload):
        events.append(dict(case_id=case, run_tag="synthetic-" + case, seq=len(events) + 1,
                           kind=kind, time="2024-03-04 " + time, **payload))

    add("identity", settings=dict(symbol=security, start_date="2024-03-04", end_date="2024-03-04",
                                 use_real_price=True, frequency="minute"))
    add("snapshot", orders=[{"price": "FORBIDDEN_REFERENCE_RESULT"}], trades=[{"filled": 1234567}])
    current = dict(last_price=12.5, day_open=12.4, high_limit=13.6, low_limit=11.2,
                   paused=paused, is_st=False)
    add("current_data", data=current, symbol=security, label="test")
    bar = dict(index="2024-03-04 10:00:00", open=12.4, close=12.5, high=12.6, low=12.3,
               volume=0 if paused else 500, money=0 if paused else 6220.01)
    add("market_bars", symbol=security, label="test", frequency="1m", fq=None, rows=[bar])
    add("current_data", time="10:01:00", data=current, symbol=security, label="test")
    add("market_bars", time="10:01:00", symbol=security, label="test", frequency="1m", fq=None, rows=[bar])
    add("daily_market", time="15:30:00", frequency="daily", fq=None,
        rows=[dict(bar, index="2024-03-04 00:00:00", volume=0 if paused else 990000,
                   high_limit=13.6, low_limit=11.2, paused=float(paused))])
    add("call_auction_data", time="09:28:00", source="get_call_auction",
        rows=[dict(code=security, time="2024-03-04 09:25:00", current=12.4, volume=100, money=1240.01)])
    script = root / "jq.py"
    script.write_text("# synthetic fixture, not a real JQ run\n")
    path = root / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return JQObservationInput(case, security, "2024-03-04", str(path),
                              hashlib.sha256(path.read_bytes()).hexdigest(), str(script),
                              hashlib.sha256(script.read_bytes()).hexdigest())


def publish(store, source):
    sid = store.import_jq_observations([source], source_kind="synthetic_fixture")
    return store.jq_observations(sid)


def read(view, clock="10:00", *, kind="current", **kwargs):
    return view.read("000001.XSHE", kind=kind, as_of=f"2024-03-04T{clock}:00+08:00", **kwargs)


def rewrite(source, fn):
    from pathlib import Path
    path = Path(source.events_path)
    events = [json.loads(line) for line in path.read_text().splitlines()]
    fn(events)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return replace(source, events_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def test_market_only_exact_decimal_and_reopen(store, tmp_path):
    source = fixture_source(tmp_path)
    view = publish(store, source)
    assert view.describe()["record_count"] == 5
    assert view.describe()["market_event_count"] == 6
    assert view.describe()["full_session_coverage"] is False
    result = read(view, kind="minute")
    assert result.rows[0]["values"]["amount"] == "6220.01"
    assert result.rows[0]["values"]["volume"] == "500"
    assert len(result.rows[0]["source_refs"]) == 1  # later observation proof is hidden
    assert len(read(view, "10:01", kind="minute").rows[0]["source_refs"]) == 2
    assert "FORBIDDEN_REFERENCE_RESULT" not in json.dumps(view.market_evidence())
    raw = (store.root / "jq_observations" / (view.dataset_id + ".json")).read_text()
    assert "FORBIDDEN_REFERENCE_RESULT" not in raw and "1234567" not in raw
    assert Store(store.root).jq_observations(view.dataset_id).describe() == view.describe()
    assert store.import_jq_observations([source], source_kind="synthetic_fixture") == view.dataset_id


def test_visibility_not_label_or_full_day_volume(store, tmp_path):
    view = publish(store, fixture_source(tmp_path))
    assert read(view, "09:25", kind="auction").status == "unavailable"
    assert read(view, "09:25", kind="auction").to_dict()["reason"] == "NOT_OBSERVED_AS_OF"
    auction = read(view, "09:28", kind="auction", role="opening_observation")
    assert auction.rows[0]["source_label_at"].endswith("09:25:00+08:00")
    assert auction.rows[0]["available_at"].endswith("09:28:00+08:00")
    assert read(view, kind="daily").rows == ()
    assert read(view, "15:00", kind="daily", role="final_valuation").rows == ()
    assert read(view, "15:30", kind="daily", role="final_valuation").status == "available"
    capacity = read(view, kind="daily", role="engine_capacity")
    assert capacity.rows[0]["values"] == {"volume": "990000"}
    assert capacity.rows[0]["strategy_visible"] is False
    assert capacity.to_dict()["retrospective"] is True
    assert capacity.rows[0]["observed_at"].endswith("15:30:00+08:00")


def test_no_forward_fill_no_quote_to_bar_conversion(store, tmp_path):
    view = publish(store, fixture_source(tmp_path))
    assert read(view, "10:02").to_dict()["reason"] == "NO_SOURCE_OBSERVATION"
    assert read(view, "09:00", kind="minute").rows == ()
    assert read(view, kind="minute", start="2024-03-04T09:30:00+08:00",
                end="2024-03-04T09:30:00+08:00").rows == ()
    assert read(view, role="matching").rows[0]["kind"] == "current"


def test_suspension_fill_and_unknown_bootstrap(store, tmp_path):
    view = publish(store, fixture_source(tmp_path, paused=True))
    for kind in ("current", "minute", "daily"):
        row = read(view, "15:30" if kind == "daily" else "10:00", kind=kind).rows[0]
        assert row["executable"] is False
        assert row["source_paused_at_observation"] is True
        if kind != "current":
            assert row["quality"] == "source_filled_suspended"
            assert row["values"]["volume"] == "0"
    bootstrap = view.bootstrap("000001.XSHE", "2024-03-04")
    assert all(v is None for v in bootstrap["unknown"].values())
    assert bootstrap["conditional_model"]["state"] == "explicit_research_assumption_not_market_fact"


def test_mutation_isolation_and_integrity(store, tmp_path):
    view = publish(store, fixture_source(tmp_path))
    result = read(view)
    result.rows[0]["values"]["last_price"] = "999"
    view.describe()["coverage"].clear()
    assert read(view).rows[0]["values"]["last_price"] == "12.5"
    path = store.root / "jq_observations" / (view.dataset_id + ".json")
    path.chmod(0o644)
    path.write_text(path.read_text() + " ")
    with pytest.raises(DataError, match="hash mismatch"):
        read(view)


def test_deterministic_union_keeps_source_provenance(store, tmp_path):
    a = fixture_source(tmp_path, case="a")
    b = fixture_source(tmp_path, case="b")
    first = store.import_jq_observations([a, b], source_kind="synthetic_fixture")
    second = store.import_jq_observations([b, a], source_kind="synthetic_fixture")
    assert first == second
    assert len(read(store.jq_observations(first)).rows[0]["source_refs"]) == 2


@pytest.mark.parametrize("change,code", [
    (lambda e: e[3]["rows"][0].update(close=100), "JQ_SCHEMA"),
    (lambda e: e[2]["data"].update(target_fill=100), "JQ_SCHEMA"),
    (lambda e: e[3].update(orders=[]), "JQ_SCHEMA"),
    (lambda e: e[3].update(fq="pre"), "JQ_SCHEMA"),
    (lambda e: e[3]["rows"][0].update(index="2024-03-04 10:02:00"), "JQ_VISIBILITY"),
    (lambda e: e[4].update(seq=20), "JQ_SCHEMA"),
    (lambda e: e[0]["settings"].update(symbol="000002.XSHE"), "JQ_SCOPE"),
    (lambda e: e[5]["rows"][0].update(money=999), "JQ_CONFLICT"),
    (lambda e: e[6].update(time="2024-03-04 10:00:00"), "JQ_VISIBILITY"),
    (lambda e: e[3]["rows"][0].update(index="2024-03-04 09:59:30"), "JQ_SCHEMA"),
    (lambda e: e[6]["rows"][0].update(index="2024-03-04 01:00:00"), "JQ_SCHEMA"),
    (lambda e: e[3]["rows"][0].update(money=1e-100), "JQ_SCHEMA"),
])
def test_reject_bad_or_conflicting_sources(store, tmp_path, change, code):
    source = rewrite(fixture_source(tmp_path), change)
    with pytest.raises(DataError) as caught:
        publish(store, source)
    assert caught.value.code == code
    assert not (store.root / "jq_observations").exists()


def test_source_hash_scope_and_role_fail_closed(store, tmp_path):
    source = fixture_source(tmp_path)
    with pytest.raises(DataError) as caught:
        publish(store, replace(source, events_sha256="0" * 64))
    assert caught.value.code == "JQ_SOURCE_HASH"
    view = publish(store, source)
    for kwargs in [dict(kind="daily", role="matching"), dict(kind="minute", role="engine_capacity")]:
        with pytest.raises(DataError) as caught:
            read(view, **kwargs)
        assert caught.value.code == "JQ_ROLE"
    with pytest.raises(DataError) as caught:
        view.read("000001.XSHE", kind="current", as_of="2024-03-05T10:00:00+08:00")
    assert caught.value.code == "JQ_SCOPE"
    with pytest.raises(DataError):
        view.read("000001.XSHE", kind="current", as_of="2024-03-04 10:00:00")
