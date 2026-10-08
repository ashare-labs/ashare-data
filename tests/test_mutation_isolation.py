"""Properties of public ownership boundaries, including nested pandas object cells."""

import copy
import json
import pickle
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import pandas as pd
import pytest

from ashare_data import Client, CoverageContract, DataError, ResearchResult, Store
from ashare_data.model import digest
from ashare_data.query import DataView
from test_research import calendar, payload, receipt, row, sina_rows, write

SEC = "600000.XSHG"
CLOCK = "2020-01-04T16:00:00+08:00"


def mutable_paths(value, path=()):
    """Enumerate every reachable dict/list, not just a few known report keys."""
    if isinstance(value, (dict, list)):
        yield path
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in items:
            yield from mutable_paths(child, path + (key,))
    elif isinstance(value, tuple):
        for key, child in enumerate(value):
            yield from mutable_paths(child, path + (key,))


def node(value, path):
    for key in path:
        value = value[key]
    return value


def scrub(value):
    # Receipt identity and fixed as_of are stable; diagnostic wall-clock age is not.
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k != "cache_age_seconds"}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


@pytest.fixture(params=["bao", "sina"])
def fixed(request, store, tmp_path):
    if request.param == "bao":
        sid = store.import_research([write(tmp_path, payload())], calendar_path=calendar(tmp_path))
        frequency = "daily"
    else:
        cache = tmp_path / "cache"
        rows = sina_rows() + sina_rows("2020-01-02 09:32:00")
        rows[0]["vendor_extra"] = {"nested": [{"labels": ["original"]}], "opaque": [1, 2]}
        receipt(cache, rows, observed="2020-01-02T09:33:00+08:00")
        sid = Client(cache=cache, cache_mode="only").save_research(store)
        frequency = "1m"
    return store, sid, frequency


def price(view, frequency):
    return view.get_price(SEC, count=2, frequency=frequency, fields=["close", "volume", "money"])


def signature(view, frequency):
    result = price(view, frequency)
    for part in result.report["provenance"]:
        for record in part["records"]:
            assert digest(record["source_row"]) == record["raw_record_sha256"]
    return scrub(
        {
            "result": result.to_dict(),
            "descriptor": view.descriptor(),
            "quality": view.quality(),
            "lineage": view.lineage(),
            "coverage": view.coverage(SEC, "2020-01-01", "2020-01-03", frequency),
        }
    )


def public_value(view, frequency, target):
    if target in ["descriptor", "quality", "lineage"]:
        return getattr(view, target)()
    if target == "coverage":
        return view.coverage(SEC, "2020-01-01", "2020-01-03", frequency)
    result = price(view, frequency)
    if target == "report":
        return result.report
    if target == "attrs":
        return result.data.attrs
    if target == "serialization":
        return result.to_dict()
    raise AssertionError(target)


@pytest.mark.parametrize(
    "target", ["report", "attrs", "serialization", "descriptor", "lineage", "quality", "coverage"]
)
def test_every_nested_public_container_cannot_change_shared_views(fixed, target):
    store, sid, frequency = fixed
    view = store.research(sid).at(CLOCK, visibility="assumed")
    before_sibling = view.at(CLOCK, visibility="assumed")
    original = signature(view, frequency)
    prior_result = price(view, frequency)
    prior_signature = scrub(prior_result.to_dict())
    paths = list(mutable_paths(public_value(view, frequency, target)))
    assert paths
    for path in paths:
        value = public_value(view, frequency, target)
        node(value, path).clear()
        # Same view, sibling created before mutation, sibling after, and fresh reopen.
        for current in [
            view,
            before_sibling,
            view.at(CLOCK, visibility="assumed"),
            Store(store.root).research(sid).at(CLOCK, visibility="assumed"),
        ]:
            assert signature(current, frequency) == original, (target, path)
        assert scrub(prior_result.to_dict()) == prior_signature


