"""Purpose-scoped source inventory, explicit inputs and independently observable gaps."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from pathlib import Path
import hashlib
import json
import os
import shutil

import pytest

from ashare_data import (Store, DataError, ResearchInputRef, ResearchSecurityInputs,
                        ResearchPreflightResult, BaoStockSource)
from ashare_data import baostock as api
from ashare_data.cli import main
from ashare_data.model import digest
from test_baostock import bundle
from test_daily_status import persist, raises_code
from test_source_calendar import captured as calendar_capture


def capture(tmp_path, monkeypatch, role, *, symbol="600519.XSHG", rows=None):
    tmp_path.mkdir()
    kind = {"price": "daily", "status": "daily_status", "preclose": "daily_preclose"}[role]
    original = api.request
    req = original(kind, symbol, "2024-01-04", "2024-01-08")
    code = req["params"]["code"]
    data = []
    for i in range(5):
        day = f"2024-01-0{4+i}"
        data.append([day, code, "10", "12", "9", "11", "100", "1100.2300", "3", "1"]
                    if role == "price" else [day, code, "1", "0", "3"] if role == "status"
                    else [day, code, "10.1250", "3"])
    with monkeypatch.context() as patch:
        patch.setattr(api, "FIELDS", req["params"]["fields"])
        patch.setattr(api, "request", lambda *a, **k: req)
        blobs, _ = bundle(tmp_path, "daily", rows=data if rows is None else rows)
    return persist(tmp_path / "export", blobs)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    calroot = tmp_path / "calendar"
    calroot.mkdir()
    store, cid, _ = calendar_capture(calroot, monkeypatch, flags=("1",) * 5, start="2024-01-04")
    refs = {role: ResearchInputRef("baostock", store.import_baostock_capture(
        capture(tmp_path / role, monkeypatch, role))) for role in ("price", "status", "preclose")}
    return store, ResearchSecurityInputs("600519.XSHG", **refs), ResearchInputRef("baostock", cid)


def run(ready, **kwargs):
    store, binding, calendar = ready
    return store.preflight_research(**dict(inputs=[binding], calendar=calendar,
                                          trading_dates=["2024-01-06"], **kwargs))


def facts(row):
    return {f.field: f for f in row.facts}


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_full_research_values_are_not_execution_admission(ready, monkeypatch):
    store, _, _ = ready
    before = hashes(store.root)
    monkeypatch.setattr(BaoStockSource, "fetch", lambda *a, **k: pytest.fail("network"))
    result = run(ready, require_complete=True)
    assert isinstance(result, ResearchPreflightResult) and result.research_values_complete
    assert [r.trade_date for r in result.rows] == [date(2024, 1, d) for d in (5, 6, 7)]
    assert [r.role for r in result.rows] == ["prior_day_research", "anchor_day_research", "successor_metadata"]
    prior, anchor, successor = map(facts, result.rows)
    assert prior["close"].value == "11" and "open" not in prior and "close" not in successor
    assert anchor["amount"].value == "1100.2300" and anchor["amount"].unit == "CNY"
    assert anchor["volume"].value == 100 and anchor["volume"].unit == "share"
    assert anchor["source_preclose"].value == "10.1250"
    assert anchor["source_preclose"].price_basis == "source_reference_adjustflag_3"
    assert anchor["source_preclose"].value != prior["close"].value
    assert anchor["source_preclose"].evidence["quality"]["reference_price_eligible"] is None
    assert anchor["close"].evidence["evidence_kind"] == "offline_test_only"
    assert anchor["close"].evidence["quality"]["synthetic"] is True
    assert anchor["suspended"].value is False and anchor["is_st"].value is False
    for row in result.rows:
        assert not row.to_dict()["execution_permission"] and not row.to_dict()["historical_pit"]
        assert facts(row)["listing_status"].value is None
        assert facts(row)["owner_execution_plan"].state == "unsupported"
    assert not result.to_dict()["network_used"]
    assert result.to_dict()["execution_admission"] == "BLOCKED_NO_OWNER_PLAN"
    assert hashes(store.root) == before
    assert run(ready).to_dict() == result.to_dict()
    body = result.to_dict()
    body.pop("report_id")
    assert result.report_id == digest(body)
    for row in result.rows:
        body = row.to_dict()
        sha = body.pop("row_sha256")
        assert sha == digest(body)


def test_nested_isolation_reopen_and_relocation(ready, tmp_path):
    store, binding, calendar = ready
    result = run(ready)
    original = result.to_dict()
    result.request["inputs"][0]["security"] = "000001.XSHE"
    result.sources.clear()
    fact = facts(result.rows[1])["close"]
    fact.evidence["quality"]["units"]["price"] = "wrong"
    fact.evidence["source_fields"]["close"] = "0"
    result.to_dict()["rows"][1]["facts"].clear()
    with pytest.raises(FrozenInstanceError):
        fact.state = "unsafe"
    assert result.to_dict() == original == run(ready).to_dict()
    moved = tmp_path / "moved"
    shutil.copytree(store.root, moved)
    moved_binding = replace(binding, **{r: replace(getattr(binding, r), store_path=str(moved))
                                        for r in ("price", "status", "preclose")})
    shifted = Store(moved).preflight_research(inputs=[moved_binding],
        calendar=replace(calendar, store_path=str(moved)), trading_dates=[date(2024, 1, 6)])
    assert shifted.to_dict() == original


@pytest.mark.parametrize("dates", [None, "2024-01-06", [], ["2024-01-06"] * 2,
    ["2024-01-06"] * 32, [True], [1], [None], [datetime(2024, 1, 6)], ["20240106"],
    ["2024-1-06"], ["2024-02-30"], ["2024-W01-6"], ["2024-01-01", "2024-02-01"]])
def test_invalid_dates_before_source_access(tmp_path, dates):
    store = Store.init(tmp_path / "empty")
    with raises_code("PREFLIGHT_ARGUMENT"):
        store.preflight_research(inputs=[ResearchSecurityInputs("600519.XSHG")],
                                 calendar=None, trading_dates=dates)


@pytest.mark.parametrize("inputs", [None, [], (), [True], ["600519.XSHG"],
    [ResearchSecurityInputs("600519.XSHG")] * 2,
    [ResearchSecurityInputs("600519.XSHG")] * 11,
    [ResearchSecurityInputs("sh.600519")], [ResearchSecurityInputs("６００５１９.XSHG")],
    [ResearchSecurityInputs(None)], [ResearchSecurityInputs(True)]])
def test_invalid_bindings(tmp_path, inputs):
    store = Store.init(tmp_path / "empty")
    with raises_code("PREFLIGHT_ARGUMENT"):
        store.preflight_research(inputs=inputs, calendar=None, trading_dates=["2024-01-06"])


@pytest.mark.parametrize("ref", [True, {}, ResearchInputRef([], "a" * 64),
    ResearchInputRef("latest", "a" * 64), ResearchInputRef("baostock", "A" * 64),
    ResearchInputRef("baostock", "a" * 63), ResearchInputRef("baostock", None),
    ResearchInputRef("baostock", "a" * 64, ""), ResearchInputRef("baostock", "a" * 64, True)])
def test_invalid_ref_types(tmp_path, ref):
    store = Store.init(tmp_path / "empty")
    with raises_code("PREFLIGHT_ARGUMENT"):
        store.preflight_research(inputs=[ResearchSecurityInputs("600519.XSHG", price=ref)],
                                 calendar=None, trading_dates=["2024-01-06"])


@pytest.mark.parametrize("strict", [None, 0, 1, "false"])
def test_strict_type(ready, strict):
    with raises_code("PREFLIGHT_ARGUMENT"):
        run(ready, require_complete=strict)


def test_missing_all_sources_and_strict_gap(tmp_path):
    store = Store.init(tmp_path / "empty")
    args = dict(inputs=[ResearchSecurityInputs("600519.XSHG")], calendar=None,
                trading_dates=["2024-01-06"])
    result = store.preflight_research(**args)
    assert [r.trade_date for r in result.rows] == [None, date(2024, 1, 6), None]
    assert not result.research_values_complete and not result.sources
    assert facts(result.rows[1])["amount"].reason == "INPUT_NOT_BOUND"
    with pytest.raises(DataError) as caught:
        store.preflight_research(**args, require_complete=True)
    assert caught.value.code == "PREFLIGHT_INCOMPLETE"
    assert caught.value.details == result.to_dict()


@pytest.mark.parametrize("anchor,reason", [("2024-01-04", "SOURCE_CALENDAR_BOUNDARY_UNKNOWN"),
                                         ("2024-01-09", "SOURCE_CALENDAR_OUT_OF_SCOPE")])
def test_calendar_gap_never_guesses_neighbours(ready, anchor, reason):
    store, binding, calendar = ready
    result = store.preflight_research(inputs=[binding], calendar=calendar, trading_dates=[anchor])
    assert result.rows[0].trade_date is result.rows[2].trade_date is None
    assert facts(result.rows[1])["calendar_relation"].reason == reason
    assert not result.research_values_complete


def test_wrong_security_and_kind_are_hard_errors(ready):
    store, binding, calendar = ready
    with raises_code("PREFLIGHT_BINDING_MISMATCH"):
        store.preflight_research(inputs=[replace(binding, security="000001.XSHE")],
                                 calendar=calendar, trading_dates=["2024-01-06"])
    with raises_code("PREFLIGHT_BINDING_MISMATCH"):
        store.preflight_research(inputs=[replace(binding, status=calendar)],
                                 calendar=calendar, trading_dates=["2024-01-06"])
    with raises_code("QUERY_KIND_MISMATCH"):
        store.preflight_research(inputs=[binding], calendar=binding.status, trading_dates=["2024-01-06"])


def test_unknown_status_keeps_independent_flag_and_zero_reference(ready, tmp_path, monkeypatch):
    store, binding, calendar = ready
    statuses = [[f"2024-01-0{d}", "sh.600519", "1", "?" if d == 6 else "0", "3"] for d in range(4, 9)]
    status = ResearchInputRef("baostock", store.import_baostock_capture(
        capture(tmp_path / "unknown-status", monkeypatch, "status", rows=statuses)))
    zeros = [[f"2024-01-0{d}", "sh.600519", "0", "3"] for d in range(4, 9)]
    preclose = ResearchInputRef("baostock", store.import_baostock_capture(
        capture(tmp_path / "zero-preclose", monkeypatch, "preclose", rows=zeros)))
    result = store.preflight_research(inputs=[replace(binding, status=status, preclose=preclose)],
                                     calendar=calendar, trading_dates=["2024-01-06"])
    anchor = facts(result.rows[1])
    assert anchor["suspended"].value is False
    assert anchor["is_st"].value is None and anchor["is_st"].reason == "unsupported_enum"
    assert anchor["source_preclose"].value == "0" and anchor["source_preclose"].state == "available"
    assert anchor["source_preclose"].evidence["source_state"] == "source_zero"
    assert not result.research_values_complete


@pytest.mark.parametrize("role,field", [("price", "close"), ("status", "is_st"),
                                      ("preclose", "source_preclose")])
def test_empty_success_is_unknown_not_zero_or_closed(ready, tmp_path, monkeypatch, role, field):
    store, binding, calendar = ready
    ref = ResearchInputRef("baostock", store.import_baostock_capture(
        capture(tmp_path / ("empty-" + role), monkeypatch, role, rows=[])))
    result = store.preflight_research(inputs=[replace(binding, **{role: ref})],
                                     calendar=calendar, trading_dates=["2024-01-06"])
    value = facts(result.rows[1])[field]
    assert value.state == "unknown" and value.value is None
    assert value.reason in {"DATE_NOT_CAPTURED", "row_missing_unknown"}
    assert facts(result.rows[1])["calendar_open"].value is True
    assert not result.research_values_complete


@pytest.mark.parametrize("empty", [True, False])
def test_empty_or_closed_calendar_preserves_anchor_unknowns(ready, tmp_path, monkeypatch, empty):
    store, binding, _ = ready
    path = tmp_path / "other-cal"
    path.mkdir()
    other, cid, _ = calendar_capture(path, monkeypatch, flags=("1", "1", "0", "1", "1"),
                                     start="2024-01-04", empty=empty)
    result = store.preflight_research(inputs=[binding],
        calendar=ResearchInputRef("baostock", cid, str(other.root)), trading_dates=["2024-01-06"])
    anchor = facts(result.rows[1])
    assert anchor["calendar_open"].value is (None if empty else False)
    assert anchor["calendar_relation"].reason == ("SOURCE_CALENDAR_UNKNOWN" if empty else
                                                 "SOURCE_CALENDAR_NOT_OPEN")
    assert not result.research_values_complete


def test_multiple_anchors_and_symbols_keep_input_order(ready, tmp_path, monkeypatch):
    store, first, calendar = ready
    refs = {r: ResearchInputRef("baostock", store.import_baostock_capture(
        capture(tmp_path / ("second-" + r), monkeypatch, r, symbol="000002.XSHE")))
        for r in ("price", "status", "preclose")}
    second = ResearchSecurityInputs("000002.XSHE", **refs)
    result = store.preflight_research(inputs=[second, first], calendar=calendar,
                                     trading_dates=["2024-01-07", "2024-01-05"])
    assert [(r.anchor_date.isoformat(), r.security) for r in result.rows[::3]] == [
        (d, s) for d in ("2024-01-07", "2024-01-05") for s in ("000002.XSHE", "600519.XSHG")]
    assert result.research_values_complete


def test_tampering_and_missing_id_do_not_become_gap_reports(ready):
    store, binding, calendar = ready
    with raises_code("BAOSTOCK_NOT_PUBLISHED"):
        store.preflight_research(inputs=[binding], calendar=replace(calendar, version_id="0" * 64),
                                 trading_dates=["2024-01-06"])
    path = next((store.root / "baostock-objects").iterdir())
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    with raises_code("INTEGRITY"):
        run(ready)


def test_json_cli_and_extra_arguments_rejected(ready, tmp_path, capsys):
    store, binding, calendar = ready
    request = {"inputs": [binding.to_dict()], "calendar": calendar.to_dict(),
               "trading_dates": ["2024-01-06"]}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    args = ["--store", str(store.root), "research-preflight", "--request", str(path)]
    assert main(args + ["--require-complete"]) == 0
    assert json.loads(capsys.readouterr().out) == run(ready).to_dict()
    for where, key in [(request, "as_of"), (request["inputs"][0], "execution_permission"),
                       (request["calendar"], "fallback")]:
        where[key] = True
        path.write_text(json.dumps(request))
        assert main(args) == 2
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "PREFLIGHT_ARGUMENT"
        where.pop(key)


def test_real_aq_priority_and_multistock_explicit_gaps(tmp_path, monkeypatch):
    location = os.environ.get("ASHARE_PREFLIGHT_EVIDENCE")
    if not location:
        pytest.skip("external real preflight inputs not supplied")
    root = Path(location)
    shutil.copytree(root / "inputs/bao", tmp_path / "bao")
    shutil.copytree(root / "inputs/prices", tmp_path / "prices")
    store = Store(tmp_path / "bao")
    request = json.loads((root / "request-multistock.json").read_text())
    for b in request["inputs"]:
        if b["price"]["kind"] == "research":
            b["price"]["store_path"] = str(tmp_path / "prices")
    before = hashes(tmp_path)
    monkeypatch.setattr(BaoStockSource, "fetch", lambda *a, **k: pytest.fail("network"))
    result = store.preflight_research_request(request)
    assert len(result.rows) == 9 and not result.research_values_complete
    assert facts(result.rows[1])["amount"].value == "739740988.4800"
    assert facts(result.rows[0])["close"].value == "9.1600"
    assert facts(result.rows[1])["source_preclose"].value == "9.1600"
    assert all(r.research_values_complete for r in result.rows[:3])
    for index in (4, 7):
        assert facts(result.rows[index])["amount"].state == "unknown"
        assert facts(result.rows[index])["amount"].reason == "UNSUPPORTED_FIELD"
    aq = dict(request, inputs=request["inputs"][:1])
    single = store.preflight_research_request(aq, require_complete=True)
    assert single.to_dict()["execution_permission"] is False
    assert single.to_dict()["historical_pit"] is False
    assert all(facts(r)["owner_execution_plan"].state == "unsupported" for r in single.rows)
    assert hashes(tmp_path) == before
    # The source field is available, but receipt completion and historical PIT remain unknown.
    assert facts(single.rows[1])["source_preclose"].evidence["quality"]["historical_pit"] is False
    # Choosing Sina explicitly keeps its missing amount even though Bao is present.
    aq["inputs"][0] = dict(aq["inputs"][0], price=request["inputs"][1]["price"])
    sina = store.preflight_research_request(aq)
    assert facts(sina.rows[1])["amount"].reason == "UNSUPPORTED_FIELD"
    assert sina.report_id != single.report_id
    # Actual recovery failures remain hard failures, not successful empty calendars.
    failed_ids = json.loads((root / "input-preparation.json").read_text())["recovery_failed_captures"]
    for cid in failed_ids:
        with raises_code("SOURCE_REQUEST_FAILED"):
            store.preflight_research_request(dict(request, calendar={"kind": "baostock", "version_id": cid}))


@pytest.mark.parametrize("slot", ["calendar", "price", "status", "preclose"])
@pytest.mark.parametrize("bad_path,code", [
    ("a\0b", "PREFLIGHT_ARGUMENT"),
    ("a\ud800b", "PREFLIGHT_ARGUMENT"),
    ("x" * 10000, "PREFLIGHT_SOURCE_IO_ERROR"),
], ids=["nul", "unencodable", "too-long"])
def test_path_errors_match_library_and_cli(ready, tmp_path, capsys, slot, bad_path, code):
    store, binding, calendar = ready
    request = {"inputs": [binding.to_dict()], "calendar": calendar.to_dict(),
               "trading_dates": ["2024-01-06"]}
    target = request["calendar"] if slot == "calendar" else request["inputs"][0][slot]
    target["store_path"] = bad_path
    before = hashes(store.root)
    with raises_code(code):
        store.preflight_research_request(request)
    path = tmp_path / "invalid-path-request.json"
    path.write_text(json.dumps(request))
    assert main(["--store", str(store.root), "research-preflight", "--request", str(path)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == code
    assert hashes(store.root) == before
