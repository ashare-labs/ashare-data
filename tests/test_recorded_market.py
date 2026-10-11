import hashlib
import json
from pathlib import Path

import pytest

from ashare_data import JQObservationInput, Store
from ashare_data.model import DataError


def fixture(tmp_path, changes=None):
    settings = dict(
        symbol="000001.XSHE",
        start_date="2025-02-10",
        end_date="2025-02-10",
        frequency="minute",
        use_real_price=True,
        recorder_revision=2,
    )

    def event(kind, time="2025-02-10 10:00:00", **kw):
        return dict(case_id="TEST", version=1, run_tag="synthetic", kind=kind, time=time, **kw)

    params = dict(
        security="000001.XSHE",
        frequency="daily",
        fields=["open"],
        fq=None,
        start_date="2025-02-10",
        end_date="2025-02-10 10:00:00",
        skip_paused=False,
    )
    events = [
        event("identity", settings=settings),
        event("day_start", time="2025-02-10 09:00:00", previous_date="2025-02-07"),
        event(
            "current_data",
            symbol="000001.XSHE",
            label="quote",
            data=dict(last_price=23, day_open=22, high_limit=25, low_limit=20, paused=False),
        ),
        event("query_request", label="open", params=params),
        event("query_return", label="open", columns=["open"], row_count=1),
        event(
            "query_rows", label="open", offset=0, rows=[dict(index="2025-02-10 00:00:00", open=22)]
        ),
        event("snapshot", portfolio={"available_cash": -999}, orders=[{"amount": 9999}]),
    ]
    if changes:
        changes(events)
    for seq, e in enumerate(events, 1):
        e["seq"] = seq
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    script = tmp_path / "jq.py"
    script.write_text("# synthetic protocol; not a real strategy\n")
    source = JQObservationInput(
        case_id="TEST",
        security="000001.XSHE",
        session_date="2025-02-10",
        events_path=str(p),
        events_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
        script_path=str(script),
        script_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
    )
    store = Store.init(tmp_path / "store")
    return store, source, params


def test_roundtrip_only_market_no_oracle_and_exact_current_clock(tmp_path):
    store, src, p = fixture(tmp_path)
    ds = store.import_recorded_market([src], source_kind="synthetic_fixture")
    v = store.recorded_market(ds)
    assert v.current(src.security, as_of="2025-02-10T10:00:00+08:00").status == "available"
    assert v.current(src.security, as_of="2025-02-10T10:01:00+08:00").status == "unavailable"
    assert v.query(p, as_of="2025-02-10T10:00:00+08:00").rows[0]["open"] == "22"
    body = json.loads((store.root / "recorded_market" / f"{ds}.json").read_text())
    assert "portfolio" not in json.dumps(body) and "-999" not in json.dumps(body)
    result = v.current(src.security, as_of="2025-02-10T10:00:00+08:00").to_dict()
    result["rows"][0]["values"]["last_price"] = "0"
    assert (
        v.current(src.security, as_of="2025-02-10T10:00:00+08:00").rows[0]["values"]["last_price"]
        == "23"
    )


@pytest.mark.parametrize(
    "fields", [["high"], ["low"], ["close"], ["volume"], ["money"], ["open", "close"]]
)
def test_incomplete_day_guard_is_independent_of_recorded_source_errors(tmp_path, fields):
    store, src, p = fixture(tmp_path)
    ds = store.import_recorded_market([src], source_kind="synthetic_fixture")
    p = {**p, "fields": fields}
    with pytest.raises(DataError) as exc:
        store.recorded_market(ds).query(p, as_of="2025-02-10T10:00:00+08:00")
    assert exc.value.code == "JQ_INCOMPLETE_DAILY"


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e.pop(5),
        lambda e: e[5].update(offset=1),
        lambda e: e[4].update(row_count=True),
        lambda e: e[5]["rows"][0].update(order_id=123),
        lambda e: e[2]["data"].update(filled=100),
        lambda e: e.append({**e[2], "data": {**e[2]["data"], "last_price": 24}}),
    ],
)
def test_reject_incomplete_conflicting_or_contaminated_input(tmp_path, change):
    store, src, _ = fixture(tmp_path, change)
    with pytest.raises(DataError):
        store.import_recorded_market([src], source_kind="synthetic_fixture")


def test_pinned_source_and_sealed_product_tampering_are_rejected(tmp_path):
    store, src, p = fixture(tmp_path)
    ds = store.import_recorded_market([src], source_kind="synthetic_fixture")
    Path(src.events_path).write_text("corrupt")
    with pytest.raises(DataError) as exc:
        store.import_recorded_market([src], source_kind="synthetic_fixture")
    assert exc.value.code == "JQ_SOURCE_HASH"
    sealed = store.root / "recorded_market" / f"{ds}.json"
    sealed.chmod(0o644)
    sealed.write_text("{}")
    with pytest.raises(DataError) as exc:
        store.recorded_market(ds)
    assert exc.value.code == "JQ_INTEGRITY"


def test_no_future_end_or_capacity_from_open_only_or_implicit_roles(tmp_path):
    store, src, p = fixture(tmp_path)
    ds = store.import_recorded_market([src], source_kind="synthetic_fixture")
    v = store.recorded_market(ds)
    with pytest.raises(DataError) as exc:
        v.query(p, as_of="2025-02-10T09:59:00+08:00")
    assert exc.value.code == "JQ_FUTURE_QUERY"
    args = dict(
        security=src.security,
        label="2025-02-10T00:00:00+08:00",
        frequency="daily",
        as_of="2025-02-10T10:00:00+08:00",
    )
    assert v.market(**args, role="retrospective_capacity").status == "unavailable"
    with pytest.raises(DataError):
        v.market(**args, role="strategy")