@pytest.mark.parametrize(
    "path,changed",
    [
        (("source_rows", 0, "close"), "999.0000"),
        (("records", 0, "source_row", "close"), "999.0000"),
        (("records", 0, "source_label"), "2019-12-29"),
        (("records", 0, "raw_record_sha256"), "f" * 64),
        (("records", 0, "object_sha256"), "f" * 64),
        (("records", 0, "security"), "000001.XSHE"),
    ],
)
def test_leaf_mutations_keep_values_labels_row_hash_and_identity(fixed, path, changed):
    store, sid, frequency = fixed
    view = store.research(sid).at(CLOCK, visibility="assumed")
    sibling = view.at(CLOCK, visibility="assumed")
    original = signature(view, frequency)
    result = price(view, frequency)
    attrs_before = copy.deepcopy(result.data.attrs)
    provenance = result.report["provenance"][0]
    node(provenance, path[:-1])[path[-1]] = changed
    assert result.data.attrs == attrs_before  # Separate public representations also own copies.
    assert signature(view, frequency) == signature(sibling, frequency) == original


@pytest.mark.parametrize(
    "mode", ["cells", "index", "columns", "attrs", "pickle_cache", "serialized_cache"]
)
def test_dataframe_and_consumer_cache_cannot_rewrite_view(fixed, mode):
    store, sid, frequency = fixed
    view = store.research(sid).at(CLOCK, visibility="assumed")
    before = signature(view, frequency)
    result = price(view, frequency)
    report = copy.deepcopy(result.report)
    if mode == "cells":
        result.data.iloc[0, 0] = Decimal("999")
    elif mode == "index":
        result.data.index.values[0] = pd.Timestamp("1999-01-01").to_datetime64()
    elif mode == "columns":
        result.data.columns = ["a", "b", "c"]
    elif mode == "attrs":
        result.data.attrs["provenance"][0]["records"][0]["source_row"]["close"] = "999"
    elif mode == "pickle_cache":
        cached = pickle.loads(pickle.dumps(result))
        cached.report["provenance"][0]["records"][0]["source_row"]["close"] = "999"
        cached.data.iloc[0, 0] = Decimal("999")
    else:
        cache = result.to_dict()
        cache["report"]["provenance"][0]["records"][0]["source_label"] = "1999-01-01"
        cache["rows"][0]["close"] = Decimal("999")
    assert result.report == report
    assert signature(view, frequency) == before


def test_research_result_constructor_detaches_nested_object_cells_attrs_and_report():
    nested = {"values": [1, {"labels": ["original"]}]}
    frame = pd.DataFrame({"custom": [nested], "price": [Decimal("1.2345")], "volume": [123]})
    frame.index = pd.DatetimeIndex(["2020-01-01"], name="time")
    report = {"nested": nested}
    frame.attrs = report
    result = ResearchResult(frame, report)
    expected = copy.deepcopy(result.to_dict())
    nested["values"][1]["labels"][0] = "changed input"
    frame.iloc[0, 1] = Decimal("999")
    frame.index.values[0] = pd.Timestamp("1999-01-01").to_datetime64()
    frame.attrs["extra"] = ["changed input"]
    assert result.to_dict() == expected
    # Serialization of arbitrary object cells must be recursive too.
    serialized = result.to_dict()
    serialized["rows"][0]["custom"]["values"][1]["labels"].clear()
    serialized["report"]["nested"]["values"].clear()
    assert result.to_dict() == expected
    result.data.iloc[0, 0]["values"][1]["labels"].clear()
    assert result.report["nested"]["values"][1]["labels"] == ["original"]
    assert result.data.attrs["nested"]["values"][1]["labels"] == ["original"]
    assert nested["values"][1]["labels"] == ["changed input"]
    assert result.data.dtypes.to_dict() == {"custom": object, "price": object, "volume": "int64"}


