"""Query budgets and partial status include the same synthetic coverage evidence."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data import Client, CoverageContract, DataError, QueryLimits, Store
from ashare_data.providers import http as transport
from ashare_data.research.datasets import save_client
import test_data_contracts as helpers

clock = helpers.clock
SEC = helpers.SEC


def frozen_query(tmp_path, monkeypatch, mode, *, semantics="verified"):
    facts = helpers.profile()
    facts["label_semantics"] = semantics
    rows = helpers.payload(["2026-09-30 09:31:00", "2026-09-30 09:32:00"])
    calls = []

    def public_read(url, *_):
        calls.append(url)
        return json.dumps(rows).encode()

    monkeypatch.setattr(transport, "public_read", public_read)
    limits = QueryLimits(max_count=1500)
    client = Client(
        cache=tmp_path / "cache", coverage_contract=CoverageContract(facts), query_limits=limits
    )
    client.get_price(SEC, frequency="1m", count=2, end_date="2026-09-30 09:32")
    if mode == "sealed":
        store = Store.init(tmp_path / "store")
        view = store.research(save_client(client, store))

        def read(**kwargs):
            result = view.get_price(SEC, frequency="1m", query_limits=limits, **kwargs)
            assert not result.report["network_used"]
            return result.data, result.report
    else:
        view = client.at("2026-10-01T00:00:00+08:00", visibility="assumed")

        def read(**kwargs):
            result = view.get_price(SEC, frequency="1m", **kwargs)
            return result, result.attrs

    monkeypatch.setattr(
        transport, "public_read", lambda *_: pytest.fail("frozen read used network")
    )
    return read, calls


@pytest.mark.parametrize("mode", ["direct", "sealed"])
def test_explicit_count_budget_reaches_coverage_without_skipping_it(
    clock, tmp_path, monkeypatch, mode
):
    read, calls = frozen_query(tmp_path, monkeypatch, mode)
    frame, report = read(count=1500, end_date="2026-09-30 09:35", allow_partial=True)
    assert len(frame) == 2 and frame.volume.tolist() == [100, 100]
    assert report["partial_result"] and report["query_status"][0]["status"] == "partial"
    proof = report["provenance"][0]["coverage_report"]
    assert proof["requested_count"] == 1500 and proof["expected_count"] == 5
    assert len(proof["missing"]) == 3 and not proof["complete"]
    assert proof["contract_id"] == CoverageContract(helpers.profile()).contract_id
    assert not report["point_in_time_verified"]
    assert len(calls) == 1 and parse_qs(urlsplit(calls[0]).query)["datalen"] == ["1023"]
    with pytest.raises(DataError) as exc:
        read(count=1501, end_date="2026-09-30 09:35", allow_partial=True)
    assert exc.value.code == "INVALID_COUNT"


@pytest.mark.parametrize("mode", ["direct", "sealed"])
@pytest.mark.parametrize("allow_partial", [False, True])
def test_known_coverage_gaps_reach_status_and_still_fail_complete_gate(
    clock,
    tmp_path,
    monkeypatch,
    mode,
    allow_partial,
):
    read, _ = frozen_query(tmp_path, monkeypatch, mode)
    request = dict(start_date="2026-09-30 09:31", end_date="2026-09-30 09:35")
    frame, report = read(**request, allow_partial=allow_partial)
    assert len(frame) == 2 and report["partial_result"]
    state = report["query_status"][0]
    assert state["status"] == "partial" and state["returned_count"] == 2
    proof = report["provenance"][0]["coverage_report"]
    assert proof["status"] == "incomplete" and len(proof["missing"]) == 3
    assert state["issues"] == [
        {
            "code": "COVERAGE_INCOMPLETE",
            "reason": "declared_coverage_incomplete",
            "coverage_report": proof,
        }
    ]
    with pytest.raises(DataError) as exc:
        read(**request, require_complete=True)
    assert exc.value.code == "COVERAGE_INCOMPLETE"
    assert exc.value.details["missing"] == proof["missing"]


@pytest.mark.parametrize("mode", ["direct", "sealed"])
@pytest.mark.parametrize("semantics", ["verified", "unverified"])
def test_unknown_coverage_is_not_a_new_research_gate(clock, tmp_path, monkeypatch, mode, semantics):
    read, _ = frozen_query(tmp_path, monkeypatch, mode, semantics=semantics)
    request = dict(start_date="2026-09-30 09:31", end_date="2026-09-30 09:32")
    frame, report = read(**request, allow_partial=True)
    assert len(frame) == 2 and not report["partial_result"]
    assert report["query_status"] == [
        {"security": SEC, "status": "ok", "returned_count": 2, "issues": []}
    ]
    proof = report["provenance"][0]["coverage_report"]
    assert proof["status"] == ("complete" if semantics == "verified" else "unknown")
    if semantics == "unverified":
        with pytest.raises(DataError) as exc:
            read(**request, require_complete=True)
        assert exc.value.code == "COVERAGE_UNKNOWN"


def test_direct_assessor_keeps_default_budget_and_accepts_explicit_budget():
    contract = CoverageContract(helpers.profile())
    args = (
        SEC,
        ["2026-09-30 09:31:00", "2026-09-30 09:32:00"],
        "2026-09-30 09:31",
        "2026-09-30 09:35",
    )
    with pytest.raises(DataError) as exc:
        contract.assess(*args, count=1001)
    assert exc.value.code == "INVALID_COUNT"
    proof = contract.assess(*args, count=1500, query_limits=QueryLimits(max_count=1500))
    assert not proof["complete"] and len(proof["missing"]) == 3
    with pytest.raises(DataError) as exc:
        contract.assess(*args, count=1501, query_limits=QueryLimits(max_count=1500))
    assert exc.value.code == "INVALID_COUNT"
    with pytest.raises(DataError) as exc:
        contract.assess(*args, count=1500, query_limits={"max_count": 1500})
    assert exc.value.code == "INVALID_REQUEST"
