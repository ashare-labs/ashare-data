"""Explicit clock/source fixtures. These do not count as actual calendar days."""

from datetime import datetime
import json

import pytest


from ashare_data.observations import phases as phase
from ashare_data.core.model import DataError, canonical
from test_action_scope import issuer_sources

pytestmark = pytest.mark.usefixtures("synthetic_action_baseline")

DAY = "2026-10-14"


def sources(monkeypatch, *, opening=False, damage=None):
    _, original = issuer_sources(monkeypatch, day=DAY)
    clock = DAY + ("T09:31:00+08:00" if opening else "T08:55:00+08:00")
    monkeypatch.setattr(phase, "now", lambda: datetime.fromisoformat(clock))

    calls = []

    def fetch(url, referer):
        calls.append(url)
        if "yunhq.sse.com.cn" in url:
            code = url.rsplit("/", 1)[1].split("?")[0]
            ref = {"600000": 10, "600036": 20, "601318": 30}[code]
            value = dict(
                date=20261014,
                time=93058 if opening else 83014,
                total=1,
                begin=0,
                end=1,
                list=[
                    [
                        code,
                        "测试证券",
                        ref,
                        ref,
                        ref,
                        ref * 1.1,
                        ref * 0.9,
                        "T111    " if opening else "S111    ",
                    ]
                ],
            )
            record = value["list"][0]
            if damage == "old_limits":
                value["date"] = 20261013
            if damage == "future_limits":
                value["time"] = 120000
            if damage == "wrong_identity":
                record[0] = "999999"
            if damage == "wrong_limits":
                record[5] = 999
            if damage == "sse_halt":
                record[7] = "P111    "
            if damage == "sse_unknown":
                record[7] = "UNKNOWN"
            if damage == "sse_unreviewed_phase":
                record[7] = "T011"
            if damage == "sse_incomplete":
                value["total"] = 2
            if damage == "sse_stale_opening":
                value["time"] = 83014
            if damage == "sse_open_conflict":
                record[3] = ref + 1
            if damage == "limits_offline":
                raise ConnectionError("controlled official limits disconnect")
            return canonical(value)
        raw = original(url, referer)
        if "hq.sinajs.cn" in url:
            text = raw.decode("gb18030")
            if opening:
                text = text.replace("08:54:58", "09:30:58")
            if not opening or damage == "stale_quote":
                text = text.replace(DAY, "2026-10-13")
            if damage == "live_halt":
                text = text.replace("2026-10-13", DAY).replace(',00"', ',02"')
            if damage == "quote_offline":
                raise ConnectionError("controlled quote disconnect")
            if damage in {"zero_open", "infinite_open"}:
                text = text.replace(
                    "测试证券,10,", "测试证券," + ("0," if damage == "zero_open" else "Infinity,")
                )
            return text.encode("gb18030")
        value = json.loads(raw)
        if "push2.eastmoney.com" in url:
            at = DAY + "T08:30:14+08:00"
            if damage == "old_limits":
                at = "2026-10-13T15:00:00+08:00"
            if damage == "future_limits":
                at = DAY + "T12:00:00+08:00"
            value["data"]["f86"] = int(datetime.fromisoformat(at).timestamp())
            if damage == "wrong_identity":
                value["data"]["f57"] = "999999"
            if damage == "wrong_limits":
                value["data"]["f51"] = 999
            if damage == "limits_offline":
                raise ConnectionError("controlled limits disconnect")
        if "GW_PL_JYTS_TFPXX" in url:
            if damage == "scheduled_halt":
                value.update(result=[{"productCode": "600000"}], pageHelp={"total": 1})
            if damage == "incomplete_sse":
                value["pageHelp"]["total"] = 1
        if "RPT_CUSTOM_SUSPEND_DATA_INTERFACE" in url:
            if damage == "provider_halt":
                value["result"].update(data=[{"SECURITY_CODE": "600000"}], count=1)
            if damage == "incomplete_em":
                value["result"]["count"] = 1
        if "domesticIndicator=S" in url and damage == "st":
            value.update(result=[{"INSTRUMENT_ID": "600000"}], pageHelp={"total": 1})
        if "security/ann" in url and damage == "new_notice":
            value["data"]["total_hits"] += 1
        return canonical(value)

    monkeypatch.setattr(phase, "fetch", fetch)
    return calls


def capture(tmp_path, *, opening=False, schema=phase.CONDITIONAL_SCHEMA):
    attempt = phase.capture(tmp_path, "opening" if opening else "admission", DAY, schema=schema)
    return phase.read(tmp_path, attempt["capture_id"])