def test_mutating_input_selection_lists_and_store_handle(fixed, tmp_path):
    store, sid, frequency = fixed
    view = store.research(sid).at(CLOCK, visibility="assumed")
    before = signature(view, frequency)
    securities, fields = [SEC], ["close", "volume"]
    result = view.get_price(securities, count=2, fields=fields, frequency=frequency)
    previous = scrub(result.to_dict())
    securities[0] = "000001.XSHE"
    fields.clear()
    # The already-open view owns its root, not a caller's mutable Store handle.
    store.root = tmp_path / "unrelated"
    store.catalog = tmp_path / "missing.sqlite"
    assert signature(view, frequency) == before
    assert scrub(result.to_dict()) == previous


def test_error_input_details_and_serialization_are_isolated():
    details = {"rows": [{"labels": ["original"]}]}
    error = DataError("EXAMPLE", "test", details)
    details["rows"][0]["labels"].clear()
    assert error.details["rows"][0]["labels"] == ["original"]
    public = error.as_dict()
    public["details"]["rows"][0]["labels"].clear()
    assert error.as_dict()["details"]["rows"][0]["labels"] == ["original"]


@pytest.mark.parametrize(
    "target",
    ["loaded", "manifest", "calendar", "sessions", "instruments", "quality", "lineage", "bars"],
)
def test_legacy_snapshot_public_metadata_isolation(published, target):
    store, _, _, sid = published
    with store.snapshot(sid) as view:

        def snapshot():
            return {
                "calendar": view.calendar("2026-04-09", "2026-04-09"),
                "instruments": view.instruments(as_of="2026-04-09T15:00:00+08:00"),
                "lineage": view.lineage(),
                "quality": view.quality(),
                "bars": view.bars(
                    ["000001.XSHE"],
                    "2026-04-09T14:53:00+08:00",
                    "2026-04-09T14:56:00+08:00",
                    quality="synthetic",
                ),
            }

        before = snapshot()

        def value():
            if target in ["loaded", "manifest"]:
                return getattr(view, target)
            if target == "sessions":
                return view.sessions("2026-04-09")
            return snapshot()[target]

        for path in mutable_paths(value()):
            output = value()
            node(output, path).clear()
            assert snapshot() == before


def test_legacy_dataview_constructor_detaches_input_and_conflicts(published):
    store, _, _, sid = published
    with store.snapshot(sid) as original:
        loaded, manifest = original.loaded, original.manifest
        # Input includes two inconsistent metadata claims solely to inspect conflict isolation.
        second = copy.deepcopy(loaded[0])
        second["bundle"]["calendar"][0]["evidence"] = "conflicting fixture"
        inputs = loaded + [second]
        with DataView(inputs, snapshot_id=sid, manifest=manifest) as view:
            before = view.lineage(), view.quality(), view.conflicts()
            inputs[0]["bundle"]["source"]["id"] = "caller changed"
            manifest["report"].clear()
            assert (view.lineage(), view.quality(), view.conflicts()) == before
            view.conflicts()[0].clear()
            assert view.conflicts() == before[2]


def test_coverage_contract_input_and_all_returned_objects_are_detached():
    data = json.loads(
        (Path(__file__).parents[1] / "examples/fixtures/paper_contract.json").read_text()
    )
    contract = CoverageContract(data)
    clock = "2026-09-30T09:31:30+08:00"

    def outputs():
        return [
            contract.trading_status(SEC, clock),
            contract.assess(
                SEC,
                ["2026-09-30 09:31:00"],
                "2026-09-30 09:31:00",
                "2026-09-30 09:31:00",
                as_of=clock,
            ),
            contract.bounds("2026-09-30 09:31:00", as_of=clock),
            contract.freshness(SEC, ["2026-09-30 09:31:00"], clock),
        ]

    before = outputs()
    data["calendar"][0]["sessions"].clear()
    data["statuses"].clear()
    assert outputs() == before
    for path in mutable_paths(outputs()):
        node(outputs(), path).clear()
        assert outputs() == before


