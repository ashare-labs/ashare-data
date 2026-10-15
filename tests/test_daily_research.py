"""Clock/integrity regressions; synthetic contract tests never substitute market inputs."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json

import pytest

from ashare_data import DailyResearchView, DataError
from ashare_data.daily_research import ROLE, SCHEMA


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_package(root, data):
    root.mkdir(exist_ok=True)
    (root / "source.txt").write_text("synthetic protocol evidence; not market observations\n")
    (root / "data.json").write_text(json.dumps(data))
    refs = [
        dict(path=p.name, bytes=p.stat().st_size, sha256=digest(p.read_bytes()))
        for p in [root / "source.txt", root / "data.json"]
    ]
    binding = dict(
        schema=SCHEMA,
        role=ROLE,
        historical_pit=False,
        execution_permission=False,
        producer={"name": "test-only"},
        data="data.json",
        files=refs,
    )
    raw = json.dumps(binding).encode()
    (root / "binding.json").write_bytes(raw)
    return DailyResearchView(root / "binding.json", expected_binding_sha256=digest(raw))


def test_shorter_warmup_is_valid_and_history_still_checks_actual_requirement(tmp_path, data):
    window = data["windows"][0]
    window["sessions"] = window["sessions"][118:]
    window["warmup_start"] = window["sessions"][0]
    window["warmup_sessions"] = 2
    view = write_package(tmp_path, data)
    result = view.history("w", "000001.XSHE", as_of=window["start"] + "T09:00:00+08:00", count=2)
    assert [r["date"] for r in result["rows"]] == window["sessions"][:2]
    with pytest.raises(DataError) as error:
        view.history("w", "000001.XSHE", as_of=window["start"] + "T09:00:00+08:00", count=3)
    assert error.value.code == "DAILY_RESEARCH_WARMUP"


@pytest.mark.parametrize("warmup", [0, True, 9999])
def test_invalid_warmup_has_structured_failure(tmp_path, data, warmup):
    data["windows"][0]["warmup_sessions"] = warmup
    with pytest.raises(DataError) as error:
        write_package(tmp_path, data)
    assert error.value.code == "DAILY_RESEARCH_WARMUP"


@pytest.fixture
def data():
    # Explicitly synthetic session sequence for validation/clock tests only.
    ds = [(date(2020, 1, 1) + timedelta(days=i)).isoformat() for i in range(125)]
    security = "000001.XSHE"
    bars = []
    for i, d in enumerate(ds):
        close = str(Decimal(10) + Decimal(i) / 100)
        state = dict(
            status="modeled",
            suspended=False,
            upper_limit="13",
            lower_limit="8",
            reference_price="10",
            assumption_ids=["synthetic_only"],
            historical_pit=False,
            source_historical_available_at=None,
            model_available_at=d + "T09:00:00+08:00",
        )
        bars.append(
            dict(
                security=security,
                date=d,
                open=close,
                high=close,
                low=close,
                close=close,
                volume="12345",
                money=str(Decimal(close) * 12345),
                traded=True,
                suspended=False,
                origin="source_observed",
                source_ref="source.txt",
                historical_available_at=None,
                research_state=state,
                suspension_origin="synthetic_only",
            )
        )
    factors = [
        dict(date=ds[0], factor="1", source_ref="source.txt"),
        dict(date=ds[119], factor="2", source_ref="source.txt"),
        dict(date=ds[121], factor="1000", source_ref="source.txt"),
    ]
    action = dict(
        security=security,
        announcement_date=ds[120],
        record_date=ds[121],
        ex_date=ds[122],
        cash_per_share="1",
        payment_date=ds[122],
        source_ref="source.txt",
        historical_available_at=None,
    )
    return dict(
        schema="ad.daily-research-data.v1",
        securities=[security],
        units=dict(price="CNY/share", volume="share", money="CNY"),
        bars=bars,
        windows=[
            dict(
                id="w",
                start=ds[120],
                end=ds[123],
                warmup_start=ds[0],
                warmup_sessions=120,
                next_session=ds[124],
                sessions=ds,
            )
        ],
        factors={security: factors},
        actions=[action],
        listing=[],
        assumptions=["synthetic_only"],
        limitations=["not market data"],
        quality={},
        action_coverage={security: {"certified": False}},
    )


def at(data, index, clock="09:00:00+08:00"):
    return data["windows"][0]["sessions"][index] + "T" + clock


def test_warmup_cutoff_dates_and_mutation_isolation(tmp_path, data):
    view = write_package(tmp_path, data)
    r = view.history(
        "w", "000001.XSHE", as_of=at(data, 120), count=120, fields=["close", "volume", "money"]
    )
    assert [x["date"] for x in r["rows"]] == data["windows"][0]["sessions"][:120]
    assert r["rows"][-1]["volume"] == "12345"
    assert Decimal(r["rows"][-1]["money"]) == Decimal("11.19") * 12345
    r["rows"][-1]["close"] = "9999"
    exported = view.read_window("w", role=ROLE)
    exported["bars"][0]["close"] = "9999"
    assert (
        view.history("w", "000001.XSHE", as_of=at(data, 120), count=1)["rows"][0]["close"]
        == "11.19"
    )
    assert (
        view.history("w", "000001.XSHE", as_of=at(data, 120, "15:00:00+08:00"), count=1)["rows"][0][
            "close"
        ]
        == "11.2"
    )
    with pytest.raises(DataError, match="Not enough"):
        view.history("w", "000001.XSHE", as_of=at(data, 120), count=121)


def test_future_factor_never_affects_pre_or_post(tmp_path, data):
    view = write_package(tmp_path, data)
    pre = view.history(
        "w",
        "000001.XSHE",
        as_of=at(data, 120),
        count=2,
        adjustment="pre",
        fields=["close", "volume"],
    )
    assert Decimal(pre["rows"][0]["close"]) == Decimal("11.18") / 2
    assert pre["rows"][-1]["volume"] == "12345"
    assert len(pre["report"]["factor_events_through_basis"]) == 2
    post = view.history("w", "000001.XSHE", as_of=at(data, 120), count=1, adjustment="post")
    assert Decimal(post["rows"][0]["close"]) == Decimal("22.38")
    with pytest.raises(DataError) as e:
        view.history(
            "w",
            "000001.XSHE",
            as_of=at(data, 120),
            count=1,
            adjustment="pre",
            basis_date=data["bars"][121]["date"],
        )
    assert e.value.code == "DAILY_RESEARCH_VISIBILITY"


def test_current_open_close_and_state_are_separate(tmp_path, data):
    view = write_package(tmp_path, data)
    with pytest.raises(DataError):
        view.opening("w", "000001.XSHE", as_of=at(data, 120))
    op = view.opening("w", "000001.XSHE", as_of=at(data, 120, "09:30:00+08:00"))
    assert op["open"] == "11.2" and not set(op) & {"close", "volume", "high", "money"}
    with pytest.raises(DataError):
        view.closing("w", "000001.XSHE", as_of=at(data, 120, "14:59:59+08:00"))
    state = view.state("w", "000001.XSHE", as_of=at(data, 120))
    assert state["observed"]["suspended"] is None and state["observed"]["upper_limit"] is None
    assert state["model"] is None
    assert (
        view.state("w", "000001.XSHE", as_of=at(data, 120), accept_model=True)["model"]["status"]
        == "modeled"
    )
    with pytest.raises(DataError):
        view.state("w", "000001.XSHE", as_of=at(data, 120, "08:59:59+08:00"), accept_model=True)


def test_action_date_is_not_a_past_publication_clock(tmp_path, data):
    data["action_coverage"]["000001.XSHE"]["factor_step_crosscheck"] = {
        "future": {"ledger_dates": [data["actions"][0]["ex_date"]]}
    }
    view = write_package(tmp_path, data)
    result = view.corporate_actions("w", "000001.XSHE", as_of=at(data, 120))
    assert not result["rows"]
    assert "factor_step_crosscheck" not in result["coverage"]
    assert len(view.corporate_actions("w", "000001.XSHE", as_of=at(data, 121))["rows"]) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "fractional_volume",
        "nan",
        "bad_units",
        "short_warmup",
        "unbound_source",
        "promoted_pit",
        "bad_state",
    ],
)
def test_reject_corrupt_coverage_and_units(tmp_path, data, mutation):
    if mutation == "missing":
        data["bars"].pop(5)
    if mutation == "duplicate":
        data["bars"].append(deepcopy(data["bars"][0]))
    if mutation == "fractional_volume":
        data["bars"][0]["volume"] = "1.5"
    if mutation == "nan":
        data["bars"][0]["close"] = "NaN"
    if mutation == "bad_units":
        data["units"]["volume"] = "lot"
    if mutation == "short_warmup":
        data["windows"][0]["warmup_sessions"] = 119
    if mutation == "unbound_source":
        data["bars"][0]["source_ref"] = "missing.csv"
    if mutation == "promoted_pit":
        data["bars"][0]["historical_available_at"] = "2020-01-01T09:00:00+08:00"
    if mutation == "bad_state":
        data["bars"][0]["research_state"]["status"] = "source_observed"
    with pytest.raises(DataError):
        write_package(tmp_path, data)


def test_halt_requires_evidence_and_adjacent_close(tmp_path, data):
    r = data["bars"][20]
    r.update(
        traded=False,
        suspended=True,
        origin="documented_halt_carry",
        evidence=["source.txt"],
        volume="0",
        money="0",
        carry_from=data["bars"][19]["date"],
    )
    for f in ("open", "high", "low", "close"):
        r[f] = data["bars"][19]["close"]
    r["research_state"]["suspended"] = True
    view = write_package(tmp_path, data)
    assert view.read_window("w", role=ROLE)["bars"][20]["origin"] == "documented_halt_carry"
    r["carry_from"] = data["bars"][18]["date"]
    with pytest.raises(DataError):
        write_package(tmp_path, data)


def test_integrity_and_root_confinement(tmp_path, data):
    view = write_package(tmp_path, data)
    (tmp_path / "source.txt").write_text("tampered")
    with pytest.raises(DataError) as e:
        DailyResearchView(tmp_path / "binding.json", expected_binding_sha256=view.binding_sha256)
    assert e.value.code == "DAILY_RESEARCH_INTEGRITY"
    write_package(tmp_path, data)
    b = json.loads((tmp_path / "binding.json").read_text())
    b["files"][0]["path"] = "../outside"
    raw = json.dumps(b).encode()
    (tmp_path / "binding.json").write_bytes(raw)
    with pytest.raises(DataError) as e:
        DailyResearchView(tmp_path / "binding.json", expected_binding_sha256=digest(raw))
    assert e.value.code == "DAILY_RESEARCH_FILE"