def test_preparation_preserves_unknown_live_state_and_never_grants_execution(tmp_path, monkeypatch):
    calls = sources(monkeypatch)
    p = capture(tmp_path)
    a = p["assessment"]
    assert a["preparation_ready"] and not a["execution_ready"] and not a["execution_permission"]
    assert all(r["suspended"] is None and r["scheduled_suspension"] is False for r in a["rows"])
    assert all(r["evidence"]["limits_semantics"]["effective_trade_day"] == DAY for r in a["rows"])
    assert calls[-1] == p["sources"]["sina_quotes"]["url"]
    # Same original v4 semantics still reject the realistic pre-open input.
    old = capture(tmp_path / "old", schema=phase.SCHEMA)
    assert not old["assessment"]["execution_permission"]
    assert any("CURRENT_LIMITS_UNVERIFIED" in x for x in old["assessment"]["blockers"])
    assert any("CURRENT_SUSPENSION_UNVERIFIED" in x for x in old["assessment"]["blockers"])


def test_opening_refreshes_all_sources_and_resolves_live_state(tmp_path, monkeypatch):
    sources(monkeypatch, opening=True)
    p = capture(tmp_path, opening=True)
    a = p["assessment"]
    assert a["execution_ready"] and a["opening_ready"] and not a["preparation_ready"]
    assert not a["execution_permission"] and len(p["sources"]) == 27
    assert all(r["suspended"] is False and r["open"] for r in a["rows"])


@pytest.mark.parametrize("opening", [False, True])
@pytest.mark.parametrize(
    "damage",
    [
        "old_limits",
        "future_limits",
        "wrong_identity",
        "wrong_limits",
        "limits_offline",
        "scheduled_halt",
        "provider_halt",
        "incomplete_sse",
        "incomplete_em",
        "st",
        "new_notice",
        "live_halt",
        "sse_halt",
        "sse_unknown",
        "sse_incomplete",
    ],
)
def test_phase_specific_contract_retains_material_refusals(tmp_path, monkeypatch, opening, damage):
    sources(monkeypatch, opening=opening, damage=damage)
    a = capture(tmp_path, opening=opening)["assessment"]
    assert a["blockers"] and not a["preparation_ready"] and not a["execution_ready"]


@pytest.mark.parametrize("damage", ["stale_quote", "quote_offline", "zero_open", "infinite_open"])
def test_unknown_quote_only_defers_preparation_but_blocks_opening(tmp_path, monkeypatch, damage):
    sources(monkeypatch, damage=damage)
    a = capture(tmp_path)["assessment"]
    assert a["preparation_ready"] and not a["execution_ready"]
    sources(monkeypatch, opening=True, damage=damage)
    a = capture(tmp_path / "open", opening=True)["assessment"]
    assert a["blockers"] and not a["execution_ready"]


def test_old_packet_cannot_be_relabelled_as_conditional(tmp_path, monkeypatch):
    sources(monkeypatch)
    p = capture(tmp_path, schema=phase.SCHEMA)
    p["schema"] = phase.CONDITIONAL_SCHEMA
    raw = canonical(p)
    key = phase.sha(raw)
    phase.immutable_write(tmp_path / "packets" / (key + ".json"), raw)
    with pytest.raises(DataError):
        phase.read(tmp_path, key)


@pytest.mark.parametrize(
    "damage", ["sse_stale_opening", "sse_open_conflict", "sse_unreviewed_phase"]
)
def test_opening_needs_current_official_phase_and_matching_price(tmp_path, monkeypatch, damage):
    sources(monkeypatch, opening=True, damage=damage)
    a = capture(tmp_path, opening=True)["assessment"]
    assert a["blockers"] and not a["execution_ready"]


def test_no_late_conditional_preparation_or_opening(tmp_path, monkeypatch):
    sources(monkeypatch)
    monkeypatch.setattr(phase, "now", lambda: datetime.fromisoformat(DAY + "T10:00:00+08:00"))
    monkeypatch.setattr(phase, "fetch", lambda *args: pytest.fail("late capture used network"))
    for kind in ("admission", "opening"):
        result = phase.capture(tmp_path, kind, DAY, schema=phase.CONDITIONAL_SCHEMA)
        assert (
            result["capture_id"] is None and result["error"]["code"] == "PHASE_OUTSIDE_REAL_WINDOW"
        )