def test_sibling_context_creation_is_lightweight_and_does_not_reload(fixed, monkeypatch):
    from ashare_data import research

    store, sid, frequency = fixed
    view = store.research(sid)

    def unexpected(*args, **kwargs):
        pytest.fail("at() reloaded or deep-copied the whole dataset")

    with monkeypatch.context() as patch:
        patch.setattr(research, "_read", unexpected)
        patch.setattr(research.copy, "deepcopy", unexpected)
        siblings = [view.at(CLOCK, visibility="assumed") for _ in range(1000)]
    assert price(siblings[-1], frequency).report["dataset_id"] == sid


def test_bounded_normal_read_performance_and_precision(store, tmp_path):
    # Keep a wide bound as a smoke control, record actual timing for independent review.
    from datetime import date, timedelta

    rows = [row((date(2020, 1, 1) + timedelta(days=i)).isoformat()) for i in range(366)]
    data = payload(rows)
    data["params"]["end_date"] = "2020-12-31"
    view = store.research(store.import_research([write(tmp_path, data)]))
    start = perf_counter()
    for _ in range(10):
        result = view.get_price(SEC, count=366, fields=["close", "volume", "money"])
        assert len(result.data) == 366 and result.data.iloc[0]["close"] == Decimal("12.3400")
        assert (
            digest(result.report["provenance"][0]["records"][0]["source_row"])
            == result.report["provenance"][0]["records"][0]["raw_record_sha256"]
        )
    elapsed = perf_counter() - start
    assert elapsed < 10


def test_result_constructor_detaches_axis_arrays_and_preserves_shape():
    frame = pd.DataFrame([[Decimal("1.23"), 4]], columns=["price", "volume"], index=["row"])
    original = ResearchResult(frame, {"status": ["original"]})
    frame.columns.values[0] = "changed"
    frame.index.values[0] = "changed"
    assert list(original.data.columns) == ["price", "volume"]
    assert list(original.data.index) == ["row"]
    assert original.data.iloc[0, 0] == Decimal("1.23")


def test_saved_client_inputs_and_nested_source_metadata_remain_pinned(store, tmp_path):
    cache = tmp_path / "cache"
    rows = sina_rows()
    rows[0]["metadata"] = {"provider": {"labels": ["original"]}}
    receipt(cache, rows)
    client = Client(cache=cache, cache_mode="only").at(
        "2020-01-02T09:33:00+08:00", visibility="received"
    )
    before = client.get_price(SEC, count=1, frequency="1m")
    expected = before.iloc[0]["close"]
    before.attrs["provenance"][0]["source_rows"][0]["metadata"]["provider"]["labels"].clear()
    sid = client.save_research(store)
    view = store.research(sid).at("2020-01-02T09:33:00+08:00", visibility="received")
    result = view.get_price(SEC, count=1, frequency="1m")
    assert float(result.data.iloc[0]["close"]) == expected
    result.report["provenance"][0]["records"][0]["source_row"]["metadata"]["provider"][
        "labels"
    ].clear()
    again = view.get_price(SEC, count=1, frequency="1m")
    assert again.report["provenance"][0]["records"][0]["source_row"]["metadata"]["provider"][
        "labels"
    ] == ["original"]
    assert client.save_research(store) == sid


def test_lineage_envelope_detaches_legacy_or_extension_metadata(store, tmp_path):
    # Valid content addressing does not imply all optional metadata is scalar.
    from ashare_data.model import canonical

    sid = store.import_research([write(tmp_path, payload())])
    manifest = json.loads((store.root / "research-manifests" / (sid + ".json")).read_text())
    manifest["receipt_semantics"] = {"legacy": {"notes": ["unknown receipt"]}}
    sid = digest(manifest)
    (store.root / "research-manifests" / (sid + ".json")).write_bytes(canonical(manifest))
    with store._db(write=True) as db:
        db.execute(
            "INSERT INTO research_snapshots VALUES (?,?,?,?)",
            (sid, canonical(manifest).decode(), "published", "test"),
        )
    view = store.research(sid)
    sibling = view.at(CLOCK, visibility="assumed")
    original = view.lineage()
    view.lineage()["receipt_semantics"]["legacy"]["notes"].clear()
    assert view.lineage() == sibling.lineage() == store.research(sid).lineage() == original
