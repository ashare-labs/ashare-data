"""Available observations survive unrelated gaps; failures remain explicit."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data import Client, DataError, QueryLimits, Store, get_price
from ashare_data import transport
from ashare_data.cli import main


def bars():
    return json.dumps([dict(day=day, open="10", high="11", low="9", close="10.5", volume="100")
                       for day in ("2020-01-02", "2020-01-03")]).encode()


def test_count_and_start_gaps_return_actual_rows_only(monkeypatch):
    monkeypatch.setattr(transport, "public_read", lambda *_: bars())
    for request in (dict(count=3), dict(start_date="2019-12-01", end_date="2020-01-03")):
        with pytest.raises(DataError) as error:
            get_price("600000.XSHG", **request)
        assert error.value.code == "COVERAGE_INCOMPLETE"
        result = get_price("600000.XSHG", allow_partial=True, **request)
        assert len(result) == 2 and result.volume.tolist() == [100, 100]
        assert result.attrs["partial_result"]
        state = result.attrs["query_status"][0]
        assert state["status"] == "partial" and state["returned_count"] == 2
        assert state["issues"][0]["code"] == "COVERAGE_INCOMPLETE"
        assert result.attrs["point_in_time_verified"] is False
        assert result.attrs["provenance"][0]["coverage_report"]["status"] == "unknown"


@pytest.mark.parametrize("code", ["NETWORK_ERROR", "SOURCE_NO_DATA", "SOURCE_FIELD_MISSING"])
def test_one_security_failure_does_not_discard_others(monkeypatch, code):
    calls = []
    def read(url, *_):
        calls.append(url)
        if "sz000001" in url:
            raise DataError(code, "source unavailable", {"source": "synthetic"})
        return bars()
    monkeypatch.setattr(transport, "public_read", read)
    result = get_price(["600000.XSHG", "000001.XSHE", "600036.XSHG"], count=2, allow_partial=True)
    assert len(calls) == 3 and len(result) == 4
    assert set(result.code) == {"600000.XSHG", "600036.XSHG"}
    assert [s["status"] for s in result.attrs["query_status"]] == ["ok", "unavailable", "ok"]
    assert result.attrs["query_status"][1]["error"]["code"] == code
    assert len(result.attrs["provenance"]) == 2


@pytest.mark.parametrize("code", ["RATE_LIMITED", "SOURCE_ACCESS_DENIED"])
def test_source_stop_retains_prior_results_and_makes_no_further_requests(monkeypatch, code):
    calls = []
    def read(url, *_):
        calls.append(url)
        if len(calls) == 2:
            raise DataError(code, "stop")
        return bars()
    monkeypatch.setattr(transport, "public_read", read)
    result = get_price(["600000.XSHG", "000001.XSHE", "600036.XSHG"], count=2, allow_partial=True)
    assert len(calls) == 2 and len(result) == 2
    assert [s["status"] for s in result.attrs["query_status"]] == ["ok", "unavailable", "not_attempted"]


def test_empty_result_has_schema_and_error(monkeypatch):
    monkeypatch.setattr(transport, "public_read", lambda *_: b"[]")
    result = get_price("600000.XSHG", count=1, allow_partial=True)
    assert result.empty and result.index.name == "time"
    assert list(result.columns) == ["open", "close", "high", "low", "volume"]
    assert result.attrs["query_status"][0]["error"]["code"] == "SOURCE_NO_DATA"
    assert result.attrs["partial_result"] and not result.attrs["provenance"]


@pytest.mark.parametrize("code", ["OBSERVATION_CONFLICT", "CACHE_CORRUPT", "SOURCE_SCHEMA_ERROR"])
def test_partial_never_hides_conflicting_or_corrupt_evidence(monkeypatch, code):
    def fail(*_):
        raise DataError(code, "invalid evidence")
    monkeypatch.setattr(transport, "public_read", fail)
    with pytest.raises(DataError) as error:
        get_price("600000.XSHG", count=1, allow_partial=True)
    assert error.value.code == code


@pytest.mark.parametrize("flag", ["require_complete", "require_fresh", "require_final", "require_tradable"])
def test_partial_does_not_weaken_requested_guarantees(monkeypatch, flag):
    monkeypatch.setattr(transport, "public_read", lambda *_: pytest.fail("invalid request used network"))
    with pytest.raises(DataError) as error:
        get_price("600000.XSHG", allow_partial=True, **{flag: True})
    assert error.value.code == "INVALID_REQUEST"


def test_local_budgets_can_exceed_demo_sizes_without_expanding_source_window(monkeypatch, tmp_path):
    calls = []
    def read(url, *_):
        calls.append(url)
        return bars()
    monkeypatch.setattr(transport, "public_read", read)
    securities = [f"6000{i:02d}.XSHG" for i in range(11)]
    limits = QueryLimits(max_securities=11, max_count=1500)
    client = Client(cache=tmp_path / "cache", query_limits=limits)
    result = client.get_price(securities, count=1500, allow_partial=True)
    assert len(result) == 22 and len(calls) == 11
    assert all(parse_qs(urlsplit(url).query)["datalen"] == ["1023"] for url in calls)
    frozen = client.at("2020-01-04T12:00:00+08:00", visibility="assumed")
    assert frozen.query_limits == limits
    monkeypatch.setattr(transport, "public_read", lambda *_: pytest.fail("frozen read used network"))
    assert len(frozen.get_price(securities, count=1500, allow_partial=True)) == 22


def test_sealed_sina_partial_results_and_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(transport, "public_read", lambda *_: bars())
    store = Store.init(tmp_path / "store")
    dataset = store.fetch_price("600000.XSHG", count=2)
    monkeypatch.setattr(transport, "public_read", lambda *_: pytest.fail("sealed read used network"))
    result = store.research(dataset).get_price(["600000.XSHG", "000001.XSHE"], count=3, allow_partial=True)
    assert len(result.data) == 2 and result.report["partial_result"]
    assert [s["status"] for s in result.report["query_status"]] == ["partial", "unavailable"]
    assert not result.report["network_used"]
    assert main(["--store", str(tmp_path / "store"), "research-price", "600000.XSHG", "--dataset", dataset,
                 "--count", "1500", "--max-count", "1500", "--allow-partial"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["report"]["query_status"][0]["status"] == "partial"


def test_legacy_bao_partial_results_keep_decimal_provenance(tmp_path):
    from test_research import payload, write
    store = Store.init(tmp_path / "store")
    dataset = store.import_research([write(tmp_path, payload())])
    result = store.research(dataset).get_price(["600000.XSHG", "000001.XSHE"], count=3, allow_partial=True)
    assert len(result.data) == 2
    assert [s["status"] for s in result.report["query_status"]] == ["partial", "unavailable"]
    assert not result.report["network_used"]
    assert len(result.report["provenance"][0]["records"]) == 2


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, float("inf")])
def test_query_budget_must_be_positive_integer(value):
    with pytest.raises(DataError):
        QueryLimits(max_count=value)


def test_explicit_transport_budget_is_not_a_freshness_guarantee(monkeypatch, tmp_path):
    calls = []
    def read(url, timeout):
        calls.append(timeout)
        return bars()
    monkeypatch.setattr(transport, "public_read", read)
    client = Client(cache=tmp_path / "cache", timeout=90, cache_ttl=7 * 86400)
    result = client.get_price("600000.XSHG", count=2)
    cached = client.get_price("600000.XSHG", count=2)
    assert calls == [90]
    assert cached.attrs["provenance"][0]["cache_hit"]
    assert result.attrs["provenance"][0]["freshness"]["admissible"] is False
    with pytest.raises(DataError) as error:
        client.get_price("600000.XSHG", count=2, require_fresh=True)
    assert error.value.code == "FRESHNESS_UNKNOWN"


@pytest.mark.parametrize("kwargs", [{"timeout": True}, {"timeout": 0}, {"timeout": float("inf")},
                                   {"timeout": 10**400}, {"cache_ttl": 10**400},
                                   {"cache_ttl": True}, {"cache_ttl": -1}, {"cache_ttl": float("nan")}])
def test_transport_budgets_stay_explicit_finite_numbers(kwargs):
    with pytest.raises(DataError):
        Client(**kwargs)
