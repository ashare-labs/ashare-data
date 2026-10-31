"""Artificial source calendars, deliberately unrelated to any acceptance answer."""

from datetime import date
import hashlib
import json

import pytest

from ashare_data import BoundedCalendarView, DataError

ROLE = "engine_calendar_retrospective_research"


def write(path, body):
    path.write_text(json.dumps(body))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, mutate=None):
    flags = [
        dict(
            jyrq=f"2030-01-{d:02d}",
            jybz="1" if d in (1, 2, 3, 8, 9, 10) else "0",
            zrxh=(date(2030, 1, d).weekday() + 1) % 7 + 1,
        )
        for d in range(1, 32)
    ]
    if mutate:
        mutate(flags)
    source = tmp_path / "source.json"
    digest = write(source, dict(data=flags, nowdate="2031-02-03"))
    binding = dict(
        schema="ad.bounded-calendar-binding.v1",
        window_id="synthetic-holiday",
        security="000001.XSHE",
        market="XSHE",
        start_date="2030-01-02",
        end_date="2030-01-09",
        role=ROLE,
        historical_pit=False,
        strategy_visible=False,
        primary_source=dict(
            path="source.json",
            sha256=digest,
            kind="synthetic_fixture",
            requested_month="2030-01",
            url="fixture:no-network",
            captured_start="2031-02-03T00:00:00+00:00",
            captured_end="2031-02-03T00:00:01+00:00",
        ),
    )
    path = tmp_path / "binding.json"
    return path, binding, write(path, binding)


def read(path, digest, label="2030-01-03", **kwargs):
    return BoundedCalendarView(path, expected_binding_sha256=digest).read_calendar(
        kwargs.get("window", "synthetic-holiday"),
        kwargs.get("security", "000001.XSHE"),
        label,
        role=kwargs.get("role", ROLE),
    )


def test_source_flags_closed_dates_and_outer_neighbours(tmp_path):
    path, _, digest = fixture(tmp_path)
    row = read(path, digest)
    assert row["previous_session"] == "2030-01-02" and row["next_session"] == "2030-01-08"
    closed = read(path, digest, "2030-01-07")
    assert closed["is_open"] is False and closed["previous_session"] == "2030-01-03"
    assert closed["next_session"] == "2030-01-08"
    assert read(path, digest, "2030-01-02")["previous_session"] == "2030-01-01"
    assert read(path, digest, "2030-01-09")["next_session"] == "2030-01-10"
    result = BoundedCalendarView(path, expected_binding_sha256=digest).read_carry(
        "synthetic-holiday", "000001.XSHE", role=ROLE
    )
    assert len(result["calendar"]["daily_states"]) == 8 and len(result["calendar"]["links"]) == 4
    assert result["synthetic"] and not result["historical_pit"] and not result["strategy_visible"]
    assert row["source_capture_time"]["completed_at"] == "2031-02-03T00:00:01+00:00"
    row["source_refs"].clear()
    assert read(path, digest)["source_refs"]


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(role="strategy"),
        dict(role="lifecycle_research"),
        dict(role="matching"),
        dict(window="wrong"),
        dict(security="600000.XSHG"),
        dict(label="2030-01-01"),
        dict(label="2030-01-10"),
    ],
)
def test_wrong_role_security_window_and_read_boundaries_refuse(tmp_path, kwargs):
    path, _, digest = fixture(tmp_path)
    with pytest.raises(DataError):
        read(path, digest, **kwargs)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda r: r.pop(5), "CALENDAR_BINDING_COVERAGE"),
        (lambda r: r.append(r[0]), "CALENDAR_BINDING_SCHEMA"),
        (lambda r: r[0].update(jybz="0"), "CALENDAR_BINDING_BOUNDARY"),
        (lambda r: r[4].update(orders=[]), "CALENDAR_BINDING_SCHEMA"),
    ],
)
def test_incomplete_or_polluted_source_never_becomes_a_calendar(tmp_path, mutation, code):
    path, _, digest = fixture(tmp_path, mutation)
    with pytest.raises(DataError) as exc:
        read(path, digest)
    assert exc.value.code == code


def test_source_flags_are_not_replaced_with_weekday_rules(tmp_path):
    path, _, digest = fixture(tmp_path, lambda r: r[1].update(jybz="0"))
    row = read(path, digest, "2030-01-02")
    assert row["is_open"] is False
    assert (row["previous_session"], row["next_session"]) == ("2030-01-01", "2030-01-03")


@pytest.mark.parametrize("target", ["binding.json", "source.json"])
def test_each_read_rechecks_integrity(tmp_path, target):
    path, _, digest = fixture(tmp_path)
    view = BoundedCalendarView(path, expected_binding_sha256=digest)
    view.read_calendar("synthetic-holiday", "000001.XSHE", "2030-01-03", role=ROLE)
    p = tmp_path / target
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(DataError, match="hash mismatch"):
        view.read_calendar("synthetic-holiday", "000001.XSHE", "2030-01-03", role=ROLE)


def test_corroborating_conflict_is_not_silently_accepted(tmp_path):
    path, body, _ = fixture(tmp_path)
    digest = write(tmp_path / "other.json", dict(dates=["2030-01-01"], synthetic_dates_added=0))
    body["corroboration"] = dict(path="other.json", sha256=digest)
    digest = write(path, body)
    with pytest.raises(DataError) as exc:
        read(path, digest)
    assert exc.value.code == "CALENDAR_BINDING_CONFLICT"


def test_invalid_capture_clock_and_path_escape_refuse(tmp_path):
    path, body, _ = fixture(tmp_path)
    body["primary_source"]["captured_end"] = "2031-02-02T00:00:00+00:00"
    with pytest.raises(DataError) as exc:
        read(path, write(path, body))
    assert exc.value.code == "CALENDAR_BINDING_CLOCK"
    body["primary_source"]["captured_end"] = "2031-02-03T00:00:01+00:00"
    body["primary_source"]["path"] = str(tmp_path / "source.json")
    with pytest.raises(DataError) as exc:
        read(path, write(path, body))
    assert exc.value.code == "CALENDAR_BINDING_FILE"
