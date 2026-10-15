"""Known omissions and unknown scope coexist without inventing coverage evidence."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data import Client, CoverageContract, DataError, Store, transport
from ashare_data.research import save_client
import test_data_contracts as helpers

clock = helpers.clock
SEC = helpers.SEC
SEC_B = "000001.XSHE"
SEC_C = "600036.XSHG"
AS_OF = "2026-10-03T00:00:00+08:00"


def fixed_query(tmp_path, monkeypatch, mode, facts, labels_by_security):
    by_symbol = {("sh" if code.endswith("XSHG") else "sz") + code[:6]: labels
                 for code, labels in labels_by_security.items()}

    def public_read(url, *_):
        symbol = parse_qs(urlsplit(url).query)["symbol"][0]
        return json.dumps(helpers.payload(by_symbol[symbol])).encode()

    monkeypatch.setattr(transport, "public_read", public_read)
    client = Client(cache=tmp_path / "cache", coverage_contract=CoverageContract(facts))
    client.get_price(list(labels_by_security), frequency="1m", count=1, end_date="2026-10-02")
    if mode == "sealed":
        store = Store.init(tmp_path / "store")
        view = store.research(save_client(client, store)).at(AS_OF, visibility="assumed")

        def read(securities, **kwargs):
            result = view.get_price(securities, frequency="1m", **kwargs)
            assert not result.report["network_used"]
            return result.data, result.report
    else:
        view = client.at(AS_OF, visibility="assumed")

        def read(securities, **kwargs):
            result = view.get_price(securities, frequency="1m", **kwargs)
            return result, result.attrs

    monkeypatch.setattr(transport, "public_read", lambda *_: pytest.fail("fixed query used network"))
    return read


def labels(day, minutes=range(31, 36)):
    return [f"{day} 09:{minute}:00" for minute in minutes]


@pytest.mark.parametrize("mode", ["direct", "sealed"])
@pytest.mark.parametrize("allow_partial", [False, True])
@pytest.mark.parametrize("unknown_day", ["2026-09-28", "2026-09-30", "2026-10-01"],
                         ids=["unknown_before", "unknown_between", "unknown_after"])
def test_multisecurity_multiday_reports_keep_known_gaps_and_unknown_scope_separate(
    clock, tmp_path, monkeypatch, mode, allow_partial, unknown_day,
):
    days = ("2026-09-29", "2026-10-01" if unknown_day == "2026-09-30" else "2026-09-30")
    facts = helpers.profile(dates=days)
    original_states = list(facts["statuses"])
    facts["statuses"] += [{**state, "security": SEC_B} for state in original_states]
    facts["statuses"] += [{**state, "security": SEC_C,
                           "state": "unknown" if state["date"] == days[0] else "trading"}
                          for state in original_states]
    actual = {
        SEC: labels(days[0], (31, 32)) + labels(days[1], (31, 32)),
        SEC_B: labels(days[0]) + labels(days[1]),
        SEC_C: labels(days[0], (31, 32)) + labels(days[1]),
    }
    read = fixed_query(tmp_path, monkeypatch, mode, facts, actual)
    bounds = dict(start_date=min(*days, unknown_day) + " 09:31",
                  end_date=max(*days, unknown_day) + " 09:35")
    for order in ([SEC, SEC_B, SEC_C], [SEC_C, SEC_B, SEC]):
        frame, report = read(order, **bounds, allow_partial=allow_partial)
        assert len(frame) == sum(map(len, actual.values()))
        assert frame.groupby("code").size().to_dict() == {sec: len(rows) for sec, rows in actual.items()}
        assert report["partial_result"] and not report["point_in_time_verified"]
        statuses = {row["security"]: row for row in report["query_status"]}
        proofs = {row["security"]: row["coverage_report"] for row in report["provenance"]}
        assert list(statuses) == order
        for sec in order:
            proof, status = proofs[sec], statuses[sec]
            assert proof["status"] == "unknown" and not proof["complete"]
            assert {"date": unknown_day, "code": "CALENDAR_UNKNOWN"} in proof["unknown"]
            assert proof["known_incomplete"] is (sec == SEC)
            assert status["status"] == ("partial" if sec == SEC else "ok")
            assert status["returned_count"] == len(actual[sec])
            if sec == SEC:
                expected = [f"{day}T09:{minute}:00+08:00" for day in days for minute in (33, 34, 35)]
                assert [gap["label"] for gap in proof["missing"]] == expected
                assert status["issues"] == [{"code": "COVERAGE_INCOMPLETE",
                                            "reason": "declared_coverage_incomplete", "coverage_report": proof}]
            else:
                assert not proof["missing"] and not status["issues"]
        assert {"date": days[0], "code": "TRADING_STATUS_UNKNOWN"} in proofs[SEC_C]["unknown"]
    # A partial sibling must not taint a later query of the unknown-only securities.
    frame, report = read([SEC_B, SEC_C], **bounds, allow_partial=allow_partial)
    assert len(frame) == len(actual[SEC_B]) + len(actual[SEC_C]) and not report["partial_result"]
    with pytest.raises(DataError) as exc:
        read([SEC, SEC_B], **bounds, require_complete=True)
    assert exc.value.code == "COVERAGE_UNKNOWN"
    assert exc.value.details["known_incomplete"] and exc.value.details["unknown"]


@pytest.mark.parametrize("mode", ["direct", "sealed"])
@pytest.mark.parametrize("uncertainty", ["label_semantics", "future_calendar", "future_state", "unknown_state"])
def test_unproven_grid_cannot_be_promoted_to_confirmed_missing(
    clock, tmp_path, monkeypatch, mode, uncertainty,
):
    facts = helpers.profile()
    if uncertainty == "label_semantics":
        facts["label_semantics"] = "unverified"
    elif uncertainty == "future_calendar":
        facts["calendar"][0]["available_at"] = "2026-10-04T00:00:00+08:00"
    elif uncertainty == "future_state":
        facts["statuses"][0]["available_at"] = "2026-10-04T00:00:00+08:00"
    else:
        facts["statuses"][0]["state"] = "unknown"
    read = fixed_query(tmp_path, monkeypatch, mode, facts, {SEC: labels("2026-09-30", (31, 32))})
    frame, report = read(SEC, start_date="2026-09-30 09:31", end_date="2026-10-01 09:35",
                         allow_partial=True)
    assert len(frame) == 2 and not report["partial_result"]
    proof = report["provenance"][0]["coverage_report"]
    assert proof["status"] == "unknown" and not proof["known_incomplete"]
    assert len(proof["missing"]) == (3 if uncertainty == "label_semantics" else 0)
    assert report["query_status"] == [{"security": SEC, "status": "ok", "returned_count": 2, "issues": []}]
